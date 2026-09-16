from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from llm_test_generation.store_dynamic_binaries import store_dynamic_binaries


class StoreDynamicBinariesTests(unittest.TestCase):
    def test_places_binary_in_flat_folder_and_indexes_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package_output = root / "dynamic-core" / "example"
            artifacts = package_output / "artifacts"
            binary_store = root / "binaries"
            binary = artifacts / "src" / "build" / "example-tool"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"\x7fELFexample")
            (binary.parent / "example-tool.orig_icall.json").write_text("{}\n")
            (binary.parent / "example-tool.orig_pairs.json").write_text("{}\n")
            (package_output / "package-info.json").write_text(
                json.dumps(
                    {
                        "package_name": "example",
                        "package_set": "core",
                        "repository_url": "https://example.invalid/example.git",
                        "git_commit": "abc123",
                    }
                )
            )
            (package_output / "icall-pair-manifest.json").write_text(
                json.dumps(
                    {
                        "reports": [
                            {
                                "binary": "src/build/example-tool",
                                "icall_json": "src/build/example-tool.orig_icall.json",
                                "pair_report": "src/build/example-tool.orig_pairs.json",
                                "summary": {
                                    "has_dynamic_pairs": True,
                                    "dynamic_pair_count": 3,
                                },
                            }
                        ]
                    }
                )
            )

            records = store_dynamic_binaries(
                package_output=package_output,
                binary_store=binary_store,
                index_path=root / "binary-index.json",
            )

            self.assertEqual(len(records), 1)
            stored = Path(records[0]["binary_path"])
            self.assertEqual(stored.parent, binary_store)
            self.assertTrue(stored.name.startswith("core--example--"))
            self.assertEqual(stored.read_bytes(), binary.read_bytes())
            self.assertEqual(records[0]["git_commit"], "abc123")
            self.assertEqual(records[0]["pair_summary"]["dynamic_pair_count"], 3)
            index = json.loads((root / "binary-index.json").read_text())
            self.assertEqual(index["binary_folder"], str(binary_store))
            self.assertEqual(index["binary_count"], 1)
            self.assertEqual(index["binaries"][0]["binary_path"], str(stored))

    def test_dynamic_only_mode_needs_no_static_pair_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package_output = root / "dynamic-extra" / "example"
            artifacts = package_output / "artifacts" / "build"
            artifacts.mkdir(parents=True)
            binary = artifacts / "tool"
            binary.write_bytes(b"\x7fELFdynamic-only")
            (artifacts / "tool.orig_icall.json").write_text(
                json.dumps({"0x1000": ["0x2000", "0x2000", "0x3000"]})
            )
            (package_output / "package-info.json").write_text(
                json.dumps(
                    {
                        "package_name": "example",
                        "package_set": "extra",
                        "repository_url": "https://example.invalid/example.git",
                        "git_commit": "def456",
                    }
                )
            )

            records = store_dynamic_binaries(
                package_output=package_output,
                binary_store=root / "binaries",
                index_path=root / "binary-index.json",
                dynamic_only=True,
            )

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["collection_mode"], "dynamic_ground_truth_only")
            self.assertEqual(records[0]["pair_report_path"], None)
            self.assertEqual(records[0]["pair_summary"]["dynamic_callsite_count"], 1)
            self.assertEqual(records[0]["pair_summary"]["dynamic_pair_count"], 2)


if __name__ == "__main__":
    unittest.main()
