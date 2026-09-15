from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from llm_test_generation.analyze_icall_pairs import (
    analyze_pair_sets,
    load_dynamic_edges,
    main,
    parse_label,
    parse_nm_output,
)


def label(
    *,
    callsite: int = 0,
    callee_type: str = "0",
    function_kind: int = 0,
    function_hash: str = "0",
    function_type: str = "type",
) -> str:
    return (
        "source-file-with-hyphens.c-0-0-"
        f"{callsite}-{callee_type}-0-t-0-0-0-"
        f"{function_kind}-{function_hash}-{function_type}"
    )


class LabelParsingTests(unittest.TestCase):
    def test_parses_schema_from_right_of_hyphenated_module_name(self) -> None:
        parsed = parse_label(
            label(
                callsite=7,
                callee_type="ABCDEF",
                function_kind=2,
                function_hash="1234ABCD",
                function_type="ABCDEF",
            )
        )

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["module"], "source-file-with-hyphens.c")
        self.assertEqual(parsed["callsite"], 7)
        self.assertEqual(parsed["callee_type"], "abcdef")
        self.assertEqual(parsed["function_kind"], 2)
        self.assertEqual(parsed["function_type"], "abcdef")

    def test_rejects_normal_symbol(self) -> None:
        self.assertIsNone(parse_label("ordinary_function"))

    def test_ignores_unnamed_llvm_nm_symbols(self) -> None:
        symbols = parse_nm_output(" a 0 0\nfoo t 401000 10\n", path=Path("test"))

        self.assertEqual(len(symbols), 1)
        self.assertEqual(symbols[0]["name"], "foo")


class PairAnalysisTests(unittest.TestCase):
    def test_treats_pintool_null_output_as_no_dynamic_edges(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "no-edges_icall.json"
            path.write_text("null\n", encoding="utf-8")

            self.assertEqual(load_dynamic_edges(path), {})

    def test_joins_all_three_pair_classes_by_type_and_address(self) -> None:
        symbols = [
            {
                "name": label(callsite=1, callee_type="a1"),
                "symbol_type": "t",
                "address": 0x401100,
                "size": 0,
            },
            {
                "name": label(
                    function_kind=1, function_hash="11", function_type="a1"
                ),
                "symbol_type": "t",
                "address": 0x402000,
                "size": 0,
            },
            {
                "name": "address_taken_target",
                "symbol_type": "t",
                "address": 0x402000,
                "size": 10,
            },
            {
                "name": label(
                    function_kind=2, function_hash="22", function_type="a1"
                ),
                "symbol_type": "t",
                "address": 0x403000,
                "size": 0,
            },
            {
                "name": "same_type_direct_only",
                "symbol_type": "t",
                "address": 0x403000,
                "size": 10,
            },
        ]

        report = analyze_pair_sets(symbols, {0x401100: {0x402000}})
        summary = report["summary"]
        callsite = report["callsites"][0]

        self.assertEqual(summary["dynamic_pair_count"], 1)
        self.assertEqual(summary["static_pair_count"], 1)
        self.assertEqual(summary["same_type_non_address_taken_pair_count"], 1)
        self.assertTrue(summary["all_dynamic_pairs_covered_by_static"])
        self.assertEqual(
            callsite["static_targets"][0]["function_names"],
            ["address_taken_target"],
        )
        self.assertEqual(
            callsite["same_type_non_address_taken_targets"][0]["function_names"],
            ["same_type_direct_only"],
        )

    def test_reports_unmatched_and_unclassified_dynamic_edges(self) -> None:
        symbols = [
            {
                "name": label(callsite=1, callee_type="a1"),
                "symbol_type": "t",
                "address": 0x401100,
                "size": 0,
            }
        ]
        report = analyze_pair_sets(
            symbols,
            {0x401100: {0x409000}, 0x401200: {0x409100}},
        )

        self.assertEqual(report["summary"]["dynamic_pair_count"], 2)
        self.assertEqual(report["summary"]["dynamic_pairs_unclassified_count"], 2)
        self.assertEqual(report["summary"]["unmatched_dynamic_callsite_count"], 1)
        self.assertFalse(report["summary"]["all_dynamic_pairs_covered_by_static"])

    def test_collection_coverage_requirement_uses_aggregate_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            aggregate = {
                "summary": {
                    "all_dynamic_pairs_covered_by_static": True,
                    "has_dynamic_pairs": True,
                    "has_static_pairs": True,
                    "has_same_type_non_address_taken_pairs": True,
                },
                "errors": [],
            }
            with patch(
                "llm_test_generation.analyze_icall_pairs.analyze_collection",
                return_value=aggregate,
            ):
                self.assertEqual(
                    main(
                        [
                            "--collection-root",
                            str(root),
                            "--llvm-nm",
                            "/bin/false",
                            "--require-dynamic-covered",
                        ]
                    ),
                    0,
                )


if __name__ == "__main__":
    unittest.main()
