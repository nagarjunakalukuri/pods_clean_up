#!/bin/bash
#
# OpenShift inactive pod cleanup.
# Default mode is DRY RUN. Pods are only deleted with --execute
# (and, unless --yes is given, after an interactive confirmation).
#
# Requires: oc, jq. Save with LF line endings.

set -u -o pipefail

DRY_RUN=true
ASSUME_YES=false
SHOW_ALL=false
OLDER_THAN_HOURS=0
DELETE_STATUSES_CSV="Failed,Succeeded,Unknown"
NAMESPACES=()

VALID_PHASES=("Pending" "Running" "Succeeded" "Failed" "Unknown")
PROTECTED_NAMESPACES=("default" "openshift" "kube-system" "kube-public" "kube-node-lease")

usage() {
    cat <<EOF
Usage: $0 [options]

  --execute                 Delete eligible pods (default is dry run)
  --dry-run                 Dry run (default)
  --yes                     Skip the confirmation prompt with --execute
  --delete-status A,B,C     Pod phases eligible for deletion
                            (default: $DELETE_STATUSES_CSV)
  --namespace NAME          Restrict to a namespace (repeatable)
  --older-than-hours N      Only pods created at least N hours ago
  --show-all                Also print the full pod inventory
  -h, --help                Show this help
EOF
}

die() { echo "ERROR: $*" >&2; exit 1; }

