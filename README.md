# AI-SOP 视觉检测平台

提供人体与骨骼、螺丝安装、贴条工序和自定义场景检测，支持视频上传、实时摄像头、检测记录、模型管理与工业训练。六工位人工流程独立记录操作、证据和复核结果。

只有现场视频时，从 `/datasets` 上传视频、后台抽帧并在网页标注，审核生成数据版本后可直接进入 `/training`。当前支持目标检测和整图分类，具体采集要求及服务器配置见 [视频训练指南](docs/guides/video-training-workflow.md)。

六工位设备管理入口为 `/terminals`，工位机入口为 `/terminal`。支持固定工位凭据、USB/RTSP 采样与 GPU 集中推理，部署和性能边界见 [六工位接入指南](docs/guides/six-station-realtime.md)。

`/settings` 的「设备与部署配置」预填 6 个工位、6 路视频、6 台终端及配套服务器、4 台 PoE 交换机、核心交换机、NVR、三色灯、大屏和布线清单。管理员可填写网络地址、相机端口与 NVR 通道映射、终端安装位置及存储目标，并手动保存到服务器；同一管理账户在其他浏览器可读取。未提供的地址、相机型号、磁盘容量留空，内存原规格 `16G×64G` 待核对。配置支持 JSON 导出与版本冲突保护，详见 [设备设置说明](docs/guides/equipment-settings.md)。

六路实时推理支持有界专项模型池，同类螺丝/贴条工位可并发，保留每路独立的时序证据。优化内容、GPU 实测与未达标项见 [六路优化记录](docs/history/six-video-optimization-2026-11-07.md)。

当前工位主流程的服务端解码、定位、ROI 和动作特征已接入 CUDA，部署范围与准确性边界见 [CUDA 工位图像处理](docs/guides/cuda-station-vision.md)。

集中大屏入口为 `/board`，首页“集中大屏”可独立打开。提供六工位状态、当日统计、违规事件滚动、类型排行、小时/班次趋势、三画面轮播和底部告警提示。`/board?demo=1` 可预览模拟数据；正式数据使用现有管理员账户登录。统计口径与使用方式见 [集中大屏指南](docs/guides/central-board.md)。

## 启动

模型权重通过 Git LFS 管理。首次克隆前先安装 Git LFS 并执行 `git lfs install`；已有工作副本执行 `git lfs pull` 下载权重后再启动服务。

在项目根目录执行：

```powershell
npm run dev:local
```

也可双击 `启动本机服务.cmd`。主入口为 http://127.0.0.1:8000/，后台为 `/admin`，训练管理为 `/training`，按键设置为 `/settings`。启动器自动检查并启动 API 和 Worker，账户配置保存在 `.runtime/local-settings.json`。

```powershell
npm run status:local
npm run restart:local
```

首次配置、Python 环境选择和故障排查见 [本机运行指南](docs/guides/local-runtime.md)；生产部署见 [服务器运行指南](docs/guides/server-quickstart.md) 和 `.env.example`。

## 目录结构

```text
AISOP/
├── frontend/           页面、浏览器脚本与样式
├── backend/            API、认证、数据库、任务调度与 Worker
├── algorithms/         视觉推理、跟踪、工序规则与训练逻辑
├── config/             检测规则、工位配置与训练配置示例
├── models/             模型权重和参考图
├── assets/             公共图片、示例视频与演示分析素材
├── experiments/        可复现的研究实验代码
├── tests/              Python 自动化测试
├── tools/              启动、训练、评估和浏览器验证脚本
├── deploy/             Dockerfile 与 Nginx 配置
├── docs/
│   ├── guides/         使用、部署、配置与训练指南
│   ├── research/       算法研究、方案和文献调研
│   ├── history/        历次实现与验收记录
│   └── reference/      原型参考图、素材来源与截图
├── outputs/            模型训练、评估与研究产物
├── test-results/       浏览器验证报告、截图和隔离测试数据（忽略）
├── runtime-data/       服务上传、检测结果与业务数据（忽略）
├── .runtime/           本机账户、数据库、进程状态与日志（忽略）
├── .venv/              可选的本机 Python 环境（忽略）
├── compose.yaml        生产服务编排
├── requirements-*.txt  API、Worker 与测试依赖
├── package.json        统一命令入口
└── 启动本机服务.cmd    Windows 启动入口
```

