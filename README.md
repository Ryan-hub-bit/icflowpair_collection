# Reproduce ICFlow Dynamic Collection

Use either the prebuilt image or compile the toolchain yourself. Both choices
run on an x86-64 Linux Docker host and use the non-root `icflow` user required
by `makepkg`.

## Choice A: download the ready-to-run image

Download the image and its checksum from the
[`docker-v1` release](https://github.com/Ryan-hub-bit/icflow_dynamic_collection/releases/tag/docker-v1):

```bash
curl -LO https://github.com/Ryan-hub-bit/icflow_dynamic_collection/releases/download/docker-v1/icflow-dynamic-collection-amd64.tar.gz
curl -LO https://github.com/Ryan-hub-bit/icflow_dynamic_collection/releases/download/docker-v1/icflow-dynamic-collection-amd64.tar.gz.sha256
sha256sum -c icflow-dynamic-collection-amd64.tar.gz.sha256
docker load < icflow-dynamic-collection-amd64.tar.gz

git clone --branch codex/llm-test-generation \
  https://github.com/Ryan-hub-bit/icflow_dynamic_collection.git
cd icflow_dynamic_collection
docker build -f Dockerfile.prebuilt \
  --build-arg BASE_IMAGE=icflow-dynamic-collection:prebuilt \
  -t icflow-dynamic-collection:llm .

docker volume create icflow-data
docker run -it --init \
  --name icflow \
  --cap-add=SYS_PTRACE \
  --security-opt seccomp=unconfined \
  -v icflow-data:/data \
  icflow-dynamic-collection:llm
```

`Dockerfile.prebuilt` layers this repository revision onto the compiled release
image and fails its build if LLVM, lld, Pin, MyPinTool, or the Python unit tests
are unavailable. LLVM, MyPinTool, and their environment variables are therefore
ready in the resulting `:llm` image. Generate current Core and Extra package
lists immediately:

```bash
cd /workspace/icflow_dynamic_collection
./collect_arch_git.sh

wc -l "$HOME/arch_packages/core/clone_urls.txt"
wc -l "$HOME/arch_packages/extra/clone_urls.txt"
```

To enter the same container later:

```bash
docker start -ai icflow
# If it is already running:
docker exec -it icflow bash
```

## Choice B: build the image and compile everything

Clone the repository and build the Arch Linux environment:

```bash
git clone --branch codex/llm-test-generation \
  https://github.com/Ryan-hub-bit/icflow_dynamic_collection.git
cd icflow_dynamic_collection

docker build \
  --build-arg USER_ID="$(id -u)" \
  --build-arg GROUP_ID="$(id -g)" \
  -t icflow-arch .

docker run -it --init \
  --name icflow \
  --cap-add=SYS_PTRACE \
  --security-opt seccomp=unconfined \
  -v icflow-data:/data \
  icflow-arch
```

Run the remaining commands inside the container.

### Compile the custom LLVM

```bash
git clone https://github.com/Ryan-hub-bit/llvm-project.git "$HOME/llvm-project"
cd "$HOME/llvm-project"

cmake -S llvm -B build -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DLLVM_ENABLE_PROJECTS="clang;lld" \
  -DLLVM_TARGETS_TO_BUILD=X86

cmake --build build --target clang llvm-nm lld -- -j"$(nproc)"

./build/bin/clang --version
./build/bin/llvm-nm --version
./build/bin/ld.lld --version
```

### Compile MyPinTool

```bash
cd /workspace/icflow_dynamic_collection/Mypintool/source/tools/MyPinTool
make clean
make obj-intel64/MyPinTool.so -j"$(nproc)"

cd /workspace/icflow_dynamic_collection
export LLVM_BUILD="$HOME/llvm-project/build"
export PIN_ROOT="$PWD/Mypintool"
export PINTOOL="$PIN_ROOT/source/tools/MyPinTool/obj-intel64/MyPinTool.so"
export MAKEPKG_CONF="$PWD/.makepkg.conf"
```

## LLM-generated tests: ordered indirect-call-pair workflow

This is the paper-style augmentation workflow. Its primary result is the number
of **new unique indirect-call pairs**, where one pair is an
`(indirect call site, resolved callee target)` combination in `*_icall.json`.
The goal is not simply to generate many tests or increase line coverage; it is
to make the same indirect call sites reach as many previously unseen targets as
possible.

The module is functional, but generated code is untrusted and project-specific.
It deliberately writes candidates to a separate directory and never executes
or inserts them automatically. A researcher must review the files and connect
them to the real project's build and `make check`/`ctest` command.

The raw `docker-v1` release predates this branch, but the
`Dockerfile.prebuilt` command in Choice A already adds the module. Only when
running the raw release image without that overlay, fetch the module inside the
container first:

```bash
cd /workspace/icflow_dynamic_collection
git fetch origin codex/llm-test-generation
git switch --detach FETCH_HEAD
```

New images built from this branch include the generator, prompt, comparison
tool, demo project, and verification script. The Docker build itself runs the
Python unit tests.

### Step 1: obtain and build one project

Start with one medium-sized project whose native tests already pass. For an
Arch package, clone its packaging repository and build it once so `makepkg`
unpacks the actual source under `src/`:

```bash
git clone PACKAGE_GIT_URL /data/projects/package-name
cd /data/projects/package-name
makepkg --config /workspace/icflow_dynamic_collection/.makepkg.conf \
  --syncdeps --noconfirm --needed --skippgpcheck --nocheck

find "$PWD/src" -maxdepth 2 -type d
```

Use the unpacked upstream source directory, not only the directory containing
`PKGBUILD`, as `PROJECT_SOURCE`:

```bash
export PROJECT_SOURCE=/data/projects/package-name/src/upstream-source
```

Record the exact native test command, for example `make check` or
`ctest --test-dir build --output-on-failure`. Run it normally before involving
the model. A failing native suite is not a valid baseline.

### Step 2: collect the native-test baseline

Run the native package through `collect_dynamic.sh`, or wrap the already-built
test executables with `wrap_with_mypintool.sh` and run the native test command.
Save the resulting `*_icall.json` files as the baseline. Do not rebuild between
the baseline and generated-test measurements: the comparison uses instruction
addresses and therefore requires the same non-PIE binaries.

### Step 3: inspect the exact project prompt without an API call

The reusable prompt is
[`llm_test_generation/prompt_template.md`](llm_test_generation/prompt_template.md).
It prioritizes diverse callback targets, function-pointer tables, virtual
implementations, handlers, parser states, and cleanup paths while avoiding
tests likely to repeat existing pairs.

```bash
cd /workspace/icflow_dynamic_collection
python3 -m llm_test_generation.generate_tests "$PROJECT_SOURCE" \
  --dry-run \
  --existing-test-command "make check" \
  --prompt-output /data/project-prompt.md
```

Review `/data/project-prompt.md` before submission. The selector excludes
common build, dependency, VCS, and secret-file locations and applies size
limits, but the researcher is responsible for confirming that every selected
source file may be sent to the API.

### Step 4: export the researcher's own OpenAI API key

Create a key in the researcher's own OpenAI project. Enter it interactively so
it is not stored in Git, the Dockerfile, or shell history:

```bash
read -rsp "OpenAI API key: " OPENAI_API_KEY
export OPENAI_API_KEY
echo
export OPENAI_MODEL=gpt-5.6-sol
```

The module reads the key only from the environment, does not write it to disk,
and sends the Responses API request with `store: false`. Each researcher pays
for usage through the API project associated with their key.

### Step 5: generate candidate test files or test inputs

```bash
python3 -m llm_test_generation.generate_tests "$PROJECT_SOURCE" \
  --output-dir /data/llm-generated-tests/package-name \
  --test-count 12 \
  --existing-test-command "make check" \
  --extra-instructions \
    "Maximize new indirect-call pairs. Prefer unseen callback targets, virtual implementations, parser handlers, and state transitions."
```

Add `--coverage-report BASELINE_SUMMARY.txt` when a baseline summary is
available. The output contains candidate files, `generation.json`, and
`generation_metadata.json`. It is not yet part of the project.

### Step 6: review and integrate the candidates

1. Read every generated file; reject unsafe, irrelevant, flaky, or incorrect
   code.
2. Copy only approved tests or inputs into the unpacked project.
3. Add them to its existing test build without changing production behavior.
4. Make its `check()` target, `make check`, or `ctest` command execute them.
5. Run the native plus generated suite normally and fix integration errors
   before using Pin.

For reproducible Arch-wide collection, put those approved files and the
`check()` integration in a packaging-repository fork or patch referenced by its
`PKGBUILD`. Then give that repository URL to `collect_dynamic.sh`.

### Step 7: run generated tests under MyPinTool through `make check`

Use the same built executables as the baseline. Wrap them, execute the updated
test command, and restore them afterward:

```bash
cd /workspace/icflow_dynamic_collection
PIN_ROOT="$PIN_ROOT" PINTOOL="$PINTOOL" \
  WRAP_LOG=/data/generated-wrapped.tsv \
  ./wrap_with_mypintool.sh "$PROJECT_SOURCE"

make -C "$PROJECT_SOURCE" check
./restore_wrapped_elfs.sh "$PROJECT_SOURCE"
```

Save this run's `*_icall.json` files separately. Some projects use `ctest` or a
custom command instead of literal `make check`; use the command recorded in
Step 1.

Join a non-PIE binary's static labels with its dynamic edges to obtain all
three per-callsite target sets:

```bash
python3 -m llm_test_generation.analyze_icall_pairs \
  "$PROJECT_SOURCE/program.orig" \
  --icall-json "$PROJECT_SOURCE/program.orig_icall.json" \
  --llvm-nm "$LLVM_BUILD/bin/llvm-nm" \
  --output /data/program-pairs.json \
  --require-dynamic \
  --require-static \
  --require-same-type-non-address-taken \
  --require-dynamic-covered
```

The static target set comes from function-kind `1` labels. The
same-function-type but local/non-address-taken set comes from function-kind `2`
labels. The report keeps those sets separate and checks each Pin edge against
the kind `1` static set.

### Step 8: count the new indirect-call pairs

Compare directories that have the same relative `*_icall.json` filenames:

```bash
python3 -m llm_test_generation.compare_icall_pairs \
  /data/baseline-icall /data/generated-icall \
  --require-new 1 \
  --json-output /data/icall-comparison.json
```

The report gives baseline, candidate, new, and union pair counts. A generated
suite is useful only when `new_pair_count` is positive and its tests remain
correct and deterministic.

### Verify the complete offline path inside Docker

After LLVM and MyPinTool are available, run:

```bash
cd /workspace/icflow_dynamic_collection
./verify_llm_pipeline.sh /data/llm-pipeline-verification
cat /data/llm-pipeline-verification/comparison.json
```

This checked-in demonstration performs prompt rendering, structured-response
materialization, compilation with the custom LLVM, MyPinTool wrapping, native
`make check`, generated-input `make check`, and pair comparison. It uses a
deterministic response fixture instead of an API key and must observe at least
two new indirect-call pairs. It proves the local integration path; only a run
with a researcher's key can verify that account's live API access and its
model-generated project-specific tests.

Verified fixture result on an x86-64 Ubuntu Docker host (August 26, 2026):

- Both `Dockerfile` and `Dockerfile.prebuilt` built successfully.
- The ready image found the custom LLVM/Clang 20 build, lld, Pin, and MyPinTool.
- All 12 Python tests passed during the image build.
- Native `make check` observed 1 indirect-call pair.
- Generated-input `make check` observed 3 pairs, including 2 new pairs from the
  same call site; the verification container exited with status 0.

This is a deterministic integration test, not a claim that an unreviewed live
model response will compile or improve every external project.

Source selected by Step 5 is sent to the OpenAI Responses API. See the official
[Responses API reference](https://developers.openai.com/api/reference/cli/resources/responses/methods/create)
and [model guide](https://developers.openai.com/api/docs/models).

## Functional test only: verify MyPinTool with `testlink`

The `testlink`, Zydis, and Expat commands in the next two sections are small
functional tests. They verify that the compiler, Pin instrumentation, static
binary extraction, and dynamic JSON collection work. They do **not** generate
the complete Core or Extra dataset.

`testlink.cpp` contains an indirect function call and provides a quick
end-to-end binary/JSON check:

```bash
cd /workspace/icflow_dynamic_collection

"$LLVM_BUILD/bin/clang++" -O0 -g -fno-pie -no-pie \
  testlink.cpp -o testlink
rm -f testlink_icall.json testlink_ijump.json
"$PIN_ROOT/pin" -t "$PINTOOL" -- "$PWD/testlink"

jq -e 'type == "object" and length > 0' testlink_icall.json
jq . testlink_icall.json
jq . testlink_ijump.json
```

An empty `{}` is valid when that type of indirect control flow was not
observed during the test.

## Functional test only: build two sample packages

`test-packages.txt` contains
[Zydis](https://gitlab.archlinux.org/archlinux/packaging/packages/zydis) and
[Expat](https://gitlab.archlinux.org/archlinux/packaging/packages/expat).

Build their static binaries:

```bash
cd /workspace/icflow_dynamic_collection

ARCH_PACKAGES_ROOT=/data/icflow-static \
MAX_JOBS=2 \
BUILD_TIMEOUT=1800 \
./buildall_timeout.sh sample "$PWD/test-packages.txt"

find /data/icflow-static/elf_outputs -type f -exec file {} + | grep ELF
cat /data/icflow-static/elf_map.txt
```

Collect their dynamic ICFlow ground truth:

```bash
BUILD_TIMEOUT=1800 TEST_TIMEOUT=1800 \
WORK_ROOT=/data/icflow-sample-work \
./collect_dynamic.sh "$PWD/test-packages.txt" /data/icflow-dynamic
```

`collect_dynamic.sh` now writes an `icall-pair-manifest.json` at the collection
root, a manifest per package, and a `*_pairs.json` next to each captured
`*_icall.json`. The root manifest lists exactly which captured binaries have
dynamic pairs and which have all three pair classes. To repeat the analysis:

```bash
python3 -m llm_test_generation.analyze_icall_pairs \
  --collection-root /data/icflow-dynamic \
  --llvm-nm "$LLVM_BUILD/bin/llvm-nm" \
  --output /data/icflow-dynamic/icall-pair-manifest.json
```

Verify the binary/ground-truth pairs:

```bash
find /data/icflow-dynamic -name wrapped-executions.tsv -size +0 -print
find /data/icflow-dynamic \
  -type f \( -name '*_icall.json' -o -name '*_ijump.json' \) \
  -print0 | while IFS= read -r -d '' result; do
    jq -e 'length > 0' "$result" >/dev/null && echo "$result"
  done

find /data/icflow-dynamic -path '*/artifacts/*' -type f -exec file {} + \
  | grep ELF
```

The validated run built both packages and copied five static ELF files. Zydis
produced ICFlow for `ZydisInfo`; Expat produced ICFlow for `runtests`. Their
associated ELF files and `*_icall.json`/`*_ijump.json` files were preserved
under each package's `artifacts/` directory.

## Full pipeline: generate and process complete Core or Extra lists

This is the full dataset pipeline:

1. Run `collect_arch_git.sh` to generate the complete current Core and Extra
   repository lists.
2. Run `buildall_timeout.sh` on a complete list to build and extract the static
   ELF binaries.
3. Run `collect_dynamic.sh` on the same list to execute package tests through
   MyPinTool and collect the dynamic `*_icall.json` and `*_ijump.json` ground
   truth. This step also creates per-callsite static, dynamic, and same-type
   non-address-taken pair reports.

Generate current lists from the Arch package API:

```bash
cd /workspace/icflow_dynamic_collection
./collect_arch_git.sh
```

Build static binaries for Core or Extra:

```bash
ARCH_PACKAGES_ROOT=/data/static-core \
BUILD_TIMEOUT=1800 \
./buildall_timeout.sh core "$HOME/arch_packages/core/clone_urls.txt"

ARCH_PACKAGES_ROOT=/data/static-extra \
BUILD_TIMEOUT=1800 \
./buildall_timeout.sh extra "$HOME/arch_packages/extra/clone_urls.txt"
```

Collect dynamic ICFlow for Core or Extra:

```bash
CLEAN_WORKTREES=1 DYNAMIC_ONLY=1 \
WORK_ROOT=/data/work-core \
./collect_dynamic.sh \
  "$HOME/arch_packages/core/clone_urls.txt" /data/dynamic-core

CLEAN_WORKTREES=1 DYNAMIC_ONLY=1 \
WORK_ROOT=/data/work-extra \
./collect_dynamic.sh \
  "$HOME/arch_packages/extra/clone_urls.txt" /data/dynamic-extra
```

`CLEAN_WORKTREES=1` removes a package checkout only after collection has copied
its logs and artifacts. `DYNAMIC_ONLY=1` retains only binaries whose MyPinTool
output contains at least one dynamic indirect-call pair. Leave either option at
its default `0` when debugging a failed package or retaining empty captures.

Core and especially Extra require substantial time, network bandwidth, and
disk space. The scripts record processed URLs so interrupted collection can be
resumed with the same command.

## Copy results from the Docker volume

Find the volume location on the host:

```bash
docker volume inspect icflow-data
```

Or copy a result directory directly from the container:

```bash
docker cp icflow:/data/icflow-static ./icflow-static
docker cp icflow:/data/icflow-dynamic ./icflow-dynamic
```
