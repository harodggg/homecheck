"""命令行入口：解析参数、执行扫描与查重、渲染报告、按需执行改动。

本模块自身只向 stdout / stderr 写文本；**所有文件系统改动都委托给 ``apply``
模块**（CONVENTIONS §6）。报告落盘请由用户用 shell 重定向完成（ADR-0008）。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Callable, NoReturn

from .apply import (
    DEFAULT_MAX_ACTIONS,
    Action,
    apply_plan,
    build_plan,
    delete_plan,
)
from .duplicates import DuplicateReport, find_duplicates
from .report import human_size, render_json, render_markdown, render_report
from .scan import scan_directory
from .suggest import DEFAULT_LARGE_BYTES, DEFAULT_STALE_DAYS, build_suggestions

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_BAD_ROOT = 2
#: 执行过程中有动作被拒绝（复核未通过或操作失败）
EXIT_PARTIAL = 3
#: 隔离目录 / 审计清单路径不可用
EXIT_PRECONDITION = 4

#: 允许不可逆删除的确认令牌。必须逐字匹配，防止从 shell 历史里误触。
DELETE_TOKEN = "DELETE"


class _Parser(argparse.ArgumentParser):
    """参数错误时以退出码 1 结束（argparse 默认是 2，与规格不符）。"""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: 参数错误: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = _Parser(
        prog="homecheck",
        description="只读扫描目录并输出体积报告。默认不产生任何写入。",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="扫描根目录（默认：当前目录）",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=20,
        metavar="N",
        help="体积排行显示前 N 项（默认：20）",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=None,
        metavar="N",
        help="限制遍历深度，根目录为第 0 层（默认：不限）",
    )

    formats = parser.add_mutually_exclusive_group()
    formats.add_argument(
        "--json",
        action="store_true",
        help="输出 JSON 到 stdout（stdout 只有 JSON，可被脚本消费）",
    )
    formats.add_argument(
        "--markdown",
        action="store_true",
        help="输出 Markdown 到 stdout",
    )

    parser.add_argument(
        "--skip-duplicates",
        action="store_true",
        help="跳过重复文件检测（大目录下更快，代价是不产出清理建议）",
    )
    parser.add_argument(
        "--min-duplicate-bytes",
        type=int,
        default=1,
        metavar="N",
        help="参与查重的最小体积，单位字节（默认：1，即忽略空文件）",
    )
    parser.add_argument(
        "--plan",
        action="store_true",
        help="生成执行计划（dry-run）：列出拟执行的动作与审计清单，但不执行任何改动",
    )
    parser.add_argument(
        "--max-actions",
        type=int,
        default=DEFAULT_MAX_ACTIONS,
        metavar="N",
        help=f"执行计划最多列出多少个动作（默认：{DEFAULT_MAX_ACTIONS}）",
    )

    parser.add_argument(
        "--apply",
        action="store_true",
        help="执行计划。去向二选一：--quarantine DIR（可恢复）或 --delete（不可逆）",
    )
    parser.add_argument(
        "--quarantine",
        metavar="DIR",
        default=None,
        help="隔离目录，必须不存在或为空；被移动的文件保持原相对路径，可随时移回",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="【不可逆】直接删除冗余副本。还需 --confirm-delete DELETE 与 --manifest FILE",
    )
    parser.add_argument(
        "--confirm-delete",
        metavar="TOKEN",
        default=None,
        help=f"防误触令牌，必须字面等于 {DELETE_TOKEN} 才允许删除",
    )
    parser.add_argument(
        "--manifest",
        metavar="FILE",
        default=None,
        help="删除模式下审计清单的写入路径（必须不存在，拒绝覆盖）",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="跳过逐条确认。非交互环境（无 TTY）下执行改动时必须显式给出",
    )

    parser.add_argument(
        "--large-bytes",
        type=int,
        default=DEFAULT_LARGE_BYTES,
        metavar="N",
        help=f"大文件阈值，单位字节（默认：{DEFAULT_LARGE_BYTES}，即 100 MB）",
    )
    parser.add_argument(
        "--stale-days",
        type=int,
        default=DEFAULT_STALE_DAYS,
        metavar="N",
        help=f"陈旧阈值，单位天（默认：{DEFAULT_STALE_DAYS}）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """程序入口。

    参数:
        argv: 参数列表；``None`` 表示取 ``sys.argv[1:]``。

    返回:
        退出码：0 正常、1 参数错误、2 扫描根不存在或不可读、
        3 执行中有动作被拒、4 隔离目录或审计清单路径不可用。
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.top < 0:
        parser.error("--top 不能为负数")
    if args.max_depth is not None and args.max_depth < 0:
        parser.error("--max-depth 不能为负数")
    if args.large_bytes < 0:
        parser.error("--large-bytes 不能为负数")
    if args.stale_days < 0:
        parser.error("--stale-days 不能为负数")
    if args.min_duplicate_bytes < 0:
        parser.error("--min-duplicate-bytes 不能为负数")
    if args.max_actions < 0:
        parser.error("--max-actions 不能为负数")

    _validate_execution_flags(parser, args)
    if args.apply and not args.yes and not sys.stdin.isatty():
        print(
            "homecheck: 非交互环境下执行改动，必须显式给出 --yes",
            file=sys.stderr,
        )
        return EXIT_USAGE

    root = Path(args.path)
    if not root.exists():
        print(f"homecheck: 扫描根不存在: {root}", file=sys.stderr)
        return EXIT_BAD_ROOT
    if not root.is_dir():
        print(f"homecheck: 扫描根不是目录: {root}", file=sys.stderr)
        return EXIT_BAD_ROOT
    if not os.access(root, os.R_OK | os.X_OK):
        print(f"homecheck: 扫描根不可读: {root}", file=sys.stderr)
        return EXIT_BAD_ROOT

    started = time.perf_counter()
    result = scan_directory(root, max_depth=args.max_depth)

    duplicates: DuplicateReport | None = None
    if not args.skip_duplicates:
        duplicates = find_duplicates(
            result.files, min_size=args.min_duplicate_bytes
        )
    elapsed = time.perf_counter() - started

    suggestions = build_suggestions(
        result,
        duplicates,
        large_bytes=args.large_bytes,
        stale_days=args.stale_days,
    )

    plan = None
    if args.plan or args.apply:
        plan = build_plan(result, duplicates, max_actions=args.max_actions)

    execution = None
    if args.apply and plan is not None:
        confirmer = None if args.yes else _interactive_confirmer(delete=args.delete)
        try:
            if args.delete:
                execution = delete_plan(
                    plan,
                    manifest_path=Path(str(args.manifest)),
                    confirm=confirmer,
                )
            else:
                execution = apply_plan(
                    plan,
                    quarantine=Path(str(args.quarantine)),
                    confirm=confirmer,
                )
        except FileExistsError as exc:
            print(f"homecheck: {exc}", file=sys.stderr)
            return EXIT_PRECONDITION

    if args.json:
        text = render_json(
            result, duplicates, suggestions, plan, execution, elapsed=elapsed
        )
    elif args.markdown:
        text = render_markdown(
            result,
            duplicates,
            suggestions,
            plan,
            execution,
            top=args.top,
            elapsed=elapsed,
        )
    else:
        text = render_report(
            result,
            duplicates,
            suggestions,
            plan,
            execution,
            top=args.top,
            elapsed=elapsed,
        )

    print(text, end="")

    if execution is not None and execution.refused_count:
        return EXIT_PARTIAL
    return EXIT_OK


