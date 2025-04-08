# k8s-pvc-migrator
CLI-based tool that simplifies migrating Kubernetes PersistentVolumeClaims (PVCs) from one storage class to another. It scans for existing PVCs, optionally creates snapshots or pivot Pods to copy data, and can patch your workloads to reference the new PVCs. You can also delete the original PVCs after a successful migration. 
