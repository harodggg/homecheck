"""``report`` 模块的单元测试。"""

from __future__ import annotations

import json
import unittest
from datetime import datetime
from pathlib import Path

from homecheck.apply import Action, ActionPlan, Refusal
from homecheck.duplicates import DuplicateGroup, DuplicateReport
from homecheck.report import human_size, render_json, render_markdown, render_report
from homecheck.scan import ScanIssue, ScanResult
from homecheck.suggest import Suggestion

FIXED = datetime(2026, 9, 13, 14, 2, 11)

GIB = 1024**3
MIB = 1024**2


class HumanSizeTest(unittest.TestCase):
    """针对 ``human_size`` 的格式化测试。"""

    def test_bytes_have_no_decimals(self) -> None:
        self.assertEqual(human_size(0), "0 B")
        self.assertEqual(human_size(512), "512 B")

    def test_unit_boundaries(self) -> None:
        self.assertEqual(human_size(0), "0 B")
        self.assertEqual(human_size(1023), "1023 B")
        self.assertEqual(human_size(1024), "1 KB")
        self.assertEqual(human_size(1024**5), "1 PB")
        # 超出最大单位时仍能渲染，不抛异常
        self.assertEqual(human_size(1024**6), "1024 PB")

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


class StageSectionsTest(unittest.TestCase):
    """S2 / S4 的数据接入人类报告后的小节渲染。"""

    def result(self) -> ScanResult:
        return ScanResult(root=Path("/tmp/demo"), file_count=2, total_size=10)

    def test_duplicate_section_is_rendered(self) -> None:
        duplicates = DuplicateReport(
            groups=[
                DuplicateGroup(
                    size=5,
                    digest="a" * 64,
                    paths=(Path("/tmp/demo/a"), Path("/tmp/demo/b")),
                    inode_count=2,
                )
            ]
        )

        text = render_report(self.result(), duplicates, timestamp=FIXED)

        self.assertIn("重复文件（1 组，可回收 5 B）", text)
        self.assertIn("sha256:" + "a" * 16, text)

    def test_duplicate_read_errors_are_not_swallowed(self) -> None:
        duplicates = DuplicateReport(
            issues=[ScanIssue(Path("/tmp/demo/x"), "Permission denied")]
        )

        text = render_report(self.result(), duplicates, timestamp=FIXED)

        self.assertIn("未能读取", text)
        self.assertIn("Permission denied", text)

    def test_suggestion_section_renders_command_and_warning(self) -> None:
        suggestions = [
            Suggestion(
                kind="duplicate-files",
                severity="warn",
                title="重复文件",
                detail="细节",
                command="rm -- /b",
            )
        ]

        text = render_report(self.result(), None, suggestions, timestamp=FIXED)

        self.assertIn("建议（1 条）", text)
        self.assertIn("$ rm -- /b", text)
        self.assertIn("本工具不会执行任何删除", text)

    def test_no_suggestion_section_when_list_is_empty(self) -> None:
        text = render_report(self.result(), None, [], timestamp=FIXED)

        self.assertNotIn("建议（", text)

    def test_plan_section_is_rendered(self) -> None:
        plan = ActionPlan(
            root=Path("/tmp/demo"),
            actions=[
                Action(
                    kind="delete-duplicate",
                    path=Path("/tmp/demo/b"),
                    size=5,
                    digest="d" * 64,
                    keep=Path("/tmp/demo/a"),
                    reason="r",
                )
            ],
            refusals=[Refusal(Path("/tmp/demo/.git/x"), "位于版本库内")],
            truncated=2,
        )

        text = render_report(self.result(), None, None, plan, timestamp=FIXED)

        self.assertIn("执行计划（1 个动作，可回收 5 B）", text)
        self.assertIn("另有 2 个动作未列出", text)
        self.assertIn("安全规则拒绝 1 项", text)
        self.assertIn("$ rm -- /tmp/demo/b", text)
        self.assertIn("本工具不会执行以上任何动作", text)

    def test_no_plan_section_when_not_requested(self) -> None:
        text = render_report(self.result(), timestamp=FIXED)

        self.assertNotIn("执行计划", text)

    def test_no_duplicate_section_when_skipped(self) -> None:
        text = render_report(self.result(), None, None, timestamp=FIXED)

        self.assertNotIn("重复文件", text)


