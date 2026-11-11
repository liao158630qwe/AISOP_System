# 数据库 v2：约束、迁移与恢复

更新日期：2026-11-08。

本机业务库已于 2026-11-08 升级并启用 v2，API/Worker 已重启，完整回归 873 项通过、2 项跳过。升级前备份、业务数据核对及上线验收见 [本机上线记录](../history/database-v2-deployment-2026-11-08.md)。

数据库继续采用 SQLAlchemy Core，生产使用 PostgreSQL，本机使用 SQLite。v2 将原先的“补表、补索引”升级成显式事务迁移，并将关键业务关系交给数据库校验。运行数据、媒体文件及冻结的模型配置保持原有存储分工。

## 新的完整性约束

| 关系或字段 | 数据库保证 |
| --- | --- |
| 分析任务 → 媒体 | 媒体存在且属于同一账户；仍有任务引用时拒绝删除媒体 |
| 运行指标 → 分析任务 | 拒绝孤立指标；删除任务时级联删除其指标 |
| 场景 / 已注册训练结果 → 模型 | 模型存在且属于同一账户；被引用时拒绝删除 |
| 训练作业 → 训练记录 | 记录存在且账户一致；保留作业时不能直接删除父记录 |
| 视频 / 后台作业 → 数据集 | 数据集存在；作业账户与数据集一致 |
| 标注帧 → 视频 | 视频和帧必须属于同一数据集；同一视频的帧序号唯一；删除视频时删除所属帧 |
| 工件事件 → 工件周期 | 周期存在且账户一致；带审计事件的周期不能直接删除 |
| 终端会话 → 终端绑定 | 绑定存在；删除绑定时撤销会话 |
| 检测回收标记 → 检测记录 | 检测存在且账户一致 |
| 状态、进度、计数、版本、图像尺寸 | 限定合法任务状态、0–1 进度、非负计数、合法版本及正图像尺寸 |

跨账户关系使用复合外键，例如 `analysis_runs(media_id, owner) → media(id, owner)`，避免仅验证媒体 ID 存在却接受错误账户。补充了引用模型、训练记录和终端绑定的子表索引。

工位历史中的冻结设备/模型信息，以及告警的多类型来源 ID，继续作为历史快照保存；当前设备配置的生命周期不决定这些历史记录的生命周期。已有告警录像保留期约束继续生效。

## 升级协议

实现入口为 `backend/init_db.py`；迁移在 `backend/migrations.py`，v2 规则在 `backend/schema_constraints.py`。

1. 先读取版本；未知版本、多版本行、缺失版本标记或不匹配的迁移历史直接报错。
2. 获取迁移锁。PostgreSQL 按 schema 使用事务 advisory lock；SQLite 使用 `BEGIN IMMEDIATE`。默认等待上限 60 秒，可用 `DB_MIGRATION_LOCK_TIMEOUT_SECONDS` 设置为 1–600 秒。
3. 检查旧字段及旧数据，报告不满足约束的表、约束名、数量和最多 5 组主键样例。重复帧的数量表示重复键组数。迁移不自动删除、修正或重新归属这些记录。
4. 在一个事务内添加约束、补齐缺少的 v1 附加表及索引，写入 `schema_migrations`，最后将 `schema_version` 更新为 2。数据校验或 DDL 失败会回滚业务表与版本。
5. 重复执行保持幂等；v2 缺少必要表或约束时报告异常，避免将意外丢失的业务表静默建成空表。

SQLite 添加约束需要重建受影响的表：复制原始字段定义及数据，保留额外字段、普通/表达式索引、触发器和视图，在提交前检查所有外键。维护连接临时关闭外键执法以完成重建，结束后恢复；其他连接保持原设置。SQLite 的 WAL 模式在迁移事务开始前配置。

升级应安排在停止 API、Worker 和其他写入进程后进行。PostgreSQL 迁移会锁住相关表的写入；SQLite 迁移同样需要写锁，数据复制和索引创建需要额外磁盘空间。这里提供的是维护窗口内的事务升级，不是大表在线迁移或主从高可用部署。

API 和 Worker 启动时检查数据库版本及迁移历史。`/api/v1/ready` 在版本不兼容时返回 503 和 `database=migration_required`，帮助部署检查发现漏执行迁移的情况。

## 只读预检

在已设置 `DATABASE_URL` 的环境运行：

```powershell
node tools/python.cjs -m backend.init_db --check
```

本机库的设置方式：

