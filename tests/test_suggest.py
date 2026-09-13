"""``suggest`` 模块的单元测试。

重点验证两件事：建议**零副作用**，以及生成的命令**不会误伤**。
"""

from __future__ import annotations

import shlex
import unittest
from pathlib import Path

from homecheck.duplicates import DuplicateGroup, DuplicateReport
from homecheck.scan import FileRecord, ScanIssue, ScanResult
from homecheck.suggest import (
    DEFAULT_LARGE_BYTES,
    DEFAULT_STALE_DAYS,
    INFO,
    WARN,
    build_suggestions,
)

NOW = 1_700_000_000.0
DAY = 86400


def make_result(**overrides: object) -> ScanResult:
    """造一个可控的 ScanResult。"""
    defaults: dict[str, object] = {
        "root": Path("/tmp/demo"),
        "file_count": 0,
        "dir_count": 0,
        "total_size": 0,
        "size_by_subdir": {},
        "size_by_suffix": {},
        "files": [],
        "issues": [],
        "symlinks_skipped": 0,
    }
    defaults.update(overrides)
    return ScanResult(**defaults)  # type: ignore[arg-type]


def record(path: str, size: int, age_days: float = 0.0) -> FileRecord:
    """造一条文件记录。"""
    return FileRecord(
        path=Path(path),
        size=size,
        mtime=NOW - age_days * DAY,
        device=1,
        inode=hash(path) % 100000,
    )


