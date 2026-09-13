"""报告渲染：把扫描结果输出为终端文本、JSON 或 Markdown。

安全约束：本模块只做字符串拼接，**不触碰文件系统**（连报告文件也不写 ——
落盘请由用户用 shell 重定向完成，见 CONVENTIONS §6）。
终端输出不含 ANSI 颜色码，可直接重定向或管道消费。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Sequence

from .duplicates import DuplicateReport
from .scan import ROOT_LABEL, ScanResult
from .suggest import Suggestion

#: JSON 契约版本。字段名变更必须递增此值并记录进 DECISIONS.md
SCHEMA_VERSION = 1

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
    duplicates: DuplicateReport | None = None,
    suggestions: Sequence[Suggestion] | None = None,
    *,
    top: int = 20,
    timestamp: datetime | None = None,
    elapsed: float | None = None,
) -> str:
    """把结果渲染成终端报告文本。

    参数:
        result: 扫描结果。
        duplicates: 查重结果；``None`` 表示本次未查重，跳过该小节。
        suggestions: 建议列表；``None`` 或空列表则跳过该小节。
        top: 体积排行、类型分布与重复组各显示前多少项。
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

    lines.extend(_render_ranking("体积排行", _rank(result.size_by_subdir, top), result.total_size))
    lines.extend(_render_ranking("文件类型分布", _rank(result.size_by_suffix, top), result.total_size, suffix=True))

    if duplicates is not None:
        lines.extend(_render_duplicates(duplicates, top))

    if suggestions:
        lines.extend(_render_suggestions(suggestions))

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


