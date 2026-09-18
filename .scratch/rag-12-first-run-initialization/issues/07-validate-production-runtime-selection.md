# 07: 约束生产 Runtime 选择和预检

**What to build:** 让管理员在初始化生产环境时只能选择经过验证、符合数据边界的 Runtime Deployment，并在创建昂贵产物前完成依赖预检；开发环境仍有明确标记的本地 Fake 默认值，但任何默认值都不能静默进入生产。

**Blocked by:** 03 / 安全 Provision 并核验 Milvus 物理索引.

**Status:** resolved

- [x] 非生产环境可展示一个明确标记的本地 Fake 选项，其 embedding 维度、rerank binding、parser/chunk 版本和独立索引位置相互一致。
- [x] 生产环境不展示或接受开发 Fake 默认值，只返回已验证且符合数据分类、provider boundary、模型路由、维度和索引位置约束的 Runtime Deployment。
- [x] 只有一个有效生产选项时页面可以预选但仍要求管理员确认；多个选项时必须由管理员明确选择。
- [x] 初始化在 Provision 前预检 PostgreSQL、Redis、对象存储、Milvus、Worker 和模型路由，并为失败项返回安全、稳定且可操作的诊断。
- [x] Runtime 选择参与配置指纹；选择或配置变化不会静默复用不兼容的 Profile、物理索引或已完成阶段。
- [x] 浏览器响应、审计、日志和遥测不返回 API key、连接凭据、原始 Prompt、问题或文档正文。
- [x] 内部 Web 测试覆盖开发默认值、生产单选确认、生产多选、无有效选项、预检失败和越权提交。

## Answer

开发环境只提供明确的 Fake 选项；生产仅接受配置的 Runtime Deployment，浏览器视图不含凭据。Provision 前统一执行数据库迁移、PostgreSQL、Redis、对象存储、Milvus、Worker、模型路由与索引配置预检，并只持久化安全失败项名称。
