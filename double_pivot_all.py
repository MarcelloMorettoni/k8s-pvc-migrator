#!/usr/bin/env python3

import argparse
import json
import os
import time
from kubernetes import client, config

# -------------------------------------------------------------------------
# Load kubeconfig & set up client
# -------------------------------------------------------------------------
config.load_kube_config()
v1 = client.CoreV1Api()

# Default metadata file name
DEFAULT_METADATA_FILE = "double_pivot_metadata.json"

def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Double-pivot all PVCs from an old StorageClass to a new one, "
            "keeping the original PVC names. Two-phase approach with optional preservation of temp PVCs."
        )
    )
    parser.add_argument("origin_sc", help="Name of the old/origin StorageClass.")
    parser.add_argument("target_sc", help="Name of the new/target StorageClass.")
    parser.add_argument("-n", "--namespace", required=True, help="Kubernetes namespace.")
    parser.add_argument("--recreate", action="store_true",
                        help="Run Phase 2: read metadata, create final PVCs, copy data from <temp> -> <old>.")
    parser.add_argument("--metadata-file", default=DEFAULT_METADATA_FILE,
                        help="JSON file to store/retrieve pivot info (default: double_pivot_metadata.json).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simulate actions without applying changes.")
    parser.add_argument("--preserve-temp", action="store_true",
                        help="If set during Phase 2, do NOT delete <pvcName>-temp after copying data back.")
    return parser.parse_args()

def list_pvcs_in_storageclass(namespace, storage_class):
    """Return all PVCs in 'namespace' that have .spec.storageClassName == storage_class."""
    all_pvcs = v1.list_namespaced_persistent_volume_claim(namespace).items
    return [p for p in all_pvcs if p.spec.storage_class_name == storage_class]

def create_pvc(namespace, name, storage_class, size, access_modes, dry_run=False):
    """Create a PVC with the given name, SC, size, and access modes."""
    if dry_run:
        print(f"[DRY-RUN] Would create PVC '{name}' in SC '{storage_class}' (size={size}).")
        return

    pvc_body = client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(name=name),
        spec=client.V1PersistentVolumeClaimSpec(
            storage_class_name=storage_class,
            access_modes=access_modes,
            resources=client.V1ResourceRequirements(requests={"storage": size}),
        )
    )
    v1.create_namespaced_persistent_volume_claim(namespace, pvc_body)
    print(f"[+] Created PVC '{name}' in SC '{storage_class}'")

def copy_data_with_pod(namespace, source_pvc, target_pvc, prefix="pivot", dry_run=False):
    """
    Create a short-lived Pod to copy data from source_pvc -> target_pvc using rsync.
    Wait for completion, then delete the Pod.
    """
    pod_name = f"{prefix}-{source_pvc[:20]}"
    if dry_run:
        print(f"[DRY-RUN] Would create pivot pod '{pod_name}' to copy {source_pvc} -> {target_pvc}.")
        return

    pod_body = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": pod_name},
        "spec": {
            "restartPolicy": "Never",
            "containers": [{
                "name": "copy",
                "image": "alpine",
                "command": ["sh", "-c", "apk add --no-cache rsync && rsync -a /old/ /new/ && sleep 2"],
                "volumeMounts": [
                    {"name": "src", "mountPath": "/old"},
                    {"name": "dst", "mountPath": "/new"}
                ]
            }],
            "volumes": [
                {"name": "src", "persistentVolumeClaim": {"claimName": source_pvc}},
                {"name": "dst", "persistentVolumeClaim": {"claimName": target_pvc}}
            ]
        }
    }
    v1.create_namespaced_pod(namespace, pod_body)
    print(f"[~] Pivot pod '{pod_name}' created. Waiting for it to finish...")

    # Wait loop
    while True:
        pod = v1.read_namespaced_pod(pod_name, namespace)
        phase = pod.status.phase
        if phase in ("Succeeded", "Failed"):
            print(f"[✓] Pivot pod finished with status: {phase}")
            break
        time.sleep(2)

    # Delete the pivot pod
    v1.delete_namespaced_pod(pod_name, namespace)
    print(f"[x] Deleted pivot pod '{pod_name}'.")

