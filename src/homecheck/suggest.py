"""建议引擎（S4）。

安全约束：本模块**零副作用** —— 只消费扫描/查重结果并产出 ``Suggestion``
数据结构，不读盘、不写盘、不执行任何命令。

按 Q5 的决策，清理类建议只附**命令文本**，由用户自行复核后执行；
本工具在任何情况下都不会自己执行删除。
"""

from __future__ import annotations

import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .duplicates import DuplicateReport, iter_redundant_paths
from .scan import ScanResult

#: 大文件阈值默认值（Q3：100 MB）
DEFAULT_LARGE_BYTES = 100 * 1024 * 1024

#: 陈旧阈值默认值（Q2：180 天）
DEFAULT_STALE_DAYS = 180

#: 单个顶层目录占比超过此值即提示（R5）
DEFAULT_DOMINANT_SHARE = 0.30

#: 建议里最多为多少组重复文件附上删除命令
DEFAULT_MAX_DUPLICATE_COMMANDS = 5

#: 人类报告里最多展示多少个陈旧大文件
MAX_LISTED_FILES = 10

INFO = "info"
WARN = "warn"


@dataclass(frozen=True)
class Suggestion:
    """一条建议。``command`` 是给用户复核后自行执行的命令文本，本工具不执行。"""

    kind: str
    severity: str
    title: str
    detail: str
    command: str | None = None


def build_suggestions(
    result: ScanResult,
    duplicates: DuplicateReport | None = None,
    *,
    large_bytes: int = DEFAULT_LARGE_BYTES,
    stale_days: int = DEFAULT_STALE_DAYS,
    dominant_share: float = DEFAULT_DOMINANT_SHARE,
    max_duplicate_commands: int = DEFAULT_MAX_DUPLICATE_COMMANDS,
    now: float | None = None,
) -> list[Suggestion]:
    """根据扫描与查重结果产出建议列表。

    参数:
        result: 扫描结果。
        duplicates: 查重结果；``None`` 表示本次未查重，跳过相关规则。
        large_bytes: 大文件阈值（字节）。
        stale_days: 陈旧阈值（天）。
        dominant_share: 单个顶层目录的占比提示阈值（0~1）。
        max_duplicate_commands: 最多为多少组重复文件附上删除命令。真实目录里
            重复组可能上千，逐组输出会把报告刷屏，因此只对最大的若干组给命令，
            其余合并成一条摘要。
        now: 当前时间戳，用于计算"陈旧"；默认取系统时间，测试可固定。

    返回:
        建议列表，按严重度（warn 在前）与规则名排序，结果稳定。
    """
    moment = time.time() if now is None else now
    suggestions: list[Suggestion] = []

    if duplicates is not None:
        suggestions.extend(_duplicate_suggestions(duplicates, max_duplicate_commands))
    suggestions.extend(_stale_large_suggestions(result, large_bytes, stale_days, moment))
    suggestions.extend(_large_file_suggestions(result, large_bytes))
    suggestions.extend(_incomplete_scan_suggestions(result))
    suggestions.extend(_dominant_directory_suggestions(result, dominant_share))

    suggestions.sort(key=lambda item: (0 if item.severity == WARN else 1, item.kind))
    return suggestions


def _duplicate_suggestions(
    duplicates: DuplicateReport,
    max_commands: int,
) -> list[Suggestion]:
    """R1：只对最大的若干组重复文件附命令，其余合并为一条摘要。

    真实目录（例如含多个 ``node_modules`` 的仓库）动辄上千组重复，
    逐组输出会让报告淹没在噪音里，因此这里对输出量做了硬性上限。
    """
    groups = duplicates.groups
    if not groups:
        return []

    shown = groups[:max_commands] if max_commands >= 0 else groups
    suggestions: list[Suggestion] = []
    for index, group in enumerate(shown, start=1):
        redundant = list(iter_redundant_paths(group))
        if not redundant:
            continue
        kept = group.paths[0]
        detail = (
            f"第 {index} 组共 {len(group.paths)} 个副本，每个 "
            f"{_human(group.size)}；保留 {kept}，可回收 {_human(group.wasted_bytes)}。"
        )
        suggestions.append(
            Suggestion(
                kind="duplicate-files",
                severity=WARN,
                title=f"重复文件：{_human(group.wasted_bytes)} 可回收",
                detail=detail,
                command=_remove_command(redundant),
            )
        )

    hidden = groups[len(shown) :]
    if hidden:
        hidden_waste = sum(group.wasted_bytes for group in hidden)
        suggestions.append(
            Suggestion(
                kind="duplicate-files",
                severity=WARN,
                title=f"另有 {len(hidden)} 组重复文件，可回收 {_human(hidden_waste)}",
                detail=(
                    "报告与建议只展示最大的若干组；完整清单见 --json，"
                    "或用 --min-duplicate-bytes 抬高查重门槛以减少噪音。"
                ),
            )
        )
    return suggestions