def _validate_execution_flags(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """校验执行相关的开关组合。任何非法组合都以退出码 1 拒绝。"""
    if args.apply and not (args.quarantine or args.delete):
        parser.error(
            "--apply 需要指定去向：--quarantine DIR（可恢复）或 --delete（不可逆）"
        )
    if args.quarantine and not args.apply:
        parser.error("--quarantine 只能与 --apply 一起使用")
    if args.quarantine and args.delete:
        parser.error("--quarantine 与 --delete 互斥：要么移动，要么删除")
    if args.delete and not args.apply:
        parser.error("--delete 需要与 --apply 一起使用")
    if args.delete and args.confirm_delete != DELETE_TOKEN:
        parser.error(
            f"不可逆删除需要显式确认令牌：--delete --confirm-delete {DELETE_TOKEN}"
        )
    if args.delete and not args.manifest:
        parser.error("--delete 需要 --manifest FILE 来记录审计清单")
    if args.manifest and not args.delete:
        parser.error("--manifest 只能与 --delete 一起使用")


def _interactive_confirmer(*, delete: bool = False) -> Callable[[Action, int, int], bool]:
    """构造逐条确认回调，用于交互式执行。

    提示写到 **stderr**，避免污染 ``--json`` 模式下的 stdout。
    默认答案是"否"（直接回车即跳过），且大小写不敏感。
    删除模式的提示会额外声明**不可恢复**。
    """

    def confirm(action: Action, index: int, total: int) -> bool:
        if delete:
            action_text = f"不可逆删除 {action.path}"
        else:
            action_text = f"移入隔离目录：{action.path}"
        prompt = (
            f"[{index}/{total}] {action_text}"
            f"（{human_size(action.size)}，保留 {action.keep}）？[y/N] "
        )
        print(prompt, end="", file=sys.stderr, flush=True)
        answer = sys.stdin.readline().strip().lower()
        return answer in {"y", "yes"}

    return confirm
