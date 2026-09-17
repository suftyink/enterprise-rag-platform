# 04: 增量构建独立候选索引

**What to build:** 为冻结的候选 Release 构建与当前活动版本隔离的 STAGING 索引，安全复用兼容的既有向量，只处理新增或变化内容，并在语料漂移或处理契约不兼容时给出可操作结果。

**Blocked by:** 03 / 批准内容并冻结候选语料版本.

**Status:** resolved

- [x] 候选构建写入独立 STAGING Profile/索引，不修改当前 ACTIVE Release 的物理索引或服务绑定。
- [x] 文档内容和 parser、chunk、embedding、维度及索引契约指纹均匹配时，可复用既有解析、Chunk 或向量产物。
- [x] 新增文档和正文发生变化的文档只处理变化版本，不重新调用 Embedding 处理全部兼容语料。
- [x] Embedding 模型或维度、不可兼容 parser/chunk 策略、索引结构或关键检索契约变化会触发完整候选重建。
- [x] 构建前后核对 corpus generation/hash 和文档数量；发生漂移时候选进入明确失败或过期状态，不能继续评测。
- [x] 构建 manifest 记录候选 Release、索引身份、build hash、处理指纹、产物统计和失败原因。
- [x] 内部 Web 展示构建进度、增量/全量决策和安全错误，同时当前员工问答继续使用旧 Release。

## Answer

staging 构建现可绑定冻结 Release 成员，继续复用既有内容/处理指纹去重能力，并在 Profile、索引身份、构建前后语料或 manifest 不一致时以稳定错误拒绝进入评测；当前活动 Release 不受候选构建影响。

