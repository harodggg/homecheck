"""只读目录扫描与体积统计。

安全约束（见 ``docs/CONVENTIONS.md`` 第 6 节）：
本模块**只读**。它不得创建、修改、删除任何文件系统对象，
包括临时文件与缓存文件。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

#: 直接位于扫描根下的文件，在体积排行中的归类名
ROOT_LABEL = "(根目录)"

#: 没有扩展名的文件的归类名
NO_EXTENSION = "(无扩展名)"


@dataclass(frozen=True)
class ScanIssue:
    """扫描过程中遇到的问题（例如权限不足）。

    问题不会中断整体扫描，而是被记录下来，最后在报告中如实列出。
    """

    path: Path
    reason: str


@dataclass
class ScanResult:
    """一次扫描的完整结果。"""

    root: Path
    file_count: int = 0
    dir_count: int = 0
    total_size: int = 0
    #: 顶层子目录名 -> 体积；根目录直接包含的文件归入 ``ROOT_LABEL``
    size_by_subdir: dict[str, int] = field(default_factory=dict)
    #: 小写扩展名（含点）或 ``NO_EXTENSION`` -> 体积
    size_by_suffix: dict[str, int] = field(default_factory=dict)
    issues: list[ScanIssue] = field(default_factory=list)
    #: 被跳过的符号链接数量（Q4：不跟随符号链接）
    symlinks_skipped: int = 0


def scan_directory(root: Path, *, max_depth: int | None = None) -> ScanResult:
    """只读扫描 *root*，返回统计结果。

    参数:
        root: 扫描根目录，必须已存在且为目录。
        max_depth: 最大遍历层数，``None`` 表示不限。根目录自身算第 0 层，
            因此 ``max_depth=1`` 表示"根目录及其直接子目录中的文件"。

    返回:
        ``ScanResult``。子目录出错（权限不足等）不会抛异常，
        而是记录在 ``issues`` 字段中。

    异常:
        FileNotFoundError: *root* 不存在。
        NotADirectoryError: *root* 存在但不是目录。
    """
    root = Path(root)
    if not root.exists():
        raise FileNotFoundError(f"扫描根不存在: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"扫描根不是目录: {root}")

    result = ScanResult(root=root)
    _walk(root, ROOT_LABEL, result, max_depth, 0)
    return result


def _walk(
    directory: Path,
    top_label: str,
    result: ScanResult,
    max_depth: int | None,
    depth: int,
) -> None:
    """递归遍历 *directory*，把统计结果累加进 *result*。

    参数:
        directory: 当前目录。
        top_label: 当前目录在体积排行中的归类名。
        result: 累加目标。
        max_depth: 最大遍历层数，``None`` 表示不限。
        depth: 当前层号，根目录为 0。
    """
    try:
        entries = list(os.scandir(directory))
    except PermissionError:
        result.issues.append(ScanIssue(directory, "Permission denied"))
        return
    except OSError as exc:
        reason = exc.strerror or str(exc)
        result.issues.append(ScanIssue(directory, reason))
        return

    for entry in entries:
        try:
            _visit(entry, top_label, result, max_depth, depth)
        except OSError as exc:
            reason = exc.strerror or str(exc)
            result.issues.append(ScanIssue(Path(entry.path), reason))


def _visit(
    entry: os.DirEntry[str],
    top_label: str,
    result: ScanResult,
    max_depth: int | None,
    depth: int,
) -> None:
    """处理单个目录项：符号链接跳过，目录递归，普通文件计体积。"""
    if entry.is_symlink():
        result.symlinks_skipped += 1
        return

    if entry.is_dir(follow_symlinks=False):
        result.dir_count += 1
        if max_depth is None or depth < max_depth:
            child_label = entry.name if depth == 0 else top_label
            _walk(Path(entry.path), child_label, result, max_depth, depth + 1)
        return

    if entry.is_file(follow_symlinks=False):
        size = entry.stat(follow_symlinks=False).st_size
        result.file_count += 1
        result.total_size += size
        result.size_by_subdir[top_label] = (
            result.size_by_subdir.get(top_label, 0) + size
        )
        suffix = Path(entry.name).suffix.lower() or NO_EXTENSION
        result.size_by_suffix[suffix] = result.size_by_suffix.get(suffix, 0) + size
