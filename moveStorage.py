#!/usr/bin/env python3

import argparse
import os
import time
import yaml
from kubernetes import client, config

# -----------------------------------------------------------------------------
# Load Kubernetes config
# -----------------------------------------------------------------------------
config.load_kube_config()
v1 = client.CoreV1Api()
apps_v1 = client.AppsV1Api()
custom_api = client.CustomObjectsApi()

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------
MANIFEST_DIR = "manifests"
os.makedirs(MANIFEST_DIR, exist_ok=True)

POST_SCRIPT_PATH = os.path.join(MANIFEST_DIR, "post-migrate.sh")
with open(POST_SCRIPT_PATH, "w") as f:
    f.write("#!/bin/bash\n")
    f.write("echo '[INFO] Running post-migration script'\n\n")

# -----------------------------------------------------------------------------
# Global track of changes for summary
# -----------------------------------------------------------------------------
dry_run_report = {
    "pvcs_found": [],
    "new_pvcs": [],
    "pivot_pods": [],
    "snapshots_used": [],
    "workloads_patched": [],
    "pvcs_deleted": [],
    "pod_manifests": []
}

# -----------------------------------------------------------------------------
# Parse CLI
# -----------------------------------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser(
        description="📦 PVC Storage Migrator — Migrate PVCs from one storage class to another safely.",
        epilog="""
Examples:
  python3 moveStorage.py old-sc new-sc -n my-namespace
  python3 moveStorage.py local-path netapp -n staging --dry-run --export-pods
  python3 moveStorage.py local-path nvme-o-tcp -n pv-test --delete-pvc
  python3 moveStorage.py local-path nvme-o-tcp -n dev --copy-only

Flags:
  --dry-run          Only simulate changes and print a summary.
  --prefer-snapshot  Try to use VolumeSnapshot instead of pivot copy (if supported).
  --export-pods      Export minimal Pod manifests to 'manifests/' for manual re-apply.
  --delete-pvc       Delete the original PVC after migration.
  --copy-only        Only copy data — skip patching workloads and deleting PVCs.
""",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("origin", help="Original StorageClass name (e.g. local-path)")
    parser.add_argument("target", help="Target StorageClass name (e.g. netapp)")
    parser.add_argument("-n", "--namespace", required=True, help="Target Kubernetes namespace")
    parser.add_argument("--dry-run", action="store_true", help="Preview actions without applying changes")
    parser.add_argument("--prefer-snapshot", action="store_true", help="Use VolumeSnapshot if supported by the origin SC")
    parser.add_argument("--export-pods", action="store_true", help="Export Pod manifests using old PVCs for manual re-creation")
    parser.add_argument("--delete-pvc", action="store_true", help="Delete original PVC after successful migration")
    parser.add_argument("--copy-only", action="store_true", help="Only copy data (skip patching workloads & PVC deletion)")
    return parser.parse_args()

# -----------------------------------------------------------------------------
# Utility: get PVCs in a specific storage class
# -----------------------------------------------------------------------------
def get_pvcs_by_storage_class(namespace, storage_class):
    pvcs = v1.list_namespaced_persistent_volume_claim(namespace).items
    return [pvc for pvc in pvcs if pvc.spec.storage_class_name == storage_class]

# -----------------------------------------------------------------------------
# Utility: create or log a VolumeSnapshot
# -----------------------------------------------------------------------------
def get_snapshot_class_for_storage_class(storage_class_name):
    try:
        snapshot_classes = custom_api.list_cluster_custom_object(
            group="snapshot.storage.k8s.io",
            version="v1",
            plural="volumesnapshotclasses"
        )["items"]
        for sc in snapshot_classes:
            if sc.get("deletionPolicy") == "Delete" and storage_class_name in sc.get("driver", ""):
                return sc["metadata"]["name"]
    except Exception as e:
        print(f"[!] Snapshot detection failed: {e}")
    return None

