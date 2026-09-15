#!/usr/bin/env python3
"""Join static X86 label metadata with MyPinTool indirect-call edges."""

from __future__ import annotations

import argparse
import json
import re
import struct
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


ICALL_SUFFIX = "_icall.json"
PAIR_SUFFIX = "_pairs.json"
LABEL_RE = re.compile(
    r"^(?P<module>.+)-"
    r"(?P<source_jump_table>[0-9]+)-"
    r"(?P<tail_call>[0-9]+)-"
    r"(?P<callsite>[0-9]+)-"
    r"(?P<callee_type>[0-9a-fA-F]+)-"
    r"(?P<direct_tail_call>[0-9]+)-t-"
    r"(?P<jump_table>[0-9]+)-"
    r"(?P<jump_entry>[0-9]+)-"
    r"(?P<return_id>[0-9]+)-"
    r"(?P<function_kind>[0-9]+)-"
    r"(?P<function_hash>[0-9a-fA-F]+)-"
    r"(?P<function_type>[0-9a-fA-F]+|type)$"
)


def format_address(address: int) -> str:
    return f"0x{address:x}"


def parse_address(value: Any, *, label: str, path: Path) -> int:
    if not isinstance(value, str):
        raise ValueError(f"{path}: {label} address must be a string")
    try:
        return int(value, 0)
    except ValueError as error:
        raise ValueError(f"{path}: invalid {label} address {value!r}") from error


def parse_label(name: str) -> dict[str, Any] | None:
    match = LABEL_RE.match(name)
    if not match:
        return None
    fields: dict[str, Any] = match.groupdict()
    for key in (
        "source_jump_table",
        "tail_call",
        "callsite",
        "direct_tail_call",
        "jump_table",
        "jump_entry",
        "return_id",
        "function_kind",
    ):
        fields[key] = int(fields[key])
    fields["callee_type"] = fields["callee_type"].lower()
    fields["function_hash"] = fields["function_hash"].lower()
    fields["function_type"] = fields["function_type"].lower()
    return fields


def parse_nm_output(output: str, *, path: Path) -> list[dict[str, Any]]:
    symbols: list[dict[str, Any]] = []
    for line_number, line in enumerate(output.splitlines(), start=1):
        if not line.strip():
            continue
        fields = line.rsplit(maxsplit=3)
        if len(fields) != 4:
            raise ValueError(
                f"{path}: unexpected llvm-nm output on line {line_number}: {line!r}"
            )
        name, symbol_type, value_text, size_text = fields
        try:
            value = int(value_text, 16)
            size = int(size_text, 16)
        except ValueError as error:
            raise ValueError(
                f"{path}: invalid llvm-nm value on line {line_number}: {line!r}"
            ) from error
        symbols.append(
            {
                "name": name,
                "symbol_type": symbol_type,
                "address": value,
                "size": size,
            }
        )
    return symbols


