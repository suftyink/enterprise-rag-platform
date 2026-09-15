# 02: 为敏感知识实施内网或本地模型路由

**What to build:** 让受限知识的索引和查询只进入批准的内网或本地 Embedding/Rerank 路径，在路由缺失或不可用时失败关闭，并通过评测证据证明没有把敏感内容发送到云端 Adapter。

**Blocked by:** 01 / 接入普通知识的真实 Embedding 与 Rerank.

**Status:** resolved

- [x] 文档和知识范围具有可执行的数据敏感级别。
- [x] RESTRICTED 索引和查询只能选择批准的内网或本地 profile。
- [x] 客户端不能覆盖数据分类或选择任意 Provider。
- [x] 本地路由不可用时返回明确失败，不降级到云端。
- [x] Recording Adapter 证明受限输入未到达云端调用。
- [x] 日志和评测报告记录安全的路由身份但不记录正文。
- [x] 普通知识的云端路径保持可用且隔离。

## Answer

数据分类由服务端文档策略产生；索引与评测检索均按分类走独立路由和集合，RESTRICTED 仅允许 INTERNAL，缺少或错配路由时在 Provider 调用前失败关闭。