def create_volume_snapshot(namespace, pvc_name, snapshot_class_name, dry_run):
    snapshot_name = f"{pvc_name}-snap"
    if dry_run:
        print(f"[DRY-RUN] Would create VolumeSnapshot '{snapshot_name}' for PVC '{pvc_name}' using class '{snapshot_class_name}'")
        dry_run_report["snapshots_used"].append(snapshot_name)
        return snapshot_name

    snapshot_body = {
        "apiVersion": "snapshot.storage.k8s.io/v1",
        "kind": "VolumeSnapshot",
        "metadata": {"name": snapshot_name},
        "spec": {
            "volumeSnapshotClassName": snapshot_class_name,
            "source": {"persistentVolumeClaimName": pvc_name}
        }
    }
    custom_api.create_namespaced_custom_object(
        group="snapshot.storage.k8s.io", version="v1",
        namespace=namespace, plural="volumesnapshots", body=snapshot_body
    )
    print(f"[+] Created snapshot: {snapshot_name}")

    # Wait for snapshot to be ready
    while True:
        snap = custom_api.get_namespaced_custom_object(
            group="snapshot.storage.k8s.io", version="v1",
            namespace=namespace, plural="volumesnapshots", name=snapshot_name
        )
        if snap["status"].get("readyToUse"):
            break
        time.sleep(2)
    return snapshot_name

# -----------------------------------------------------------------------------
# Utility: create new PVC
# -----------------------------------------------------------------------------
def create_new_pvc(old_pvc, target_storage_class, namespace, dry_run, snapshot_name=None):
    clean_sc = target_storage_class.lower().replace('_', '-')
    new_pvc_name = f"{old_pvc.metadata.name}-{clean_sc}"
    if dry_run:
        print(f"[DRY-RUN] Would create new PVC: {new_pvc_name} with storageClass '{target_storage_class}'")
        dry_run_report["new_pvcs"].append(new_pvc_name)
        return new_pvc_name

    if snapshot_name:
        pvc_spec = client.V1PersistentVolumeClaimSpec(
            access_modes=old_pvc.spec.access_modes,
            resources=old_pvc.spec.resources,
            storage_class_name=target_storage_class,
            data_source=client.V1TypedLocalObjectReference(
                name=snapshot_name, kind="VolumeSnapshot", api_group="snapshot.storage.k8s.io"
            )
        )
    else:
        pvc_spec = client.V1PersistentVolumeClaimSpec(
            access_modes=old_pvc.spec.access_modes,
            resources=old_pvc.spec.resources,
            storage_class_name=target_storage_class
        )

    new_pvc = client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(name=new_pvc_name),
        spec=pvc_spec
    )
    v1.create_namespaced_persistent_volume_claim(namespace, new_pvc)
    print(f"[+] Created new PVC: {new_pvc_name}")
    return new_pvc_name

# -----------------------------------------------------------------------------
# Utility: pivot pod for data copy
# -----------------------------------------------------------------------------
def create_pivot_pod(namespace, source_pvc, target_pvc, dry_run):
    pod_name = f"pivot-copy-{source_pvc[:20]}"
    if dry_run:
        print(f"[DRY-RUN] Would create pivot pod '{pod_name}' to copy data from '{source_pvc}' to '{target_pvc}'")
        dry_run_report["pivot_pods"].append(pod_name)
        return

    pod_manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": pod_name},
        "spec": {
            "containers": [{
                "name": "copy",
                "image": "alpine",
                "command": ["sh", "-c", "apk add --no-cache rsync && rsync -a /old/ /new/ && sleep 2"],
                "volumeMounts": [
                    {"name": "source", "mountPath": "/old"},
                    {"name": "target", "mountPath": "/new"}
                ]
            }],
            "volumes": [
                {"name": "source", "persistentVolumeClaim": {"claimName": source_pvc}},
                {"name": "target", "persistentVolumeClaim": {"claimName": target_pvc}}
            ],
            "restartPolicy": "Never"
        }
    }
    v1.create_namespaced_pod(namespace=namespace, body=pod_manifest)
    print(f"[~] Pivot pod '{pod_name}' created. Waiting for copy to finish...")

    # Wait for pivot to finish
    while True:
        pod_status = v1.read_namespaced_pod(pod_name, namespace)
        if pod_status.status.phase in ["Succeeded", "Failed"]:
            break
        time.sleep(2)

    print(f"[✓] Pivot pod finished with status: {pod_status.status.phase}")
    v1.delete_namespaced_pod(pod_name, namespace)

