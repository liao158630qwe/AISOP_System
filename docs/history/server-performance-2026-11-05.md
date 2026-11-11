# 服务器性能与可运行部署

先在目标服务器跑通登录、上传、独立 Worker 分析、回放和单路实时检测，再按实测结果扩展并发。保持单 API 进程、单离线 Worker；初始共享人体姿态池为1。六工位十五路仍是后续全量验收目标，单路通过不代表已经具备全线能力。

## 本次修复

- Compose 显式配置 YOLO 作为实时和离线默认后端，避免基础镜像缺少可选 RTMO 依赖导致离线任务不可用。
- `.env` 的后端、模型池、预热与保留期参数传入 API、Worker 和初始化服务。人体池默认1；该参数不是全局 GPU 并发上限，螺丝、贴条、场景和 Worker 仍可能竞争资源。
- API 镜像补齐 `screw-inspection.html/js/css`，避免注册页面在容器里缺失。
- Nginx 实时代理等待由30秒调到65秒，覆盖页面最长60秒模型准备；正式检测仍按实际时延验收。
- Compose 各服务日志轮转为10MB × 3；`.dockerignore` 排除 `.venv`、`.wheels`、`test-results` 及嵌套 Python 缓存，减少无关构建内容。

## 首次启动

在项目目录执行以下 Linux 命令。先准备匹配 PyTorch 2.8/CUDA 12.8 的 NVIDIA 驱动与容器 GPU 环境，再准备可信站点证书和数据库、登录密码。

```sh
nvidia-smi
docker version
docker compose version
test -f .env || cp .env.example .env
# 编辑 .env 中密码、DATABASE_URL；准备 deploy/certs/ 两个证书文件
docker compose config --quiet
docker compose up --build -d
docker compose ps
docker compose logs --tail=100 init api worker web
docker compose exec api python -m pip check
docker compose exec worker python -m pip check
docker compose exec web nginx -t
docker compose exec api python -c "import torch; assert torch.cuda.is_available(); print(torch.cuda.get_device_name(0))"
docker compose exec worker python -c "import torch; assert torch.cuda.is_available(); print(torch.cuda.get_device_name(0))"
```

`init` 正常完成后退出0属于成功；其余服务应持续运行。健康接口正常后，还必须从实际 HTTPS 入口验证登录与真实任务。单独检查 `nvidia-smi` 或健康接口不能证明模型业务可运行。

模型通过 `./models:/app/models:ro` 挂载。默认姿态需要 `yolo11s-pose.pt`；当前螺丝配置需要 `screw-head-v4-yolo26m.pt`、`screw-head-v4b-yolo26m.pt`、`screw-reference.jpg`、`screw-reference-overhead-v4.jpg`；贴条需要 `sticker-actions-v1.joblib`、`sticker-reference-v1.jpg`。启用手部或其他发布场景时，另核对对应模型。模型名单随配置版本变化，以配置和已发布场景为准。

RTMO/RTMPose 需要额外 rtmlib、匹配的 ONNX Runtime GPU 及预置权重，基础镜像不包含这些。首次部署使用 YOLO；切换后端视为单独变更，重新测准确性与时延。已排队任务冻结了旧后端和权重，切换不会改写历史任务，应新建任务验证。

`.env` 变更后执行 `docker compose up -d` 应用；仅 `restart` 不会更新容器环境。代码或依赖变更需要 `up --build -d`。升级前保存旧镜像、配置、模型哈希以及数据库和数据卷的一致备份，不删除持久卷。

## 保守的性能策略

API 进程内保存实时流与工序窗口，因此不要直接加 `uvicorn --workers` 或复制 API 容器。增加进程会拆散流状态并额外加载模型；后续需要先实现共享状态或明确的流路由与恢复策略。

