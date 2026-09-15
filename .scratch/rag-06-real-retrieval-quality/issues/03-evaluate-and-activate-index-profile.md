# 03: 评测调优并激活合格索引 Profile

**What to build:** 让候选模型、Chunk 和检索参数在 staging 索引上通过同一真实评测集比较，只有同时满足召回、排序、拒答、泄漏和延迟门禁的 profile 才能原子激活，并可回滚到上一合格版本。

**Blocked by:** 02 / 为敏感知识实施内网或本地模型路由.

**Status:** resolved

- [x] 每个候选 profile 在独立 staging 索引和固定数据集上评测。
- [x] 报告比较候选数、RRF、Rerank 阈值及必要的 Chunk 变体。
- [x] Recall@5、MRR@5、拒答、泄漏和延迟全部达标后才能激活。
- [x] 未达标、评测失败或证据过期的 profile 不能切换为活动版本。
- [x] 激活原子更新查询绑定并保留上一合格 profile。
- [x] 回滚恢复上一模型、索引和配置组合。
- [x] Parent-child 或查询扩展只有在分类指标证明收益时才启用。

## Answer

新增持久化 staging/active/rollback profile、固定数据集与完整质量门禁、证据新鲜度和精确绑定校验；激活在事务内切换并由唯一活动索引保护，生产查询只接受活动 profile 指纹。
