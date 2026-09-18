# 06: 从首批审核内容构建候选 Release

**What to build:** 让处于 `WAITING_FOR_CONTENT` 的首次初始化在首批文档完成上传、处理和审批后，冻结不可变语料快照并构建独立候选 Release，再移交现有评测与发布流程；整个过程不改变当前活动服务，也不自行生成发布证据。

**Blocked by:** 04 / 支持初始化恢复、重试和取消.

**Status:** resolved

- [x] `WAITING_FOR_CONTENT` 状态允许已授权知识管理员使用现有治理流程上传、处理和审批文档，员工问答继续关闭。
- [x] 继续初始化只冻结已批准、仍保留且数据分类匹配的文档；零文档或无合格文档不能完成候选构建。
- [x] 冻结结果包含不可变成员关系、corpus generation/hash 和文档数量，后续治理变化不会静默改写候选。
- [x] 首个候选使用独立 STAGING Profile/物理索引和完整 build manifest，不修改任何 ACTIVE Release 或服务绑定。
- [x] 构建前后核对语料及配置指纹；发生漂移或不兼容变化时以可恢复状态停止，不把不一致产物交给评测。
- [x] 构建成功后初始化移交现有检索评测、回答评测、Readiness、批准、激活和 rollout 流程，不制造 PASS 或自动批准。
- [x] 只有完成现有评测、有效 PASS Readiness、人工批准、激活和 rollout 后，员工才能针对精确的活动 Release 获得引用回答。
- [x] 内部 Web 工作流覆盖等待内容、治理首批文档、冻结、候选构建、评测移交及激活前后的员工访问差异。

## Answer

“继续初始化”现复用现有候选 Release 冻结规则并由 Celery Worker 构建；成功仅到 `EVALUATION_HANDOFF`/`EVALUATING`，不会生成 PASS、批准、激活或 rollout。现有发布工作流继续负责最终员工开放和 Release 身份贯穿。
