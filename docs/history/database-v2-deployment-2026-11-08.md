# 数据库 v2 本机上线与验收

日期：2026-11-08，Asia/Singapore。

本机业务库 `.runtime/local.db` 已从 v1 升级至 v2。API 与 Worker 已重启并使用本次新代码，8000 服务的数据库与 Worker 就绪检查均通过。

## 上线

执行 `npm run restart:local`，完成只读预检、停止旧服务、SQLite 一致性备份、迁移、启动新服务和登录检查。

- 升级前备份：`.runtime/database-backups/local-before-upgrade-20261108-140954-97dc9116.db`。
- 当前数据库版本：2。
- 迁移历史：`legacy_core_baseline`、`relational_integrity`。
- 新进程：API PID 25096、Worker PID 34808；PID 仅记录验收时状态，后续应通过启动器验证服务归属。

## 业务库验证

使用只读连接对比升级前备份与升级后业务库：20 张业务表、215 条记录内容哈希完全一致。会话与 Worker 心跳是运行态数据，单独排除于内容对比；原库和备份中的业务记录未为测试做清理。

- `PRAGMA integrity_check`：`ok`。
- `PRAGMA foreign_key_check`：0 条违规。
- v2 结构与数据预检：通过，没有待执行迁移。
- 实际业务库事务探针：合法媒体/任务/指标写入通过，跨账户关联、不存在的父记录和越界进度均被拒绝；全部探针数据在事务内回滚，并确认无残留。
- HTTP：13 个已登录 API 与 7 个页面入口均返回 200，覆盖任务、模型/场景目录、训练、数据集、终端、看板、历史和告警。
- 鉴权：未登录拒绝、登录、已登录查询、退出及退出后拒绝均通过。
- 就绪状态：数据库 ready、Worker ready，活动 Worker 为 1。

详细结果：`test-results/database-upgrade-live-smoke.json`。

## 完整回归

升级后开启独立临时 PostgreSQL 16.6 实例，设置测试连接后运行整个 `tests` 目录：**873 通过、2 跳过、0 失败**，耗时 147.25 秒。包括 SQLite 与 PostgreSQL 的迁移、约束、锁等待、并发、失败回滚及原有业务回归。

两项跳过分别是需要两张 CUDA 显卡的测试，以及 PostgreSQL 参数下不适用的 SQLite 表重建测试。

报告：`test-results/database-upgrade-live-suite.xml`。测试结束时 PostgreSQL 测试 schema 残留为 0，临时实例已停止并清理。本机业务 API 和 Worker 保持运行。业务库仍使用 SQLite；PostgreSQL 实例用于隔离的兼容性验证。

运行与恢复说明见 [数据库 v2 指南](../guides/database-architecture.md)。
