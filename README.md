# AI-SOP 服务器视频分析平台

在现有工业监控页面和Python姿态分析原型上开发的首版Web应用。

**基本链路已实现：登录 → 上传视频 → 独立Worker分析 → Web回放、时间轴、证据及报告。** 当前识别人体与姿态规则，SOP保留“证据不足／待复核”，不输出未经验证的工序合格率。上传前可在“检测工位”旁选择离线分析姿态模型：RTMO-m 单阶段（默认，rtmlib）、RTMPose-m 两阶段或 YOLO11s-pose；服务端默认值由`POSE_BACKEND`决定，任务创建时记录所选权重哈希，模型变化会拒绝发布结果。

主页面视频分析栏可选择“实时摄像头检测”：浏览器授权电脑或USB摄像头后，由服务器检测采样帧并返回人体框、骨架和姿态。支持开始、停止、流畅预览/检测画面切换、5–60 fps目标设置（默认30），切回回放时释放摄像头；需要HTTPS或本机地址。实时检测默认使用RTMO-m单阶段多人姿态（rtmlib），服务启动后在后台预热，首帧无需等待模型编译；可用`REALTIME_BACKEND`切换`rtmo`/`rtmpose`/`yolo`。更新后重启`npm start`并刷新页面。实时检测不依赖离线Worker，不保存录像，详见运行说明。

支持选择灯具六工位、十一项检测需求。实时与离线共用版本化ROI、HSV、模板及专用YOLO配置；未配置项目显示“未配置／待复核”。现有权重只有人体姿态，专用资源与工艺证据须现场补齐。详见[灯具算法与配置说明](docs/station-algorithms.md)。

## 启动与部署

主页面也可选择“算法测试工位”，勾选举手、握拳／抓握候选、通用目标识别并调整测试顺序。实时画面默认带标注，支持紧凑显示。使用方法与算法边界见[算法测试说明](docs/algorithm-test.md)。

完整步骤见 [服务器运行说明](docs/server-quickstart.md)。生产部署采用FastAPI、PostgreSQL、GPU Worker、Nginx HTTPS及Docker Compose；本机开发可显式选择隔离的SQLite数据库。

```sh
# 先配置环境变量和数据库，详见运行说明
npm run server:init
npm start
# 第二个终端，使用相同环境变量
npm run server:worker
# 可先审计清理范围，再执行清理
npm run server:cleanup -- --dry-run
```

本机服务器默认入口：http://127.0.0.1:8000 。`npm run preview`保留原静态演示入口http://localhost:8080，与服务器分析模式不同。

已有本机 Python 依赖时，可运行 `npm run dev:local` 一次启动 API 和 Worker；登录信息保存在 `.runtime/local-settings.json`。该命令沿用已有本机数据库与登录配置，不覆盖分析记录。

## 综合优化（截至 2026-10-12）

- 上传显示百分比与传输大小，支持中途取消，文件限额与服务器配置同步。
- 任务历史显示视频名称，支持名称／编号搜索、状态筛选及分页；切换任务会取消旧查询。
- 摄像头检测遇到繁忙或短暂网络错误会自动有限重试，后台页面暂停发送采样帧。
- 回放按实际视频帧重绘，暂停时停止重绘；人员轨迹和时间轴建立索引缓存。
- 调整桌面与手机端布局、按钮触达、状态反馈和表格说明，默认工位支持直接上传。
- 任务与会话查询增加索引，本机 SQLite 启用 WAL；在原环境执行 `npm run server:init` 可为 v1 数据库补建索引，再重启服务。
- 实时工位与动作流程配置增加进程内快照缓存，按配置和资源文件签名自动失效；重复采样不再反复读取 JSON、执行 Pydantic 校验和计算资源哈希，资源变更仍会触发完整校验。
- Worker 增加可配置保留期清理（终态任务、孤儿上传、`.part` 文件）和 `server:cleanup -- --dry-run` 审计入口；实时 API 使用有界模型槽位池，满载快速返回 429。
- 任务状态支持带超时长轮询和游标分页；`/api/v1/metrics` 汇总队列等待、处理/推理 P95、显存、吞吐和失败原因；生产 Compose 可用 Nginx `X-Accel-Redirect` 直接传输大产物。
- 新增独立现场评测格式与 `evaluate:accuracy` 工具，按遮挡、光照、工位条件输出指标，明确区分 train/validation/test，禁止用测试集调阈值。
- 骨骼拓扑收拢为 `algorithms/skeleton.py` 单一事实来源（参考 ARecognition 的 skeleton_defs 做法）：证据图补齐头部连线，`analysis.json` 与实时接口下发拓扑，前端优先按服务端定义绘制，内置常量仅作静态预览兜底并有测试校验一致性。

