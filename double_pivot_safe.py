#!/usr/bin/env python3

import argparse
import json
import os
import time
import re
from kubernetes import client, config

# -------------------------------------------------------------------------
# Setup Kubernetes client
# -------------------------------------------------------------------------
config.load_kube_config()
v1 = client.CoreV1Api()
apps_v1 = client.AppsV1Api()
batch_v1 = client.BatchV1Api()

DEFAULT_METADATA_FILE = "double_pivot_metadata.json"
SCALE_TRACK_FILE = "workload_scale_backup.json"

# -------------------------------------------------------------------------
# Argument parsing
# -------------------------------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser(description="Safe double pivot with workload scale-down/up.")
    parser.add_argument("origin_sc", help="Original StorageClass")
    parser.add_argument("target_sc", help="Target StorageClass")
    parser.add_argument("-n", "--namespace", required=True, help="Kubernetes namespace")
    parser.add_argument("--recreate", action="store_true", help="Phase 2: finalize migration")
    parser.add_argument("--dry-run", action="store_true", help="Simulate actions")
    parser.add_argument("--set-replica-0", action="store_true", help="Ensure workloads are scaled down before copying")
    return parser.parse_args()

# -------------------------------------------------------------------------
# PVC and Pod helpers
# -------------------------------------------------------------------------
def list_pvcs(namespace, storage_class):
    return [p for p in v1.list_namespaced_persistent_volume_claim(namespace).items if p.spec.storage_class_name == storage_class]

def create_pvc(namespace, name, storage_class, size, access_modes, dry_run):
    if dry_run:
        print(f"[DRY-RUN] Create PVC '{name}' in SC '{storage_class}' ({size})")
        return
    body = client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(name=name),
        spec=client.V1PersistentVolumeClaimSpec(
            storage_class_name=storage_class,
            access_modes=access_modes,
            resources=client.V1ResourceRequirements(requests={"storage": size})
        )
    )
    try:
        v1.create_namespaced_persistent_volume_claim(namespace, body)
        print(f"[+] Created PVC '{name}'")
    except client.exceptions.ApiException as e:
        if e.status == 409:
            print(f"[!] PVC '{name}' already exists. Skipping creation.")
        else:
            raise

def sanitize_pod_name(name, prefix):
    base = re.sub(r'[^a-z0-9\-]', '', name.lower())[:20].rstrip('-')
    return f"{prefix}-{base}"

def copy_data(namespace, src, dst, prefix, dry_run):
    pod_name = sanitize_pod_name(src, prefix)
    if dry_run:
        print(f"[DRY-RUN] Copy data {src} -> {dst} via pod {pod_name}")
        return
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": pod_name},
        "spec": {
            "restartPolicy": "Never",
            "containers": [{
                "name": "copy",
                "image": "alpine",
                "command": ["sh", "-c", "apk add rsync && rsync -a /old/ /new/ && sleep 2"],
                "volumeMounts": [
                    {"name": "src", "mountPath": "/old"},
                    {"name": "dst", "mountPath": "/new"}
                ]
            }],
            "volumes": [
                {"name": "src", "persistentVolumeClaim": {"claimName": src}},
                {"name": "dst", "persistentVolumeClaim": {"claimName": dst}}
            ]
        }
    }
    v1.create_namespaced_pod(namespace, pod)
    print(f"[~] Waiting for pod '{pod_name}'...")
    while True:
        p = v1.read_namespaced_pod(pod_name, namespace)
        if p.status.phase in ["Succeeded", "Failed"]:
            break
        time.sleep(2)
    v1.delete_namespaced_pod(pod_name, namespace)
    print(f"[x] Deleted pivot pod '{pod_name}'")

