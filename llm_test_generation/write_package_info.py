#!/usr/bin/env python3
"""Persist package provenance beside dynamic ICFlow collection artifacts."""

from __future__ import annotations

import argparse
import datetime
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Sequence


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def git_value(repository: Path, *arguments: str) -> str | None:
    if not (repository / ".git").is_dir():
        return None
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def snapshot_package_files(repository: Path, output: Path) -> Path | None:
    pkgbuild = repository / "PKGBUILD"
    if pkgbuild.is_file():
        shutil.copy2(pkgbuild, output / "PKGBUILD")

    srcinfo_output = output / ".SRCINFO"
    srcinfo_source = repository / ".SRCINFO"
    if srcinfo_source.is_file():
        shutil.copy2(srcinfo_source, srcinfo_output)
        return srcinfo_output

    if pkgbuild.is_file() and shutil.which("makepkg"):
        result = subprocess.run(
            ["makepkg", "--printsrcinfo"],
            cwd=repository,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            srcinfo_output.write_text(result.stdout, encoding="utf-8")
            return srcinfo_output
    return srcinfo_output if srcinfo_output.is_file() else None


def parse_srcinfo(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    values: dict[str, list[str]] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if " = " not in line:
            continue
        key, value = line.split(" = ", 1)
        values.setdefault(key, []).append(value)

    result: dict[str, Any] = {}
    for key in ("pkgbase", "pkgver", "pkgrel", "epoch"):
        if values.get(key):
            result[key] = values[key][0]
    for key in ("pkgname", "arch", "license"):
        if values.get(key):
            result[key] = values[key]
    return result


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def update_package_info(
    *,
    repository_url: str,
    package_name: str,
    package_set: str,
    source_list: Path,
    repository: Path,
    output: Path,
    status: str,
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    info_path = output / "package-info.json"
    existing = load_json(info_path)
    now = utc_now()
    previous_status = existing.get("status")
    attempts = int(existing.get("attempt_count", 0))
    if status == "started" and previous_status != "started":
        attempts += 1

    srcinfo_path = snapshot_package_files(repository, output)
    info: dict[str, Any] = {
        "schema_version": 1,
        "package_name": package_name,
        "package_set": package_set,
        "repository_url": repository_url,
        "source_list": str(source_list),
        "git_commit": git_value(repository, "rev-parse", "HEAD"),
        "git_commit_time": git_value(
            repository, "show", "-s", "--format=%cI", "HEAD"
        ),
        "package": parse_srcinfo(srcinfo_path),
        "pkgbuild_snapshot": "PKGBUILD" if (output / "PKGBUILD").is_file() else None,
        "srcinfo_snapshot": ".SRCINFO" if srcinfo_path is not None else None,
        "status": status,
        "attempt_count": attempts,
        "first_seen_at": existing.get("first_seen_at", now),
        "last_updated_at": now,
    }

    if status == "started":
        info["last_attempt_started_at"] = now
    elif existing.get("last_attempt_started_at"):
        info["last_attempt_started_at"] = existing["last_attempt_started_at"]

    manifest_path = output / "icall-pair-manifest.json"
    manifest = load_json(manifest_path)
    summary = manifest.get("summary")
    if isinstance(summary, dict):
        info["pair_summary"] = summary
    dynamic_binaries = manifest.get("binaries_with_dynamic_pairs")
    if isinstance(dynamic_binaries, list):
        info["retained_dynamic_binaries"] = dynamic_binaries
        info["retained_dynamic_binary_count"] = len(dynamic_binaries)
    else:
        info["retained_dynamic_binaries"] = []
        info["retained_dynamic_binary_count"] = 0

    flat_records_path = output / "retained-binaries.json"
    if flat_records_path.is_file():
        try:
            flat_records = json.loads(flat_records_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            flat_records = []
        if isinstance(flat_records, list):
            info["flat_binary_paths"] = [
                record["binary_path"]
                for record in flat_records
                if isinstance(record, dict) and isinstance(record.get("binary_path"), str)
            ]
            info["retained_dynamic_binary_count"] = len(info["flat_binary_paths"])
            info["dynamic_summary"] = {
                "binary_count": len(info["flat_binary_paths"]),
                "dynamic_pair_count": sum(
                    int(record.get("pair_summary", {}).get("dynamic_pair_count", 0))
                    for record in flat_records
                    if isinstance(record, dict)
                    and isinstance(record.get("pair_summary"), dict)
                ),
                "dynamic_callsite_count": sum(
                    int(
                        record.get("pair_summary", {}).get(
                            "dynamic_callsite_count", 0
                        )
                    )
                    for record in flat_records
                    if isinstance(record, dict)
                    and isinstance(record.get("pair_summary"), dict)
                ),
            }
        else:
            info["flat_binary_paths"] = []
    else:
        info["flat_binary_paths"] = []

    temporary = info_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    temporary.replace(info_path)
    return info


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-url", required=True)
    parser.add_argument("--package-name", required=True)
    parser.add_argument("--package-set", required=True)
    parser.add_argument("--source-list", required=True, type=Path)
    parser.add_argument("--repository-dir", required=True, type=Path)
    parser.add_argument("--package-output", required=True, type=Path)
    parser.add_argument(
        "--status", required=True, choices=("started", "completed", "failed")
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    update_package_info(
        repository_url=arguments.repository_url,
        package_name=arguments.package_name,
        package_set=arguments.package_set,
        source_list=arguments.source_list.resolve(),
        repository=arguments.repository_dir.resolve(),
        output=arguments.package_output.resolve(),
        status=arguments.status,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