改动与测试范围见 [综合优化验收记录](docs/optimization-2026-10-08.md)。

## 项目结构

- `backend/`：认证、上传、任务API、数据表、租约与独立Worker。
- `algorithms/`：视频校验、跟踪与姿态规则、分析流水线、报告导出。
- `app.js` / `server-ui.js`：原页面及服务器适配器、任务历史、异步状态与结果回放。
- `tools/analyze-workers.py`：兼容的离线命令行入口；默认输出到runtime-data，避免覆盖既有样例。
- `tests/`：API、算法与任务恢复测试；可选PostgreSQL并发测试。
- `tools/server-e2e.cjs`：真实GPU分析的浏览器端到端测试。
- `deploy/` / `compose.yaml`：服务器部署配置。
- `outputs/`：研究与架构文档、开发验收记录。

## 测试

时序与目标检测已增加推理优化：TCN 按层缓存历史特征，工位 YOLO 按同尺寸、同阈值 ROI 合批。本机逐帧时序中位耗时 3.58 → 1.18 ms，8 ROI 检测 54.65 → 18.71 ms；14 段已有时序回放的动作、阶段及 SOP 事件一致。具体测量范围、缓存开销及检测分数差异见 [推理优化记录](docs/inference-optimization-2026-10-10.md)，可运行 `npm run benchmark:inference -- --iterations 60` 复测。此次优化未重新训练，也不代表现场识别精度提升。

HA4M 四步装配实验已完成 V2.3 优化：重训 13 个因果模型并在验证集上枚举子集集成与滤波网格（新增 `evidence-decay-v3` 衰减证据滤波：候选短暂翻转时证据按帧衰减而非清零），冻结协议后重跑原测试集回归与全部 14 段浏览器回放。完整序列匹配保持 10/14，稳定帧准确率 83.53% → 84.09%，宏 F1 75.95% → 76.95%，分段 F1@0.5 74.14% → 81.48%，序列对齐后多余事件 4 → 3，匹配事件起点绝对偏移中位数 1.8 → 1.6 秒；帧准确率增量的描述性 bootstrap 区间包含 0，且这仍是此前已使用的测试人员，不是新外部验证。指标、事件对齐与逐段结果见 [V2.3 验收记录](docs/simple-sop-v23-results-2026-10-10.md)，此前结果见 [V2.2 记录](docs/simple-sop-v22-results-2026-10-10.md)。可用 `npm run test:scene` 和 `npm run test:scene-ui` 复测；后者需要已生成的 `work/simple-sop-2026-10-10/v2.3` 回放与 CLI 结果。

离线管线新增可选姿态后端 RTMPose / RTMO（rtmlib + ONNX Runtime，CLI 参数 `--pose-backend`，YOLO11s-pose 仍为默认），跟踪、规则与报告输出不变。安装方式、实现位置与同片对比结果见 [RTMPose/RTMO 后端记录](docs/rtmpose-rtmo-backend-2026-10-10.md)。

骨骼拓扑已统一到 `algorithms/skeleton.py`（COCO-17 与 MediaPipe Hands 21 点、可见度阈值、证据图与前端共用），判定逻辑与阈值数值未变；接入方式与测试见 [骨骼框架统一记录](docs/skeleton-framework-2026-10-12.md)。

```sh
npm run test:api
npm run test:e2e
# 自动启动独立预览服务，结束后关闭
npm test
```

`test:api`使用临时数据和测试替身验证编排；`test:e2e`使用已有YOLO11s-pose权重进行真实推理；`npm test`验证原静态界面、已有分析样例及模拟API的异常交互。测试截图写入 `.runtime`，不覆盖 `reference` 素材。PostgreSQL与Docker未在当前Windows环境实际运行，部署配置不能代替服务器验收。

## 当前范围

支持基础登录、限额上传、异步分析、取消、过期租约恢复、任务历史、授权结果下载和模型／源文件哈希。上传分析与浏览器生命周期分离，模型计算在服务器执行。

首版只提供一个配置管理员账户，采用轮询查询任务，尚未接入RTSP工业相机、PLC/MES、正式SOP、生产事件inbox/outbox或多角色管理。浏览器摄像头功能采用单帧实时采样，不提供跨帧身份跟踪。架构文档包含后续设计，不代表已全部实现。详细限制、故障处理和部署前检查见运行说明。

## 原型资料与素材来源

保留的界面复刻、示例视频来源、许可说明及历史验收记录见 [原型资料](docs/legacy-preview.md)。这些既有样例不是新上传视频的实时结果，界面明确区分演示和实际分析。
