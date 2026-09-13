"""``cli`` 模块的端到端测试。

这些测试直接调用 ``main()``，覆盖退出码、stdout 纯度与各输出模式。
所有文件操作都在 ``tempfile`` 临时目录内，绝不触碰真实用户文件。
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from homecheck.cli import EXIT_BAD_ROOT, EXIT_OK, EXIT_USAGE, main


class MainTest(unittest.TestCase):
    """针对 ``main`` 的端到端行为测试。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "a.bin").write_bytes(b"x" * 100)
        (self.root / "copy.bin").write_bytes(b"x" * 100)
        (self.root / "sub").mkdir()
        (self.root / "sub" / "b.txt").write_bytes(b"y" * 10)

    def run_main(self, argv: list[str]) -> tuple[int, str, str]:
        """执行 ``main`` 并捕获退出码与两路输出。"""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(argv)
            except SystemExit as exc:  # argparse 的错误路径
                code = int(exc.code or 0)
        return code, out.getvalue(), err.getvalue()

    def test_success_exit_code(self) -> None:
        code, out, _ = self.run_main([str(self.root)])

        self.assertEqual(code, EXIT_OK)
        self.assertIn("homecheck — 扫描报告", out)

    def test_missing_root_exit_code(self) -> None:
        code, _, err = self.run_main([str(self.root / "nope")])

        self.assertEqual(code, EXIT_BAD_ROOT)
        self.assertIn("扫描根不存在", err)

    def test_file_as_root_exit_code(self) -> None:
        code, _, err = self.run_main([str(self.root / "a.bin")])

        self.assertEqual(code, EXIT_BAD_ROOT)
        self.assertIn("不是目录", err)

    def test_negative_top_is_usage_error(self) -> None:
        code, _, err = self.run_main(["--top", "-1", str(self.root)])

        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("参数错误", err)

    def test_negative_max_actions_is_usage_error(self) -> None:
        code, _, _ = self.run_main(["--max-actions", "-1", str(self.root)])

        self.assertEqual(code, EXIT_USAGE)

    def test_json_mode_emits_only_json(self) -> None:
        code, out, _ = self.run_main(["--json", str(self.root)])

        self.assertEqual(code, EXIT_OK)
        payload = json.loads(out)  # 解析成功即证明 stdout 干净
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["totals"]["files"], 3)

    def test_json_and_markdown_are_mutually_exclusive(self) -> None:
        code, _, _ = self.run_main(["--json", "--markdown", str(self.root)])

        self.assertEqual(code, EXIT_USAGE)

    def test_skip_duplicates_leaves_duplicates_null(self) -> None:
        code, out, _ = self.run_main(["--json", "--skip-duplicates", str(self.root)])

        self.assertEqual(code, EXIT_OK)
        self.assertIsNone(json.loads(out)["duplicates"])

    def test_duplicates_are_found_end_to_end(self) -> None:
        code, out, _ = self.run_main(["--json", str(self.root)])

        payload = json.loads(out)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(payload["duplicates"]["group_count"], 1)

    def test_plan_flag_adds_plan_without_executing(self) -> None:
        before = sorted(p.name for p in self.root.rglob("*"))

        code, out, _ = self.run_main(["--plan", "--json", str(self.root)])

        self.assertEqual(code, EXIT_OK)
        payload = json.loads(out)
        self.assertIsNotNone(payload["plan"])
        self.assertEqual(payload["plan"]["action_count"], 1)
        # 关键：计划里的动作没有被执行
        self.assertEqual(sorted(p.name for p in self.root.rglob("*")), before)
        self.assertTrue((self.root / "copy.bin").exists())

    def test_plan_is_null_by_default(self) -> None:
        _, out, _ = self.run_main(["--json", str(self.root)])

        self.assertIsNone(json.loads(out)["plan"])

    def test_markdown_mode_has_plan_section(self) -> None:
        code, out, _ = self.run_main(["--plan", "--markdown", str(self.root)])

        self.assertEqual(code, EXIT_OK)
        self.assertIn("## 执行计划", out)

    def test_min_duplicate_bytes_filters_small_files(self) -> None:
        code, out, _ = self.run_main(
            ["--json", "--min-duplicate-bytes", "1000", str(self.root)]
        )

        payload = json.loads(out)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(payload["duplicates"]["group_count"], 0)


if __name__ == "__main__":
    unittest.main()