def read_symbols(binary: Path, llvm_nm: Path) -> list[dict[str, Any]]:
    command = [
        str(llvm_nm),
        "-an",
        "--defined-only",
        "--format=posix",
        str(binary),
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ValueError(f"llvm-nm failed for {binary}: {detail}")
    return parse_nm_output(result.stdout, path=binary)


def read_elf_type(binary: Path) -> str:
    with binary.open("rb") as stream:
        header = stream.read(18)
    if len(header) < 18 or header[:4] != b"\x7fELF":
        raise ValueError(f"Not an ELF file: {binary}")
    if header[5] == 1:
        byte_order = "<"
    elif header[5] == 2:
        byte_order = ">"
    else:
        raise ValueError(f"{binary}: invalid ELF data encoding {header[5]}")
    elf_type = struct.unpack(f"{byte_order}H", header[16:18])[0]
    return {2: "ET_EXEC", 3: "ET_DYN"}.get(elf_type, f"ET_{elf_type}")


def load_dynamic_edges(path: Path) -> dict[int, set[int]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    # MyPinTool serializes its default-constructed JSON value as `null` when an
    # execution observes no indirect calls.  Treat that as an empty edge map so
    # packages whose test suite runs but does not exercise an indirect call do
    # not turn the entire collection into an analysis failure.
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: top-level JSON value must be an object")
    edges: dict[int, set[int]] = {}
    for source_text, targets in payload.items():
        source = parse_address(source_text, label="source", path=path)
        if not isinstance(targets, list):
            raise ValueError(f"{path}: targets for {source_text!r} must be a list")
        edges[source] = {
            parse_address(target, label="target", path=path) for target in targets
        }
    return edges


def target_record(address: int, names_by_address: dict[int, list[str]]) -> dict[str, Any]:
    return {
        "address": format_address(address),
        "function_names": names_by_address.get(address, []),
    }


def analyze_pair_sets(
    symbols: Iterable[dict[str, Any]],
    dynamic_edges: dict[int, set[int]] | None = None,
) -> dict[str, Any]:
    dynamic_edges = dynamic_edges or {}
    callsites: list[dict[str, Any]] = []
    targets_by_kind_and_type: dict[tuple[int, str], set[int]] = defaultdict(set)
    names_by_address: dict[int, list[str]] = defaultdict(list)

    for symbol in symbols:
        label = parse_label(symbol["name"])
        address = symbol["address"]
        if label is None:
            if symbol["symbol_type"].lower() in {"t", "w"}:
                names_by_address[address].append(symbol["name"])
            continue
        if label["callsite"] and label["callee_type"] != "0":
            callsites.append(
                {
                    "address_value": address,
                    "callsite_id": label["callsite"],
                    "type_id": label["callee_type"],
                }
            )
        if label["function_kind"] in {1, 2} and label["function_type"] not in {
            "0",
            "type",
        }:
            targets_by_kind_and_type[
                (label["function_kind"], label["function_type"])
            ].add(address)

    for names in names_by_address.values():
        names.sort()

    rendered_callsites: list[dict[str, Any]] = []
    matched_dynamic_sources: set[int] = set()
    dynamic_pair_count = 0
    static_pair_count = 0
    negative_pair_count = 0
    dynamic_covered_count = 0
    dynamic_negative_count = 0
    dynamic_unclassified_count = 0

    for callsite in sorted(
        callsites, key=lambda item: (item["address_value"], item["callsite_id"])
    ):
        source = callsite["address_value"]
        type_id = callsite["type_id"]
        dynamic = dynamic_edges.get(source, set())
        static = targets_by_kind_and_type.get((1, type_id), set())
        negative = targets_by_kind_and_type.get((2, type_id), set())
        dynamic_in_static = dynamic & static
        dynamic_in_negative = dynamic & negative
        dynamic_unclassified = dynamic - static - negative
        if source in dynamic_edges:
            matched_dynamic_sources.add(source)

        dynamic_pair_count += len(dynamic)
        static_pair_count += len(static)
        negative_pair_count += len(negative)
        dynamic_covered_count += len(dynamic_in_static)
        dynamic_negative_count += len(dynamic_in_negative)
        dynamic_unclassified_count += len(dynamic_unclassified)

        rendered_callsites.append(
            {
                "address": format_address(source),
                "callsite_id": callsite["callsite_id"],
                "type_id": type_id,
                "dynamic_targets": [
                    target_record(address, names_by_address)
                    for address in sorted(dynamic)
                ],
                "static_targets": [
                    target_record(address, names_by_address)
                    for address in sorted(static)
                ],
                "same_type_non_address_taken_targets": [
                    target_record(address, names_by_address)
                    for address in sorted(negative)
                ],
                "dynamic_targets_in_static": [
                    target_record(address, names_by_address)
                    for address in sorted(dynamic_in_static)
                ],
                "dynamic_targets_in_same_type_non_address_taken": [
                    target_record(address, names_by_address)
                    for address in sorted(dynamic_in_negative)
                ],
                "dynamic_targets_unclassified": [
                    target_record(address, names_by_address)
                    for address in sorted(dynamic_unclassified)
                ],
            }
        )

    unmatched_dynamic = [
        {
            "address": format_address(source),
            "targets": [
                target_record(address, names_by_address)
                for address in sorted(dynamic_edges[source])
            ],
        }
        for source in sorted(dynamic_edges.keys() - matched_dynamic_sources)
    ]
    unmatched_pair_count = sum(len(item["targets"]) for item in unmatched_dynamic)
    total_dynamic_pair_count = dynamic_pair_count + unmatched_pair_count

    return {
        "summary": {
            "callsite_count": len(rendered_callsites),
            "dynamic_callsite_count": len(matched_dynamic_sources),
            "dynamic_pair_count": total_dynamic_pair_count,
            "static_pair_count": static_pair_count,
            "same_type_non_address_taken_pair_count": negative_pair_count,
            "dynamic_pairs_covered_by_static_count": dynamic_covered_count,
            "dynamic_pairs_hitting_same_type_non_address_taken_count": dynamic_negative_count,
            "dynamic_pairs_unclassified_count": dynamic_unclassified_count
            + unmatched_pair_count,
            "unmatched_dynamic_callsite_count": len(unmatched_dynamic),
            "has_dynamic_pairs": total_dynamic_pair_count > 0,
            "has_static_pairs": static_pair_count > 0,
            "has_same_type_non_address_taken_pairs": negative_pair_count > 0,
            "all_dynamic_pairs_covered_by_static": (
                total_dynamic_pair_count > 0
                and dynamic_covered_count == total_dynamic_pair_count
            ),
        },
        "callsites": rendered_callsites,
        "unmatched_dynamic_callsites": unmatched_dynamic,
    }


def analyze_binary(binary: Path, icall_json: Path, llvm_nm: Path) -> dict[str, Any]:
    binary = binary.resolve(strict=True)
    icall_json = icall_json.resolve(strict=True)
    elf_type = read_elf_type(binary)
    analysis = analyze_pair_sets(
        read_symbols(binary, llvm_nm), load_dynamic_edges(icall_json)
    )
    if elf_type != "ET_EXEC":
        analysis["summary"]["all_dynamic_pairs_covered_by_static"] = False
    analysis.update(
        {
            "binary": str(binary),
            "icall_json": str(icall_json),
            "elf_type": elf_type,
            "static_dynamic_addresses_comparable": elf_type == "ET_EXEC",
        }
    )
    return analysis


def discover_binary(icall_json: Path) -> Path | None:
    base = Path(str(icall_json)[: -len(ICALL_SUFFIX)])
    candidates = [base]
    if base.name.endswith(".orig"):
        candidates.append(base.with_name(base.name[: -len(".orig")]))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def analyze_collection(root: Path, llvm_nm: Path) -> dict[str, Any]:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"Collection root must be a directory: {root}")
    json_files = sorted(root.rglob(f"*{ICALL_SUFFIX}"))
    if not json_files:
        raise ValueError(f"No *{ICALL_SUFFIX} files found under: {root}")

    reports: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    aggregate_keys = (
        "callsite_count",
        "dynamic_callsite_count",
        "dynamic_pair_count",
        "static_pair_count",
        "same_type_non_address_taken_pair_count",
        "dynamic_pairs_covered_by_static_count",
        "dynamic_pairs_hitting_same_type_non_address_taken_count",
        "dynamic_pairs_unclassified_count",
        "unmatched_dynamic_callsite_count",
    )
    totals = {key: 0 for key in aggregate_keys}

    for json_path in json_files:
        binary = discover_binary(json_path)
        identity = json_path.relative_to(root).as_posix()
        if binary is None:
            errors.append(
                {"icall_json": identity, "error": "matching ELF binary not found"}
            )
            continue
        try:
            report = analyze_binary(binary, json_path, llvm_nm)
            report_path = json_path.with_name(
                json_path.name[: -len(ICALL_SUFFIX)] + PAIR_SUFFIX
            )
            write_json(report_path, report)
            summary = report["summary"]
            for key in aggregate_keys:
                totals[key] += summary[key]
            reports.append(
                {
                    "binary": binary.relative_to(root).as_posix(),
                    "icall_json": identity,
                    "pair_report": report_path.relative_to(root).as_posix(),
                    "elf_type": report["elf_type"],
                    "summary": summary,
                }
            )
        except (OSError, ValueError, json.JSONDecodeError) as error:
            errors.append({"icall_json": identity, "error": str(error)})

    totals.update(
        {
            "binary_count": len(reports),
            "binary_error_count": len(errors),
            "binaries_with_dynamic_pairs": sum(
                entry["summary"]["has_dynamic_pairs"] for entry in reports
            ),
            "binaries_with_static_pairs": sum(
                entry["summary"]["has_static_pairs"] for entry in reports
            ),
            "binaries_with_same_type_non_address_taken_pairs": sum(
                entry["summary"]["has_same_type_non_address_taken_pairs"]
                for entry in reports
            ),
            "dynamic_binaries_with_noncomparable_addresses": sum(
                entry["summary"]["has_dynamic_pairs"]
                and entry["elf_type"] != "ET_EXEC"
                for entry in reports
            ),
        }
    )
    totals["has_dynamic_pairs"] = totals["dynamic_pair_count"] > 0
    totals["has_static_pairs"] = totals["static_pair_count"] > 0
    totals["has_same_type_non_address_taken_pairs"] = (
        totals["same_type_non_address_taken_pair_count"] > 0
    )
    totals["all_dynamic_pairs_covered_by_static"] = (
        totals["dynamic_pair_count"] > 0
        and totals["dynamic_pairs_covered_by_static_count"]
        == totals["dynamic_pair_count"]
        and totals["dynamic_binaries_with_noncomparable_addresses"] == 0
    )
    dynamic_binaries = [
        entry["binary"] for entry in reports if entry["summary"]["has_dynamic_pairs"]
    ]
    all_three_binaries = [
        entry["binary"]
        for entry in reports
        if entry["summary"]["has_dynamic_pairs"]
        and entry["summary"]["has_static_pairs"]
        and entry["summary"]["has_same_type_non_address_taken_pairs"]
    ]
    return {
        "collection_root": str(root),
        "summary": totals,
        "binaries_with_dynamic_pairs": dynamic_binaries,
        "binaries_with_all_three_pair_classes": all_three_binaries,
        "reports": reports,
        "errors": errors,
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


def check_requirements(summary: dict[str, Any], args: argparse.Namespace) -> list[str]:
    failures: list[str] = []
    requirements = (
        (args.require_dynamic, "has_dynamic_pairs", "dynamic indirect-call pairs"),
        (args.require_static, "has_static_pairs", "static indirect-call pairs"),
        (
            args.require_same_type_non_address_taken,
            "has_same_type_non_address_taken_pairs",
            "same-type non-address-taken pairs",
        ),
        (
            args.require_dynamic_covered,
            "all_dynamic_pairs_covered_by_static",
            "complete static coverage of dynamic pairs",
        ),
    )
    for required, key, description in requirements:
        if required and not summary[key]:
            failures.append(f"required {description}, but the check failed")
    return failures


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Join custom LLVM callsite/function labels with MyPinTool *_icall.json "
            "edges. Static targets use function kind 1; same-type, local, "
            "non-address-taken targets use function kind 2."
        )
    )
    parser.add_argument("binary", nargs="?", type=Path, help="Non-PIE ELF binary")
    parser.add_argument("--icall-json", type=Path, help="MyPinTool *_icall.json file")
    parser.add_argument(
        "--collection-root",
        type=Path,
        help="Analyze every *_icall.json and matching binary under this directory",
    )
    parser.add_argument(
        "--llvm-nm", type=Path, default=Path("llvm-nm"), help="Path to custom llvm-nm"
    )
    parser.add_argument("--output", type=Path, help="Write report or manifest as JSON")
    parser.add_argument("--require-dynamic", action="store_true")
    parser.add_argument("--require-static", action="store_true")
    parser.add_argument("--require-same-type-non-address-taken", action="store_true")
    parser.add_argument("--require-dynamic-covered", action="store_true")
    args = parser.parse_args(argv)

    if (args.binary is None) == (args.collection_root is None):
        parser.error("provide either BINARY or --collection-root")
    if args.binary is not None and args.icall_json is None:
        parser.error("--icall-json is required with BINARY")
    if args.collection_root is not None and args.icall_json is not None:
        parser.error("--icall-json cannot be used with --collection-root")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.collection_root is not None:
        result = analyze_collection(args.collection_root, args.llvm_nm)
    else:
        result = analyze_binary(args.binary, args.icall_json, args.llvm_nm)

    rendered = json.dumps(result, indent=2)
    print(rendered)
    if args.output:
        write_json(args.output, result)

    failures = check_requirements(result["summary"], args)
    if result.get("elf_type") != "ET_EXEC" and args.require_dynamic_covered:
        failures.append("dynamic/static address coverage requires an ET_EXEC non-PIE binary")
    if result.get("errors"):
        failures.append(f"collection contains {len(result['errors'])} analysis error(s)")
    for failure in failures:
        print(f"error: {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
