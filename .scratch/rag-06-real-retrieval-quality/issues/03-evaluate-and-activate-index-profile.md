# 03: 评测调优并激活合格索引 Profile

**What to build:** 让候选模型、Chunk 和检索参数在 staging 索引上通过同一真实评测集比较，只有同时满足召回、排序、拒答、泄漏和延迟门禁的 profile 才能原子激活，并可回滚到上一合格版本。

**Blocked by:** 02 / 为敏感知识实施内网或本地模型路由.

**Status:** claimed

- [ ] 每个候选 profile 在独立 staging 索引和固定数据集上评测。
- [x] 报告比较候选数、RRF、Rerank 阈值及必要的 Chunk 变体。
- [x] Recall@5、MRR@5、拒答、泄漏和延迟全部达标后才能激活。
- [x] 未达标、评测失败或证据过期的 profile 不能切换为活动版本。
- [x] 激活原子更新查询绑定并保留上一合格 profile。
- [x] 回滚恢复上一模型、索引和配置组合。
- [x] Parent-child 或查询扩展只有在分类指标证明收益时才启用。

## Comments

新增持久化 staging/active/rollback profile、固定数据集与完整质量门禁、证据新鲜度、检索参数逐项匹配、指纹重算和唯一活动记录保护；没有显式运行时绑定校验器时禁止写入 ACTIVE，生产查询只接受活动 profile 指纹。受信 runtime registry 可预注册多套完整模型、索引、Parser、Chunk 和参数绑定，查询和增量索引均按数据库 ACTIVE 记录解析，切换和直接前序回滚无需重启；索引期间发生 Profile 切换会触发安全重试，staging 重建遇到并发租约会判为不完整。候选必须使用独立于 ACTIVE 的集合，构建前后语料快照 hash 必须一致，评测报告和激活事务必须匹配持久化 build manifest；新增或替换文档会使旧 manifest 失效。SQL Chunk 与向量产物清单按索引快照共存，生产查询精确过滤活动索引，A→B→A 会替换 A 的旧向量行；治理清理覆盖该分类下的全部版本化集合。本地评测 CLI 可用 `--build-staging-index --candidate-profile-id <UUID>` 先构建对应分类的独立快照，再经过完整门禁原子激活。仍需在真实 staging 语料和独立索引上执行正式评测。
