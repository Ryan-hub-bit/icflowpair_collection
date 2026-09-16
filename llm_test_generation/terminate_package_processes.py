#!/usr/bin/env python3
"""Terminate escaped package processes after a timed build or test."""

from __future__ import annotations

import argparse
import os
import signal
import time
from pathlib import Path
from typing import Sequence


def protected_processes() -> set[int]:
    protected: set[int] = set()
    pid = os.getpid()
    while pid > 1 and pid not in protected:
        protected.add(pid)
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().split()
            pid = int(fields[3])
        except (OSError, ValueError, IndexError):
            break
    return protected


def package_processes(repository: Path) -> list[int]:
    repository = repository.resolve()
    repository_prefix = f"{repository}{os.sep}"
    current_uid = os.getuid()
    protected = protected_processes()
    matches: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid in protected:
            continue
        try:
            if entry.stat().st_uid != current_uid:
                continue
            cwd = os.readlink(entry / "cwd")
        except OSError:
            cwd = ""
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                errors="replace"
            )
        except OSError:
            command = ""
        if (
            cwd == str(repository)
            or cwd.startswith(repository_prefix)
            or str(repository) in command
        ):
            matches.append(pid)
    return sorted(matches)


def terminate_package_processes(repository: Path, grace_seconds: float = 5.0) -> list[int]:
    terminated = package_processes(repository)
    for pid in terminated:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    deadline = time.monotonic() + grace_seconds
    remaining = terminated
    while remaining and time.monotonic() < deadline:
        time.sleep(0.1)
        remaining = [pid for pid in package_processes(repository) if pid in terminated]
    for pid in remaining:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return terminated


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", type=Path)
    parser.add_argument("--grace-seconds", type=float, default=5.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    terminated = terminate_package_processes(
        arguments.repository, grace_seconds=arguments.grace_seconds
    )
    if terminated:
        print(
            f"Terminated {len(terminated)} escaped package process(es): "
            + " ".join(str(pid) for pid in terminated)
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
