"""执行计划生成与执行（S5 / S7）。

本模块是**整个项目里唯一允许改动文件系统的模块**（CONVENTIONS §6）。

两个能力：
- ``build_plan``：只读地生成可复核的执行计划（dry-run），零副作用。
- ``apply_plan``：按计划把冗余副本**移入隔离目录**（可恢复）。执行前先把
  审计清单落盘，执行前对每个文件重新校验大小与 sha256。

**关于删除**：``apply_plan`` 只做"移动"，不做不可逆删除。``ADR-0012`` 部分
覆盖了 Q5（「工具自身不执行删除」）：现在可以执行，但必须**可恢复**。
不可逆删除仍未实现，仍然只能由用户自行运行打印出来的命令。

移动前会重新校验，任一不满足就跳过该动作：
1. 待移动文件仍存在、是普通文件、大小与 sha256 与计划一致
2. **要保留的那一份仍存在且 sha256 一致** —— 否则移走就等于丢数据
3. 路径仍在扫描根之下，且不位于 ``.git`` 等元数据目录
"""

from __future__ import annotations

import json
import shlex
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .duplicates import DuplicateReport, hash_file
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


# --------------------------------------------------------------------------
# S7：执行计划（把冗余副本移入隔离目录）
# --------------------------------------------------------------------------

#: 执行结果状态：已移动
MOVED = "moved"
#: 执行结果状态：用户拒绝，未改动
SKIPPED = "skipped"
#: 执行结果状态：复核未通过或移动失败
REFUSED = "refused"

#: 审计清单的文件名（写在隔离目录内）
MANIFEST_NAME = "manifest.json"

#: 审计清单的 schema 版本
MANIFEST_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ExecutionOutcome:
    """单个动作的执行结果。"""

    action: Action
    status: str
    detail: str


@dataclass
class ExecutionResult:
    """一次执行的完整结果。"""

    quarantine: Path
    manifest: Path
    outcomes: list[ExecutionOutcome] = field(default_factory=list)

    def _count(self, status: str) -> int:
        return sum(1 for outcome in self.outcomes if outcome.status == status)

    @property
    def moved_count(self) -> int:
        """成功移入隔离目录的动作数。"""
        return self._count(MOVED)

    @property
    def skipped_count(self) -> int:
        """被用户拒绝、因而未改动的动作数。"""
        return self._count(SKIPPED)

    @property
    def refused_count(self) -> int:
        """复核未通过或移动失败的动作数。"""
        return self._count(REFUSED)

    @property
    def moved_bytes(self) -> int:
        """已移动文件合计的字节数。"""
        return sum(
            outcome.action.size
            for outcome in self.outcomes
            if outcome.status == MOVED
        )


def apply_plan(
    plan: ActionPlan,
    *,
    quarantine: Path,
    confirm: Callable[[Action, int, int], bool] | None = None,
    timestamp: datetime | None = None,
) -> ExecutionResult:
    """按 *plan* 把冗余副本移入 *quarantine*。

    这是本项目**唯一会改动文件系统**的函数，因此做了三重保护：
    隔离目录必须干净、审计清单先落盘、每个动作执行前重新校验。

    参数:
        plan: 要执行的计划，通常来自 :func:`build_plan`。
        quarantine: 隔离目录。必须是**不存在或为空**的目录；被移动的文件会
            保持相对扫描根的目录结构存放其中，因此可以原样移回。
        confirm: 逐条确认回调 ``(action, index, total) -> bool``。
            ``None`` 表示不询问，直接执行 —— 调用方必须已获得显式授权。
        timestamp: 审计清单的时间戳；默认取当前时间。

    返回:
        ``ExecutionResult``，其中逐条记录了 moved / skipped / refused。

    异常:
        FileExistsError: *quarantine* 已被占用或非空 —— 拒绝覆盖任何东西。
    """
    quarantine = Path(quarantine)
    if quarantine.exists():
        if not quarantine.is_dir():
            raise FileExistsError(f"隔离目录路径已被占用: {quarantine}")
        if any(quarantine.iterdir()):
            raise FileExistsError(f"隔离目录非空，拒绝使用: {quarantine}")
    else:
        quarantine.mkdir(parents=True)

    manifest = quarantine / MANIFEST_NAME
    _write_manifest(manifest, plan, quarantine, timestamp)

    result = ExecutionResult(quarantine=quarantine, manifest=manifest)
    total = len(plan.actions)

    for index, action in enumerate(plan.actions, start=1):
        reason = execution_refusal(action, plan.root)
        if reason is not None:
            result.outcomes.append(ExecutionOutcome(action, REFUSED, reason))
            continue
        if confirm is not None and not confirm(action, index, total):
            result.outcomes.append(ExecutionOutcome(action, SKIPPED, "用户拒绝"))
            continue
        try:
            destination = _move_into_quarantine(action.path, quarantine, plan.root)
        except OSError as exc:
            detail = exc.strerror or str(exc)
            result.outcomes.append(
                ExecutionOutcome(action, REFUSED, f"移动失败: {detail}")
            )
            continue
        result.outcomes.append(ExecutionOutcome(action, MOVED, f"已移至 {destination}"))

    return result