# -------------------------------------------------------------------------
# Workload Scale Helpers
# -------------------------------------------------------------------------
def detect_and_scale_down(namespace, pvc_names, dry_run):
    replicas_backup = {}

    deployments = apps_v1.list_namespaced_deployment(namespace).items
    statefulsets = apps_v1.list_namespaced_stateful_set(namespace).items
    replicasets = apps_v1.list_namespaced_replica_set(namespace).items
    daemonsets = apps_v1.list_namespaced_daemon_set(namespace).items
    cronjobs = batch_v1.list_namespaced_cron_job(namespace).items
    jobs = batch_v1.list_namespaced_job(namespace).items

    def match_pvc_template(workload):
        volumes = []
        if hasattr(workload.spec, "template"):
            volumes = workload.spec.template.spec.volumes or []
        elif hasattr(workload.spec, "job_template"):
            volumes = workload.spec.job_template.spec.template.spec.volumes or []
        elif hasattr(workload.spec, "volumes"):
            volumes = workload.spec.volumes or []
        for vol in volumes:
            pvc = vol.persistent_volume_claim
            if pvc and pvc.claim_name in pvc_names:
                return True
        return False

    for deploy in deployments:
        if match_pvc_template(deploy):
            replicas = deploy.spec.replicas
            print(f"[=] Scaling down Deployment '{deploy.metadata.name}'...")
            if replicas > 0:
                if dry_run:
                    print(f"[DRY-RUN] Would scale Deployment '{deploy.metadata.name}' to 0")
                else:
                    apps_v1.patch_namespaced_deployment_scale(
                        name=deploy.metadata.name,
                        namespace=namespace,
                        body={"spec": {"replicas": 0}}
                    )
            replicas_backup[deploy.metadata.uid] = ("Deployment", deploy.metadata.name, replicas)

    for rs in replicasets:
        if match_pvc_template(rs):
            replicas = rs.spec.replicas or 0
            print(f"[=] Scaling down ReplicaSet '{rs.metadata.name}'...")
            if replicas > 0:
                if dry_run:
                    print(f"[DRY-RUN] Would scale ReplicaSet '{rs.metadata.name}' to 0")
                else:
                    apps_v1.patch_namespaced_replica_set_scale(
                        name=rs.metadata.name,
                        namespace=namespace,
                        body={"spec": {"replicas": 0}}
                    )
            replicas_backup[rs.metadata.uid] = ("ReplicaSet", rs.metadata.name, replicas)

    for sts in statefulsets:
        volume_claim_names = [tpl.metadata.name for tpl in sts.spec.volume_claim_templates or []]
        found_match = False
        for pvc_name in pvc_names:
            for vct_name in volume_claim_names:
                if pvc_name.startswith(f"{vct_name}-{sts.metadata.name}-"):
                    found_match = True
                    break
            if found_match:
                break
        for vol in sts.spec.template.spec.volumes or []:
            pvc = vol.persistent_volume_claim
            if pvc and pvc.claim_name in pvc_names:
                found_match = True
                break
        if found_match:
            replicas = sts.spec.replicas
            print(f"[=] Scaling down StatefulSet '{sts.metadata.name}'...")
            if replicas > 0:
                if dry_run:
                    print(f"[DRY-RUN] Would scale StatefulSet '{sts.metadata.name}' to 0")
                else:
                    apps_v1.patch_namespaced_stateful_set_scale(
                        name=sts.metadata.name,
                        namespace=namespace,
                        body={"spec": {"replicas": 0}}
                    )
            replicas_backup[sts.metadata.uid] = ("StatefulSet", sts.metadata.name, replicas)

    for ds in daemonsets:
        if match_pvc_template(ds):
            print(f"[=] Pausing DaemonSet '{ds.metadata.name}' (nodeSelector patch)...")
            if dry_run:
                print(f"[DRY-RUN] Would patch DaemonSet '{ds.metadata.name}' with nodeSelector")
            else:
                patch_body = {"spec": {"template": {"spec": {"nodeSelector": {"migration-paused": "true"}}}}}
                apps_v1.patch_namespaced_daemon_set(name=ds.metadata.name, namespace=namespace, body=patch_body)
            replicas_backup[ds.metadata.uid] = ("DaemonSet", ds.metadata.name, "patched")

    for cj in cronjobs:
        if match_pvc_template(cj):
            print(f"[=] Suspending CronJob '{cj.metadata.name}'...")
            if dry_run:
                print(f"[DRY-RUN] Would suspend CronJob '{cj.metadata.name}'")
            else:
                batch_v1.patch_namespaced_cron_job(
                    name=cj.metadata.name,
                    namespace=namespace,
                    body={"spec": {"suspend": True}}
                )
            replicas_backup[cj.metadata.uid] = ("CronJob", cj.metadata.name, True)

    for job in jobs:
        if match_pvc_template(job):
            print(f"[=] Deleting active Job '{job.metadata.name}'...")
            if dry_run:
                print(f"[DRY-RUN] Would delete Job '{job.metadata.name}'")
            else:
                batch_v1.delete_namespaced_job(
                    name=job.metadata.name,
                    namespace=namespace,
                    body=client.V1DeleteOptions(propagation_policy="Background")
                )
            replicas_backup[job.metadata.uid] = ("Job", job.metadata.name, "deleted")

    if not dry_run:
        with open(SCALE_TRACK_FILE, "w") as f:
            json.dump(replicas_backup, f)

    return replicas_backup

