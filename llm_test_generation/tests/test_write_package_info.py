from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from llm_test_generation.write_package_info import update_package_info


class PackageInfoTests(unittest.TestCase):
    def test_preserves_package_provenance_and_dynamic_binary_index(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "work" / "example"
            output = root / "output" / "example"
            source_list = root / "arch_packages" / "core" / "clone_urls.txt"
            repository.mkdir(parents=True)
            source_list.parent.mkdir(parents=True)
            source_list.write_text("https://example.invalid/example.git\n")
            (repository / "PKGBUILD").write_text("pkgname=example\n")
            (repository / ".SRCINFO").write_text(
                "pkgbase = example\n"
                "\tpkgver = 1.2.3\n"
                "\tpkgrel = 4\n"
                "\tarch = x86_64\n"
                "pkgname = example\n"
            )

            started = update_package_info(
                repository_url="https://example.invalid/example.git",
                package_name="example",
                package_set="core",
                source_list=source_list,
                repository=repository,
                output=output,
                status="started",
            )
            self.assertEqual(started["attempt_count"], 1)
            self.assertEqual(started["package"]["pkgbase"], "example")
            self.assertEqual(started["package"]["pkgname"], ["example"])
            self.assertTrue((output / "PKGBUILD").is_file())
            self.assertTrue((output / ".SRCINFO").is_file())

            (output / "icall-pair-manifest.json").write_text(
                json.dumps(
                    {
                        "summary": {"dynamic_pair_count": 7},
                        "binaries_with_dynamic_pairs": ["src/example"],
                    }
                )
            )
            completed = update_package_info(
                repository_url="https://example.invalid/example.git",
                package_name="example",
                package_set="core",
                source_list=source_list,
                repository=repository,
                output=output,
                status="completed",
            )

            self.assertEqual(completed["attempt_count"], 1)
            self.assertEqual(completed["retained_dynamic_binary_count"], 1)
            self.assertEqual(
                completed["retained_dynamic_binaries"], ["src/example"]
            )
            self.assertEqual(completed["pair_summary"]["dynamic_pair_count"], 7)


if __name__ == "__main__":
    unittest.main()
