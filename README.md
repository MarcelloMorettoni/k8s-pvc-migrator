# k8s-pvc-migrator

# Kubernetes PVC Storage Migration Tool: `double_pivot_safe.py`

## Overview

`double_pivot_safe.py` is a Kubernetes-native Python tool designed to **safely migrate PersistentVolumeClaims (PVCs)** from one StorageClass to another, without risking data loss and with a small application downtime.

The script automates workload scaling, PVC creation, and data transfer using temporary pivot pods and supports a wide variety of Kubernetes workload types.

---

## Features

- Detects PVCs in a specified source StorageClass.
- Scales down workloads using those PVCs (optional).
- Creates new PVCs in the target StorageClass.
- Copies data between PVCs using temporary `rsync` pods.
- Recreates original PVCs in the target StorageClass.
- Restores scaled workloads after migration.
- Dry-run mode for full visibility before applying changes.

---

## Migration Workflow

### Phase 1: Preparation & Initial Copy
```bash
python3 double_pivot_safe.py old-storage-class new-storage-class -n my-namespace --set-replica-0
```
- Scales down workloads using PVCs from the source StorageClass.
- Creates new temporary PVCs in the target StorageClass.
- Copies data to the temp PVCs using pivot pods.

### Phase 2: Final Switch & Cleanup
```bash
python3 double_pivot_safe.py old-storage-class new-storage-class -n my-namespace --recreate
```
- Deletes the original PVCs.
- Recreates them in the new StorageClass.
- Copies data back from the temp PVCs.
- Deletes the temp PVCs.
- Restores workload replicas.

---

## Workloads Automatically Supported

| Workload Type | Phase 1: Scale Down | Phase 2: Restore |
|---------------|---------------------|------------------|
| Deployment    | ✅ Yes              | ✅ Yes           |
| StatefulSet   | ✅ Yes              | ✅ Yes           |
| ReplicaSet    | ✅ Yes              | ✅ Yes           |
| DaemonSet     | ✅ (patched)        | ✅ (unpatched)   |
| CronJob       | ✅ (suspended)      | ✅ (resumed)     |
| Job           | ✅ (deleted)        | ❌ Not Restored  |

---

## Manual Intervention May Be Required

- **Standalone Pods**: Not modified automatically.
- **Jobs**: Not recreated. Manually trigger if needed.
- **Custom operators**: You may need to pause and resume custom controllers or database operators during migration.
- **PVCs with ReadOnly mounts** may require manual inspection.

---

## Dry-Run Support

Use `--dry-run` to preview migration actions without making changes.

### Example Dry Run:
```bash
python3 double_pivot_safe.py old-storage-class new-storage-class -n my-namespace --set-replica-0 --dry-run
python3 double_pivot_safe.py old-storage-class new-storage-class -n my-namespace --recreate --dry-run
```

---

## Requirements

- Python 3.6+
- Kubernetes client configuration (`~/.kube/config`)
- `kubernetes` Python module (install with `pip install kubernetes`)

---

## Post-Migration Cleanup

After a successful migration, you can safely remove the old StorageClass and any unused CSI drivers or plugins if they are no longer needed.

