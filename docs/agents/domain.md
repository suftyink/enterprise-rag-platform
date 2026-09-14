# Domain Docs

## Before exploring

开始代码调查前，按需读取：

- 根目录 `CONTEXT.md`
- 根目录 `CONTEXT-MAP.md`（如果存在）
- `docs/adr/` 中与当前改动相关的 ADR

文件不存在时直接继续，不要求提前创建。

## Layout

本仓库采用 single-context 布局：

```text
/
├── CONTEXT.md
├── docs/adr/
└── src/
```

领域术语以 `CONTEXT.md` 为准。需要使用尚未定义的术语时，先判断是否为命名漂移或真实领域缺口。

如果实现方案与现有 ADR 冲突，必须明确指出冲突，不得静默覆盖已有决策。