def render_json(
    result: ScanResult,
    duplicates: DuplicateReport | None = None,
    suggestions: Sequence[Suggestion] | None = None,
    *,
    timestamp: datetime | None = None,
    elapsed: float | None = None,
) -> str:
    """把结果渲染成 JSON 文本（stdout 专用，不含任何装饰性文字）。

    ``schema_version`` 与字段集合是稳定契约，见 ``docs/SPEC-S2-S4.md``。

    参数:
        result: 扫描结果。
        duplicates: 查重结果；``None`` 时输出的 ``duplicates`` 为 ``null``。
        suggestions: 建议列表。
        timestamp: 生成时间；默认取当前时间。
        elapsed: 扫描耗时（秒）。

    返回:
        JSON 文本，末尾带一个换行。
    """
    moment = timestamp or datetime.now()
    payload = {
        "schema_version": SCHEMA_VERSION,
        "tool": "homecheck",
        "generated_at": moment.strftime("%Y-%m-%dT%H:%M:%S"),
        "root": str(result.root),
        "elapsed_seconds": round(elapsed, 4) if elapsed is not None else None,
        "totals": {
            "files": result.file_count,
            "directories": result.dir_count,
            "bytes": result.total_size,
            "human": human_size(result.total_size),
        },
        "by_subdirectory": [
            {"name": name, "bytes": size, "percent": _percent_value(size, result.total_size)}
            for name, size in _rank(result.size_by_subdir, -1)
        ],
        "by_extension": [
            {"extension": name, "bytes": size, "percent": _percent_value(size, result.total_size)}
            for name, size in _rank(result.size_by_suffix, -1)
        ],
        "duplicates": _duplicates_payload(duplicates),
        "suggestions": [
            {
                "kind": item.kind,
                "severity": item.severity,
                "title": item.title,
                "detail": item.detail,
                "command": item.command,
            }
            for item in suggestions or []
        ],
        "issues": [
            {"path": str(issue.path), "reason": issue.reason} for issue in result.issues
        ],
        "skipped_symlinks": result.symlinks_skipped,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def render_markdown(
    result: ScanResult,
    duplicates: DuplicateReport | None = None,
    suggestions: Sequence[Suggestion] | None = None,
    *,
    top: int = 20,
    timestamp: datetime | None = None,
    elapsed: float | None = None,
) -> str:
    """把结果渲染成 Markdown 文档文本。

    参数与 ``render_report`` 相同。

    返回:
        Markdown 文本，末尾带一个换行。
    """
    moment = timestamp or datetime.now()
    lines: list[str] = ["# homecheck 扫描报告", ""]
    lines.append(f"- **扫描根**：`{result.root}`")
    lines.append(f"- **完成于**：{moment.strftime('%Y-%m-%d %H:%M:%S')}")
    if elapsed is not None:
        lines.append(f"- **耗时**：{elapsed:.2f}s")
    lines.append("")

    lines.append("## 总体")
    lines.append("")
    lines.extend(
        _md_table(
            ["指标", "数值"],
            [
                ["文件", f"{result.file_count:,} 个"],
                ["目录", f"{result.dir_count:,} 个"],
                ["占用", human_size(result.total_size)],
            ],
        )
    )
    lines.append("")

    rows = [
        [str(index), human_size(size), _percent(size, result.total_size), _label(name, suffix=False)]
        for index, (name, size) in enumerate(_rank(result.size_by_subdir, top), start=1)
    ]
    lines.append(f"## 体积排行（前 {len(rows)} 项）")
    lines.append("")
    lines.extend(_md_table(["#", "体积", "占比", "目录"], rows) if rows else ["_（空）_"])
    lines.append("")

    rows = [
        [human_size(size), _percent(size, result.total_size), name]
        for name, size in _rank(result.size_by_suffix, top)
    ]
    lines.append(f"## 文件类型分布（前 {len(rows)}）")
    lines.append("")
    lines.extend(_md_table(["体积", "占比", "类型"], rows) if rows else ["_（空）_"])
    lines.append("")

    if duplicates is not None:
        lines.append("## 重复文件")
        lines.append("")
        if duplicates.groups:
            lines.append(
                f"共 {len(duplicates.groups)} 组，可回收 {human_size(duplicates.wasted_bytes)}。"
            )
            lines.append("")
            rows = [
                [human_size(group.size), str(len(group.paths)), human_size(group.wasted_bytes), group.digest[:16]]
                for group in duplicates.groups[:top]
            ]
            lines.extend(_md_table(["单份体积", "副本数", "可回收", "sha256"], rows))
            lines.append("")
            for index, group in enumerate(duplicates.groups[:top], start=1):
                lines.append(f"**第 {index} 组**（保留第一份）")
                lines.append("")
                for path in group.paths:
                    lines.append(f"- `{path}`")
                lines.append("")
        else:
            lines.append("未发现重复文件。")
            lines.append("")
        for issue in duplicates.issues:
            lines.append(f"- ⚠ 未能读取 `{issue.path}` — {issue.reason}")
        if duplicates.issues:
            lines.append("")

    if suggestions:
        lines.append("## 建议")
        lines.append("")
        for item in suggestions:
            marker = "⚠" if item.severity == "warn" else "ℹ"
            lines.append(f"### {marker} {item.title}")
            lines.append("")
            lines.append(item.detail)
            lines.append("")
            if item.command:
                lines.append("```bash")
                lines.append(item.command)
                lines.append("```")
                lines.append("")
        if any(item.command for item in suggestions):
            lines.append("> ⚠ 以上命令仅供参考，本工具**不会**执行任何删除。执行前请自行复核。")
            lines.append("")

    if result.issues:
        lines.append(f"## 未扫描目录（{len(result.issues):,} 个）")
        lines.append("")
        lines.extend(
            _md_table(
                ["路径", "原因"],
                [[f"`{_display_path(issue.path, result.root)}`", issue.reason] for issue in result.issues],
            )
        )
        lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n"


def _render_ranking(
    title: str,
    items: list[tuple[str, int]],
    total: int,
    *,
    suffix: bool = False,
) -> list[str]:
    """渲染「体积排行」或「文件类型分布」小节。"""
    lines = [f"{title}（前 {len(items)} 项）" if not suffix else f"{title}（前 {len(items)}）"]
    if not items:
        lines.append("  （空）")
    for index, (name, size) in enumerate(items, start=1):
        label = name if suffix else _label(name, suffix=False)
        prefix = f"  {index}.  " if not suffix else "  "
        lines.append(
            f"{prefix}{human_size(size):<9} {_percent(size, total):>6}  {label}"
        )
    lines.append("")
    return lines


def _render_duplicates(duplicates: DuplicateReport, top: int) -> list[str]:
    """渲染重复文件小节。"""
    lines = [
        f"重复文件（{len(duplicates.groups):,} 组，可回收 {human_size(duplicates.wasted_bytes)}）"
    ]
    if not duplicates.groups:
        lines.append("  （无）")
    for index, group in enumerate(duplicates.groups[:top], start=1):
        lines.append(
            f"  {index}.  {human_size(group.size)} × {len(group.paths)} 份"
            f"（{group.inode_count} 个实体）  sha256:{group.digest[:16]}"
        )
        for path in group.paths:
            lines.append(f"        {path}")
    if len(duplicates.groups) > top:
        lines.append(f"  （另有 {len(duplicates.groups) - top} 组，完整清单见 --json）")
    for issue in duplicates.issues:
        lines.append(f"  ⚠ 未能读取 {issue.path} — {issue.reason}")
    lines.append("")
    return lines


def _render_suggestions(suggestions: Sequence[Suggestion]) -> list[str]:
    """渲染建议小节，并在存在命令时附上安全警告。"""
    lines = [f"建议（{len(suggestions)} 条）"]
    for item in suggestions:
        marker = "⚠" if item.severity == "warn" else "ℹ"
        lines.append(f"  {marker} {item.title}")
        lines.append(f"      {item.detail}")
        if item.command:
            lines.append(f"      $ {item.command}")
    if any(item.command for item in suggestions):
        lines.append("")
        lines.append("  ⚠ 以上命令仅供参考，本工具不会执行任何删除。执行前请自行复核。")
    lines.append("")
    return lines


def _duplicates_payload(duplicates: DuplicateReport | None) -> dict[str, object] | None:
    """把查重结果转成 JSON 可序列化的结构；``None`` 表示本次未查重。"""
    if duplicates is None:
        return None
    return {
        "group_count": len(duplicates.groups),
        "path_count": duplicates.path_count,
        "wasted_bytes": duplicates.wasted_bytes,
        "groups": [
            {
                "size": group.size,
                "digest": group.digest,
                "inode_count": group.inode_count,
                "wasted_bytes": group.wasted_bytes,
                "paths": [str(path) for path in group.paths],
            }
            for group in duplicates.groups
        ],
    }


def _md_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    """渲染一个 Markdown 表格，转义单元格里的竖线。"""

    def cell(value: str) -> str:
        return str(value).replace("|", "\\|")

    lines = ["| " + " | ".join(cell(h) for h in headers) + " |"]
    lines.append("|" + "|".join("---" for _ in headers) + "|")
    for row in rows:
        lines.append("| " + " | ".join(cell(value) for value in row) + " |")
    return lines


def _label(name: str, *, suffix: bool) -> str:
    """目录名加尾斜杠；``ROOT_LABEL`` 原样显示。"""
    if suffix or name == ROOT_LABEL:
        return name
    return f"{name}/"


def _rank(mapping: dict[str, int], top: int) -> list[tuple[str, int]]:
    """按体积降序取前 *top* 项；``top`` 为负表示全部。

    体积相同时按名称升序，保证输出稳定、可断言。
    """
    ordered = sorted(mapping.items(), key=lambda item: (-item[1], item[0]))
    if top < 0:
        return ordered
    return ordered[:top]


def _percent(part: int, whole: int) -> str:
    """把占比渲染成一位小数的百分比；``whole`` 为 0 时返回 ``0.0%``。"""
    return f"{_percent_value(part, whole):.1f}%"


def _percent_value(part: int, whole: int) -> float:
    """占比数值（百分比，保留一位小数）。"""
    if whole <= 0:
        return 0.0
    return round(part / whole * 100, 1)


def _display_path(path: Path, root: Path) -> str:
    """把路径渲染成相对扫描根的形式；不在根之下时原样返回。"""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return str(path)
    text = str(relative)
    return str(root) if text == "." else text