# ---------- argument parsing ----------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --execute)   DRY_RUN=false ;;
        --dry-run)   DRY_RUN=true ;;
        --yes)       ASSUME_YES=true ;;
        --show-all)  SHOW_ALL=true ;;
        --delete-status)
            [[ $# -ge 2 ]] || die "--delete-status requires a value"
            DELETE_STATUSES_CSV="$2"; shift ;;
        --namespace)
            [[ $# -ge 2 ]] || die "--namespace requires a value"
            NAMESPACES+=("$2"); shift ;;
        --older-than-hours)
            [[ $# -ge 2 ]] || die "--older-than-hours requires a value"
            OLDER_THAN_HOURS="$2"; shift ;;
        -h|--help)   usage; exit 0 ;;
        *)           echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
    esac
    shift
done

[[ "$OLDER_THAN_HOURS" =~ ^[0-9]+$ ]] || die "--older-than-hours must be a non-negative integer"

IFS=',' read -ra DELETE_STATUSES <<< "$DELETE_STATUSES_CSV"
[[ ${#DELETE_STATUSES[@]} -gt 0 ]] || die "--delete-status must list at least one phase"
for s in "${DELETE_STATUSES[@]}"; do
    valid=false
    for v in "${VALID_PHASES[@]}"; do [[ "$s" == "$v" ]] && valid=true; done
    $valid || die "Invalid pod phase '$s'. Valid: ${VALID_PHASES[*]}"
done

# ---------- prerequisites ----------
command -v oc &>/dev/null || die "oc CLI not installed."
command -v jq &>/dev/null || die "jq not installed."
oc whoami &>/dev/null     || die "Not logged into OpenShift (run 'oc login')."

echo "Connected user: $(oc whoami)"
echo "Dry run       : $DRY_RUN"
echo "Delete phases : ${DELETE_STATUSES[*]}"
[[ ${#NAMESPACES[@]} -gt 0 ]] && echo "Namespaces    : ${NAMESPACES[*]}"
[[ "$OLDER_THAN_HOURS" -gt 0 ]] && echo "Older than    : ${OLDER_THAN_HOURS}h"

# ---------- helpers ----------
is_protected_namespace() {
    local ns="$1" p
    [[ "$ns" == openshift-* || "$ns" == kube-* ]] && return 0
    for p in "${PROTECTED_NAMESPACES[@]}"; do
        [[ "$ns" == "$p" ]] && return 0
    done
    return 1
}

is_delete_status() {
    local s
    for s in "${DELETE_STATUSES[@]}"; do
        [[ "$1" == "$s" ]] && return 0
    done
    return 1
}

fetch_pods() {
    local ns
    if [[ ${#NAMESPACES[@]} -gt 0 ]]; then
        for ns in "${NAMESPACES[@]}"; do
            oc get pods -n "$ns" -o json || return 1
        done
    else
        oc get pods -A -o json
    fi
}

# ---------- collect pods ----------
PODS_JSON=$(fetch_pods) || die "Failed to list pods."

# One TSV line per pod: namespace, name, phase, terminating, age in hours.
POD_TSV=$(printf '%s' "$PODS_JSON" | jq -r -s '
    [.[].items[]] | .[] | [
        .metadata.namespace,
        .metadata.name,
        (.status.phase // "Unknown"),
        (if .metadata.deletionTimestamp then "true" else "false" end),
        (((now - (.metadata.creationTimestamp | fromdateiso8601)) / 3600) | floor)
    ] | @tsv
') || die "Failed to parse pod list with jq."

TOTAL=0
ELIG_NS=(); ELIG_NAME=(); ELIG_PHASE=(); ELIG_AGE=()

[[ "$SHOW_ALL" == "true" ]] && echo -e "\n========== POD INVENTORY =========="

while IFS=$'\t' read -r namespace name phase terminating age; do
    [[ -z "$name" ]] && continue
    TOTAL=$((TOTAL + 1))

    [[ "$SHOW_ALL" == "true" ]] && printf "%-30s %-50s %s\n" "$namespace" "$name" "$phase"

    is_delete_status "$phase"                 || continue
    [[ "$terminating" == "true" ]]            && continue
    is_protected_namespace "$namespace"       && continue
    [[ "$age" -lt "$OLDER_THAN_HOURS" ]]      && continue

    ELIG_NS+=("$namespace"); ELIG_NAME+=("$name")
    ELIG_PHASE+=("$phase");  ELIG_AGE+=("$age")
done <<< "$POD_TSV"

ELIGIBLE=${#ELIG_NS[@]}

echo -e "\n========== ELIGIBLE PODS =========="
if [[ $ELIGIBLE -eq 0 ]]; then
    echo "None."
fi
for ((i = 0; i < ELIGIBLE; i++)); do
    echo "${ELIG_NS[i]}/${ELIG_NAME[i]}  [${ELIG_PHASE[i]}, ${ELIG_AGE[i]}h old]"
done

# ---------- delete ----------
DELETED=0
FAILED=0

if [[ $ELIGIBLE -gt 0 && "$DRY_RUN" == "false" ]]; then
    if [[ "$ASSUME_YES" != "true" ]]; then
        [[ -t 0 ]] || die "Non-interactive session; pass --yes to skip confirmation."
        read -r -p "Delete $ELIGIBLE pod(s)? [y/N] " answer
        if [[ ! "$answer" =~ ^[Yy]([Ee][Ss])?$ ]]; then
            echo "Aborted. No pods were deleted."
            exit 1
        fi
    fi

    echo -e "\n========== DELETING PODS =========="
    for ((i = 0; i < ELIGIBLE; i++)); do
        if oc delete pod "${ELIG_NAME[i]}" -n "${ELIG_NS[i]}" --wait=false </dev/null; then
            echo "Deletion requested: ${ELIG_NS[i]}/${ELIG_NAME[i]}"
            DELETED=$((DELETED + 1))
        else
            echo "FAILED: ${ELIG_NS[i]}/${ELIG_NAME[i]}"
            FAILED=$((FAILED + 1))
        fi
    done
fi

# ---------- summary ----------
echo -e "\n========== CLEANUP SUMMARY =========="
echo "Total pods scanned : $TOTAL"
echo "Eligible pods      : $ELIGIBLE"
echo "Deletion requested : $DELETED"
echo "Deletion failures  : $FAILED"
echo "Dry run enabled    : $DRY_RUN"

if [[ "$DRY_RUN" == "true" ]]; then
    echo -e "\nDRY RUN: No pods were deleted. Use --execute to delete."
fi

[[ $FAILED -eq 0 ]]