def _stale_large_suggestions(
    result: ScanResult,
    large_bytes: int,
    stale_days: int,
    now: float,
) -> list[Suggestion]:
    """R2：大文件且长期未修改 —— 归档候选。"""
    cutoff = now - stale_days * 86400
    stale = [
        record
        for record in result.files
        if record.size >= large_bytes and record.mtime < cutoff
    ]
    if not stale:
        return []

    stale.sort(key=lambda record: (-record.size, str(record.path)))
    total = sum(record.size for record in stale)
    listed = "、".join(str(record.path) for record in stale[:MAX_LISTED_FILES])
    if len(stale) > MAX_LISTED_FILES:
        listed += f" 等 {len(stale)} 个"
    return [
        Suggestion(
            kind="stale-large-files",
            severity=WARN,
            title=f"陈旧大文件：{len(stale)} 个，合计 {_human(total)}",
            detail=f"{stale_days} 天以上未修改且不小于 {_human(large_bytes)}：{listed}",
        )
    ]


def _large_file_suggestions(result: ScanResult, large_bytes: int) -> list[Suggestion]:
    """R3：仅提示存在大文件，不区分新旧。"""
    large = [record for record in result.files if record.size >= large_bytes]
    if not large:
        return []
    total = sum(record.size for record in large)
    return [
        Suggestion(
            kind="large-files",
            severity=INFO,
            title=f"大文件：{len(large)} 个，合计 {_human(total)}",
            detail=f"单个文件不小于 {_human(large_bytes)}，可通过 --large-bytes 调整阈值。",
        )
    ]


def _incomplete_scan_suggestions(result: ScanResult) -> list[Suggestion]:
    """R4：有目录没扫到，提醒报告可能不完整。"""
    if not result.issues:
        return []
    return [
        Suggestion(
            kind="incomplete-scan",
            severity=WARN,
            title=f"扫描不完整：{len(result.issues)} 个位置未能读取",
            detail="这些位置的体积未计入报告，实际占用可能更大。",
        )
    ]


def _dominant_directory_suggestions(
    result: ScanResult,
    dominant_share: float,
) -> list[Suggestion]:
    """R5：某个顶层子目录吃掉了大部分空间。"""
    if result.total_size <= 0:
        return []
    suggestions: list[Suggestion] = []
    for name, size in sorted(result.size_by_subdir.items()):
        share = size / result.total_size
        if share < dominant_share:
            continue
        suggestions.append(
            Suggestion(
                kind="dominant-directory",
                severity=INFO,
                title=f"{name} 占用 {share * 100:.1f}%",
                detail=f"{_human(size)}，是当前扫描根里最占地方的部分。",
            )
        )
    return suggestions


def _remove_command(paths: Sequence[Path]) -> str:
    """生成删除命令。

    安全约束（D10）：路径一律 ``shlex.quote`` 转义；**绝不使用 ``-r`` / ``-f``**，
    因此只会作用于普通文件，不会递归删除目录。
    """
    return "rm -- " + " ".join(shlex.quote(str(path)) for path in paths)


def _human(num_bytes: int) -> str:
    """体积的人类可读形式 —— 复用 report 的格式化，避免两份实现漂移。"""
    from .report import human_size

    return human_size(num_bytes)
