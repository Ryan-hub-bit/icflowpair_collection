#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

usage() {
    cat <<'EOF'
Usage: collect_dynamic.sh URL_LIST [OUTPUT_DIRECTORY]

URL_LIST contains one Arch packaging or AUR Git URL per line. Blank lines and
lines beginning with # are ignored.

Required environment variables:
  LLVM_BUILD   Build directory of Ryan-hub-bit/llvm-project. The repository
               .makepkg.conf expects $HOME/llvm-project/build.
  PIN_ROOT     Root directory of the Intel Pin/MyPinTool repository

Optional environment variables:
  PINTOOL       MyPinTool .so path
  WORK_ROOT     Package checkout directory (default: ./work)
  MAKEPKG_CONF  makepkg config (default: repository .makepkg.conf)
  BUILD_TIMEOUT First build timeout in seconds (default: 2000)
  TEST_TIMEOUT  Instrumented test timeout in seconds (default: 1800)
  CLEAN_WORKTREES
                Remove each package checkout after its logs/artifacts are
                copied (0 or 1; default: 0)
  DYNAMIC_ONLY  Keep artifacts only for binaries with an observed indirect
                call edge (0 or 1; default: 0)
EOF
}

if [[ ${1:-} == "-h" || ${1:-} == "--help" ]]; then
    usage
    exit 0
fi

