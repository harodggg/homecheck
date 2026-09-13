"""重复文件检测（S2）。

安全约束：本模块**只读**。它只读取文件内容计算摘要，不创建、修改或删除
任何文件系统对象。

算法见 ``docs/SPEC-S2-S4.md``：先按大小分桶（避免对唯一大小的文件读盘），
再对桶内文件计算 sha256，最后按 ``(size, digest)`` 归组。
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from .scan import FileRecord, ScanIssue

#: 计算摘要时的分块大小（1 MiB）
CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class DuplicateGroup:
    """一组内容完全相同的文件。

    ``paths`` 包含组内所有路径（按字符串去重）。
    ``inode_count`` 是不同 ``(device, inode)`` 的数量 —— 硬链接只算一份，
    因此可回收空间以它为准，避免虚报。
    """

    size: int
    digest: str
    paths: tuple[Path, ...]
    inode_count: int

    @property
    def wasted_bytes(self) -> int:
        """删掉冗余副本后能回收的字节数（不含保留的那一份）。"""
        return self.size * max(0, self.inode_count - 1)


@dataclass
class DuplicateReport:
    """一次查重的完整结果。"""

    groups: list[DuplicateGroup] = field(default_factory=list)
    issues: list[ScanIssue] = field(default_factory=list)

    @property
    def wasted_bytes(self) -> int:
        """所有重复组可回收的字节数合计。"""
        return sum(group.wasted_bytes for group in self.groups)

    @property
    def path_count(self) -> int:
        """涉及的文件路径总数（含每个组保留的那一份）。"""
        return sum(len(group.paths) for group in self.groups)


def find_duplicates(
    files: Sequence[FileRecord],
    *,
    min_size: int = 1,
) -> DuplicateReport:
    """在 *files* 中查找内容重复的文件。

    参数:
        files: 扫描得到的文件明细。
        min_size: 参与查重的最小体积。默认 1，即**空文件不参与**（Q4）。

    返回:
        ``DuplicateReport``。单个文件读取失败不会中断整体检测，
        而是记录在 ``issues`` 中。
    """
    report = DuplicateReport()

    by_size: dict[int, list[FileRecord]] = defaultdict(list)
    for record in files:
        if record.size >= max(1, min_size):
            by_size[record.size].append(record)

    for size in sorted(by_size, reverse=True):
        bucket = by_size[size]
        if len(bucket) < 2:
            continue  # 同大小只有一个，不可能是重复
        report.groups.extend(_group_bucket(bucket, size, report))

    report.groups.sort(key=lambda group: (-group.wasted_bytes, group.digest))
    return report


def _group_bucket(
    bucket: Sequence[FileRecord],
    size: int,
    report: DuplicateReport,
) -> list[DuplicateGroup]:
    """对同一大小的文件计算摘要并归组，把不可读的文件记入 ``report.issues``。"""
    by_digest: dict[str, list[FileRecord]] = defaultdict(list)
    for record in bucket:
        try:
            digest = hash_file(record.path)
        except OSError as exc:
            reason = exc.strerror or str(exc)
            report.issues.append(ScanIssue(record.path, reason))
            continue
        by_digest[digest].append(record)

    groups: list[DuplicateGroup] = []
    for digest, records in by_digest.items():
        if len(records) < 2:
            continue
        inodes = {(record.device, record.inode) for record in records}
        if len(inodes) < 2:
            continue  # 全是硬链接，实际只占一份空间
        paths = tuple(dict.fromkeys(record.path for record in records))
        groups.append(
            DuplicateGroup(
                size=size,
                digest=digest,
                paths=paths,
                inode_count=len(inodes),
            )
        )
    return groups


def hash_file(path: Path, *, chunk_size: int = CHUNK_SIZE) -> str:
    """流式计算文件的 sha256 十六进制摘要。

    参数:
        path: 目标文件。
        chunk_size: 每次读取的字节数。

    返回:
        十六进制摘要字符串。

    异常:
        OSError: 文件不存在、无权限或读取过程中出错。
    """
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def iter_redundant_paths(group: DuplicateGroup) -> Iterable[Path]:
    """产出组内可以删除的冗余副本 —— 保留第一份，其余视为冗余。"""
    return group.paths[1:]
