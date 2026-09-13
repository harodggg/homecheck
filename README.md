# homecheck

只读的本机环境体检工具：扫描目录，找出重复文件，给出体积报告与清理建议。
**默认不产生任何写入，也永远不会自己执行删除。**

## 状态

S1–S6 已实现：扫描统计、重复检测、JSON / Markdown 导出、建议引擎、执行计划。

**S5 只生成计划，不执行任何改动** —— 按 Q5 的决策，v1 只输出命令。
详见 `docs/SPEC-S5.md` 第 0 节（其中说明了 Q5 与 G7 的冲突如何取舍）。

## 运行

无需安装任何依赖（Python 3.12+，仅用标准库）：

```bash
cd ~/projects/homecheck

# 基本扫描
PYTHONPATH=src python3 -m homecheck ~/Downloads --top 10

# 不传路径 = 扫描当前目录（不会扫整个 home）
PYTHONPATH=src python3 -m homecheck

# 只看大文件重复，过滤 node_modules 之类的小文件噪音
PYTHONPATH=src python3 -m homecheck ~/code --min-duplicate-bytes 1048576

# 给脚本消费（stdout 只有 JSON）
PYTHONPATH=src python3 -m homecheck . --json > report.json

# 给人看（Markdown）
PYTHONPATH=src python3 -m homecheck . --markdown

# 只扫一层，跳过查重，快
PYTHONPATH=src python3 -m homecheck / --max-depth 1 --skip-duplicates
```

## 选项

| 选项 | 说明 | 默认 |
|---|---|---|
| `路径` | 扫描根 | `.`（当前目录） |
| `--top N` | 排行/分布/重复组各显示前 N 项 | 20 |
| `--max-depth N` | 限制遍历深度，根为第 0 层 | 不限 |
| `--json` | 输出 JSON（stdout 只有 JSON） | 关 |
| `--markdown` | 输出 Markdown | 关 |
| `--skip-duplicates` | 跳过查重，更快 | 关 |
| `--plan` | 生成执行计划（dry-run），**不执行** | 关 |
| `--max-actions N` | 执行计划最多列出多少个动作 | 100 |
| `--min-duplicate-bytes N` | 参与查重的最小体积 | 1（忽略空文件） |
| `--large-bytes N` | 大文件阈值 | 104857600（100 MB） |
| `--stale-days N` | 陈旧阈值 | 180 |

`--json` 与 `--markdown` 互斥。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 安全承诺

- 不传 `--apply`（尚未实现）时，程序对文件系统**零写入** —— 包括**报告也只写 stdout**，
  落盘请自行重定向（见 `DECISIONS.md` ADR-0008）
- 不跟随符号链接，避免循环与重复计数
- 权限不足的目录**跳过并如实汇报**，绝不静默吞掉
- 硬链接不虚报可回收空间
- 建议里的删除命令一律 `shlex.quote` 转义，**绝不使用 `-r` / `-f`**，
  并且**本工具永不执行它们**
- `--plan` 只生成计划，**不执行任何动作**。计划中的路径若位于 `.git` 等元数据目录、
  在扫描根之外、或与保留项是硬链接，一律被拒绝并**显式列出**（不静默丢弃）
- 完整的非目标清单见 `docs/PRODUCT.md` 第 4 节

## 文档

| 文件 | 内容 |
|---|---|
| `docs/PRODUCT.md` | 做什么 / 不做什么 |
| `docs/SPEC-S1.md` | S1 切片规格 |
| `docs/SPEC-S2-S4.md` | S2–S4 合并规格与决策清单 D1–D14 |
| `docs/SPEC-S5.md` | S5 规格：执行计划与 Q5/G7 冲突的取舍 |
| `docs/DECISIONS.md` | 决策记录（ADR） |
| `docs/CONVENTIONS.md` | 编码与协作约定 |
| `docs/TASKS.md` | 进度 |