def phase_one(args):
    """
    Phase 1: For each PVC in the old storage class:
        1) Create <oldName>-temp in the new SC
        2) Copy old->temp
        3) Save pivot info to JSON
    Then instruct the user to manually delete the old PVCs.
    """
    origin_sc = args.origin_sc
    target_sc = args.target_sc
    ns = args.namespace
    dry_run = args.dry_run
    meta_path = args.metadata_file

    # 1) Find matching PVCs
    old_pvcs = list_pvcs_in_storageclass(ns, origin_sc)
    if not old_pvcs:
        print(f"[!] No PVCs found in '{ns}' with SC '{origin_sc}'. Nothing to do.")
        return

    pivot_info = []
    print(f"[=] Found {len(old_pvcs)} PVC(s) in '{ns}' using SC '{origin_sc}'. Starting Phase 1.")

    # 2) For each, create temp + copy
    for pvc in old_pvcs:
        old_name = pvc.metadata.name
        size = pvc.spec.resources.requests["storage"]
        access_modes = pvc.spec.access_modes
        temp_name = f"{old_name}-temp"

        print(f"\n[>] Migrating '{old_name}' → temp '{temp_name}'")

        create_pvc(ns, temp_name, target_sc, size, access_modes, dry_run)
        copy_data_with_pod(ns, old_name, temp_name, prefix="pivot1", dry_run=dry_run)

        pivot_info.append({
            "old_name": old_name,
            "temp_name": temp_name,
            "size": size,
            "access_modes": access_modes
        })

    # 3) Write pivot metadata
    if not dry_run and pivot_info:
        with open(meta_path, "w") as f:
            json.dump(pivot_info, f, indent=2)
        print(f"[+] Wrote pivot metadata to '{meta_path}'")

    print("\n[!] Phase 1 complete. Please manually delete the old PVC(s) in the cluster.")
    print(f"    After that, run this script again with `--recreate` to finalize the migration to '{target_sc}'.\n")

def phase_two(args):
    """
    Phase 2: For each pivot record, recreate the old PVC name in the new SC,
    copy from <tempName> -> <oldName>, and optionally delete the temp PVC if not preserving.
    """
    target_sc = args.target_sc
    ns = args.namespace
    dry_run = args.dry_run
    meta_path = args.metadata_file
    preserve_temp = args.preserve_temp

    if not os.path.exists(meta_path):
        print(f"[!] Metadata file '{meta_path}' not found. Cannot proceed with --recreate.")
        return

    with open(meta_path, "r") as f:
        pivot_info = json.load(f)

    if not pivot_info:
        print("[!] Metadata is empty. Nothing to recreate.")
        return

    print(f"[=] Found {len(pivot_info)} pivot records in '{meta_path}'. Starting Phase 2.")

    for entry in pivot_info:
        old_name = entry["old_name"]
        temp_name = entry["temp_name"]
        size = entry["size"]
        access_modes = entry["access_modes"]

        print(f"\n[>] Recreating old PVC '{old_name}' in SC '{target_sc}' from temp '{temp_name}'")

        # 1) Create final PVC with old_name
        create_pvc(ns, old_name, target_sc, size, access_modes, dry_run)

        # 2) Copy temp->old
        copy_data_with_pod(ns, temp_name, old_name, prefix="pivot2", dry_run=dry_run)

        # 3) Possibly delete the temp
        if not preserve_temp:
            if dry_run:
                print(f"[DRY-RUN] Would delete temp PVC '{temp_name}'")
            else:
                v1.delete_namespaced_persistent_volume_claim(temp_name, ns)
                print(f"[x] Deleted temp PVC '{temp_name}'")
        else:
            print(f"[~] Preserving temp PVC '{temp_name}' as requested by --preserve-temp")

    # Cleanup metadata file
    if not dry_run:
        os.remove(meta_path)
        print(f"[~] Removed metadata file '{meta_path}'")

    print("\n[✓] Phase 2 complete. Your PVCs now exist under their original names in the new storage class.")

def main():
    args = parse_args()
    if args.recreate:
        phase_two(args)
    else:
        phase_one(args)

if __name__ == "__main__":
    main()