```powershell
$env:DATABASE_URL = 'sqlite:///D:/Project/AISOP/.runtime/local.db'
node tools/python.cjs -m backend.init_db --check
```

命令只读取一致快照，不执行 DDL，不修改记录或 WAL 模式；文件不存在时拒绝创建。它只需要数据库配置，不需要提供应用账户密码或创建数据目录。

| 退出码 | 含义 |
| --- | --- |
| 0 | 已是 v2，结构与数据检查通过 |
| 2 | 数据可升级，但尚有迁移待执行 |
| 1 | 数据问题或已知 schema 错误；先根据诊断处理 |

无效命令参数由 argparse 返回 2，并显示参数错误；自动化调用应同时检查输出是否为预检 JSON。

## 本机上线

```powershell
npm run restart:local
```

启动器先检查依赖并执行只读预检。预检失败时保留正在运行的服务；预检通过后停止属于本项目的 API/Worker，使用 SQLite Backup API 保存升级前数据库到 `.runtime/database-backups/`，执行 `quick_check`，再迁移并启动新服务。备份包含 WAL 中已提交的数据，每次使用独立文件名。

存在待升级版本且服务仍在运行时，普通 `npm start` 会提示使用重启命令；补启单个服务或重复启动健康服务只做预检。这样可以避免旧进程继续写入时在后台修改表结构。

数据库备份只覆盖数据库。媒体、告警录像、模型和训练文件在 `.runtime/local-data/` 或配置的 `DATA_DIR`，应同时纳入备份。备份失败或迁移失败后不启动新服务，保留备份与日志供恢复；再次启动前处理诊断。

## PostgreSQL 部署

在维护窗口停止写入进程，备份数据库与共享数据卷，再使用新镜像运行预检和迁移。示例沿用 Compose 中的数据库和用户名称：

```powershell
docker compose stop api worker
New-Item -ItemType Directory -Force .runtime/backups
docker compose exec -T db pg_dump -U aisop -Fc -f /tmp/aisop-before-v2.dump aisop
docker compose cp db:/tmp/aisop-before-v2.dump .runtime/backups/aisop-before-v2.dump
# 此时同时备份 analysis-data 卷，并将备份移到独立存储。
docker compose build init api worker
docker compose run --rm init python -m backend.init_db --check
# 预检退出码 2 表示待升级；确认 JSON 中 can_upgrade=true 后执行。
docker compose run --rm init
docker compose up -d api worker web
```

自定义部署可直接执行 `python -m backend.init_db`。不将失败的写事务整体自动重放：提交回执丢失时继续沿用业务接口原有的幂等键与租约确认方式。

恢复旧版本时，停止全部写入进程，恢复匹配的数据库、数据卷和应用代码后再启动。SQLite 应恢复完整备份到新路径并配置连接，避免把旧 `-wal` / `-shm` 与恢复文件混用；PostgreSQL 用 `pg_restore` 恢复到独立数据库验证后切换。v2 没有自动向下迁移命令。

## 验证

```powershell
node tools/python.cjs -m pytest tests/test_database_migrations.py tests/test_local_runtime.py -q
node tools/python.cjs tools/test-postgres-local.py -- tests/test_database_migrations.py tests/test_postgres.py tests/test_inspection_history_postgres.py -q
npm test
```

迁移专项覆盖新库、完整/部分 v1、旧数据异常、DDL 后故障回滚、失败后重试、并发初始化、锁等待超时、账户关联约束、父子删除、帧唯一性、SQLite 自定义字段/索引/触发器/视图保留及只读预检。启动器测试覆盖“预检→停止服务→备份→迁移→启动”的顺序，以及 WAL 备份。

2026-11-08 另从本机业务库取得只读一致性副本并演练升级：22 张业务表、219 条记录，升级前后逐表内容哈希一致。副本预检与升级后检查通过。该演练没有切换原业务库；结果保存在 `test-results/database-upgrade-rehearsal.json`。

本次完整 Python 回归为 **849 通过、26 跳过**，报告为 `test-results/database-architecture-suite.xml`。另在临时 PostgreSQL 16.6 实例运行迁移及原有数据库专项：**44 通过、1 跳过**，其中 24 项实际访问 PostgreSQL；其余为同组 SQLite/启动就绪测试，跳过项是仅适用于 SQLite 的重建检查。报告为 `test-results/database-upgrade-postgres.xml`，结束后测试 schema 残留为 0，临时实例已停止并清理。原配置的 15432 端口当时无法绑定，因此此次测试使用独立数据目录和临时端口。