def execution_refusal(action: Action, root: Path) -> str | None:
    """执行前复核单个动作；返回拒绝原因，或 ``None`` 表示可以执行。

    这一步是防止"计划过期"的关键：计划生成到执行之间，文件可能被改动、
    被删除，或者**要保留的那一份**消失了。最后一种情况下移走副本就是丢数据。
    """
    path = action.path
    if not _is_within(path, root):
        return "路径不在扫描根之下"
    if FORBIDDEN_COMPONENTS.intersection(path.parts):
        return "位于版本库或回收站元数据目录内"
    if path.is_symlink():
        return "路径已被替换为符号链接"
    if not path.is_file():
        return "文件已不存在或不是普通文件"

    try:
        size = path.stat().st_size
    except OSError as exc:
        return f"无法读取属性: {exc.strerror or exc}"
    if size != action.size:
        return f"大小已变化（计划 {action.size}，实际 {size}）"

    try:
        digest = hash_file(path)
    except OSError as exc:
        return f"无法读取内容: {exc.strerror or exc}"
    if digest != action.digest:
        return "内容已变化（sha256 不匹配）"

    keep = action.keep
    if keep.is_symlink() or not keep.is_file():
        return f"保留项不可用: {keep}"
    try:
        keep_digest = hash_file(keep)
    except OSError as exc:
        return f"无法读取保留项: {exc.strerror or exc}"
    if keep_digest != action.digest:
        return f"保留项内容已变化: {keep}"
    return None


def _move_into_quarantine(path: Path, quarantine: Path, root: Path) -> Path:
    """把 *path* 移动到隔离目录，保持相对 *root* 的目录结构。"""
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = Path(path.name)
    destination = quarantine / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"隔离目录中已存在同名文件: {destination}")
    shutil.move(str(path), str(destination))
    return destination


def _write_manifest(
    manifest: Path,
    plan: ActionPlan,
    quarantine: Path,
    timestamp: datetime | None,
) -> None:
    """在隔离目录内写下审计清单。必须在任何移动之前调用。"""
    moment = timestamp or datetime.now()
    payload = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "tool": "homecheck",
        "created_at": moment.strftime("%Y-%m-%dT%H:%M:%S"),
        "root": str(plan.root),
        "quarantine": str(quarantine),
        "action_count": len(plan.actions),
        "total_bytes": plan.total_bytes,
        "truncated": plan.truncated,
        "actions": [
            {
                "kind": action.kind,
                "path": str(action.path),
                "size": action.size,
                "digest": action.digest,
                "keep": str(action.keep),
                "reason": action.reason,
            }
            for action in plan.actions
        ],
        "refusals": [
            {"path": str(refusal.path), "reason": refusal.reason}
            for refusal in plan.refusals
        ],
    }
    manifest.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