# -----------------------------------------------------------------------------
# Export minimal Pod manifest
# -----------------------------------------------------------------------------
def append_post_script(pod_name, namespace):
    """Add commands to post-migrate.sh"""
    with open(POST_SCRIPT_PATH, "a") as f:
        f.write(f"echo 'Deleting old pod: {pod_name}'\n")
        f.write(f"kubectl delete pod {pod_name} -n {namespace} --ignore-not-found=true\n")
        f.write(f"echo 'Applying pod: {pod_name}'\n")
        f.write(f"kubectl apply -f {pod_name}.yaml -n {namespace}\n\n")

def export_pod_manifest(pod, new_pvc_name):
    pod_name = pod.metadata.name
    namespace = pod.metadata.namespace

    clean_volumes = []
    valid_volume_names = []

    for vol in pod.spec.volumes:
        # 1) Skip injected serviceaccount volumes
        if vol.projected and vol.name.startswith("kube-api-access"):
            continue

        # 2) Construct a minimal dictionary:
        vol_dict = {"name": vol.name}
        if vol.persistent_volume_claim:
            # Overwrite the claimName with the new one
            vol_dict["persistentVolumeClaim"] = {
                "claimName": new_pvc_name
            }
            valid_volume_names.append(vol.name)

        # Only add the volume to the final list if it’s used
        # e.g. if it had a persistentVolumeClaim or maybe you allow emptyDir, etc.
        if "persistentVolumeClaim" in vol_dict:
            clean_volumes.append(vol_dict)

    clean_containers = []
    for container in pod.spec.containers:
        c_dict = {
            "name": container.name,
            "image": container.image,
        }
        if container.command:
            c_dict["command"] = container.command
        if container.args:
            c_dict["args"] = container.args
        if container.ports:
            c_dict["ports"] = [{"containerPort": p.container_port} for p in container.ports]
        if container.volume_mounts:
            # Only include mounts referencing volumes we kept
            c_dict["volumeMounts"] = [
                {"mountPath": m.mount_path, "name": m.name}
                for m in container.volume_mounts
                if m.name in valid_volume_names
            ]
        clean_containers.append(c_dict)

    pod_manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name,
            "namespace": namespace
        },
        "spec": {
            "volumes": clean_volumes,
            "containers": clean_containers,
            # Only minimal needed:
            "restartPolicy": "Never"
        }
    }

    path = os.path.join(MANIFEST_DIR, f"{pod_name}.yaml")
    with open(path, "w") as f:
        yaml.safe_dump(pod_manifest, f, sort_keys=False)

    # Append to post-migrate script
    with open(POST_SCRIPT_PATH, "a") as f:
        f.write(f"echo 'Deleting old pod: {pod_name}'\n")
        f.write(f"kubectl delete pod {pod_name} -n {namespace} --ignore-not-found=true\n")
        f.write(f"echo 'Applying pod: {pod_name}'\n")
        f.write(f"kubectl apply -f {pod_name}.yaml -n {namespace}\n\n")

    dry_run_report["pod_manifests"].append(path)

