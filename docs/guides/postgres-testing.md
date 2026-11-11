# 本机 PostgreSQL 实库测试

2026-11-06 已在 Windows 本机配置项目专用 PostgreSQL 16.6 测试环境，版本与当前 `compose.yaml` 的数据库版本相同。解释器使用项目的 `tools/python.cjs`，已安装 `requirements-api.txt` 要求的 `psycopg[binary]==3.2.9`。

## 运行

从项目根目录执行：

```powershell
npm run test:postgres
```

命令自动启动专用测试集群、设置子进程的 `TEST_POSTGRES_URL`、运行 PostgreSQL 专项测试，并在结束后停止本次启动的数据库。再次运行会复用配置和数据目录，不需手动复制密码。默认生成 `test-results/postgres-integration.xml`。

自数据库 v2 起，默认选择也包含 `tests/test_database_migrations.py` 和 `tests/test_inspection_history_postgres.py`，覆盖旧库升级、回滚和历史删除并发；升级说明见 [数据库 v2 指南](database-architecture.md)。

需要同时运行其他回归时，在 `--` 后传入 pytest 参数：

```powershell
node tools/python.cjs tools/test-postgres-local.py -- tests/test_postgres.py tests/test_scene_management.py tests/test_scene_logic.py -q
```

直接执行普通 `pytest` 或 `npm test` 不会自动启动 PostgreSQL；若未设置 `TEST_POSTGRES_URL`，其中的 PostgreSQL 测试仍会跳过。需要实库验证时使用上述入口。

可单独控制测试数据库：

```powershell
node tools/python.cjs tools/test-postgres-local.py --status
node tools/python.cjs tools/test-postgres-local.py --start
node tools/python.cjs tools/test-postgres-local.py --stop
```

显式 `--start` 后，测试命令会保留这个已有进程，需用 `--stop` 关闭。脚本只控制自己的数据目录；同一时间只允许一个脚本操作，端口被其他进程占用时停止并报错。

## 配置和隔离

| 项目 | 值 |
| --- | --- |
| 监听地址 | `127.0.0.1:15432` |
| 专用数据库 | `aisop_integration_test` |
| 测试用户 | `aisop_test`，非超级用户、不能创建数据库或角色 |
| 数据目录 | `.runtime/postgres-test/data/` |
| 本地随机凭据 | `.runtime/postgres-test/settings.json` |
| 数据库日志 | `.runtime/postgres-test/server.log` |
| 便携程序 | `.runtime/postgres-test-runtime/pgsql/bin/` |

`.runtime/` 已被 Git 忽略，配置含本地密码，不应提交。测试每次创建随机 `test_<uuid>` schema，结束时删除；执行器额外核对残留数量。本次验证残留为0。应用的 `DATABASE_URL`、本地SQLite业务库和8000服务配置不受此入口修改，也没有注册常驻Windows服务或修改系统PATH。

## 新机器准备

如果已经有 PostgreSQL 程序，可在首次运行时指定 `--bin-dir` 和可选 `--port`；初始化后使用保存的路径与端口。不同主版本不能直接复用同一数据目录。

Windows便携包来自 PostgreSQL下载页指向的EDB分发站。当前测试使用的归档与实测哈希：

```text
https://get.enterprisedb.com/postgresql/postgresql-16.6-1-windows-x64-binaries.zip
SHA256: 6a1bfb6435b13d9563ae481445c70ac2a19846bd8a430b12903b408eec300f9b
Bytes: 304112325
```

将包内 `pgsql/bin/`、`pgsql/lib/`、`pgsql/share/` 解压到 `.runtime/postgres-test-runtime/`。下载记录位于该目录的 `download.json`；上述哈希来自本次实际下载，后续复现可用它检查归档一致性。该版本用于复现项目当前部署基线，不表示它是最新维护版本。

```powershell
node tools/python.cjs -m pip install "psycopg[binary]==3.2.9"
node tools/python.cjs tools/test-postgres-local.py --bin-dir '.runtime/postgres-test-runtime/pgsql/bin'
```

Linux服务器或已有独立测试库也可继续设置 `TEST_POSTGRES_URL=postgresql+psycopg://...` 后直接运行 `tests/test_postgres.py`；应指向专用测试数据库，不使用业务库。

## 验证项目

原先跳过的5项已通过，并补充场景幂等创建的真实并发竞争：

1. 数据集并发创建、任务单槽领取、租约回收和拒绝过期结果。
2. 管理列表JSON读取、索引幂等初始化、停用模型与发布的行锁竞争。
3. 场景创建请求的并发幂等，以及不同模型竞争同一请求编号时只有一个提交成功，另一个返回409。
4. 看板JSON聚合、小时边界和账户隔离。
5. 多Worker并发领取同一任务只有一个成功。
6. 告警归档 `SKIP LOCKED`、JSONB和保留期约束。

本次独立专项结果：6项通过、0项跳过；各轮结束后均无测试schema残留。

启用PostgreSQL后，将场景管理/规则、实时、Worker、传输、集中推理、终端、静态资源与GPU约束合并复测：**175项通过、0项跳过，52.63秒**。结果文件为 `test-results/postgres-enabled-regression.xml`；测试结束已关闭测试数据库，8000端口的API与Worker保持ready。
