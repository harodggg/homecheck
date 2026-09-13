# homecheck

只读的本机环境体检工具：扫描目录，输出体积报告。
**默认不产生任何写入。**

## 状态

S1（只读扫描 + 体积统计）已实现。规格见 `docs/SPEC-S1.md`。

## 运行

无需安装任何依赖（Python 3.12+，仅用标准库）：

```bash
PYTHONPATH=src python3 -m homecheck ~/Downloads --top 5
```

不传路径时默认扫描**当前目录**（不会扫整个 home）：

```bash
PYTHONPATH=src python3 -m homecheck
```

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 安全承诺

- 不传 `--apply` 时，程序对文件系统**零写入**
- 不跟随符号链接，避免循环与重复计数
- 权限不足的目录**跳过并如实汇报**，绝不静默吞掉
- 完整的非目标清单见 `docs/PRODUCT.md` 第 4 节

## 文档

| 文件 | 内容 |
|---|---|
| `docs/PRODUCT.md` | 做什么 / 不做什么 |
| `docs/SPEC-S1.md` | S1 切片规格 |
| `docs/DECISIONS.md` | 决策记录（ADR） |
| `docs/CONVENTIONS.md` | 编码与协作约定 |
| `docs/TASKS.md` | 进度 |
