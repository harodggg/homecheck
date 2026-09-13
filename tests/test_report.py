"""``report`` 模块的单元测试。"""

from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path

from homecheck.report import human_size, render_report
from homecheck.scan import ScanIssue, ScanResult

GIB = 1024**3
MIB = 1024**2


class HumanSizeTest(unittest.TestCase):
    """针对 ``human_size`` 的格式化测试。"""

    def test_bytes_have_no_decimals(self) -> None:
        self.assertEqual(human_size(0), "0 B")
        self.assertEqual(human_size(512), "512 B")

    def test_larger_units_use_1024_base(self) -> None:
        self.assertEqual(human_size(1024), "1 KB")
        self.assertEqual(human_size(1536), "1.5 KB")
        self.assertEqual(human_size(2 * GIB), "2 GB")
        self.assertEqual(human_size(int(6.21 * GIB)), "6.21 GB")
        self.assertEqual(human_size(18 * GIB + 4 * GIB // 10), "18.4 GB")


class RenderReportTest(unittest.TestCase):
    """针对 ``render_report`` 的渲染测试。"""

    def make_result(self, **overrides: object) -> ScanResult:
        """造一个可预测的 ScanResult，字段可用关键字覆盖。"""
        defaults: dict[str, object] = {
            "root": Path("/tmp/demo"),
            "file_count": 12430,
            "dir_count": 892,
            "total_size": 18 * GIB,
            "size_by_subdir": {"video": 6 * GIB, "datasets": 3 * GIB},
            "size_by_suffix": {".mp4": 6 * GIB, ".zip": 100 * MIB},
            "issues": [],
            "symlinks_skipped": 0,
        }
        defaults.update(overrides)
        return ScanResult(**defaults)  # type: ignore[arg-type]

    def render(self, result: ScanResult, **kwargs: object) -> str:
        """用固定时间戳渲染，保证输出可断言。"""
        return render_report(
            result,
            timestamp=datetime(2026, 9, 13, 14, 2, 11),
            elapsed=1.24,
            **kwargs,  # type: ignore[arg-type]
        )

    def test_header_and_totals(self) -> None:
        text = self.render(self.make_result())

        self.assertIn("homecheck — 扫描报告", text)
        self.assertIn("/tmp/demo", text)
        self.assertIn("2026-09-13 14:02:11", text)
        self.assertIn("1.24s", text)
        self.assertIn("12,430", text)
        self.assertIn("892", text)
        self.assertIn("18 GB", text)

    def test_ranking_is_sorted_desc_and_truncated(self) -> None:
        text = self.render(self.make_result(), top=1)

        self.assertIn("体积排行（前 1 项）", text)
        self.assertIn("video/", text)
        self.assertNotIn("datasets/", text)

    def test_root_level_files_use_root_label(self) -> None:
        text = self.render(self.make_result(size_by_subdir={"(根目录)": 10}))

        self.assertIn("(根目录)", text)

    def test_percentages_are_relative_to_total(self) -> None:
        text = self.render(self.make_result())

        # video 占 18 GiB 中的 6 GiB = 33.3%
        self.assertIn("33.3%", text)

    def test_permission_issues_are_listed_not_swallowed(self) -> None:
        result = self.make_result(
            issues=[ScanIssue(Path("/tmp/demo/locked"), "Permission denied")]
        )

        text = self.render(result)

        self.assertIn("未扫描目录（1 个）", text)
        self.assertIn("⚠ locked — Permission denied", text)

    def test_symlink_note_only_appears_when_nonzero(self) -> None:
        self.assertNotIn("符号链接", self.render(self.make_result()))
        self.assertIn(
            "符号链接", self.render(self.make_result(symlinks_skipped=3))
        )

    def test_empty_result_renders_without_crashing(self) -> None:
        text = self.render(
            self.make_result(
                file_count=0,
                dir_count=0,
                total_size=0,
                size_by_subdir={},
                size_by_suffix={},
            )
        )

        self.assertIn("0 B", text)
        self.assertEqual(text.count("（空）"), 2)

    def test_output_contains_no_ansi_escape(self) -> None:
        self.assertNotIn("\x1b[", self.render(self.make_result()))

    def test_top_zero_hides_rankings(self) -> None:
        text = self.render(self.make_result(), top=0)

        self.assertIn("体积排行（前 0 项）", text)
        self.assertNotIn("video/", text)


if __name__ == "__main__":
    unittest.main()
