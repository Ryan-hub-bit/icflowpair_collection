#!/usr/bin/env python3
"""Place retained dynamic-ICALL binaries in one flat, indexed directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Sequence


def load_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return payload


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_component(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._+-]+", "_", value).strip("._")
    return sanitized or "unnamed"


def atomic_write_json(path: Path, payload: Any) -> None:
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def place_binary(source: Path, destination: Path) -> None:
    if destination.is_file():
        return
    temporary = destination.with_name(f".{destination.name}.tmp.{os.getpid()}")
    try:
        os.link(source, temporary)
    except OSError:
        shutil.copy2(source, temporary)
    temporary.replace(destination)


def store_dynamic_binaries(
    *, package_output: Path, binary_store: Path, index_path: Path
) -> list[dict[str, Any]]:
    package_output = package_output.resolve()
    binary_store = binary_store.resolve()
    index_path = index_path.resolve()
    artifact_root = (package_output / "artifacts").resolve()
    package_info_path = package_output / "package-info.json"
    package_info = load_object(package_info_path)
    manifest = load_object(package_output / "icall-pair-manifest.json")

    package_name = str(package_info.get("package_name") or package_output.name)
    package_set = str(package_info.get("package_set") or "unknown")
    repository_url = package_info.get("repository_url")
    git_commit = package_info.get("git_commit")
    reports = manifest.get("reports", [])
    if not isinstance(reports, list):
        raise ValueError("icall-pair-manifest.json reports must be an array")

    binary_store.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    for report in reports:
        if not isinstance(report, dict):
            continue
        summary = report.get("summary", {})
        if not isinstance(summary, dict) or not summary.get("has_dynamic_pairs"):
            continue
        relative_binary = report.get("binary")
        if not isinstance(relative_binary, str):
            continue
        source = (artifact_root / relative_binary).resolve()
        if artifact_root not in source.parents or not source.is_file():
            raise ValueError(f"Invalid or missing retained binary: {source}")

        digest = sha256(source)
        filename = "--".join(
            (
                safe_component(package_set),
                safe_component(package_name),
                digest[:16],
                safe_component(source.name),
            )
        )
        destination = binary_store / filename
        place_binary(source, destination)

        def artifact_path(key: str) -> str | None:
            relative = report.get(key)
            if not isinstance(relative, str):
                return None
            candidate = (artifact_root / relative).resolve()
            if artifact_root not in candidate.parents:
                raise ValueError(f"Invalid {key} path: {candidate}")
            return str(candidate)

        records.append(
            {
                "binary_path": str(destination),
                "binary_filename": filename,
                "binary_sha256": digest,
                "package_name": package_name,
                "package_set": package_set,
                "repository_url": repository_url,
                "git_commit": git_commit,
                "package_info_path": str(package_info_path.resolve()),
                "artifact_binary_path": str(source),
                "icall_json_path": artifact_path("icall_json"),
                "pair_report_path": artifact_path("pair_report"),
                "pair_summary": summary,
            }
        )

    records.sort(key=lambda record: record["binary_path"])
    atomic_write_json(package_output / "retained-binaries.json", records)

    existing_index = load_object(index_path)
    existing_records = existing_index.get("binaries", [])
    if not isinstance(existing_records, list):
        existing_records = []
    other_records = [
        record
        for record in existing_records
        if isinstance(record, dict)
        and not (
            record.get("package_name") == package_name
            and record.get("package_set") == package_set
        )
    ]
    all_records = other_records + records
    all_records.sort(key=lambda record: str(record.get("binary_path", "")))
    atomic_write_json(
        index_path,
        {
            "schema_version": 1,
            "binary_folder": str(binary_store),
            "binary_count": len(all_records),
            "binaries": all_records,
        },
    )
    return records


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-output", required=True, type=Path)
    parser.add_argument("--binary-store", required=True, type=Path)
    parser.add_argument("--index", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    index_path = arguments.index or arguments.binary_store.parent / "binary-index.json"
    store_dynamic_binaries(
        package_output=arguments.package_output,
        binary_store=arguments.binary_store,
        index_path=index_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
