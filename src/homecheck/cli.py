"""命令行入口：解析参数、执行只读扫描、打印报告。

本模块只读：除向 stdout / stderr 写文本外，不产生任何文件系统改动。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import NoReturn

from .report import render_report
from .scan import scan_directory

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_BAD_ROOT = 2


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
    return parser


def main(argv: list[str] | None = None) -> int:
    """程序入口。

    参数:
        argv: 参数列表；``None`` 表示取 ``sys.argv[1:]``。

    返回:
        退出码：0 正常、1 参数错误、2 扫描根不存在或不可读。
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.top < 0:
        parser.error("--top 不能为负数")
    if args.max_depth is not None and args.max_depth < 0:
        parser.error("--max-depth 不能为负数")

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
    elapsed = time.perf_counter() - started

    print(render_report(result, top=args.top, elapsed=elapsed), end="")
    return EXIT_OK