# -----------------------------------------------------------------------------
# Patch Deployments / DaemonSets / etc., plus handle bare Pods if --export-pods
# -----------------------------------------------------------------------------
def patch_workload_using_pvc(namespace, old_pvc, new_pvc_name, dry_run, export_pods=False):
    """
    Patches any Deployment/StatefulSet/DaemonSet referencing 'old_pvc'
    to use 'new_pvc_name'. If it's a StatefulSet using volumeClaimTemplates,
    patch the storageClassName to the new one if it matches old_pvc.spec.storage_class_name.

    :param old_pvc: V1PersistentVolumeClaim object for the old PVC
    :param new_pvc_name: Name of the new PVC
    :param dry_run: bool
    :param export_pods: bool
    """
    old_pvc_name = old_pvc.metadata.name
    old_sc = old_pvc.spec.storage_class_name  # The old StorageClass
    workload_types = [
        ("Deployment", apps_v1.list_namespaced_deployment, apps_v1.patch_namespaced_deployment),
        ("StatefulSet", apps_v1.list_namespaced_stateful_set, apps_v1.patch_namespaced_stateful_set),
        ("DaemonSet", apps_v1.list_namespaced_daemon_set, apps_v1.patch_namespaced_daemon_set),
    ]

    found = False

    # 1) Patch known workload controllers
    for kind, list_func, patch_func in workload_types:
        workloads = list_func(namespace).items
        for workload in workloads:
            updated = False

            if kind == "StatefulSet":
                #
                # For a StatefulSet with volumeClaimTemplates, we look at .spec.volume_claim_templates
                # because the PVC name doesn't appear in .spec.template.spec.volumes.
                #
                vct_list = workload.spec.volume_claim_templates or []
                for vct in vct_list:
                    # If the old StorageClass matches what's in the template, patch it to the new one
                    if vct.spec.storage_class_name == old_sc:
                        if dry_run:
                            print(f"[DRY-RUN] Would patch StatefulSet '{workload.metadata.name}' "
                                  f"volumeClaimTemplate '{vct.metadata.name}' from '{old_sc}' to the new StorageClass.")
                            dry_run_report["workloads_patched"].append(f"StatefulSet:{workload.metadata.name}")
                        else:
                            vct.spec.storage_class_name = new_pvc_name  # patch from old_sc to new SC
                            patch_func(workload.metadata.name, namespace, workload)
                            print(f"[~] Patched StatefulSet '{workload.metadata.name}' volumeClaimTemplate "
                                  f"from '{old_sc}' to '{new_pvc_name}'.")
                        updated = True

                #
                # Also, if the user had an inline .spec.template.spec.volumes, handle that:
                #
                volumes = workload.spec.template.spec.volumes or []
                for vol in volumes:
                    pvc_claim = getattr(vol, "persistent_volume_claim", None)
                    if pvc_claim and pvc_claim.claim_name == old_pvc_name:
                        # It's unusual for a StatefulSet to declare volumes this way, but let's handle it anyway.
                        if dry_run:
                            print(f"[DRY-RUN] Would patch StatefulSet: {workload.metadata.name} to use PVC '{new_pvc_name}'")
                            dry_run_report["workloads_patched"].append(f"{kind}:{workload.metadata.name}")
                        else:
                            pvc_claim.claim_name = new_pvc_name
                            patch_func(workload.metadata.name, namespace, workload)
                            print(f"[~] Patched StatefulSet: {workload.metadata.name}")
                        updated = True

            else:
                #
                # For Deployments/DaemonSets, we do what you had before:
                #
                volumes = workload.spec.template.spec.volumes or []
                for vol in volumes:
                    pvc_claim = getattr(vol, "persistent_volume_claim", None)
                    if pvc_claim and pvc_claim.claim_name == old_pvc_name:
                        if dry_run:
                            print(f"[DRY-RUN] Would patch {kind}: {workload.metadata.name} "
                                  f"to use PVC '{new_pvc_name}'")
                            dry_run_report["workloads_patched"].append(f"{kind}:{workload.metadata.name}")
                        else:
                            pvc_claim.claim_name = new_pvc_name
                            patch_func(workload.metadata.name, namespace, workload)
                            print(f"[~] Patched {kind}: {workload.metadata.name}")
                        updated = True

            if updated:
                found = True

    # 2) Check for truly "standalone" Pods (no higher-level controller)
    all_pods = v1.list_namespaced_pod(namespace).items
    for pod in all_pods:
        # If a Pod is owned by a controller, skip it
        if pod.metadata.owner_references:
            if any(ref.controller for ref in pod.metadata.owner_references):
                continue

        volumes = pod.spec.volumes or []
        for vol in volumes:
            pvc_claim = getattr(vol, "persistent_volume_claim", None)
            if pvc_claim and pvc_claim.claim_name == old_pvc_name:
                found = True
                if dry_run:
                    print(f"[DRY-RUN] Standalone Pod '{pod.metadata.name}' uses PVC '{old_pvc_name}' — manual redeployment needed.")
                    dry_run_report["workloads_patched"].append(f"Pod:{pod.metadata.name}")
                    if export_pods:
                        export_pod_manifest(pod, new_pvc_name)
                else:
                    print(f"[!] Standalone Pod '{pod.metadata.name}' uses PVC '{old_pvc_name}', "
                          "but can't be live-patched. Manual re-apply required.")
                    if export_pods:
                        export_pod_manifest(pod, new_pvc_name)
                break

    if not found:
        print(f"[!] Warning: No workload found referencing PVC '{old_pvc_name}' or StorageClass '{old_sc}'")




