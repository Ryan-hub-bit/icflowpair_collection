#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
DEMO_DIR="$SCRIPT_DIR/examples/llm_icall_demo"

usage() {
    cat <<'EOF'
Usage: verify_llm_pipeline.sh [OUTPUT_DIRECTORY]

Run the LLM-to-ICFlow workflow offline with a checked-in structured response.
This verifies prompt rendering, response materialization, custom LLVM, MyPinTool,
make check, and indirect-call-pair comparison without using an OpenAI API key.

Required environment variables:
  LLVM_BUILD  Custom LLVM build directory containing bin/clang and bin/ld.lld
  PIN_ROOT    Intel Pin/MyPinTool root directory

Optional environment variables:
  PINTOOL     MyPinTool .so path
EOF
}

if [[ ${1:-} == "-h" || ${1:-} == "--help" ]]; then
    usage
    exit 0
fi
if [[ $# -gt 1 ]]; then
    usage >&2
    exit 2
fi

: "${LLVM_BUILD:?LLVM_BUILD must point to the custom LLVM build directory}"
: "${PIN_ROOT:?PIN_ROOT must point to the Intel Pin/MyPinTool root}"

for command in cp file find grep jq make python3 realpath; do
    if ! command -v "$command" >/dev/null 2>&1; then
        echo "Required command not found: $command" >&2
        exit 1
    fi
done

LLVM_BUILD=$(realpath "$LLVM_BUILD")
PIN_ROOT=$(realpath "$PIN_ROOT")
PINTOOL=${PINTOOL:-"$PIN_ROOT/source/tools/MyPinTool/obj-intel64/MyPinTool.so"}
PINTOOL=$(realpath "$PINTOOL")

if [[ ! -x "$LLVM_BUILD/bin/clang" ||
      ! -x "$LLVM_BUILD/bin/ld.lld" ||
      ! -x "$LLVM_BUILD/bin/llvm-nm" ]]; then
    echo "Custom clang, ld.lld, and llvm-nm are required under: $LLVM_BUILD/bin" >&2
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

if [[ $# -eq 1 ]]; then
    mkdir -p "$1"
    OUTPUT_ROOT=$(realpath "$1")
    if find "$OUTPUT_ROOT" -mindepth 1 -print -quit | grep -q .; then
        echo "Output directory must be empty: $OUTPUT_ROOT" >&2
        exit 1
    fi
else
    OUTPUT_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/icflow-llm-demo.XXXXXX")
fi

PROJECT_DIR="$OUTPUT_ROOT/project"
GENERATED_DIR="$OUTPUT_ROOT/generated"
BASELINE_DIR="$OUTPUT_ROOT/baseline"
CANDIDATE_DIR="$OUTPUT_ROOT/candidate"
mkdir -p "$PROJECT_DIR" "$BASELINE_DIR" "$CANDIDATE_DIR"
cp -a "$DEMO_DIR/project/." "$PROJECT_DIR/"

echo "[1/8] Render the project-specific prompt without an API request"
(
    cd "$SCRIPT_DIR"
    python3 -m llm_test_generation.generate_tests "$PROJECT_DIR" \
        --dry-run \
        --prompt-output "$OUTPUT_ROOT/rendered_prompt.md" >/dev/null
)
grep -q "maximize the number of unique" "$OUTPUT_ROOT/rendered_prompt.md"

echo "[2/8] Materialize a deterministic example of the structured model response"
(
    cd "$SCRIPT_DIR"
    python3 -m llm_test_generation.generate_tests "$PROJECT_DIR" \
        --output-dir "$GENERATED_DIR" \
        --response-file "$DEMO_DIR/offline_response.json" >/dev/null
)
cp -a "$GENERATED_DIR/tests/." "$PROJECT_DIR/tests/"

echo "[3/8] Build the project with the custom LLVM clang and lld"
"$LLVM_BUILD/bin/clang" --version | head -n 1 | tee "$OUTPUT_ROOT/clang-version.txt"
make -C "$PROJECT_DIR" \
    CC="$LLVM_BUILD/bin/clang" \
    LDFLAGS="-fuse-ld=$LLVM_BUILD/bin/ld.lld -no-pie" \
    all
file "$PROJECT_DIR/icall_demo" | tee "$OUTPUT_ROOT/binary-file.txt"

wrapped=0
restore_binary() {
    if (( wrapped == 1 )); then
        "$SCRIPT_DIR/restore_wrapped_elfs.sh" "$PROJECT_DIR" >/dev/null || true
    fi
}
trap restore_binary EXIT

echo "[4/8] Wrap the test executable with MyPinTool"
PIN_ROOT="$PIN_ROOT" PINTOOL="$PINTOOL" \
    WRAP_LOG="$OUTPUT_ROOT/wrapped-executions.tsv" \
    "$SCRIPT_DIR/wrap_with_mypintool.sh" "$PROJECT_DIR"
wrapped=1

ICALL_JSON="$PROJECT_DIR/icall_demo.orig_icall.json"
IJUMP_JSON="$PROJECT_DIR/icall_demo.orig_ijump.json"

echo "[5/8] Run the native input through make check and save the baseline"
make -C "$PROJECT_DIR" check TEST_CASE_FILE=tests/native_cases.txt
jq -e 'type == "object" and length > 0' "$ICALL_JSON" >/dev/null
cp "$ICALL_JSON" "$BASELINE_DIR/$(basename "$ICALL_JSON")"
cp "$IJUMP_JSON" "$BASELINE_DIR/$(basename "$IJUMP_JSON")"

echo "[6/8] Run the generated input through make check"
rm -f -- "$ICALL_JSON" "$IJUMP_JSON"
make -C "$PROJECT_DIR" check TEST_CASE_FILE=tests/generated_cases.txt
jq -e 'type == "object" and length > 0' "$ICALL_JSON" >/dev/null
cp "$ICALL_JSON" "$CANDIDATE_DIR/$(basename "$ICALL_JSON")"
cp "$IJUMP_JSON" "$CANDIDATE_DIR/$(basename "$IJUMP_JSON")"

echo "[7/8] Verify all three pair classes for every labeled callsite"
(
    cd "$SCRIPT_DIR"
    python3 -m llm_test_generation.analyze_icall_pairs \
        "$PROJECT_DIR/icall_demo.orig" \
        --icall-json "$CANDIDATE_DIR/$(basename "$ICALL_JSON")" \
        --llvm-nm "$LLVM_BUILD/bin/llvm-nm" \
        --output "$OUTPUT_ROOT/icall-pairs.json" \
        --require-dynamic \
        --require-static \
        --require-same-type-non-address-taken \
        --require-dynamic-covered
)

echo "[8/8] Require the generated input to add at least two indirect-call pairs"
(
    cd "$SCRIPT_DIR"
    python3 -m llm_test_generation.compare_icall_pairs \
        "$BASELINE_DIR" "$CANDIDATE_DIR" \
        --require-new 2 \
        --json-output "$OUTPUT_ROOT/comparison.json"
)

echo "Offline LLM-to-ICFlow verification passed. Results: $OUTPUT_ROOT"
