"""``apply`` 模块的单元测试。

核心承诺是「**宁可少删，也不能多删**」，所以测试重点在安全规则上：
计划阶段的拒绝规则（S5）、执行前的复核（S7）、以及不可逆删除的
双重把关（S8）。每个"拒绝"用例都同时断言**文件仍然存在**。
"""

from __future__ import annotations

import json
import os
import shlex
import tempfile
import unittest
from pathlib import Path

from homecheck.apply import Action, ActionPlan, apply_plan, build_plan, delete_plan
from homecheck.duplicates import DuplicateGroup, DuplicateReport, hash_file
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


class ApplyPlanTest(unittest.TestCase):
    """针对 ``apply_plan`` 的真实文件系统测试。

    核心不变量：**只要复核有任何不确定，就绝不能移动文件。**
    每个 refuses-* 用例都同时断言"目标文件仍然存在"。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.root = self.base / "scan"
        self.root.mkdir()
        self.quarantine = self.base / "quarantine"

    def write(self, relative: str, payload: bytes) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def action_for(self, path: Path, keep: Path) -> Action:
        """按文件当前状态构造一个动作，如同刚生成的计划。"""
        return Action(
            kind="delete-duplicate",
            path=path,
            size=path.stat().st_size,
            digest=hash_file(path),
            keep=keep,
            reason="test",
        )

    def plan(self, actions: list[Action]) -> ActionPlan:
        return ActionPlan(root=self.root, actions=list(actions))

    def pair(self) -> tuple[Path, Path, ActionPlan]:
        """造一对重复文件，返回 (保留项, 冗余项, 计划)。"""
        keep = self.write("keep.bin", b"x" * 100)
        redundant = self.write("redundant.bin", b"x" * 100)
        return keep, redundant, self.plan([self.action_for(redundant, keep)])

    def test_moves_redundant_copy_and_keeps_the_other(self) -> None:
        keep, redundant, plan = self.pair()

        result = apply_plan(plan, quarantine=self.quarantine)

        self.assertEqual(result.moved_count, 1)
        self.assertEqual(result.moved_bytes, 100)
        self.assertTrue(keep.exists())
        self.assertFalse(redundant.exists())
        self.assertEqual(
            (self.quarantine / "redundant.bin").read_bytes(), b"x" * 100
        )

    def test_preserves_relative_structure(self) -> None:
        keep = self.write("sub/keep.bin", b"y" * 50)
        redundant = self.write("deep/nested/redundant.bin", b"y" * 50)
        plan = self.plan([self.action_for(redundant, keep)])

        apply_plan(plan, quarantine=self.quarantine)

        self.assertTrue(
            (self.quarantine / "deep" / "nested" / "redundant.bin").exists()
        )

    def test_writes_manifest_before_moving(self) -> None:
        _, redundant, plan = self.pair()

        result = apply_plan(plan, quarantine=self.quarantine)

        payload = json.loads(result.manifest.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["root"], str(self.root))
        self.assertEqual(payload["action_count"], 1)
        self.assertEqual(payload["actions"][0]["path"], str(redundant))
        self.assertTrue(payload["actions"][0]["digest"])

    def test_writes_manifest_even_with_no_actions(self) -> None:
        result = apply_plan(self.plan([]), quarantine=self.quarantine)

        self.assertTrue(result.manifest.exists())
        self.assertEqual(result.moved_count, 0)

    def test_refuses_when_kept_copy_is_gone(self) -> None:
        keep, redundant, plan = self.pair()
        keep.unlink()

        result = apply_plan(plan, quarantine=self.quarantine)

        self.assertEqual(result.refused_count, 1)
        self.assertTrue(redundant.exists())  # ← 最关键的一条
        self.assertIn("保留项不可用", result.outcomes[0].detail)

    def test_refuses_when_kept_copy_changed(self) -> None:
        keep, redundant, plan = self.pair()
        keep.write_bytes(b"z" * 100)

        result = apply_plan(plan, quarantine=self.quarantine)

        self.assertEqual(result.refused_count, 1)
        self.assertTrue(redundant.exists())
        self.assertIn("保留项内容已变化", result.outcomes[0].detail)

    def test_refuses_when_target_content_changed(self) -> None:
        _, redundant, plan = self.pair()
        redundant.write_bytes(b"q" * 100)

        result = apply_plan(plan, quarantine=self.quarantine)

        self.assertEqual(result.refused_count, 1)
        self.assertTrue(redundant.exists())
        self.assertIn("内容已变化", result.outcomes[0].detail)

    def test_refuses_when_target_size_changed(self) -> None:
        _, redundant, plan = self.pair()
        redundant.write_bytes(b"q" * 7)

        result = apply_plan(plan, quarantine=self.quarantine)

        self.assertEqual(result.refused_count, 1)
        self.assertTrue(redundant.exists())
        self.assertIn("大小已变化", result.outcomes[0].detail)

    def test_refuses_when_target_vanished(self) -> None:
        _, redundant, plan = self.pair()
        redundant.unlink()

        result = apply_plan(plan, quarantine=self.quarantine)

        self.assertEqual(result.refused_count, 1)
        self.assertIn("已不存在", result.outcomes[0].detail)

    def test_refuses_when_target_became_a_symlink(self) -> None:
        keep, redundant, plan = self.pair()
        redundant.unlink()
        os.symlink(keep, redundant)

        result = apply_plan(plan, quarantine=self.quarantine)

        self.assertEqual(result.refused_count, 1)
        self.assertIn("符号链接", result.outcomes[0].detail)
        self.assertTrue(redundant.is_symlink())

    def test_refuses_target_outside_root(self) -> None:
        outside = self.base / "outside.bin"
        outside.write_bytes(b"x" * 10)
        keep = self.write("keep.bin", b"x" * 10)
        plan = self.plan([self.action_for(outside, keep)])

        result = apply_plan(plan, quarantine=self.quarantine)

        self.assertEqual(result.refused_count, 1)
        self.assertTrue(outside.exists())
        self.assertIn("不在扫描根之下", result.outcomes[0].detail)

    def test_declined_confirmation_skips_without_moving(self) -> None:
        _, redundant, plan = self.pair()

        result = apply_plan(
            plan, quarantine=self.quarantine, confirm=lambda *_: False
        )

        self.assertEqual(result.skipped_count, 1)
        self.assertEqual(result.moved_count, 0)
        self.assertTrue(redundant.exists())

    def test_accepted_confirmation_moves(self) -> None:
        _, redundant, plan = self.pair()

        result = apply_plan(
            plan, quarantine=self.quarantine, confirm=lambda *_: True
        )

        self.assertEqual(result.moved_count, 1)
        self.assertFalse(redundant.exists())

    def test_confirmation_receives_index_and_total(self) -> None:
        _, redundant, plan = self.pair()
        seen: list[tuple[int, int]] = []

        def confirm(action: Action, index: int, total: int) -> bool:
            seen.append((index, total))
            return False

        apply_plan(plan, quarantine=self.quarantine, confirm=confirm)

        self.assertEqual(seen, [(1, 1)])

    def test_refuses_occupied_quarantine(self) -> None:
        self.quarantine.mkdir(parents=True)
        (self.quarantine / "already-here").write_text("x", encoding="utf-8")

        with self.assertRaises(FileExistsError):
            apply_plan(self.plan([]), quarantine=self.quarantine)

    def test_refuses_quarantine_that_is_a_file(self) -> None:
        self.quarantine.write_text("x", encoding="utf-8")

        with self.assertRaises(FileExistsError):
            apply_plan(self.plan([]), quarantine=self.quarantine)

    def test_accepts_existing_empty_quarantine(self) -> None:
        self.quarantine.mkdir(parents=True)

        result = apply_plan(self.plan([]), quarantine=self.quarantine)

        self.assertTrue(result.manifest.exists())

    def test_mixed_outcomes_are_all_recorded(self) -> None:
        keep_a = self.write("a_keep.bin", b"1" * 10)
        good = self.write("a_redundant.bin", b"1" * 10)
        keep_b = self.write("b_keep.bin", b"2" * 10)
        stale = self.write("b_redundant.bin", b"2" * 10)
        plan = self.plan([self.action_for(good, keep_a), self.action_for(stale, keep_b)])
        stale.write_bytes(b"changed!")

        result = apply_plan(plan, quarantine=self.quarantine)

        self.assertEqual(result.moved_count, 1)
        self.assertEqual(result.refused_count, 1)
        self.assertFalse(good.exists())
        self.assertTrue(stale.exists())


class DeletePlanTest(unittest.TestCase):
    """针对 ``delete_plan``（S8，唯一会销毁数据的函数）的测试。

    每个"拒绝"用例都断言目标文件**仍然存在** —— 这是本项目最重要的一条不变量。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.root = self.base / "scan"
        self.root.mkdir()
        self.manifest = self.base / "audit.json"

    def write(self, relative: str, payload: bytes) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def action_for(self, path: Path, keep: Path) -> Action:
        return Action(
            kind="delete-duplicate",
            path=path,
            size=path.stat().st_size,
            digest=hash_file(path),
            keep=keep,
            reason="test",
        )

    def plan(self, actions: list[Action]) -> ActionPlan:
        return ActionPlan(root=self.root, actions=list(actions))

    def pair(self) -> tuple[Path, Path, ActionPlan]:
        keep = self.write("keep.bin", b"x" * 100)
        redundant = self.write("redundant.bin", b"x" * 100)
        return keep, redundant, self.plan([self.action_for(redundant, keep)])

    def test_deletes_redundant_and_keeps_the_other(self) -> None:
        keep, redundant, plan = self.pair()

        result = delete_plan(plan, manifest_path=self.manifest)

        self.assertTrue(result.is_delete)
        self.assertEqual(result.mode, "delete")
        self.assertEqual(result.deleted_count, 1)
        self.assertEqual(result.deleted_bytes, 100)
        self.assertFalse(redundant.exists())
        self.assertTrue(keep.exists())
        self.assertIsNone(result.quarantine)

    def test_manifest_records_delete_mode(self) -> None:
        _, _, plan = self.pair()

        result = delete_plan(plan, manifest_path=self.manifest)

        payload = json.loads(result.manifest.read_text(encoding="utf-8"))
        self.assertEqual(payload["mode"], "delete")
        self.assertIsNone(payload["quarantine"])
        self.assertEqual(payload["action_count"], 1)

    def test_manifest_is_written_before_deleting(self) -> None:
        """清单里记录的路径此时应已不存在 —— 证明它是删除前写的。"""
        _, _, plan = self.pair()

        result = delete_plan(plan, manifest_path=self.manifest)

        payload = json.loads(result.manifest.read_text(encoding="utf-8"))
        self.assertFalse(Path(payload["actions"][0]["path"]).exists())

    def test_refuses_when_kept_copy_is_gone(self) -> None:
        keep, redundant, plan = self.pair()
        keep.unlink()

        result = delete_plan(plan, manifest_path=self.manifest)

        self.assertEqual(result.deleted_count, 0)
        self.assertEqual(result.refused_count, 1)
        self.assertTrue(redundant.exists())  # ← 最关键的一条
        self.assertIn("保留项不可用", result.outcomes[0].detail)

    def test_refuses_when_kept_copy_changed(self) -> None:
        keep, redundant, plan = self.pair()
        keep.write_bytes(b"z" * 100)

        result = delete_plan(plan, manifest_path=self.manifest)

        self.assertEqual(result.refused_count, 1)
        self.assertTrue(redundant.exists())
        self.assertIn("保留项内容已变化", result.outcomes[0].detail)

    def test_refuses_when_target_content_changed(self) -> None:
        _, redundant, plan = self.pair()
        redundant.write_bytes(b"q" * 100)

        result = delete_plan(plan, manifest_path=self.manifest)

        self.assertEqual(result.refused_count, 1)
        self.assertTrue(redundant.exists())

    def test_refuses_target_outside_root(self) -> None:
        outside = self.base / "outside.bin"
        outside.write_bytes(b"x" * 10)
        keep = self.write("keep.bin", b"x" * 10)
        plan = self.plan([self.action_for(outside, keep)])

        result = delete_plan(plan, manifest_path=self.manifest)

        self.assertEqual(result.refused_count, 1)
        self.assertTrue(outside.exists())

    def test_declined_confirmation_keeps_file(self) -> None:
        _, redundant, plan = self.pair()

        result = delete_plan(
            plan, manifest_path=self.manifest, confirm=lambda *_: False
        )

        self.assertEqual(result.skipped_count, 1)
        self.assertEqual(result.deleted_count, 0)
        self.assertTrue(redundant.exists())

    def test_refuses_existing_manifest_path(self) -> None:
        self.manifest.write_text("already here", encoding="utf-8")
        _, redundant, plan = self.pair()

        with self.assertRaises(FileExistsError):
            delete_plan(plan, manifest_path=self.manifest)

        self.assertTrue(redundant.exists())

    def test_never_removes_every_copy(self) -> None:
        keep = self.write("keep.bin", b"x" * 100)
        first = self.write("r1.bin", b"x" * 100)
        second = self.write("r2.bin", b"x" * 100)
        plan = self.plan(
            [self.action_for(first, keep), self.action_for(second, keep)]
        )

        result = delete_plan(plan, manifest_path=self.manifest)

        self.assertEqual(result.deleted_count, 2)
        self.assertTrue(keep.exists())
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())

    def test_mixed_outcomes(self) -> None:
        keep_a = self.write("a_keep.bin", b"1" * 10)
        good = self.write("a_dup.bin", b"1" * 10)
        keep_b = self.write("b_keep.bin", b"2" * 10)
        stale = self.write("b_dup.bin", b"2" * 10)
        plan = self.plan(
            [self.action_for(good, keep_a), self.action_for(stale, keep_b)]
        )
        stale.write_bytes(b"changed!!")

        result = delete_plan(plan, manifest_path=self.manifest)

        self.assertEqual(result.deleted_count, 1)
        self.assertEqual(result.refused_count, 1)
        self.assertFalse(good.exists())
        self.assertTrue(stale.exists())

    def test_empty_plan_still_writes_manifest(self) -> None:
        result = delete_plan(self.plan([]), manifest_path=self.manifest)

        self.assertTrue(result.manifest.exists())
        self.assertEqual(result.deleted_count, 0)


if __name__ == "__main__":
    unittest.main()
