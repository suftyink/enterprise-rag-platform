# 03: 安全 Provision 并核验 Milvus 物理索引

**What to build:** 让首次初始化以完整且不可变的索引身份创建或协调 Milvus Collection；已有资源只有在 schema、Embedding binding 和索引配置全部匹配时才能复用，任何冲突都以稳定错误停止初始化，不执行覆盖或删除。

**Blocked by:** 02 / 贯通空库首次初始化最小路径.

**Status:** resolved

- [x] 物理索引身份覆盖数据分类、schema 版本、向量维度、Embedding binding 指纹和确定性的 Profile/build token。
- [x] 初始化持久化物理索引定位符、schema 指纹和 Embedding binding 指纹，并使同一操作重试解析到同一物理名称。
- [x] Milvus Provision 在创建前后描述实际 Collection，并核对字段、维度、metric、analyzer/BM25、过滤字段、索引定义和存储指纹。
- [x] 已存在且完全匹配的 Collection 被视为幂等成功，包括并发创建后返回 already-exists 的场景。
- [x] 任一身份或结构不匹配时，内部 Web 显示安全的 `INDEX_IDENTITY_COLLISION`，初始化不能继续，既有 Collection 不被删除、重命名或覆盖。
- [x] Fake Adapter 与 Milvus Adapter 遵循同一 Provision 契约，并且 Provision 不能激活 CorpusRelease 或改变员工服务指针。
- [x] 契约测试覆盖首次创建、匹配复用、并发协调和多种冲突；内部 Web 工作流可观察成功与冲突结果。

## Answer

新增 Fake/Milvus Provision seam；物理名称和 Collection description 固化完整身份，创建后及并发 already-exists 后均严格 reconcile。字段类型、动态字段、analyzer、BM25 function、AUTOINDEX/metric、维度和指纹任一不符均失败关闭且不执行删除。
