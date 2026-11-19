#!/usr/bin/env python3
"""OpenShift inactive pod cleanup.

Default mode is DRY RUN. Pods are only deleted when --execute is given
(and, unless --yes is passed, after an interactive confirmation).
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone

VALID_PHASES = {"Pending", "Running", "Succeeded", "Failed", "Unknown"}
DEFAULT_DELETE_STATUSES = "Failed,Succeeded,Unknown"

# Pods in these namespaces are listed but never deleted.
PROTECTED_NAMESPACES = {
    "default",
    "openshift",
    "kube-system",
    "kube-public",
    "kube-node-lease",
}
PROTECTED_PREFIXES = ("kube-", "openshift-")


class OcError(Exception):
    """An oc invocation failed."""


def run_oc(args, timeout=120):
    """Run `oc <args>` and return stdout. Raises OcError on any failure."""
    cmd = ["oc", *args]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=True, timeout=timeout
        )
    except FileNotFoundError:
        raise OcError("OpenShift CLI (oc) not found in PATH.")
    except subprocess.TimeoutExpired:
        raise OcError(f"Timed out after {timeout}s: {' '.join(cmd)}")
    except subprocess.CalledProcessError as e:
        raise OcError(f"{' '.join(cmd)} failed: {e.stderr.strip()}")
    return result.stdout


def is_protected(namespace):
    return namespace in PROTECTED_NAMESPACES or namespace.startswith(PROTECTED_PREFIXES)


def get_pods(namespaces=None):
    """Return pod objects from all namespaces, or only the given ones."""
    if not namespaces:
        return json.loads(run_oc(["get", "pods", "-A", "-o", "json"]))["items"]

    pods = []
    for ns in namespaces:
        pods.extend(json.loads(run_oc(["get", "pods", "-n", ns, "-o", "json"]))["items"])
    return pods


def age_hours(pod):
    """Hours since the pod was created (0 if the timestamp is unavailable)."""
    ts = pod["metadata"].get("creationTimestamp")
    if not ts:
        return 0
    created = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return int((datetime.now(timezone.utc) - created).total_seconds() // 3600)


def parse_statuses(value):
    statuses = {s.strip() for s in value.split(",") if s.strip()}
    invalid = statuses - VALID_PHASES
    if invalid:
        raise ValueError(
            f"Invalid pod phase(s): {', '.join(sorted(invalid))}. "
            f"Valid phases: {', '.join(sorted(VALID_PHASES))}"
        )
    if not statuses:
        raise ValueError("--delete-status must list at least one phase.")
    return statuses


def find_eligible(pods, statuses, older_than_hours):
    eligible = []
    for pod in pods:
        meta = pod["metadata"]
        ns, name = meta["namespace"], meta["name"]
        phase = pod.get("status", {}).get("phase", "Unknown")
        if phase not in statuses:
            continue
        if meta.get("deletionTimestamp"):  # already terminating
            continue
        if is_protected(ns):
            continue
        age = age_hours(pod)
        if age < older_than_hours:
            continue
        eligible.append((ns, name, phase, age, meta.get("uid", "")))
    return eligible


def still_eligible(ns, name, uid, statuses):
    """Re-fetch the pod right before deleting it.

    Returns (ok, reason). A pod can be replaced between listing and deleting
    (e.g. a StatefulSet recreating a failed pod under the same name), so skip
    it unless it is the same pod (same UID) and still eligible.
    """
    out = run_oc(["get", "pod", name, "-n", ns, "--ignore-not-found", "-o", "json"])
    if not out.strip():
        return False, "already gone"
    pod = json.loads(out)
    meta = pod["metadata"]
    if uid and meta.get("uid") != uid:
        return False, "pod was replaced (UID changed)"
    if meta.get("deletionTimestamp"):
        return False, "already terminating"
    phase = pod.get("status", {}).get("phase", "Unknown")
    if phase not in statuses:
        return False, f"phase is now {phase}"
    return True, ""


def confirm(count):
    if not sys.stdin.isatty():
        print("ERROR: non-interactive session; pass --yes to skip confirmation.")
        return False
    return input(f"\nDelete {count} pod(s)? [y/N] ").strip().lower() in ("y", "yes")


def cleanup_pods(dry_run, statuses, namespaces, older_than_hours, show_all, assume_yes):
    user = run_oc(["whoami"]).strip()
    print(f"Connected user: {user}")
    print(f"Dry run       : {str(dry_run).lower()}")
    print(f"Delete phases : {', '.join(sorted(statuses))}")
    if namespaces:
        print(f"Namespaces    : {', '.join(namespaces)}")
    if older_than_hours:
        print(f"Older than    : {older_than_hours}h")

    pods = get_pods(namespaces)

    if show_all:
        print("\n========== POD INVENTORY ==========")
        for pod in pods:
            meta = pod["metadata"]
            phase = pod.get("status", {}).get("phase", "Unknown")
            print(f"{meta['namespace']:<30} {meta['name']:<50} {phase}")

    eligible = find_eligible(pods, statuses, older_than_hours)

    print("\n========== ELIGIBLE PODS ==========")
    if not eligible:
        print("None.")
    for ns, name, phase, age, _uid in eligible:
        print(f"{ns}/{name}  [{phase}, {age}h old]")

    deleted = failed = skipped = 0
    if eligible and not dry_run:
        if not assume_yes and not confirm(len(eligible)):
            print("Aborted. No pods were deleted.")
            return 1
        print("\n========== DELETING PODS ==========")
        for ns, name, _phase, _age, uid in eligible:
            try:
                ok, reason = still_eligible(ns, name, uid, statuses)
                if not ok:
                    print(f"SKIPPED: {ns}/{name}: {reason}")
                    skipped += 1
                    continue
                run_oc(["delete", "pod", name, "-n", ns, "--wait=false"])
                print(f"Deletion requested: {ns}/{name}")
                deleted += 1
            except OcError as e:
                print(f"FAILED: {ns}/{name}: {e}")
                failed += 1

    print("\n========== CLEANUP SUMMARY ==========")
    print(f"Total pods scanned : {len(pods)}")
    print(f"Eligible pods      : {len(eligible)}")
    print(f"Deletion requested : {deleted}")
    print(f"Skipped (changed)  : {skipped}")
    print(f"Deletion failures  : {failed}")
    print(f"Dry run enabled    : {str(dry_run).lower()}")

    if dry_run:
        print("\nDRY RUN: No pods were deleted. Use --execute to delete.")
    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(description="OpenShift inactive pod cleanup")
    parser.add_argument("--execute", action="store_true",
                        help="Actually delete eligible pods (default is dry run).")
    parser.add_argument("--yes", action="store_true",
                        help="Skip the confirmation prompt when using --execute.")
    parser.add_argument("--delete-status", default=DEFAULT_DELETE_STATUSES,
                        help="Comma-separated pod phases eligible for deletion "
                             f"(default: {DEFAULT_DELETE_STATUSES}).")
    parser.add_argument("--namespace", action="append",
                        help="Restrict to a namespace. Can be repeated.")
    parser.add_argument("--older-than-hours", type=int, default=0,
                        help="Only delete pods created at least this many hours ago.")
    parser.add_argument("--show-all", action="store_true",
                        help="Also print the full pod inventory.")
    args = parser.parse_args()

    if args.older_than_hours < 0:
        parser.error("--older-than-hours must be a non-negative integer.")

    try:
        statuses = parse_statuses(args.delete_status)
    except ValueError as e:
        parser.error(str(e))

    try:
        return cleanup_pods(
            dry_run=not args.execute,
            statuses=statuses,
            namespaces=list(dict.fromkeys(args.namespace)) if args.namespace else None,
            older_than_hours=args.older_than_hours,
            show_all=args.show_all,
            assume_yes=args.yes,
        )
    except OcError as e:
        print(f"ERROR: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
