# 02: 处理索引幂等与分类失败

**What to build:** 让重复任务安全跳过或复用已完成产物，并把 OCR_REQUIRED、永久解析失败和存储、模型、索引等临时故障转换为可操作的结构化结果或可重试错误。

**Blocked by:** 01 / 通过 DocumentIndexer 完成 PDF 索引.

**Status:** resolved

- [x] 同一文档、源哈希和管线版本重复执行不会重复 Chunk 或向量。
- [x] 文本不足的扫描 PDF 返回 OCR_REQUIRED，并持久化可见状态。
- [x] 加密、损坏或不支持的内容返回稳定的永久失败代码。
- [x] 临时存储、数据库、Embedding 或向量故障产生可重试错误。
- [x] Celery 只重试明确可重试错误，并保留尝试信息。
- [x] 任何失败均不把文档误报为可用。

## Answer

已按文档、源哈希和管线修订实现幂等判断，并区分 OCR、永久 PDF 错误与数据库、存储、Embedding、向量临时故障；任务采用原子 attempt 认领。
