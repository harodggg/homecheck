"""``apply`` 模块的单元测试。

S5 的核心承诺是「只出计划、绝不动手」，所以测试重点在**安全规则**上：
宁可少删，也不能多删。
"""

from __future__ import annotations

import shlex
import tempfile
import unittest
from pathlib import Path

from homecheck.apply import build_plan
from homecheck.duplicates import DuplicateGroup, DuplicateReport
from homecheck.scan import FileRecord, ScanResult

ROOT = Path("/tmp/scan")


def record(path: str, *, size: int = 10, device: int = 1, inode: int = 1) -> FileRecord:
    """造一条文件记录。"""
    return FileRecord(
        path=Path(path), size=size, mtime=0.0, device=device, inode=inode
    )


def result_with(records: list[FileRecord], root: Path = ROOT) -> ScanResult:
    """造一个只带 files 的扫描结果。"""
    return ScanResult(root=root, files=records)


def group(
    paths: tuple[str, ...],
    *,
    size: int = 10,
    digest: str = "d",
) -> DuplicateGroup:
    """造一个重复组，保留第一份。"""
    return DuplicateGroup(
        size=size,
        digest=digest,
        paths=tuple(Path(path) for path in paths),
        inode_count=len(paths),
    )


class BuildPlanTest(unittest.TestCase):
    """针对 ``build_plan`` 的安全规则测试。"""

    def test_no_duplicates_yields_empty_plan(self) -> None:
        plan = build_plan(result_with([]), DuplicateReport())

        self.assertEqual(plan.actions, [])
        self.assertEqual(plan.command, "")
        self.assertEqual(plan.total_bytes, 0)

    def test_none_duplicates_yields_empty_plan(self) -> None:
        self.assertEqual(build_plan(result_with([]), None).actions, [])

    def test_keeps_first_and_plans_the_rest(self) -> None:
        paths = ("/tmp/scan/a.bin", "/tmp/scan/b.bin", "/tmp/scan/c.bin")
        records = [record(path, inode=index) for index, path in enumerate(paths)]

        plan = build_plan(result_with(records), DuplicateReport(groups=[group(paths)]))

        self.assertEqual(
            [str(action.path) for action in plan.actions],
            ["/tmp/scan/b.bin", "/tmp/scan/c.bin"],
        )
        self.assertTrue(
            all(action.keep == Path("/tmp/scan/a.bin") for action in plan.actions)
        )
        self.assertEqual(plan.total_bytes, 20)

    def test_refuses_paths_outside_root(self) -> None:
        records = [record("/tmp/scan/a.bin", inode=1), record("/etc/b.bin", inode=2)]

        plan = build_plan(
            result_with(records),
            DuplicateReport(groups=[group(("/tmp/scan/a.bin", "/etc/b.bin"))]),
        )

        self.assertEqual(plan.actions, [])
        self.assertEqual(len(plan.refusals), 1)
        self.assertIn("不在扫描根之下", plan.refusals[0].reason)

    def test_refuses_git_metadata_paths(self) -> None:
        records = [
            record("/tmp/scan/a.bin", inode=1),
            record("/tmp/scan/.git/objects/b", inode=2),
        ]

        plan = build_plan(
            result_with(records),
            DuplicateReport(groups=[group(("/tmp/scan/a.bin", "/tmp/scan/.git/objects/b"))]),
        )

        self.assertEqual(plan.actions, [])
        self.assertIn("版本库", plan.refusals[0].reason)

    def test_refuses_hardlink_of_kept_file(self) -> None:
        records = [
            record("/tmp/scan/a.bin", device=1, inode=7),
            record("/tmp/scan/b.bin", device=1, inode=7),
        ]

        plan = build_plan(
            result_with(records),
            DuplicateReport(groups=[group(("/tmp/scan/a.bin", "/tmp/scan/b.bin"))]),
        )

        self.assertEqual(plan.actions, [])
        self.assertIn("硬链接", plan.refusals[0].reason)

    def test_refusals_are_reported_not_silently_dropped(self) -> None:
        records = [record("/tmp/scan/a.bin", inode=1), record("/etc/b.bin", inode=2)]

        plan = build_plan(
            result_with(records),
            DuplicateReport(groups=[group(("/tmp/scan/a.bin", "/etc/b.bin"))]),
        )

        self.assertEqual(len(plan.refusals), 1)
        self.assertTrue(plan.refusals[0].reason)

    def test_max_actions_truncates_and_reports(self) -> None:
        paths = tuple(f"/tmp/scan/f{index}.bin" for index in range(5))
        records = [record(path, inode=index) for index, path in enumerate(paths)]

        plan = build_plan(
            result_with(records),
            DuplicateReport(groups=[group(paths)]),
            max_actions=2,
        )

        self.assertEqual(len(plan.actions), 2)
        self.assertEqual(plan.truncated, 2)

    def test_no_truncation_when_under_limit(self) -> None:
        paths = ("/tmp/scan/a.bin", "/tmp/scan/b.bin")
        records = [record(path, inode=index) for index, path in enumerate(paths)]

        plan = build_plan(result_with(records), DuplicateReport(groups=[group(paths)]))

        self.assertEqual(plan.truncated, 0)

    def test_actions_sorted_by_size_desc(self) -> None:
        groups = [
            group(("/tmp/scan/small_keep", "/tmp/scan/small_del"), size=1, digest="a"),
            group(("/tmp/scan/big_keep", "/tmp/scan/big_del"), size=999, digest="b"),
        ]
        records = [
            record(path, inode=index)
            for index, path in enumerate(
                [
                    "/tmp/scan/small_keep",
                    "/tmp/scan/small_del",
                    "/tmp/scan/big_keep",
                    "/tmp/scan/big_del",
                ]
            )
        ]

        plan = build_plan(result_with(records), DuplicateReport(groups=groups))

        self.assertEqual(plan.actions[0].size, 999)

    def test_command_is_quoted_and_non_recursive(self) -> None:
        paths = ("/tmp/scan/a b.bin", "/tmp/scan/c'd.bin")
        records = [record(path, inode=index) for index, path in enumerate(paths)]

        plan = build_plan(result_with(records), DuplicateReport(groups=[group(paths)]))

        tokens = shlex.split(plan.command)
        self.assertEqual(tokens[0], "rm")
        self.assertEqual(tokens[1], "--")
        self.assertEqual(tokens[2], "/tmp/scan/c'd.bin")
        for token in tokens[1:]:
            self.assertFalse(token.startswith("-r"), token)
            self.assertFalse(token.startswith("-f"), token)

    def test_plan_generation_writes_nothing(self) -> None:
        """S5 的硬约束：生成计划本身零写入。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.bin").write_bytes(b"x" * 10)
            (root / "b.bin").write_bytes(b"x" * 10)
            before = {p.name: p.stat().st_mtime_ns for p in root.iterdir()}

            scan_result = ScanResult(
                root=root,
                file_count=2,
                total_size=20,
                files=[
                    FileRecord(path=root / "a.bin", size=10, mtime=0.0, device=1, inode=1),
                    FileRecord(path=root / "b.bin", size=10, mtime=0.0, device=1, inode=2),
                ],
            )
            plan = build_plan(
                scan_result,
                DuplicateReport(
                    groups=[group((str(root / "a.bin"), str(root / "b.bin")))]
                ),
            )

            self.assertEqual(len(plan.actions), 1)
            self.assertEqual(
                {p.name: p.stat().st_mtime_ns for p in root.iterdir()}, before
            )


if __name__ == "__main__":
    unittest.main()
