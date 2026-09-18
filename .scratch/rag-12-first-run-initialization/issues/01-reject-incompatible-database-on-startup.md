# 01: 启动期阻止不兼容数据库运行

**What to build:** 让 API 和 Worker 在开始承接业务请求或任务前确认数据库迁移版本与当前代码兼容；版本落后、超前或分叉时立即停止启动，并向操作者返回可执行且不泄密的修复说明，而不是在业务请求中暴露 HTTP 500。

**Blocked by:** None (can start immediately).

**Status:** resolved

- [x] API 和 Worker 通过同一兼容性规则比较数据库当前 Alembic revision 与代码 head，兼容时可以正常启动。
- [x] 数据库落后时启动失败并报告 `DATABASE_SCHEMA_OUTDATED`、当前 revision、预期 revision 和安全的迁移操作提示。
- [x] 数据库超前或与代码分叉时分别报告 `DATABASE_SCHEMA_AHEAD` 或 `DATABASE_SCHEMA_DIVERGED`，且不会继续承接请求或任务。
- [x] 诊断信息不包含数据库密码、连接串或其他凭据，API 与 Worker 对同一状态给出一致结果。
- [x] Alembic 升级命令和隔离迁移测试仍可运行，不会因为导入应用而被启动检查反向阻塞。
- [x] 应用生命周期测试覆盖兼容、落后、超前和分叉 revision，并证明不兼容状态在业务请求前失败。

## Answer

API 与 Worker 已在其他生产探针之前复用同一 Alembic 图兼容检查；落后、超前和分叉状态分别返回稳定诊断。迁移 `20260923_0015` 已在空 SQLite 数据库完成全链升级验证。