class BuildSuggestionsTest(unittest.TestCase):
    """针对 ``build_suggestions`` 的规则测试。"""

    def build(self, result: ScanResult, duplicates: DuplicateReport | None = None, **kwargs: object) -> list:
        return build_suggestions(result, duplicates, now=NOW, **kwargs)  # type: ignore[arg-type]

    def kinds(self, suggestions: list) -> set[str]:
        return {item.kind for item in suggestions}

    def test_clean_result_yields_no_suggestions(self) -> None:
        self.assertEqual(self.build(make_result()), [])

    def test_duplicate_rule_emits_one_suggestion_per_group(self) -> None:
        duplicates = DuplicateReport(
            groups=[
                DuplicateGroup(
                    size=100,
                    digest="abc",
                    paths=(Path("/a"), Path("/b")),
                    inode_count=2,
                )
            ]
        )

        suggestions = self.build(make_result(), duplicates)

        self.assertEqual(len(suggestions), 1)
        item = suggestions[0]
        self.assertEqual(item.kind, "duplicate-files")
        self.assertEqual(item.severity, WARN)
        self.assertEqual(item.command, "rm -- /b")

    def test_command_quotes_paths_with_spaces(self) -> None:
        duplicates = DuplicateReport(
            groups=[
                DuplicateGroup(
                    size=1,
                    digest="d",
                    paths=(Path("/keep me.bin"), Path("/del me's.bin")),
                    inode_count=2,
                )
            ]
        )

        command = self.build(make_result(), duplicates)[0].command

        self.assertIsNotNone(command)
        assert command is not None
        # 逐个 token 用 shlex 还原，确认转义没有破坏路径
        tokens = shlex.split(command)
        self.assertEqual(tokens[0], "rm")
        self.assertEqual(tokens[1], "--")
        self.assertEqual(tokens[2], "/del me's.bin")

    def test_command_never_uses_recursive_or_force_flags(self) -> None:
        duplicates = DuplicateReport(
            groups=[
                DuplicateGroup(
                    size=1,
                    digest="d",
                    paths=(Path("/a"), Path("/b")),
                    inode_count=2,
                )
            ]
        )

        command = self.build(make_result(), duplicates)[0].command

        assert command is not None
        tokens = shlex.split(command)
        self.assertEqual(tokens[0], "rm")
        for token in tokens[1:]:
            self.assertFalse(token.startswith("-r"), token)
            self.assertFalse(token.startswith("-f"), token)

    def test_duplicate_commands_are_capped_with_summary(self) -> None:
        """真实目录里重复组可能上千，输出量必须有硬上限。"""
        groups = [
            DuplicateGroup(
                size=100 - index,
                digest=f"{index:064d}",
                paths=(Path(f"/a{index}"), Path(f"/b{index}")),
                inode_count=2,
            )
            for index in range(10)
        ]

        suggestions = self.build(
            make_result(),
            DuplicateReport(groups=groups),
            max_duplicate_commands=3,
        )

        with_command = [item for item in suggestions if item.command]
        self.assertEqual(len(with_command), 3)
        summary = [item for item in suggestions if item.command is None]
        self.assertEqual(len(summary), 1)
        self.assertIn("另有 7 组", summary[0].title)

    def test_no_summary_when_all_groups_fit(self) -> None:
        duplicates = DuplicateReport(
            groups=[
                DuplicateGroup(
                    size=1,
                    digest="d",
                    paths=(Path("/a"), Path("/b")),
                    inode_count=2,
                )
            ]
        )

        suggestions = self.build(make_result(), duplicates)

        self.assertEqual(len(suggestions), 1)
        self.assertIsNotNone(suggestions[0].command)

    def test_no_duplicate_rule_when_duplicates_skipped(self) -> None:
        suggestions = self.build(make_result(), None)

        self.assertNotIn("duplicate-files", self.kinds(suggestions))

    def test_stale_large_files_rule(self) -> None:
        result = make_result(
            files=[
                record("/old.bin", DEFAULT_LARGE_BYTES + 1, age_days=DEFAULT_STALE_DAYS + 10),
                record("/fresh.bin", DEFAULT_LARGE_BYTES + 1, age_days=1),
            ]
        )

        suggestions = self.build(result)

        self.assertIn("stale-large-files", self.kinds(suggestions))
        stale = next(item for item in suggestions if item.kind == "stale-large-files")
        self.assertEqual(stale.severity, WARN)
        self.assertIn("/old.bin", stale.detail)
        self.assertNotIn("/fresh.bin", stale.detail)
        self.assertIsNone(stale.command)

    def test_large_files_rule_ignores_small_files(self) -> None:
        result = make_result(files=[record("/small.bin", 10)])

        self.assertNotIn("large-files", self.kinds(self.build(result)))

    def test_large_files_rule_triggers_on_big_file(self) -> None:
        result = make_result(files=[record("/big.bin", DEFAULT_LARGE_BYTES)])

        suggestions = self.build(result)

        large = next(item for item in suggestions if item.kind == "large-files")
        self.assertEqual(large.severity, INFO)
        self.assertIsNone(large.command)

    def test_incomplete_scan_rule(self) -> None:
        result = make_result(issues=[ScanIssue(Path("/locked"), "Permission denied")])

        suggestions = self.build(result)

        incomplete = next(item for item in suggestions if item.kind == "incomplete-scan")
        self.assertEqual(incomplete.severity, WARN)

    def test_dominant_directory_rule(self) -> None:
        result = make_result(
            total_size=1000,
            size_by_subdir={"big": 400, "small": 100},
        )

        suggestions = self.build(result)

        dominant = [item for item in suggestions if item.kind == "dominant-directory"]
        self.assertEqual(len(dominant), 1)
        self.assertIn("big", dominant[0].title)

    def test_dominant_directory_skipped_when_total_is_zero(self) -> None:
        result = make_result(total_size=0, size_by_subdir={"a": 0})

        self.assertNotIn("dominant-directory", self.kinds(self.build(result)))

    def test_warnings_sort_before_info(self) -> None:
        result = make_result(
            total_size=1000,
            size_by_subdir={"big": 900},
            files=[record("/big.bin", DEFAULT_LARGE_BYTES)],
            issues=[ScanIssue(Path("/locked"), "Permission denied")],
        )

        severities = [item.severity for item in self.build(result)]

        self.assertEqual(severities, sorted(severities, key=lambda s: 0 if s == WARN else 1))

    def test_result_is_stable_across_calls(self) -> None:
        result = make_result(files=[record("/a.bin", DEFAULT_LARGE_BYTES)])

        self.assertEqual(self.build(result), self.build(result))

    def test_does_not_mutate_the_scan_result(self) -> None:
        """建议引擎必须零副作用 —— 不能改动传入的数据。"""
        result = make_result(files=[record("/a.bin", DEFAULT_LARGE_BYTES)])
        before = list(result.files)
        before_total = result.total_size

        self.build(result)

        self.assertEqual(result.files, before)
        self.assertEqual(result.total_size, before_total)


if __name__ == "__main__":
    unittest.main()