class RenderJsonTest(unittest.TestCase):
    """针对 ``render_json`` 的契约测试。"""

    def make(self, **overrides: object) -> ScanResult:
        defaults: dict[str, object] = {
            "root": Path("/tmp/demo"),
            "file_count": 3,
            "dir_count": 1,
            "total_size": 1024,
        }
        defaults.update(overrides)
        return ScanResult(**defaults)  # type: ignore[arg-type]

    def parse(
        self,
        result: ScanResult,
        duplicates: DuplicateReport | None = None,
        suggestions: list[Suggestion] | None = None,
        plan: ActionPlan | None = None,
    ) -> dict:
        text = render_json(
            result, duplicates, suggestions, plan, timestamp=FIXED, elapsed=0.5
        )
        return json.loads(text)  # 解析成功即证明 stdout 只有 JSON

    def test_top_level_contract(self) -> None:
        payload = self.parse(self.make())

        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(
            set(payload),
            {
                "schema_version",
                "tool",
                "generated_at",
                "root",
                "elapsed_seconds",
                "totals",
                "by_subdirectory",
                "by_extension",
                "duplicates",
                "suggestions",
                "plan",
                "issues",
                "skipped_symlinks",
            },
        )

    def test_totals(self) -> None:
        payload = self.parse(self.make())

        self.assertEqual(payload["root"], "/tmp/demo")
        self.assertEqual(payload["tool"], "homecheck")
        self.assertEqual(payload["generated_at"], "2026-09-13T14:02:11")
        self.assertEqual(payload["elapsed_seconds"], 0.5)
        self.assertEqual(payload["totals"]["files"], 3)
        self.assertEqual(payload["totals"]["directories"], 1)
        self.assertEqual(payload["totals"]["bytes"], 1024)
        self.assertEqual(payload["totals"]["human"], "1 KB")

    def test_duplicates_is_null_when_skipped(self) -> None:
        self.assertIsNone(self.parse(self.make())["duplicates"])

    def test_duplicates_payload_shape(self) -> None:
        duplicates = DuplicateReport(
            groups=[
                DuplicateGroup(
                    size=10,
                    digest="d" * 64,
                    paths=(Path("/a"), Path("/b")),
                    inode_count=2,
                )
            ]
        )

        payload = self.parse(self.make(), duplicates)

        self.assertEqual(payload["duplicates"]["group_count"], 1)
        self.assertEqual(payload["duplicates"]["path_count"], 2)
        self.assertEqual(payload["duplicates"]["wasted_bytes"], 10)
        group = payload["duplicates"]["groups"][0]
        self.assertEqual(
            set(group),
            {"size", "digest", "inode_count", "wasted_bytes", "paths"},
        )
        self.assertEqual(group["paths"], ["/a", "/b"])

    def test_suggestions_payload_shape(self) -> None:
        suggestions = [
            Suggestion(
                kind="large-files",
                severity="info",
                title="T",
                detail="D",
                command=None,
            )
        ]

        payload = self.parse(self.make(), None, suggestions)

        self.assertEqual(
            payload["suggestions"][0],
            {
                "kind": "large-files",
                "severity": "info",
                "title": "T",
                "detail": "D",
                "command": None,
            },
        )

    def test_percentages_are_numbers(self) -> None:
        payload = self.parse(self.make(size_by_subdir={"a": 512}))

        entry = payload["by_subdirectory"][0]
        self.assertEqual(entry["name"], "a")
        self.assertEqual(entry["bytes"], 512)
        self.assertEqual(entry["percent"], 50.0)

    def test_plan_is_null_by_default(self) -> None:
        self.assertIsNone(self.parse(self.make())["plan"])

    def test_plan_payload_shape(self) -> None:
        plan = ActionPlan(
            root=Path("/tmp/demo"),
            actions=[
                Action(
                    kind="delete-duplicate",
                    path=Path("/a"),
                    size=5,
                    digest="d",
                    keep=Path("/b"),
                    reason="r",
                )
            ],
            refusals=[Refusal(Path("/c"), "why")],
            truncated=3,
        )

        payload = self.parse(self.make(), None, None, plan)["plan"]

        self.assertEqual(
            set(payload),
            {
                "action_count",
                "total_bytes",
                "truncated",
                "command",
                "actions",
                "refusals",
            },
        )
        self.assertEqual(payload["action_count"], 1)
        self.assertEqual(payload["total_bytes"], 5)
        self.assertEqual(payload["truncated"], 3)
        self.assertEqual(payload["command"], "rm -- /a")
        self.assertEqual(
            set(payload["actions"][0]),
            {"kind", "path", "size", "digest", "keep", "reason"},
        )

    def test_empty_plan_command_is_null(self) -> None:
        payload = self.parse(self.make(), None, None, ActionPlan(root=Path("/tmp/demo")))["plan"]

        self.assertEqual(payload["action_count"], 0)
        self.assertIsNone(payload["command"])

    def test_output_is_bare_json(self) -> None:
        text = render_json(self.make(), timestamp=FIXED)

        self.assertTrue(text.startswith("{"))
        self.assertTrue(text.endswith("}\n"))


