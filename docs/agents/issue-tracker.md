# Issue tracker: Local Markdown

问题与规格存放在仓库的 `.scratch/` 目录中。

## Conventions

- 每个功能使用一个目录：`.scratch/<feature-slug>/`
- 规格文件：`.scratch/<feature-slug>/spec.md`
- 实施任务：`.scratch/<feature-slug>/issues/<NN>-<slug>.md`
- 每个任务单独一个文件，编号从 `01` 开始
- 使用文件顶部的 `Status:` 表示 triage 状态
- 评论追加到文件底部的 `## Comments`

## Skill operations

- 发布问题：在对应 `.scratch/<feature-slug>/` 下创建文件
- 读取问题：读取用户提供的文件路径
- Map：`.scratch/<effort>/map.md`
- 子任务：`.scratch/<effort>/issues/NN-<slug>.md`
- 阻塞关系：使用 `Blocked by: NN, NN`
- 认领任务：将 `Status:` 改为 `claimed`
- 完成任务：追加 `## Answer`，将状态改为 `resolved`
