"""把扫描结果渲染成人类可读的终端报告。

安全约束：本模块只做字符串拼接，**不触碰文件系统**。
输出不含 ANSI 颜色码，可直接重定向到文件或通过管道消费。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .scan import ROOT_LABEL, ScanResult

#: 体积单位，按 1024 进制递增
UNITS: tuple[str, ...] = ("B", "KB", "MB", "GB", "TB", "PB")


def human_size(num_bytes: int) -> str:
    """把字节数渲染成人类可读的字符串，例如 ``6.21 GB``。

    使用 1024 进制；保留两位小数并去掉多余的尾随零。

    参数:
        num_bytes: 字节数，需为非负整数。

    返回:
        形如 ``"0 B"`` / ``"1.5 KB"`` / ``"6.21 GB"`` 的字符串。
    """
    size = float(num_bytes)
    for unit in UNITS:
        if size < 1024 or unit == UNITS[-1]:
            if unit == "B":
                return f"{int(size)} B"
            text = f"{size:.2f}".rstrip("0").rstrip(".")
            return f"{text} {unit}"
        size /= 1024
    raise AssertionError("UNITS 非空时不可达")


def render_report(
    result: ScanResult,
    *,
    top: int = 20,
    timestamp: datetime | None = None,
    elapsed: float | None = None,
) -> str:
    """把扫描结果渲染成终端报告文本。

    参数:
        result: 扫描结果。
        top: 体积排行与类型分布各显示前多少项。
        timestamp: 报告时间；默认取当前时间。测试中可固定以便断言。
        elapsed: 扫描耗时（秒）；``None`` 表示不显示。

    返回:
        完整报告文本，末尾带一个换行。
    """
    lines: list[str] = ["homecheck — 扫描报告", f"扫描根   {result.root}"]

    moment = timestamp or datetime.now()
    header = f"完成于   {moment.strftime('%Y-%m-%d %H:%M:%S')}"
    if elapsed is not None:
        header += f"    耗时 {elapsed:.2f}s"
    lines.append(header)
    lines.append("")

    lines.append("总体")
    lines.append(f"  文件   {result.file_count:,} 个")
    lines.append(f"  目录   {result.dir_count:,} 个")
    lines.append(f"  占用   {human_size(result.total_size)}")
    lines.append("")

    subdirs = _rank(result.size_by_subdir, top)
    lines.append(f"体积排行（前 {len(subdirs)} 项）")
    if subdirs:
        for index, (name, size) in enumerate(subdirs, start=1):
            label = name if name == ROOT_LABEL else f"{name}/"
            lines.append(
                f"  {index}.  {human_size(size):<9} "
                f"{_percent(size, result.total_size):>6}  {label}"
            )
    else:
        lines.append("  （空）")
    lines.append("")

    suffixes = _rank(result.size_by_suffix, top)
    lines.append(f"文件类型分布（前 {len(suffixes)}）")
    if suffixes:
        for name, size in suffixes:
            lines.append(
                f"  {human_size(size):<9} "
                f"{_percent(size, result.total_size):>6}  {name}"
            )
    else:
        lines.append("  （空）")
    lines.append("")

    if result.symlinks_skipped:
        lines.append(f"跳过符号链接（不跟随）：{result.symlinks_skipped:,} 个")
        lines.append("")

    if result.issues:
        lines.append(f"未扫描目录（{len(result.issues):,} 个）")
        for issue in result.issues:
            shown = _display_path(issue.path, result.root)
            lines.append(f"  ⚠ {shown} — {issue.reason}")
        lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n"


def _rank(mapping: dict[str, int], top: int) -> list[tuple[str, int]]:
    """按体积降序取前 *top* 项。

    体积相同时按名称升序，保证输出稳定、可断言。
    长度不足 ``top`` 时返回全部。
    """
    ordered = sorted(mapping.items(), key=lambda item: (-item[1], item[0]))
    if top < 0:
        return ordered
    return ordered[:top]


def _percent(part: int, whole: int) -> str:
    """把占比渲染成一位小数的百分比；``whole`` 为 0 时返回 ``0.0%``。"""
    if whole <= 0:
        return "0.0%"
    return f"{part / whole * 100:.1f}%"


def _display_path(path: Path, root: Path) -> str:
    """把路径渲染成相对扫描根的形式；不在根之下时原样返回。"""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return str(path)
    text = str(relative)
    return str(root) if text == "." else text