继续使用已有单流单在途、忙时429、最新帧采样、模型缓存和 ROI 合批。429保护服务可用性，但持续拒绝仍表示该负载未满足容量要求。必须同时记录成功数、拒绝数、实际分析帧率和证据缺口。默认池1只作为上线起点，验证共享姿态池竞争后再评估2。

API 与离线 Worker 在启动阶段统一配置 `OPENCV_THREADS`，默认4，可设1–16且不超过系统报告的CPU数。初始化在依赖导入之后、预热和请求处理之前执行，避免依赖初始化覆盖设置；运行中不切换线程数，修改后重启服务。CPU受限的服务器从1开始对照测试。该设置只控制每个进程的OpenCV并行度，不是整个服务器的CPU或GPU总预算，也不改变独立训练子进程的线程策略。登录后可从 `/api/v1/metrics` 的 `inferenceRuntime.opencvThreads` 检查API实际值。

本机对照中，螺丝接口中位耗时从196.31ms降至126.40ms，贴条从391.96ms降至240.83ms；278帧的非诊断输出与最终判定一致。具体测试口径、混合负载及未解决的R05识别问题见 [推理线程优化验收](inference-runtime-2026-11-05.md)。

实时期间先不提交离线批量任务和训练任务；当前尚无跨进程 GPU 优先级调度。Worker 保持运行，以便处理已提交任务、续租和清理。实时空闲后安排离线分析；若需要实时与离线长期并行，以混合负载单独验收。贴条动作分类已迁移CUDA；配准、特征提取、视频处理等辅助阶段仍需服务器CPU，不能仅依据GPU利用率判断系统瓶颈。

先保留输入分辨率、模型权重和判定阈值。现场确认最短动作窗口后再调整采样率，不能用任意降帧或压缩小目标换取表面低延迟。暂缓 TensorRT、INT8、动态大批量、Redis任务系统和 Kubernetes；只有测量确认瓶颈、且有可复现回退路径时再引入。

## 测试与验收

本轮后端回归291通过、1跳过；跳过项需要独立PostgreSQL测试库。螺丝上传的隔离API/Worker真实模型浏览器验证通过，含连续上传、回放、错误反馈；人体真实模型上传与实时摄像头验证通过，页面异常0。螺丝样本是参考图组成的合成视频，人体摄像头由录制画面驱动，均用于验证运行链路，不代表现场精度。

部署配置完成YAML解析、环境变量连通性及镜像静态文件清单核对。当前Windows环境无Docker，尚未完成Linux镜像构建、PostgreSQL、Nginx HTTPS及目标服务器实际GPU验收。原始螺丝链路结果位于 `test-results/screw-upload-tests/run-e_qwwsl_/`；补充的三任务单池实测保存在 `test-results/server-performance-20261105/qa/runtime-smoke.json`。

上线依次验证：单路完整业务；人体/螺丝/贴条混合负载；逐步增加3、6、15路；72小时全负载；原方案30天试运行。每步记录硬件、镜像和模型版本、分辨率、每路频率、P50/P95/P99/最大时延、429/5xx、内存显存、队列与磁盘增长。15路验证仍需真实采集、终端、看板和录像同时工作。

正式端到端500ms要求按原方案冻结的起止点验收；冷启动、观察窗口和正式检测单列。通过有界拒绝保持进程存活，不等于十五路按所需频率持续检测。故障期间缺失的证据不能补成自动合格。

## 持续运行

同时备份PostgreSQL和analysis-data卷。实时窗口目前主要保存在API内存，进程重启应开启新轮次并复核中断周期；不能自动沿用旧通过状态。工位人工周期及已完成任务另有持久化，按各自记录恢复。

`RUN_RETENTION_SECONDS` 是离线任务产物保留策略，`UPLOAD_RETENTION_SECONDS` 是孤儿上传等清理策略，两者不提供相机连续录像。30天录像须独立建设NVR或录像存储。容器日志轮转也不替代业务审计保留要求。定期检查磁盘、证书有效期、Worker任务进展，备份必须做恢复演练。
