# S2–S4 合并规格

> **状态：由 AI 起草，实现采用"倾向值"，决策待项目所有者事后否决。**
> 原因：所有者要求 S2–S4 一次做完。为不阻塞进度，所有需要决策的点都在
> 下表显式列出。**否决哪条，我改哪条** —— 每条都在独立 commit 里可回滚。

---

## 0. 决策清单（我替你定的，请复核）

| 编号 | 决策点 | 取值 | 理由 |
|---|---|---|---|
| D1 | 重复判定 | 大小相同 **且** sha256 相同 | 内容级（G3），同名不同内容不算重复 |
| D2 | 空文件 | 排除在查重之外 | Q4：噪音太大 |
| D3 | 硬链接 | 同 `(dev, inode)` 只算一份，可回收空间按 inode 数算 | 否则会虚报可回收空间 |
| D4 | 哈希算法 | sha256，1 MiB 分块流式读取 | 保守、不吃内存 |
| D5 | JSON / Markdown 输出目标 | **仅 stdout**，不写文件 | CONVENTIONS §6：`apply.py` 之外禁止写文件 |
| D6 | JSON 契约 | 固定 `schema_version: 1`，字段名变更视为破坏性 | CONVENTIONS §7 |
| D7 | 大文件阈值 | 100 MB（Q3） | 可通过 `--large-bytes` 覆盖 |
| D8 | 陈旧阈值 | 180 天（Q2） | 可通过 `--stale-days` 覆盖 |
| D9 | "陈旧文件"定义 | 大文件 **且** 超过陈旧阈值 | G2 的原文 |
| D10 | 清理命令 | 只输出、不执行（Q5）；`shlex.quote` 转义；**不用 `-r`** | 防复制粘贴误伤 |
| D11 | 建议的副作用 | 零 —— 只产出数据结构 | G6 |
| D12 | 新增模块 | `duplicates.py` / `suggest.py` | CONVENTIONS §2 目录结构 |
| D13 | 建议输出量上限 | 最多为 5 组重复文件附命令，其余合并为一条摘要 | **实跑发现**：`~/code` 有 2000+ 组重复，逐组输出会刷屏 |
| D14 | 查重体积门槛 | `--min-duplicate-bytes`，默认 1（即仅排除空文件） | 默认保持 Q4 语义；需要过滤 `node_modules` 噪音时由用户抬高 |

---

## S2 重复文件检测

**做**：按内容找出重复文件组，计算可回收空间。

**算法**：
1. 过滤 `size > 0` 的文件（D2）
2. 按 `size` 分桶 —— 只有同大小的文件才可能重复，**避免对唯一文件读盘**
3. 桶内文件计算 sha256，按 `(size, digest)` 归组（D1、D4）
4. 组内按 `(dev, inode)` 去重（D3）
5. 只剩 ≥ 2 个不同 inode 的组才算重复组
6. 按可回收空间降序排列

**数据模型**：

```python
DuplicateGroup(size, digest, paths, inode_count)
    .wasted_bytes == size * (inode_count - 1)

DuplicateReport(groups, issues)
    .wasted_bytes  # 合计
    .path_count    # 涉及的文件路径总数
```

**明确不做**：模糊匹配（相似文件）、按文件名判重、自动删除。

---

## S3 报告导出 JSON + Markdown

新增 `--json` 与 `--markdown`（互斥），输出到 **stdout**，替代人类报告。

**不写文件的理由**：CONVENTIONS §6 规定 `apply.py` 之外禁止写文件。用户需要落盘
时用 shell 重定向：`homecheck . --json > report.json`。

**JSON 契约（`schema_version: 1`）**：

```
{
  "schema_version": 1,
  "tool": "homecheck",
  "generated_at": "<ISO 8601>",
  "root": "<绝对或相对路径>",
  "elapsed_seconds": 0.28,
  "totals": { "files": int, "directories": int, "bytes": int, "human": str },
  "by_subdirectory": [ { "name": str, "bytes": int, "percent": float } ],
  "by_extension":    [ { "extension": str, "bytes": int, "percent": float } ],
  "duplicates": {
    "group_count": int, "path_count": int, "wasted_bytes": int,
    "groups": [ { "size": int, "digest": str, "inode_count": int,
                  "wasted_bytes": int, "paths": [str] } ]
  },
  "suggestions": [ { "kind": str, "severity": str, "title": str,
                     "detail": str, "command": str|null } ],
  "issues": [ { "path": str, "reason": str } ],
  "skipped_symlinks": int
}
```

`duplicates` 在 `--skip-duplicates` 时为 `null`。

---

## S4 建议引擎

**做**：只根据扫描结果产出建议，**零副作用**（D11）。清理类建议只附**命令文本**（D10）。

| 规则 | 触发条件 | 严重度 | 附带命令 |
|---|---|---|---|
| R1 `duplicate-files` | 存在重复组 | warn | ✅ 每组一条 `rm --`（保留第一份） |
| R2 `stale-large-files` | 文件 ≥ 大文件阈值 **且** mtime 超陈旧阈值 | warn | ❌ 归档属人工决策 |
| R3 `large-files` | 存在 ≥ 大文件阈值的文件 | info | ❌ |
| R4 `incomplete-scan` | 扫描存在权限/IO 问题 | warn | ❌ 提示报告可能不完整 |
| R5 `dominant-directory` | 某顶层子目录占比 ≥ 30% | info | ❌ |

**命令安全约束**：仅对重复组的冗余副本生成 `rm -- <quoted>`；
路径一律 `shlex.quote` 转义；**绝不生成 `rm -r` / `rm -rf`**；
建议输出时附带"执行前请自行复核"的警告。

**明确不做**：自动执行任何命令（Q5 / N3）、模糊的"优化建议"。

---

## 验收清单

- [ ] 重复检测：同内容不同名 → 判为重复；同大小不同内容 → **不**判为重复
- [ ] 空文件不进入重复组
- [ ] 硬链接不虚报可回收空间
- [ ] `--json` 的 stdout **只有** JSON，无任何装饰文字
- [ ] JSON 含 `schema_version`，字段集合与本文档一致
- [ ] `--markdown` 输出合法 Markdown 表格
- [ ] 建议引擎对同一个输入产出稳定结果，且**不产生任何文件系统改动**
- [ ] 所有生成的 `rm` 命令经过转义，路径含空格/引号时不破坏
- [ ] 全部测试在临时目录内运行，不触碰真实文件
- [ ] 扫描的零写入承诺仍然成立
