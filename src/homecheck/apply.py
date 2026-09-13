"""执行计划生成（S5）。

**安全约束：本模块不执行任何改动。** 它只读地分析扫描结果，产出可复核的
「执行计划」—— 一份动作清单与审计记录，由用户自行复核后执行。

之所以只产出计划而不执行，是因为 Q5 的决策：「v1 只输出命令，工具自身不执行删除」。
这与 ``PRODUCT.md`` 的 G7（三重授权的执行流程）存在冲突；本实现遵循更具体、
更新的 Q5，并把 G7 的执行部分留待明确授权后再做。

计划里每一项都带有规划时的 sha256 与体积，可作为变更前的审计依据。
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path

from .duplicates import DuplicateReport
from .scan import ScanResult

#: 计划里默认最多列出多少个动作
DEFAULT_MAX_ACTIONS = 100

#: 路径中出现这些目录分量时一律拒绝纳入计划
FORBIDDEN_COMPONENTS = frozenset({".git", ".hg", ".svn", ".Trash"})

#: 动作类型：删除重复副本
DELETE_DUPLICATE = "delete-duplicate"


@dataclass(frozen=True)
class Action:
    """计划中的单个动作。``kind`` 目前只有 ``delete-duplicate``。"""

    kind: str
    path: Path
    size: int
    digest: str
    keep: Path
    reason: str


@dataclass(frozen=True)
class Refusal:
    """被安全规则拒绝纳入计划的候选动作。"""

    path: Path
    reason: str


@dataclass
class ActionPlan:
    """一份执行计划。**它本身不产生任何副作用。**"""

    root: Path
    actions: list[Action] = field(default_factory=list)
    refusals: list[Refusal] = field(default_factory=list)
    #: 因超过 ``max_actions`` 而未列出的动作数量
    truncated: int = 0

    @property
    def total_bytes(self) -> int:
        """计划中所有动作合计可回收的字节数。"""
        return sum(action.size for action in self.actions)

    @property
    def command(self) -> str:
        """把全部动作拼成一条可复核的命令；没有动作时返回空串。

        安全约束：路径一律 ``shlex.quote`` 转义；**绝不使用 ``-r`` / ``-f``**。
        """
        if not self.actions:
            return ""
        return "rm -- " + " ".join(
            shlex.quote(str(action.path)) for action in self.actions
        )


def build_plan(
    result: ScanResult,
    duplicates: DuplicateReport | None = None,
    *,
    max_actions: int = DEFAULT_MAX_ACTIONS,
) -> ActionPlan:
    """根据查重结果生成执行计划。

    参数:
        result: 扫描结果，用于查 inode 与确定扫描根边界。
        duplicates: 查重结果；``None`` 时返回空计划。
        max_actions: 计划最多列出多少个动作；负数表示不限。

    返回:
        ``ActionPlan``。被安全规则拦下的候选记录在 ``refusals`` 中，
        而不是静默丢弃。
    """
    plan = ActionPlan(root=result.root)
    if duplicates is None:
        return plan

    root = result.root
    records = {record.path: record for record in result.files}
    candidates: list[Action] = []

    for group in duplicates.groups:
        if len(group.paths) < 2:
            continue
        kept = group.paths[0]
        kept_record = records.get(kept)
        for path in group.paths[1:]:
            reason = _refusal_reason(path, root, kept, records.get(path), kept_record)
            if reason is not None:
                plan.refusals.append(Refusal(path, reason))
                continue
            candidates.append(
                Action(
                    kind=DELETE_DUPLICATE,
                    path=path,
                    size=group.size,
                    digest=group.digest,
                    keep=kept,
                    reason=f"与 {kept} 内容相同，保留后者",
                )
            )

    candidates.sort(key=lambda action: (-action.size, str(action.path)))
    if 0 <= max_actions < len(candidates):
        plan.truncated = len(candidates) - max_actions
        candidates = candidates[:max_actions]

    plan.actions = candidates
    return plan


def _refusal_reason(
    path: Path,
    root: Path,
    kept: Path,
    record: object,
    kept_record: object,
) -> str | None:
    """判断某个候选路径是否应被安全规则拒绝；返回原因或 ``None``。

    这些规则是"宁可少删"的：任何一条命中，该路径都不会进入计划。
    """
    if path == kept:
        return "与保留项路径相同（防御性拒绝）"
    if path == root:
        return "等于扫描根，拒绝纳入计划"
    if not _is_within(path, root):
        return "不在扫描根之下，拒绝纳入计划"
    if FORBIDDEN_COMPONENTS.intersection(path.parts):
        return "位于版本库或回收站元数据目录内，删除会破坏数据"

    device = getattr(record, "device", None)
    inode = getattr(record, "inode", None)
    kept_device = getattr(kept_record, "device", None)
    kept_inode = getattr(kept_record, "inode", None)
    if None not in (device, inode, kept_device, kept_inode):
        if (device, inode) == (kept_device, kept_inode):
            return "与保留项是同一个文件实体（硬链接），删除不会释放空间"
    return None


def _is_within(path: Path, root: Path) -> bool:
    """判断 *path* 是否位于 *root* 之下（纯字符串比较，不触碰文件系统）。"""
    root_parts = root.parts
    path_parts = path.parts
    return len(path_parts) > len(root_parts) and path_parts[: len(root_parts)] == root_parts
