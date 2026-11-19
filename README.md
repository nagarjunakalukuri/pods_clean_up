# OpenShift Pod Cleanup Automation

Two standalone scripts that find inactive pods in an OpenShift cluster and, only when explicitly asked, delete them. Both use the OpenShift CLI (`oc`) and share the same behavior and options.

| Script | Notes |
|---|---|
| `pod_cleanup.py` | Python 3, no third-party packages. Recommended. |
| `pod_cleanup.sh` | Bash, requires `jq`. |

Both default to **dry run**: nothing is deleted unless `--execute` is passed.

## Project structure

```text
pod-cleanup/
├── pod_cleanup.py
├── pod_cleanup.sh
└── README.md
```

## What gets deleted

A pod is eligible when **all** of these are true:

- Its phase is in the delete list (default: `Failed`, `Succeeded`, `Unknown`).
- It is not already terminating.
- Its namespace is not protected.
- It is at least `--older-than-hours` old, measured from creation time (default: 0, no age limit).

| Pod phase | Default action |
|---|---|
| Running | Retain |
| Pending | Retain |
| Succeeded | Eligible |
| Failed | Eligible |
| Unknown | Eligible |

The scripts classify by Kubernetes **pod phase**, not by container readiness or individual container states.

**Protected namespaces** (listed, never deleted): `default`, `openshift`, `kube-system`, `kube-public`, `kube-node-lease`, and anything starting with `kube-` or `openshift-`.

## Prerequisites

- `oc` (OpenShift CLI): `oc version`
- Python 3 for the Python script: `python3 --version`
- `jq` for the Bash script: `jq --version`
- A logged-in session: `oc login <OPENSHIFT_CLUSTER_URL>`, then `oc whoami`
- Permission to list pods (cluster-wide, or in the namespaces you target) and delete pods in the target namespaces.

On Windows, run the Bash script from Git Bash or WSL and keep the file saved with LF line endings (CRLF breaks bash).

Check permissions:

```bash
oc auth can-i list pods --all-namespaces
oc auth can-i delete pods -n <NAMESPACE_NAME>
```

## Options

The two scripts accept the same options.

| Option | Description |
|---|---|
| `--execute` | Delete eligible pods. Without it the run is a dry run. |
| `--dry-run` | Explicit dry run (Bash only; Python is a dry run unless `--execute` is given). |
| `--yes` | Skip the confirmation prompt when using `--execute`. Required in non-interactive sessions. |
| `--delete-status A,B,C` | Pod phases eligible for deletion. Validated; typos are rejected. |
| `--namespace NAME` | Restrict to a namespace. Repeat for several namespaces. |
| `--older-than-hours N` | Only pods created at least N hours ago. |
| `--show-all` | Also print every pod scanned (off by default, since it is noisy). |

Exit code is `0` on success and `1` on any error or failed deletion.

## Usage

### Dry run (default)

```bash
python3 pod_cleanup.py
./pod_cleanup.sh            # after: chmod +x pod_cleanup.sh
```

This connects with your existing `oc` session, lists the eligible pods and prints a summary. Nothing is deleted.

### Delete

```bash
python3 pod_cleanup.py --execute
./pod_cleanup.sh --execute
```

You are asked `Delete N pod(s)? [y/N]` before anything is deleted.

### Common variations

```bash
# Only Failed pods, older than 24 hours
python3 pod_cleanup.py --delete-status Failed --older-than-hours 24 --execute

# One or more namespaces
python3 pod_cleanup.py --namespace project-a --namespace project-b
./pod_cleanup.sh --namespace project-a

# Save a log of the run
python3 pod_cleanup.py --execute 2>&1 | tee cleanup-$(date +%F).log
```

## Example output

Dry run (both scripts print this format):

```text
Connected user: developer
Dry run       : True
Delete phases : Failed, Succeeded, Unknown

========== ELIGIBLE PODS ==========
project-a/application-456  [Failed, 30h old]
project-b/application-789  [Succeeded, 52h old]

========== CLEANUP SUMMARY ==========
Total pods scanned : 3
Eligible pods      : 2
Deletion requested : 0
Deletion failures  : 0
Dry run enabled    : True

DRY RUN: No pods were deleted. Use --execute to delete.
```

The Bash script prints `true`/`false` in lowercase for the dry-run values. Actual pod names and phases depend on the cluster.

With `--execute`, after confirmation, a `DELETING PODS` section is added:

```text
========== DELETING PODS ==========
Deletion requested: project-a/application-456
Deletion requested: project-b/application-789
```

Deletes use `--wait=false`, so "Deletion requested" means the API accepted the request, not that the pod is already gone. Verify with `oc get pods -A`.

## Safety notes

1. Always run a dry run first and review the eligible list.
2. Deleting `Failed` and `Succeeded` pods **removes their logs and events**. Capture anything you need first, and consider `--older-than-hours` so recent failures stay available for debugging.
3. Deleting a finished pod does not normally cause it to be recreated; the owning Job or ReplicaSet has already finished or replaced it.
4. `Unknown` pods usually sit on unreachable nodes and may stay in `Terminating` after a plain delete. The scripts do not force-delete (`--force --grace-period=0`), because that is risky for stateful workloads.
5. Pending and Running pods are never touched by default.
6. Make sure the executing account has the intended RBAC permissions, and follow your cluster's change-approval policy before using `--execute` in production.

## Troubleshooting

| Issue | Possible solution |
|---|---|
| `oc: command not found` | Install the OpenShift CLI and add it to `PATH`. |
| `jq not installed` | Install `jq` (Bash script only). |
| `Not logged into OpenShift` / authentication error | Run `oc login` again. |
| Forbidden error | Check list permission (cluster-wide, or use `--namespace`) and delete permission. |
| No pods listed | Check `oc project`, `oc get namespaces` and namespace access. |
| Pod not deleted | Check RBAC and the pod's termination state; `Unknown` pods may need manual force deletion. |
| `$'\r': command not found` | The `.sh` file has CRLF line endings; convert to LF. |
| `non-interactive session; pass --yes` | You used `--execute` without a terminal; add `--yes`. |

Useful diagnostics:

```bash
oc whoami
oc project
oc get namespaces
oc get pods -A
```

## Recommended execution strategy

1. Validate cluster connectivity (`oc whoami`).
2. Run a dry run.
3. Review the eligible pods.
4. Obtain operational approval.
5. Run with `--execute`.
6. Verify the results with `oc get pods -A`.
7. Save the execution log (`| tee`).

## Possible future enhancements

- Namespace allowlist/denylist from a config file.
- CSV cleanup reports and timestamped logging.
- Excluding pods that belong to specific workloads.
- Scheduled cleanup (for example, as a CronJob).