class RenderMarkdownTest(unittest.TestCase):
    """针对 ``render_markdown`` 的测试。"""

    def test_has_title_and_tables(self) -> None:
        result = ScanResult(
            root=Path("/tmp/demo"),
            file_count=3,
            total_size=100,
            size_by_subdir={"alpha": 60},
            size_by_suffix={".txt": 100},
        )

        text = render_markdown(result, timestamp=FIXED, elapsed=0.1)

        self.assertTrue(text.startswith("# homecheck 扫描报告"))
        self.assertIn("| 指标 | 数值 |", text)
        self.assertIn("## 体积排行（前 1 项）", text)
        self.assertIn("alpha/", text)
        self.assertIn("| 体积 | 占比 | 类型 |", text)

    def test_pipes_in_table_cells_are_escaped(self) -> None:
        result = ScanResult(
            root=Path("/tmp/demo"), total_size=10, size_by_subdir={"a|b": 10}
        )

        text = render_markdown(result, timestamp=FIXED)

        self.assertIn("a\\|b/", text)

    def test_duplicate_and_suggestion_sections(self) -> None:
        result = ScanResult(root=Path("/tmp/demo"))
        duplicates = DuplicateReport(
            groups=[
                DuplicateGroup(
                    size=5,
                    digest="d" * 64,
                    paths=(Path("/a"), Path("/b")),
                    inode_count=2,
                )
            ]
        )
        suggestions = [
            Suggestion(
                kind="duplicate-files",
                severity="warn",
                title="标题",
                detail="细节",
                command="rm -- /b",
            )
        ]

        text = render_markdown(result, duplicates, suggestions, timestamp=FIXED)

        self.assertIn("## 重复文件", text)
        self.assertIn("## 建议", text)
        self.assertIn("```bash", text)
        self.assertIn("rm -- /b", text)
        self.assertIn("不会**执行任何删除", text)

    def test_plan_section(self) -> None:
        plan = ActionPlan(
            root=Path("/tmp/demo"),
            actions=[
                Action(
                    kind="delete-duplicate",
                    path=Path("/a"),
                    size=5,
                    digest="d",
                    keep=Path("/b"),
                    reason="r",
                )
            ],
        )

        text = render_markdown(
            ScanResult(root=Path("/tmp/demo")), None, None, plan, timestamp=FIXED
        )

        self.assertIn("## 执行计划", text)
        self.assertIn("rm -- /a", text)
        self.assertIn("不会**执行以上任何动作", text)

    def test_no_command_warning_when_no_commands(self) -> None:
        result = ScanResult(root=Path("/tmp/demo"))
        suggestions = [
            Suggestion(
                kind="large-files",
                severity="info",
                title="T",
                detail="D",
                command=None,
            )
        ]

        text = render_markdown(result, None, suggestions, timestamp=FIXED)

        self.assertNotIn("不会**执行任何删除", text)


if __name__ == "__main__":
    unittest.main()
