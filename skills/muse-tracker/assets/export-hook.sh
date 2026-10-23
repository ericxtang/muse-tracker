#!/usr/bin/env bash
# One offline native invocation. Decides whether an export is due; no credentials.
set -uo pipefail
source "${HATCH_HOOK_RUNTIME:?Run this script through the Muse native hook runtime}"
config=${1:?usage: export-hook.sh ABSOLUTE_CONFIG_PATH [--dry-run]}
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
args=(hook --config "$config")
if [[ ${2:-} == --dry-run || ${HATCH_HOOK_DRY_RUN:-0} == 1 ]]; then args+=(--dry-run); fi
result=$(python3 "$script_dir/../scripts/muse_tracker.py" "${args[@]}") || {
  log 'export check failed' "$result"
  silent 'export check failed; retry on the next invocation'
  exit 0
}
case $(jq -r '.decision' <<<"$result") in
  wake) wake "$(jq -r '.reason' <<<"$result")" "$(jq -c '.payload' <<<"$result")" ;;
  *) silent "$(jq -r '.reason' <<<"$result")" ;;
esac
