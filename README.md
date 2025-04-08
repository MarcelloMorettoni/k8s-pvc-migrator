# k8s-pvc-migrator

This repo contains two files that act differently. moveStorage.py is experimental (help me to develop it), double_pivot_all.py is solid but requires manual intervention. 

# moveStorage.py

This is a Python script (`moveStorage.py`) that enables you to **safely migrate PVCs (Persistent Volume Claims) from one Kubernetes StorageClass to another**. It supports both snapshot-based migration (if available) and pivot-based data copying. After the migration, it can optionally delete the old PVCs and patch your workloads to use the newly created ones.

---

## Features

- **Volume Snapshot Support**: If your cluster supports `VolumeSnapshot` for the origin storage class, the script can utilize it to clone data instead of using a pivot pod.
- **Pivot Copy**: If snapshots aren't available or desired, the script creates a temporary pod to `rsync` data from the old PVC to the new one.
- **Automated Workload Updates**: The script can patch `Deployments`, `StatefulSets`, and `DaemonSets` that reference the old PVC or the old StorageClass.
- **Minimal Pod Manifests Export**: It can export standalone pods (which can’t be live-patched) into a `manifests/` directory, along with a `post-migrate.sh` script to help you re-deploy them.
- **Dry Run Mode**: Allows you to see which objects would be affected without making any changes.
- **Optional Cleanup**: Optionally delete the old PVC after the migration is verified.

---

## Prerequisites

1. **Kubernetes cluster access**: Ensure you have a valid `kubeconfig` and the correct context selected.
2. **Python 3.7+**: This script uses modern Python features.
3. **Kubernetes Python client**: (`pip install kubernetes pyyaml`)
4. **Optional**: For snapshot-based migration, you need a valid `VolumeSnapshotClass` that is compatible with your storage driver.

---

## Usage

From a command line where you have Kubernetes credentials (e.g., `kubectl` can connect), run:

```bash
python3 moveStorage.py <origin_storage_class> <target_storage_class> -n <namespace> [flags...]

# Simple usage:
python3 moveStorage.py local-path target-path -n staging

# Dry run:
python3 moveStorage.py local-path target-path -n staging --dry-run

# Prefer snapshot (if supported):
python3 moveStorage.py local-path target-path -n staging --prefer-snapshot

# Export standalone pod manifests for manual re-creation:
python3 moveStorage.py local-path target-path -n staging --export-pods

# Delete old PVC after successful migration:
python3 moveStorage.py local-path target-path -n staging --delete-pvc

# Copy only (no workload patching or PVC deletion):
python3 moveStorage.py local-path target-path -n staging --copy-only
```

# Double Pivot PVC Migration - double_pivot_all.py

This tool migrates one or more PVCs from an **old storage class** to a **new storage class** while **keeping the same PVC name**. It uses a “double pivot” approach in **two phases**, managed by a **single Python script**.

By default, it:
1. **Phase 1**: 
   - Finds all PVCs referencing the old storage class.
   - Creates “temp” PVCs (e.g. `<originalName>-temp`) in the new storage class.
   - Copies data from `<originalName>` → `<originalName>-temp`.
   - Stores metadata (mapping old → temp) in a JSON file.
   - Tells you to **manually delete** the old PVC(s) once ready.
2. **Phase 2**:
   - Recreates each old PVC name in the new storage class.
   - Copies data from `<originalName>-temp` → `<originalName>`.
   - (Optionally) deletes `<originalName>-temp`.
   - Removes the metadata file.

This preserves the original PVC names so you **do not** need to patch workloads. All data is copied to the “temp” PVC first; after you delete the original, the final PVC is recreated in the new storage class.

---


Below is an outline for how to run and use `double_pivot_all.py`.

### Installation

1. Clone or copy this script into your environment.  
2. Install dependencies (e.g., `kubectl`, a valid Python environment, and [Kubernetes Python client](https://pypi.org/project/kubernetes/)):

   ```bash
   pip install kubernetes
   ```

### USAGE:
```bash
python3 double_pivot_all.py <oldStorageClass> <newStorageClass> -n <namespace> [--recreate] [options]
```

### Phase 1: Creating Temp PVCs

Identify all PVCs in the old storage class.

Create <pvcName>-temp in the new storage class.

Copy data from <pvcName> → <pvcName>-temp.

Write pivot mapping into double_pivot_metadata.json.

```bash
python3 double_pivot_all.py local-path nvme-o-tcp -n pv-test
```

output:
```
[=] Found 2 PVC(s) in 'pv-test' using SC 'local-path'.
[>] Handling PVC 'data1' → 'data1-temp'
[+] Created temp PVC 'data1-temp' in SC 'nvme-o-tcp'
[~] Pivot pod 'pivot1-data1' created. Waiting for it to finish...
[✓] Pod finished with status: Succeeded
[x] Deleted pivot pod 'pivot1-data1'
[>] Handling PVC 'data2' → 'data2-temp'
...
[+] Wrote pivot metadata to 'double_pivot_metadata.json'
[!] Phase 1 complete. Please manually delete the old PVC(s). Then run this script again with --recreate.
```

# MANUAL STEP:
```
kubectl delete pvc data1 -n pv-test
kubectl delete pvc data2 -n pv-test
```

### Phase 2: Recreating new pvcs with the original names
You must then use the same script with "--recreate"

```
python3 double_pivot_all.py local-path target-path -n pv-test --recreate
```

## OUTPUT:
```
[=] Found 2 pivot records in 'double_pivot_metadata.json'.
[>] Recreating 'data1' in SC 'nvme-o-tcp'
[~] Pivot pod 'pivot2-data1' created. ...
[✓] Pod finished with status: Succeeded
[x] Deleted pivot pod 'pivot2-data1'
[x] Deleting temp PVC 'data1-temp'
...
[✓] Phase 2 complete. PVCs now exist with their original names in the new storage class.
```

### EXTRA FLAGS: --preserve-temp

Preserves the temp PVC used by the pivot

```bash
python3 double_pivot_all.py local-path target-path -n pv-test --recreate --preserve-temp
```