# -----------------------------------------------------------------------------
# Delete old PVC (only if --delete-pvc is set)
# -----------------------------------------------------------------------------
def delete_old_pvc(namespace, pvc_name, dry_run, delete_enabled):
    if not delete_enabled:
        print(f"[SKIPPED] PVC deletion disabled for '{pvc_name}' (use --delete-pvc to enable)")
        return

    if dry_run:
        print(f"[DRY-RUN] Would delete old PVC: {pvc_name}")
        dry_run_report["pvcs_deleted"].append(pvc_name)
    else:
        v1.delete_namespaced_persistent_volume_claim(pvc_name, namespace)
        print(f"[x] Deleted old PVC: {pvc_name}")

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def main():
    args = parse_args()
    namespace = args.namespace

    print(f"[=] Scanning for PVCs in storage class '{args.origin}' in namespace '{namespace}'")
    pvcs = get_pvcs_by_storage_class(namespace, args.origin)
    if not pvcs:
        print(f"[!] No PVCs found with the specified origin storage class '{args.origin}'. Nothing to do.")
        return

    for pvc in pvcs:
        print(f"\n[>] Migrating PVC: {pvc.metadata.name}")
        dry_run_report["pvcs_found"].append(pvc.metadata.name)

        snapshot_name = None
        if args.prefer_snapshot:
            snapshot_class = get_snapshot_class_for_storage_class(args.origin)
            if snapshot_class:
                print(f"[~] Detected snapshot class '{snapshot_class}'. Creating snapshot.")
                snapshot_name = create_volume_snapshot(namespace, pvc.metadata.name, snapshot_class, args.dry_run)
            else:
                print("[!] No usable snapshot class found. Falling back to pivot copy.")

        new_pvc_name = create_new_pvc(pvc, args.target, namespace, args.dry_run, snapshot_name)

        # If no snapshot was used, do pivot copy
        if not snapshot_name:
            create_pivot_pod(namespace, pvc.metadata.name, new_pvc_name, args.dry_run)

        # If user wants only copy, skip patching + old PVC deletion
        if args.copy_only:
            print(f"[~] Skipping workload patching and PVC deletion due to --copy-only.")
        else:
            # Instead of passing pvc.metadata.name, pass the entire pvc object
            patch_workload_using_pvc(namespace, pvc, new_pvc_name, args.dry_run, args.export_pods)
            delete_old_pvc(namespace, pvc.metadata.name, args.dry_run, args.delete_pvc)
    # Summaries
    if args.dry_run:
        print("\n====== DRY-RUN SUMMARY ======")
        print(f"PVCs found:             {len(dry_run_report['pvcs_found'])}")
        print(f"New PVCs to create:     {len(dry_run_report['new_pvcs'])}")
        print(f"Snapshots to create:    {len(dry_run_report['snapshots_used'])}")
        print(f"Pivot pods to launch:   {len(dry_run_report['pivot_pods'])}")
        print(f"Workloads to patch:     {len(dry_run_report['workloads_patched'])}")
        print(f"PVCs to delete:         {len(dry_run_report['pvcs_deleted'])}")
        print(f"Pod manifests exported: {len(dry_run_report['pod_manifests'])}")
        print("====== END OF DRY-RUN ======")

        if args.copy_only:
            print("\n[!] NOTE: --copy-only was set. No workloads were patched or PVCs deleted.")

        print(f"\n[!] To apply new pods:\n  cd {MANIFEST_DIR} && chmod +x post-migrate.sh && ./post-migrate.sh")
    else:
        print("\n[✓] Migration completed successfully.")

if __name__ == "__main__":
    main()