if [[ $# -lt 1 || $# -gt 2 ]]; then
    usage >&2
    exit 2
fi

if [[ ! -f /etc/arch-release ]]; then
    echo "Dynamic collection must run on an Arch Linux machine." >&2
    exit 1
fi

if (( EUID == 0 )); then
    echo "Do not run this script as root; makepkg refuses to run as root." >&2
    exit 1
fi

: "${LLVM_BUILD:?LLVM_BUILD must point to the custom LLVM build directory}"
: "${PIN_ROOT:?PIN_ROOT must point to the Intel Pin kit root}"

for command in file find git grep makepkg python3 realpath sed timeout; do
    if ! command -v "$command" >/dev/null 2>&1; then
        echo "Required command not found: $command" >&2
        exit 1
    fi
done

URL_LIST=$(realpath "$1")
OUTPUT_ROOT=${2:-"$PWD/output"}
mkdir -p "$OUTPUT_ROOT"
OUTPUT_ROOT=$(realpath "$OUTPUT_ROOT")

WORK_ROOT=${WORK_ROOT:-"$PWD/work"}
mkdir -p "$WORK_ROOT"
WORK_ROOT=$(realpath "$WORK_ROOT")

LLVM_BUILD=$(realpath "$LLVM_BUILD")
PIN_ROOT=$(realpath "$PIN_ROOT")
PINTOOL=${PINTOOL:-"$PIN_ROOT/source/tools/MyPinTool/obj-intel64/MyPinTool.so"}
PINTOOL=$(realpath "$PINTOOL")
DEFAULT_MAKEPKG_CONF="$SCRIPT_DIR/.makepkg.conf"
MAKEPKG_CONF=${MAKEPKG_CONF:-"$DEFAULT_MAKEPKG_CONF"}
if [[ ! -f $MAKEPKG_CONF ]]; then
    echo "makepkg config does not exist: $MAKEPKG_CONF" >&2
    exit 1
fi
MAKEPKG_CONF=$(realpath "$MAKEPKG_CONF")
DEFAULT_MAKEPKG_CONF=$(realpath "$DEFAULT_MAKEPKG_CONF")
BUILD_TIMEOUT=${BUILD_TIMEOUT:-2000}
TEST_TIMEOUT=${TEST_TIMEOUT:-1800}
CLEAN_WORKTREES=${CLEAN_WORKTREES:-0}
DYNAMIC_ONLY=${DYNAMIC_ONLY:-0}

if [[ ! $BUILD_TIMEOUT =~ ^[1-9][0-9]*$ || ! $TEST_TIMEOUT =~ ^[1-9][0-9]*$ ]]; then
    echo "BUILD_TIMEOUT and TEST_TIMEOUT must be positive integers." >&2
    exit 1
fi
if [[ $CLEAN_WORKTREES != 0 && $CLEAN_WORKTREES != 1 ]]; then
    echo "CLEAN_WORKTREES must be 0 or 1." >&2
    exit 1
fi
if [[ $DYNAMIC_ONLY != 0 && $DYNAMIC_ONLY != 1 ]]; then
    echo "DYNAMIC_ONLY must be 0 or 1." >&2
    exit 1
fi

if [[ ! -x "$LLVM_BUILD/bin/clang" ||
      ! -x "$LLVM_BUILD/bin/clang++" ||
      ! -x "$LLVM_BUILD/bin/ld.lld" ||
      ! -x "$LLVM_BUILD/bin/llvm-nm" ]]; then
    echo "Custom clang/clang++/ld.lld/llvm-nm not found under: $LLVM_BUILD/bin" >&2
    exit 1
fi

if [[ $MAKEPKG_CONF == "$DEFAULT_MAKEPKG_CONF" && $LLVM_BUILD != "$HOME/llvm-project/build" ]]; then
    echo "The arch_scripts .makepkg.conf expects LLVM_BUILD=$HOME/llvm-project/build" >&2
    echo "Use that path or provide a different MAKEPKG_CONF." >&2
    exit 1
fi

if [[ ! -x "$PIN_ROOT/pin" ]]; then
    echo "Pin launcher is not executable: $PIN_ROOT/pin" >&2
    exit 1
fi

if [[ ! -f "$PINTOOL" ]]; then
    echo "MyPinTool shared object does not exist: $PINTOOL" >&2
    exit 1
fi

export PATH="$LLVM_BUILD/bin:$PATH"
export LLVM_BUILD
export CC="$LLVM_BUILD/bin/clang"
export CXX="$LLVM_BUILD/bin/clang++"

PROCESSED_FILE="$OUTPUT_ROOT/processed_urls.txt"
FAILURE_FILE="$OUTPUT_ROOT/failed_urls.txt"
SUMMARY_LOG="$OUTPUT_ROOT/collection.log"
touch "$PROCESSED_FILE" "$FAILURE_FILE" "$SUMMARY_LOG"

current_source_dir=""

restore_current_package() {
    if [[ -n $current_source_dir && -d $current_source_dir ]]; then
        "$SCRIPT_DIR/restore_wrapped_elfs.sh" "$current_source_dir" || true
    fi
    current_source_dir=""
}

trap restore_current_package EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

has_dynamic_icall_edges() {
    python3 - "$1" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as stream:
        payload = json.load(stream)
except (OSError, json.JSONDecodeError):
    raise SystemExit(2)

has_edges = isinstance(payload, dict) and any(
    isinstance(targets, list) and bool(targets) for targets in payload.values()
)
raise SystemExit(0 if has_edges else 1)
PY
}

copy_artifact() {
    local repository_dir=$1
    local package_output=$2
    local source=$3
    local relative destination

    relative=${source#"$repository_dir"/}
    destination="$package_output/artifacts/$relative"
    mkdir -p "$(dirname "$destination")"
    cp -- "$source" "$destination"
}

collect_artifacts() {
    local repository_dir=$1
    local package_output=$2
    local result binary ijump edge_status

    mkdir -p "$package_output/artifacts"

    while IFS= read -r -d '' result; do
        if [[ $DYNAMIC_ONLY == 1 ]]; then
            [[ $result == *'_icall.json' ]] || continue
            if has_dynamic_icall_edges "$result"; then
                edge_status=0
            else
                edge_status=$?
            fi
            if (( edge_status == 1 )); then
                continue
            fi
        fi

        copy_artifact "$repository_dir" "$package_output" "$result"

        if [[ $DYNAMIC_ONLY == 1 ]]; then
            ijump=${result%_icall.json}_ijump.json
            if [[ -f $ijump ]]; then
                copy_artifact "$repository_dir" "$package_output" "$ijump"
            fi
        fi

        binary=${result%_icall.json}
        binary=${binary%_ijump.json}
        if [[ ! -f $binary && $binary == *.orig && -f ${binary%.orig} ]]; then
            binary=${binary%.orig}
        fi
        if [[ -f $binary ]]; then
            copy_artifact "$repository_dir" "$package_output" "$binary"
        fi
    done < <(find "$repository_dir/src" -type f \( -name '*_icall.json' -o -name '*_ijump.json' \) -size +2c -print0 2>/dev/null)
}

cleanup_worktree() {
    local url=$1
    local package_name repository_dir

    [[ $CLEAN_WORKTREES == 1 ]] || return 0
    package_name=$(basename "$url" .git)
    if [[ -z $package_name || $package_name == . || $package_name == .. ]]; then
        echo "Refusing to clean invalid package worktree for: $url" >&2
        return 1
    fi
    repository_dir="$WORK_ROOT/$package_name"
    case "$repository_dir" in
        "$WORK_ROOT"/*) ;;
        *)
            echo "Refusing to clean worktree outside WORK_ROOT: $repository_dir" >&2
            return 1
            ;;
    esac
    if [[ -d $repository_dir ]]; then
        chmod -R u+w "$repository_dir" 2>/dev/null || true
        rm -rf -- "$repository_dir"
        echo "Cleaned package worktree: $repository_dir" | tee -a "$SUMMARY_LOG"
    fi
}

write_instrumented_pkgbuild() {
    local original_pkgbuild=$1
    local instrumented_pkgbuild=$2
    local execution_log=$3

    {
        printf 'source %q\n' "$original_pkgbuild"
        cat <<'EOF'

if declare -F check >/dev/null; then
    eval "$(declare -f check | sed '1s/^check /icflow_original_check /')"

    check() {
        local icflow_check_status=0

EOF
        printf '        PIN_ROOT=%q PINTOOL=%q WRAP_LOG=%q %q "$srcdir"\n' \
            "$PIN_ROOT" "$PINTOOL" "$execution_log" "$SCRIPT_DIR/wrap_with_mypintool.sh"
        cat <<'EOF'
        icflow_original_check "$@" || icflow_check_status=$?
EOF
        printf '        %q "$srcdir" || true\n' "$SCRIPT_DIR/restore_wrapped_elfs.sh"
        cat <<'EOF'
        return "$icflow_check_status"
    }
fi
EOF
    } > "$instrumented_pkgbuild"
}

process_package() {
    local url=$1
    local package_name repository_dir source_dir package_output execution_log
    local instrumented_pkgbuild

    package_name=$(basename "$url" .git)
    repository_dir="$WORK_ROOT/$package_name"
    source_dir="$repository_dir/src"
    package_output="$OUTPUT_ROOT/$package_name"
    execution_log="$package_output/wrapped-executions.tsv"
    mkdir -p "$package_output"

    echo "[$(date --iso-8601=seconds)] START $url" | tee -a "$SUMMARY_LOG"

    if [[ ! -d "$repository_dir/.git" ]]; then
        if ! git clone --depth 1 "$url" "$repository_dir" 2>&1 | tee -a "$package_output/build.log"; then
            echo "Clone failed: $url" >&2
            return 1
        fi
    fi

    if [[ ! -f "$repository_dir/PKGBUILD" ]]; then
        echo "PKGBUILD not found: $url" | tee -a "$package_output/build.log" >&2
        return 1
    fi

    if ! (
        cd "$repository_dir"
        timeout "$BUILD_TIMEOUT" makepkg \
            --config "$MAKEPKG_CONF" \
            --force --syncdeps --noconfirm --needed --skippgpcheck --nocheck
    ) 2>&1 | tee -a "$package_output/build.log"; then
        echo "Initial build failed: $url" >&2
        return 1
    fi

    if [[ ! -d "$source_dir" ]]; then
        echo "makepkg did not create a src directory: $url" >&2
        return 1
    fi

    : > "$execution_log"
    current_source_dir=$source_dir
    instrumented_pkgbuild="$repository_dir/PKGBUILD.icflow"
    write_instrumented_pkgbuild \
        "$repository_dir/PKGBUILD" "$instrumented_pkgbuild" "$execution_log"

    if ! (
        cd "$repository_dir"
        timeout "$TEST_TIMEOUT" makepkg \
            --config "$MAKEPKG_CONF" \
            -p "${instrumented_pkgbuild##*/}" \
            --force --syncdeps --noconfirm --needed --skippgpcheck
    ) 2>&1 | tee -a "$package_output/test.log"; then
        echo "Instrumented tests failed or timed out: $url" | tee -a "$SUMMARY_LOG" >&2
    fi
    rm -f -- "$instrumented_pkgbuild"

    collect_artifacts "$repository_dir" "$package_output"
    restore_current_package

    if find "$package_output/artifacts" -type f -name '*_icall.json' -print -quit | grep -q .; then
        if ! python3 "$SCRIPT_DIR/llm_test_generation/analyze_icall_pairs.py" \
            --collection-root "$package_output/artifacts" \
            --llvm-nm "$LLVM_BUILD/bin/llvm-nm" \
            --output "$package_output/icall-pair-manifest.json" \
            >> "$package_output/pair-analysis.log" 2>&1; then
            echo "Indirect-call pair analysis failed: $url" \
                | tee -a "$SUMMARY_LOG" >&2
            return 1
        fi
    fi

    if [[ ! -s "$execution_log" ]]; then
        echo "No wrapped test executable ran: $url" | tee -a "$SUMMARY_LOG" >&2
        return 1
    fi

    echo "$url" >> "$PROCESSED_FILE"
    echo "[$(date --iso-8601=seconds)] DONE  $url" | tee -a "$SUMMARY_LOG"
}

failures=0

while IFS= read -r url || [[ -n $url ]]; do
    url=${url%$'\r'}
    [[ -z $url || $url =~ ^[[:space:]]*# ]] && continue

    if grep -Fxq "$url" "$PROCESSED_FILE"; then
        echo "Already processed: $url" | tee -a "$SUMMARY_LOG"
        cleanup_worktree "$url" || true
        continue
    fi

    if ! process_package "$url"; then
        restore_current_package
        echo "$url" >> "$FAILURE_FILE"
        echo "[$(date --iso-8601=seconds)] FAIL  $url" | tee -a "$SUMMARY_LOG" >&2
        failures=$((failures + 1))
    fi
    cleanup_worktree "$url" || true
done < "$URL_LIST"

if find "$OUTPUT_ROOT" -path '*/artifacts/*' -type f \
    -name '*_icall.json' -print -quit | grep -q .; then
    python3 "$SCRIPT_DIR/llm_test_generation/analyze_icall_pairs.py" \
        --collection-root "$OUTPUT_ROOT" \
        --llvm-nm "$LLVM_BUILD/bin/llvm-nm" \
        --output "$OUTPUT_ROOT/icall-pair-manifest.json" \
        > "$OUTPUT_ROOT/pair-analysis.log"
fi

if (( failures > 0 )); then
    echo "Collection finished with $failures failed package(s). See: $FAILURE_FILE" >&2
    exit 1
fi

echo "Collection finished successfully. Results: $OUTPUT_ROOT"