前端文件集中存放在 `frontend/`，网页地址仍为 `/`、`/admin` 等原地址；API 与静态预览服务负责文件映射，部署镜像复制整个 `frontend/`。详细文件职责见 [前端说明](frontend/README.md)，脚本选择见 [工具说明](tools/README.md)，全部文档见 [文档索引](docs/README.md)。

服务默认在后台预热人体与贴条模型（`REALTIME_WARMUP=true`）。进入贴条工位后也会提前准备模型；首次准备未完成时，开始检测会等待准备完成再播放视频和建立检测轮次。准备过程不生成工位记录或累计动作，后续检测复用已加载模型；模型或标定更新后重新准备。

实时检测默认连续 60 秒未检测到目标、或单轮达到 600 秒时自动停止。可通过 `REALTIME_NO_TARGET_SECONDS` 和 `REALTIME_MAX_DURATION_SECONDS` 调整（均为 1–86400 秒，重启服务生效）。服务端按实际经过时间检查，持续发送视频也不会续期；螺丝/贴条以当前帧工件定位、场景以当前规则检出目标、人体模式以当前人体检出为准，历史累计结果不重置无目标计时。工位超时记录为中断并保留原因，采集端停止且需手动开始下一轮；首页也会在最长时限关闭摄像头，网络或画面中断不会无限采样。上传视频的离线分析不受这两个实时限制影响。

本机启动也可在 `.runtime/local-settings.json` 的 `inference` 中设置这两个参数；生效值可从已登录的 `/api/v1/metrics` 的 `realtimeLimits` 查看。

首页“服务器 GPU 利用率”从 API 所在服务器读取 NVIDIA 驱动指标，按推理设备 UUID 匹配显卡，适用于 Windows/Linux NVIDIA 服务器，与访问网页的电脑无关。数值表示该卡全部进程在驱动最近采样窗口内执行 GPU 内核的时间比例，不是显存占比或单个检测任务的占用。页面约每秒刷新，悬停可查看显卡、来源和采样时间；超过3秒未取得有效新数据时显示“—”。

## 常用验证

```powershell
npm test                    # Python API、算法、任务和页面资源回归
npm run test:admin           # 模型/场景管理逻辑
npm run test:realtime-ui     # 实时传输浏览器回归
npm run test:trigger-ui      # 检测按键与设置页面回归
npm run test:pilot-ui        # 六工位人工流程浏览器回归
npm run test:upload          # 隔离服务中的真实模型上传链路
```

浏览器测试需要 Playwright 和相应浏览器；真实推理测试还需要权重及推理环境。PostgreSQL 测试需要独立测试库。`npm run preview:legacy` 保留早期静态演示，默认端口为 8080。

## 数据与维护约定

数据库索引、告警录像自动归档和至少30天保留期见 [数据库与存储指南](docs/guides/database-storage.md)，实测结果见 [优化验收记录](docs/history/database-storage-2026-11-06.md)。首页“告警录像”可查看和播放已归档录像。

数据库 v2 增加账户关联外键、业务约束和原子迁移，支持只读预检；本机重启在需要升级时先备份再迁移。上线和恢复步骤见 [数据库架构升级指南](docs/guides/database-architecture.md)。

- 新页面、脚本和样式放在 `frontend/`；新增页面需同步 `backend/app.py` 的静态资源白名单。
- 使用指南放在 `docs/guides/`，研究方案放在 `docs/research/`，注明日期的验收记录放在 `docs/history/`。
- 训练与评估结果放在 `outputs/`，浏览器验证产物统一写入 `test-results/`。
- `outputs/` 中部分训练元数据由 `config/` 和算法代码引用，不能当作普通缓存整目录删除。模型权重、参考图、训练数据和业务数据库均按有效资产保留。
- 缓存、安装包下载、一次性补丁脚本和临时构建不应提交。`Ultralytics/` 是工具自动生成的本机配置目录，已忽略。

识别结果限于视觉与已配置工序规则；人工流程、历史样例和模型推理的适用范围分别见对应指南。原有完整说明保存在 [整理前的项目记录](docs/history/project-notes-before-cleanup.md)，本次移动与删除范围见 [结构整理记录](docs/history/project-structure-cleanup.md)。
