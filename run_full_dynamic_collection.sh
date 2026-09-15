#!/usr/bin/env bash

set -uo pipefail
umask 0002

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ARCH_LIST_ROOT=${ARCH_LIST_ROOT:-"$HOME/arch_packages"}
DATA_ROOT=${DATA_ROOT:-/data}
LLVM_BUILD=${LLVM_BUILD:-"$HOME/llvm-project/build"}
PIN_ROOT=${PIN_ROOT:-"$SCRIPT_DIR/Mypintool"}
MAKEPKG_CONF=${MAKEPKG_CONF:-"$SCRIPT_DIR/.makepkg.conf"}
MIN_FREE_GB=${MIN_FREE_GB:-25}

run_collection() {
    local package_set=$1
    local url_list="$ARCH_LIST_ROOT/$package_set/clone_urls.txt"
    local output="$DATA_ROOT/dynamic-$package_set"
    local work="$DATA_ROOT/work-$package_set"

    if [[ ! -s $url_list ]]; then
        echo "Package URL list is missing or empty: $url_list" >&2
        return 2
    fi

    echo "[$(date --iso-8601=seconds)] Beginning $package_set dynamic collection"
    CLEAN_WORKTREES=1 \
    DYNAMIC_ONLY=1 \
    PACKAGE_SET="$package_set" \
    BINARY_STORE="$DATA_ROOT/binaries" \
    MIN_FREE_GB="$MIN_FREE_GB" \
    WORK_ROOT="$work" \
    LLVM_BUILD="$LLVM_BUILD" \
    PIN_ROOT="$PIN_ROOT" \
    MAKEPKG_CONF="$MAKEPKG_CONF" \
        "$SCRIPT_DIR/collect_dynamic.sh" "$url_list" "$output"
}

core_status=0
extra_status=0
run_collection core || core_status=$?
if (( core_status == 75 )); then
    echo "Core paused for low disk space; Extra was not started." >&2
    exit 75
fi

run_collection extra || extra_status=$?
if (( extra_status == 75 )); then
    echo "Extra paused for low disk space." >&2
    exit 75
fi

if (( core_status != 0 || extra_status != 0 )); then
    echo "Full collection traversed both lists with package failures (Core=$core_status, Extra=$extra_status)." >&2
    exit 1
fi

echo "Full Core and Extra dynamic collection completed successfully."
