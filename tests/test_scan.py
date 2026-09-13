"""``scan`` 模块的单元测试。

所有测试都在 ``tempfile`` 临时目录内进行，**绝不触碰真实用户文件**。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from homecheck.scan import NO_EXTENSION, ROOT_LABEL, scan_directory


def _snapshot(root: Path) -> list[tuple[str, int, int]]:
    """记录目录树的 (相对路径, 大小, mtime_ns)，用于证明扫描没有写入。"""
    entries: list[tuple[str, int, int]] = []
    for path in sorted(root.rglob("*")):
        stat = path.lstat()
        entries.append((str(path.relative_to(root)), stat.st_size, stat.st_mtime_ns))
    return entries


def _running_as_root() -> bool:
    """判断当前是否为 root —— root 不受权限位限制。"""
    geteuid = getattr(os, "geteuid", None)
    return geteuid is not None and geteuid() == 0


class ScanDirectoryTest(unittest.TestCase):
    """针对 ``scan_directory`` 的行为测试。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def write(self, relative: str, size: int) -> Path:
        """在临时目录里造一个指定大小的文件。"""
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        return path

    def test_counts_files_dirs_and_total_size(self) -> None:
        self.write("a.txt", 10)
        self.write("b.txt", 20)
        self.write("sub/c.txt", 30)

        result = scan_directory(self.root)

        self.assertEqual(result.file_count, 3)
        self.assertEqual(result.dir_count, 1)
        self.assertEqual(result.total_size, 60)
        self.assertEqual(result.issues, [])

    def test_size_by_subdir_groups_by_top_level(self) -> None:
        self.write("root_file.txt", 5)
        self.write("alpha/x.bin", 100)
        self.write("alpha/deep/y.bin", 200)
        self.write("beta/z.bin", 50)

        result = scan_directory(self.root)

        self.assertEqual(
            result.size_by_subdir,
            {ROOT_LABEL: 5, "alpha": 300, "beta": 50},
        )

    def test_size_by_suffix_is_case_insensitive(self) -> None:
        self.write("a.txt", 10)
        self.write("b.TXT", 20)
        self.write("noext", 5)
        self.write(".hidden", 7)

        result = scan_directory(self.root)

        self.assertEqual(result.size_by_suffix[".txt"], 30)
        self.assertEqual(result.size_by_suffix[NO_EXTENSION], 12)

    def test_skips_symlinks_and_does_not_follow_them(self) -> None:
        self.write("real/inside.txt", 10)
        self.write("real/other.txt", 10)
        os.symlink(self.root / "real", self.root / "link_dir")
        os.symlink(self.root / "real" / "inside.txt", self.root / "link_file")

        result = scan_directory(self.root)

        self.assertEqual(result.file_count, 2)
        self.assertEqual(result.total_size, 20)
        self.assertEqual(result.dir_count, 1)
        self.assertEqual(result.symlinks_skipped, 2)
        self.assertNotIn("link_dir", result.size_by_subdir)

    def test_max_depth_limits_traversal(self) -> None:
        self.write("top.txt", 1)
        self.write("one/mid.txt", 10)
        self.write("one/two/deep.txt", 100)

        shallow = scan_directory(self.root, max_depth=1)
        self.assertEqual(shallow.file_count, 2)
        self.assertEqual(shallow.total_size, 11)
        self.assertEqual(shallow.size_by_subdir, {ROOT_LABEL: 1, "one": 10})

        full = scan_directory(self.root)
        self.assertEqual(full.file_count, 3)
        self.assertEqual(full.total_size, 111)

    def test_empty_directory_is_not_an_error(self) -> None:
        result = scan_directory(self.root)

        self.assertEqual(result.file_count, 0)
        self.assertEqual(result.total_size, 0)
        self.assertEqual(result.size_by_subdir, {})
        self.assertEqual(result.issues, [])

    def test_missing_root_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            scan_directory(self.root / "does_not_exist")

    def test_file_as_root_raises(self) -> None:
        target = self.write("plain.txt", 1)
        with self.assertRaises(NotADirectoryError):
            scan_directory(target)

    @unittest.skipIf(_running_as_root(), "root 不受权限位限制")
    def test_permission_denied_is_recorded_not_fatal(self) -> None:
        self.write("open/ok.txt", 5)
        locked = self.root / "locked"
        locked.mkdir()
        (locked / "secret.txt").write_bytes(b"x" * 50)
        os.chmod(locked, 0o000)
        self.addCleanup(os.chmod, locked, 0o755)

        result = scan_directory(self.root)

        self.assertEqual(result.file_count, 1)
        self.assertEqual(result.total_size, 5)
        self.assertEqual(len(result.issues), 1)
        self.assertEqual(result.issues[0].path, locked)
        self.assertIn("Permission denied", result.issues[0].reason)

    def test_scan_does_not_modify_the_filesystem(self) -> None:
        """直接验证 S1 的核心安全承诺：扫描全程零写入。"""
        self.write("a/b.txt", 10)
        self.write("c.txt", 5)
        self.write("a/deep/d.txt", 20)

        before = _snapshot(self.root)
        scan_directory(self.root)

        self.assertEqual(before, _snapshot(self.root))


if __name__ == "__main__":
    unittest.main()
