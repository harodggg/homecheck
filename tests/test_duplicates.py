"""``duplicates`` 模块的单元测试。

所有测试都在 ``tempfile`` 临时目录内进行，**绝不触碰真实用户文件**。
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from homecheck.duplicates import (
    DuplicateGroup,
    find_duplicates,
    hash_file,
    iter_redundant_paths,
)
from homecheck.scan import FileRecord, scan_directory


def _running_as_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return geteuid is not None and geteuid() == 0


class FindDuplicatesTest(unittest.TestCase):
    """针对 ``find_duplicates`` 的行为测试。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def write(self, relative: str, content: bytes) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def collect(self) -> list[FileRecord]:
        """扫描临时目录，拿到文件明细。"""
        return scan_directory(self.root).files

    def test_same_content_different_names_is_duplicate(self) -> None:
        self.write("a.txt", b"hello")
        self.write("nested/b.txt", b"hello")

        report = find_duplicates(self.collect())

        self.assertEqual(len(report.groups), 1)
        group = report.groups[0]
        self.assertEqual(group.size, 5)
        self.assertEqual(group.inode_count, 2)
        self.assertEqual(group.wasted_bytes, 5)
        self.assertEqual(len(group.paths), 2)
        self.assertEqual(report.path_count, 2)
        self.assertEqual(report.wasted_bytes, 5)

    def test_same_size_different_content_is_not_duplicate(self) -> None:
        self.write("a.txt", b"hello")
        self.write("b.txt", b"world")

        report = find_duplicates(self.collect())

        self.assertEqual(report.groups, [])

    def test_empty_files_are_excluded(self) -> None:
        self.write("a.txt", b"")
        self.write("b.txt", b"")

        report = find_duplicates(self.collect())

        self.assertEqual(report.groups, [])

    def test_all_hardlinks_are_not_a_duplicate_group(self) -> None:
        target = self.write("a.txt", b"hello")
        os.link(target, self.root / "b.txt")

        report = find_duplicates(self.collect())

        self.assertEqual(report.groups, [])

    def test_hardlink_plus_copy_reports_one_recoverable_copy(self) -> None:
        target = self.write("a.txt", b"hello")
        os.link(target, self.root / "b.txt")
        self.write("c.txt", b"hello")

        report = find_duplicates(self.collect())

        self.assertEqual(len(report.groups), 1)
        group = report.groups[0]
        self.assertEqual(group.inode_count, 2)
        self.assertEqual(group.wasted_bytes, 5)
        self.assertEqual(len(group.paths), 3)

    def test_groups_sorted_by_wasted_bytes_desc(self) -> None:
        self.write("big1.bin", b"x" * 1000)
        self.write("big2.bin", b"x" * 1000)
        self.write("small1.bin", b"y" * 10)
        self.write("small2.bin", b"y" * 10)

        report = find_duplicates(self.collect())

        self.assertEqual([group.size for group in report.groups], [1000, 10])
        self.assertEqual(report.wasted_bytes, 1010)
        self.assertEqual(report.path_count, 4)

    def test_three_copies_waste_two_copies(self) -> None:
        for name in ("a.bin", "b.bin", "c.bin"):
            self.write(name, b"z" * 100)

        report = find_duplicates(self.collect())

        group = report.groups[0]
        self.assertEqual(group.inode_count, 3)
        self.assertEqual(group.wasted_bytes, 200)

    @unittest.skipIf(_running_as_root(), "root 不受权限位限制")
    def test_unreadable_file_is_recorded_not_fatal(self) -> None:
        target = self.write("secret.bin", b"data")
        self.write("copy.bin", b"data")
        os.chmod(target, 0o000)
        self.addCleanup(os.chmod, target, 0o644)

        report = find_duplicates(self.collect())

        self.assertEqual(report.groups, [])
        self.assertEqual(len(report.issues), 1)
        self.assertIn("Permission denied", report.issues[0].reason)

    def test_no_files_yields_empty_report(self) -> None:
        report = find_duplicates([])

        self.assertEqual(report.groups, [])
        self.assertEqual(report.wasted_bytes, 0)
        self.assertEqual(report.path_count, 0)


class HashFileTest(unittest.TestCase):
    """针对 ``hash_file`` 的测试。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_matches_hashlib_reference(self) -> None:
        path = self.root / "x.bin"
        path.write_bytes(b"abc")

        self.assertEqual(hash_file(path), hashlib.sha256(b"abc").hexdigest())

    def test_streams_across_chunk_boundaries(self) -> None:
        payload = bytes(range(256)) * 100
        path = self.root / "big.bin"
        path.write_bytes(payload)

        self.assertEqual(
            hash_file(path, chunk_size=7),
            hashlib.sha256(payload).hexdigest(),
        )


class IterRedundantPathsTest(unittest.TestCase):
    """针对 ``iter_redundant_paths`` 的测试。"""

    def test_keeps_first_path_only(self) -> None:
        group = DuplicateGroup(
            size=1,
            digest="d",
            paths=(Path("/a"), Path("/b"), Path("/c")),
            inode_count=3,
        )

        self.assertEqual(
            list(iter_redundant_paths(group)),
            [Path("/b"), Path("/c")],
        )


if __name__ == "__main__":
    unittest.main()