# -------------------------------------------------------------------------
# Main logic
# -------------------------------------------------------------------------
def scale_back_up(namespace, dry_run):
    if not os.path.exists(SCALE_TRACK_FILE):
        print("[!] No scale backup file found. Cannot restore replicas.")
        return
    with open(SCALE_TRACK_FILE) as f:
        backups = json.load(f)

    for uid, (kind, name, value) in backups.items():
        if kind == "Deployment":
            print(f"[+] Restoring Deployment '{name}' to replicas={value}")
            if dry_run:
                print(f"[DRY-RUN] Would scale Deployment '{name}' to {value}")
            else:
                apps_v1.patch_namespaced_deployment_scale(
                    name=name,
                    namespace=namespace,
                    body={"spec": {"replicas": value}}
                )

        elif kind == "ReplicaSet":
            print(f"[+] Restoring ReplicaSet '{name}' to replicas={value}")
            if dry_run:
                print(f"[DRY-RUN] Would scale ReplicaSet '{name}' to {value}")
            else:
                apps_v1.patch_namespaced_replica_set_scale(
                    name=name,
                    namespace=namespace,
                    body={"spec": {"replicas": value}}
                )

        elif kind == "StatefulSet":
            print(f"[+] Restoring StatefulSet '{name}' to replicas={value}")
            if dry_run:
                print(f"[DRY-RUN] Would scale StatefulSet '{name}' to {value}")
            else:
                apps_v1.patch_namespaced_stateful_set_scale(
                    name=name,
                    namespace=namespace,
                    body={"spec": {"replicas": value}}
                )

        elif kind == "DaemonSet":
            print(f"[+] Unpausing DaemonSet '{name}' (removing nodeSelector)")
            if dry_run:
                print(f"[DRY-RUN] Would unpatch DaemonSet '{name}'")
            else:
                apps_v1.patch_namespaced_daemon_set(
                    name=name,
                    namespace=namespace,
                    body={"spec": {"template": {"spec": {"nodeSelector": None}}}}
                )

        elif kind == "CronJob":
            print(f"[+] Resuming CronJob '{name}'...")
            if dry_run:
                print(f"[DRY-RUN] Would resume CronJob '{name}'")
            else:
                batch_v1.patch_namespaced_cron_job(
                    name=name,
                    namespace=namespace,
                    body={"spec": {"suspend": False}}
                )

        elif kind == "Job":
            print(f"[~] Job '{name}' was deleted earlier and will not be resumed")

    if not dry_run:
        os.remove(SCALE_TRACK_FILE)

# -------------------------------------------------------------------------
# Main execution
# -------------------------------------------------------------------------
def main():
    args = parse_args()
    ns = args.namespace

    if not args.recreate:
        pvcs = list_pvcs(ns, args.origin_sc)
        if not pvcs:
            print(f"[!] No PVCs found in SC '{args.origin_sc}'")
            return

        pvc_names = [p.metadata.name for p in pvcs]

        if args.set_replica_0:
            print("[=] Scaling workloads using these PVCs to 0 before starting...")
            detect_and_scale_down(ns, pvc_names, args.dry_run)

        metadata = []
        for pvc in pvcs:
            old_name = pvc.metadata.name
            temp_name = f"{old_name}-temp"
            size = pvc.spec.resources.requests["storage"]
            modes = pvc.spec.access_modes

            create_pvc(ns, temp_name, args.target_sc, size, modes, args.dry_run)
            copy_data(ns, old_name, temp_name, prefix="pivot1", dry_run=args.dry_run)

            metadata.append({
                "old_name": old_name,
                "temp_name": temp_name,
                "size": size,
                "modes": modes
            })

        if not args.dry_run:
            with open(DEFAULT_METADATA_FILE, "w") as f:
                json.dump(metadata, f, indent=2)
            print(f"[~] Phase 1 complete. Delete old PVCs and run with --recreate")

    else:
        if not os.path.exists(DEFAULT_METADATA_FILE):
            print("[!] No metadata found")
            return
        with open(DEFAULT_METADATA_FILE) as f:
            records = json.load(f)

        for r in records:
            if not args.dry_run:
                try:
                    v1.delete_namespaced_persistent_volume_claim(r["old_name"], ns)
                    print(f"[x] Deleted original PVC '{r['old_name']}'")
                    time.sleep(2)
                except client.exceptions.ApiException as e:
                    print(f"[!] Could not delete original PVC '{r['old_name']}': {e}")

            create_pvc(ns, r["old_name"], args.target_sc, r["size"], r["modes"], args.dry_run)
            copy_data(ns, r["temp_name"], r["old_name"], prefix="pivot2", dry_run=args.dry_run)

            if not args.dry_run:
                try:
                    v1.delete_namespaced_persistent_volume_claim(r["temp_name"], ns)
                    print(f"[x] Deleted temp PVC '{r['temp_name']}'")
                except client.exceptions.ApiException as e:
                    print(f"[!] Could not delete temp PVC '{r['temp_name']}': {e}")

        scale_back_up(ns, args.dry_run)

        if not args.dry_run:
            os.remove(DEFAULT_METADATA_FILE)

if __name__ == "__main__":
    main()
