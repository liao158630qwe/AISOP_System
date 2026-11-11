# 8000 统一入口与本机服务恢复

主界面、后台、视频产物和 `/api/v1` 接口由同一 FastAPI 服务在 `http://127.0.0.1:8000` 提供。前端使用同源相对地址，不需要单独启动 8080 页面服务。API 负责交互和实时采样，Worker 独立处理上传视频任务。

```powershell
npm start
npm run status:local
# 更新代码、模型配置或恢复不健康服务时
npm run restart:local
```

`npm start`、`npm run dev:local`、`npm run preview` 共用本机启动器，使用已有 `.runtime/local-settings.json`、`.runtime/local.db` 和 `.runtime/local-data`。启动前验证依赖并只读预检数据库；已有健康 API 会校验登录及数据目录，Worker 缺失时仅补启 Worker。API 缺失而 Worker 正常时补启 API。重复执行不会重复创建服务。

数据库有待执行迁移时，运行 `npm run restart:local`：预检通过后停止本项目服务，将数据库一致性备份保存到 `.runtime/database-backups/`，再升级并启动。预检失败时保留运行中的服务，备份或迁移失败时停止启动流程。详细步骤见 [数据库 v2 升级指南](database-architecture.md)。

所有启动与重启命令共用操作系统文件锁，避免同时点击生成多组服务。服务归属由实际进程命令和项目工作目录确认，停止时还使用进程创建时间防止 PID 复用；不会依据保存的旧 PID 直接关闭程序。8000 被其他程序占用、服务使用其他数据库或 API 不健康时，命令会明确报错。启动失败只关闭本次创建的子进程。

`status:local` 不创建账户或改变服务，输出 API 健康检查、API 和 Worker 的 PID；缺失任一服务时返回非零退出码。Worker 检查表示进程存活，任务处理是否正常还需要结合队列状态及 `.runtime/local-worker.log` 判断。

双击 `启动本机服务.cmd` 或 `.bat` 等同于 `restart:local`。重启会中断正在处理的视频，未完成任务按照 Worker 租约恢复规则重试。更新代码后应重启两项服务并刷新浏览器。

日志保存在 `.runtime/local-api.log` 和 `.runtime/local-worker.log`。`.runtime/local-processes.json` 记录项目目录、服务 PID 和创建时间，供诊断使用；其中不保存密码。

如果需要使用自己设置的数据库、用户名和环境变量，继续使用 `npm run server:start` 与 `npm run server:worker` 分别启动 API 和 Worker。本机一键命令明确使用上述本机 SQLite 配置。`npm run preview:legacy` 只用于历史静态演示，不提供登录、上传和实际分析接口。

生产部署继续由 Nginx 提供 HTTPS，内部 API 仍使用 8000；Nginx 复用上游连接，实时帧请求关闭缓冲，上传与长轮询维持独立超时配置。本机单服务验证不代表 15 路现场并发、72 小时稳定性或 500 ms 端到端指标已经验收。

启动器回归：`node tools/python.cjs -m pytest tests/test_local_runtime.py -q`。测试使用临时配置和进程替身，覆盖误占端口、进程归属、服务补启、失败清理、依赖失败、数据配置不一致及并发锁。
