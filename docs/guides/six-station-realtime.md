# 六工位设备接入与集中推理

当前已实现六工位固定身份、USB 浏览器采样、RTSP/USB 常驻采集端、服务器公平调度、结果隔离和实时状态页。入口为 `/terminals`（管理员）及 `/terminal`（工位机）。管理后台导航可直接进入设备管理。

首页工位1–6的人工工件登记目前默认暂停（`PILOT_REGISTRATION_ENABLED=false`）。选择工位后可在原界面直接按F8启动检测，无需产品型号、工件号或跳转页面。未配置工位可在右侧选择检测模型并保存；已有AI绑定沿用设备配置。摄像头、视频和图片共用工位流接口，结果在原页面显示，结束后按检测轮次归档并同步大屏。视频结束自动归档，重复F8不会重复启动；记录保存 `inputSource` 区分相机和本地素材。原人工新建及返工接口仍拦截，`/terminal` 入口保留。恢复人工登记的方法见[人工流程说明](station-one-pilot.md)。

这版交付不代表六路现场性能已验收。工位调度的代码默认全局串行，最多支持六个工位并发；双路至强＋RTX4090 的候选配置采用单 API、全局并发上限 3，接入六路并不要求同时执行六次推理。姿态按人体模型池容量调度；螺丝和贴条通过 `SPECIALIST_POOL_SIZE` 配置各自的独立推理实例，支持同类工位同时运行。同一工位保持串行，各工位最多排队一张最新帧，最多六张。本轮场景并发改造采用不同 `model_key` 可并行、同一模型实例互斥的有界执行路径；场景容量取 `min(STATION_INFERENCE_CONCURRENCY, REALTIME_POOL_SIZE, SCENE_MODEL_CACHE_SIZE)`，默认缓存 1 个模型时仍限制为 1。同模型跨流合批、人体检测降频、TensorRT、原始 60fps 连续录像、媒体网关以及一次解码后直接送模型尚未实现；常驻采集端仍通过 JPEG HTTP 入口送入集中推理服务。

管理员在首页选中工位后，点击“告警录像”右侧的“工位配置”，即可在弹窗中刷新配置、查看绑定信息与设备凭据，或点击“修改检测配置”切换螺丝、贴条、人体姿态、已发布的自定义场景，并调整目标采样率。编辑器会载入当前配置；修改后需保存或取消，关闭弹窗再开始下一轮。仅改变 `sampleFps` / `maxSampleGapSeconds` 时保留设备凭据和当前终端登录，revision 递增，新轮检测使用新参数；改变设备、模型、接入方式等其他配置仍重新签发凭据，外部采集端须同步更新。检测中先结束或停止本轮，再修改配置。原有检测记录仍可在历史记录中查询。

首页的“画面显示”可切换检测采样与识别框、实时视频（无识别框）。识别框始终绘制在对应采样快照上；实时视频模式的文字判定来自最近有效采样。页脚分别显示视频预览帧率和近 10 秒有效检测帧率，运行详情显示编码、待送、请求耗时及丢弃计数。图片联调或浏览器不支持视频帧计数时，视频帧率显示“—”。

首页页面采集的即时结果从浏览器采图时刻起最多显示 1 秒，耗时已超过 1 秒的响应不会恢复即时判定或触发自动归档；计时不含相机曝光和驱动缓存。切后台、断网、视频暂停或相机静音会使当前结果和待送帧失效，恢复后等待新采样。停止与归档先停新采样，再等待编码和上传收尾；归档成功身份核对后才释放流，失败可在原轮重试。正式模式新增首页常驻采集器逐件控制，页面刷新、关闭或切换工位不停止后台采样；详见[正式后台采集与逐件控制](controlled-station-collection.md)。独立 `/terminal` 的浏览器采集行为保持原有边界。

## 1. 启动与数据库

本机在项目根目录运行：

```powershell
npm run dev:local
```

启动器会执行幂等的数据库初始化。生产环境在配置好 `.env` 后，通过既有 `init` 服务执行 `python -m backend.init_db`，再启动一个 API 进程。新增 `station_terminals`、`terminal_sessions`、`station_stream_leases`、`station_inspections` 四张表，不删除既有业务记录。最后一张按工位和检测轮次保存设备、模型版本、结束结果或中断原因。旧版工件关联列保留以兼容历史数据库，新检测不写入该列。生产数据库按已有运维流程备份。

**当前必须使用一个 API worker、一个 API 实例。** 推理时序状态、调度队列和即时结果保存在进程内；SQL 租约用于防止旧生产者回写，不能代替跨进程状态服务。不要通过增加 `uvicorn --workers` 扩展六路。

可配置项：

| 参数 | 默认 | 说明 |
| --- | ---: | --- |
| `STATION_QUEUE_WAIT_MS` | 500 | 50–5000ms；超过排队时限的采样返回 429 |
| `STATION_INFERENCE_CONCURRENCY` | 1 | 1–6；工位全局并发上限，实际还受人体模型池容量限制 |
| `SPECIALIST_POOL_SIZE` | 1 | 1–6；螺丝、贴条分别可用的模型实例数；各实例延迟加载，流历史独立于实例保存 |
| `SCENE_MODEL_CACHE_SIZE` | 1 | 1–6；场景模型驻留缓存上限，也限制可同时执行的不同模型数量；需确认显存预算，忙碌模型不被驱逐 |
| `REALTIME_POOL_SIZE` | 沿用原配置 | 人体模型池，合法范围 1–6；螺丝、贴条也使用该池 |
| `OPENCV_THREADS` | 沿用原配置 | OpenCV CPU 算子线程数；双路服务器建议先以 4 起测，再按实际端到端表现调整 |
| `TORCH_THREADS` | 4 | 每进程 PyTorch CPU 算子线程数，1–16；inter-op 固定为1；不等于 GPU 并发数 |
| `WORKER_ANALYSIS_ENABLED` | true | false 时 Worker 不领取离线视频分析任务，仍执行心跳与清理 |
| `WORKER_TRAINING_ENABLED` | true | false 时 Worker 不启动训练调度；已排队训练等待启用的 Worker |

`STATION_QUEUE_WAIT_MS` 从服务器接收请求起算，包含上传、鉴权、队列与推理前校验；在调度及模型调用前检查，过期帧返回429。`queueWaitMs` 仍只表示调度队列等待，`serverProcessingMs` 包含证据保存。模型一旦开始运行不会被期限强行中断，故此参数不能保证完整响应在500ms以内。

模型应先在维护窗口预热并固定版本。模型文件/规则变化会中止现有流；固定姿态模型更新后重启 API，避免复用旧的常驻权重。准备耗时单列，不应计入已预热吞吐。单帧处理超过 30 秒会失去租约，需要重新开流。

工位调度的实际总并发为 `min(STATION_INFERENCE_CONCURRENCY, REALTIME_POOL_SIZE)`，避免多个专项工位耗尽共享人体槽位；通用实时入口的额外请求仍可能争用容量并返回429。当前专项使用 CUDA 图像定位/特征处理及相应的 YOLO/分类模型，各实例持有独立推理状态；早期 CPU SIFT 剖析不代表当前链路。流的跟踪、动作历史和结束记录保存在公共注册表中，帧切换实例不会重置本轮证据。实例只在首次分配任务时加载权重；增加池大小会多驻留模型副本，需同时检查显存。CUDA 迁移范围和部署资产见 [CUDA 工位视觉说明](cuda-station-vision.md)。

工位 `/api/v1/station-streams/{id}/frames` 与通用 `/api/v1/realtime/frame` 共用 `RealtimeTransport`，同时检查模型类别容量和所有类别 `active` 之和。总准入上限同样为 `min(STATION_INFERENCE_CONCURRENCY, REALTIME_POOL_SIZE)`，因此通用入口不能绕开工位总预算；这两个配置也会限制只使用通用实时入口的客户端。`/api/v1/metrics` 的 `realtimeTransport.capacity` / `active` 分别显示共享上限和当前占用。工位队列保留公平调度，通用入口忙碌时返回 429，由客户端发送下一张新采样。

贴条保持原定位分辨率、校准权重和判定阈值，多个位置的特征、分类校验及观测结果成批处理；`warp_regions` 按原坐标和插值规则仅生成需要的锚点搜索块、目标块，并缓存坐标网格，减少整幅对齐图的重复计算。此处是同一帧多个位置的批处理，同模型跨工位合批尚未实现。螺丝按各位置所选模型批量推理，复用同一帧的低阈值候选，避免对专项位置先跑通用模型再覆盖。

`/api/v1/metrics` 的 `specialistPools` 返回容量、空闲和已加载数；工位调度指标包含 `groupLimits` 和 `activeGroups`。贴条 `inspection.timingsMs` 列出定位、特征、分类与输出处理时间，`serviceTimingsMs` 分开记录推理前校验、模型、推理后校验和取证耗时，页面运行详情显示采图、JPEG 编码与 HTTP 往返。服务器处理已包含在往返中，不能重复相加。最新 [帧率与 GPU 优化记录](../history/throughput-optimization-2026-11-07.md) 包含 242 帧交替对照、fixed/serial API 压测和真实浏览器回放；较早的 [六路优化记录](../history/six-video-optimization-2026-11-07.md) 保留为历史证据，不能仅用其中旧 SIFT 结果判断当前瓶颈。

RTX4090 六路生产采样可从 `deploy/rtx4090-six-stations.env.example` 的候选值开始验收。该配置关闭 Worker 的离线分析与训练领取，保留心跳、清理及 CPU 数据集抽帧；维护窗口再启用并重建 Worker 容器。开关在进程启动时生效，不会抢占已运行任务，也不限制手工启动的其他 GPU 进程。已有实测与未达标项见 [服务器整体审查](../history/server-review-2026-11-07.md)。

### 六台工位机接入双路至强＋RTX4090 的建议起点

六台工位机分别负责相机采集、解码和 JPEG 编码，通过各自固定身份向中央服务器发送最新帧；模型集中在服务器 GPU 上执行。每路保持一个在途请求及有界的最新帧缓存，过时帧丢弃，不能靠积压旧视频提高吞吐数字。持续生产可优先采用本指南的常驻采集端，页面用于核对画面、查看结果和明确结束检测轮次。

先以“六路接入、全局最多三路执行”作为起测配置：一个 API 实例、一个 Uvicorn worker，`STATION_INFERENCE_CONCURRENCY=3`、`REALTIME_POOL_SIZE=3`、`SPECIALIST_POOL_SIZE=3`。六路模型可能不同，目前配方未定；不预设三路螺丝加三路贴条，也不要把 `mixed` 压测的排列当作生产配方。模型组和驻留缓存会进一步限制实际并发，尤其样例中的 `SCENE_MODEL_CACHE_SIZE=1` 仍只能同时执行一个场景模型。专项实例延迟加载，实际显存和执行容量应以真实任务组合测量。

本轮场景执行路径区分“驻留多少模型”和“同时执行多少模型”：缓存按 `SCENE_MODEL_CACHE_SIZE` 有界，场景执行并发取上述三个参数的最小值。`model_key` 对应实际缓存实例，包括所属用户、模型身份、权重版本、路径、任务类型和设备；不同场景使用同一缓存实例时仍互斥。不同实例可在全局预算内并发，同工位保持顺序，待处理帧继续公平调度。正在加载或执行的模型不得被 LRU 驱逐；缓存没有可用槽位时返回忙碌，不能临时无界加载。此处是模型间并行执行，不是把不同模型放入一个 batch。

本机 RTX 5070 Ti 的两模型 CUDA smoke 已确认独立工作线程和 CUDA 流，6/6 次结果的非计时字段与改造前一致，见[实测报告](../../test-results/six-workstation-4090-prep-20261107/scene-cuda-smoke.json)，这仅验证该样本下的运行与输出一致性；通用场景仍调用 Ultralytics `model.predict`，当前安装版本的 `Profile.time` 会调用 `self.accelerator.synchronize(self.device)`，本轮未替换其预处理、后处理或全设备计时同步，因此独立线程和 CUDA 流不能证明 GPU 内核全程重叠，也不构成容量或准确率验收，此限制不应套用到已有独立实现的专项 `cuda_yolo` 路径。

部署前先确认每路模型和输入规格，逐模型测量冷启动、预热后耗时、显存及目标帧率，再依据完整组合设置驻留缓存预算。默认缓存 1 时，不同模型轮流请求可能反复驱逐、重载；若六个模型同时驻留的实测显存预算允许，才考虑设为 6。在全局/人体池上限均为 3 时，缓存 6 对应最多驻留 6、最多执行 3，并不等于六模型同时推理。需要验证同模型互斥、跨模型并行、异常/取消后的槽位释放、模型版本与租约校验、结束归档，再进行真实六机端到端测量。源码实现和 CPU 并发测试不能证明 RTX4090 已达到六路各 10 fps；同模型跨流合批仍未实现，不同权重或输入/输出契约也不能直接合成同一个推理 batch。

用户确定的最终验收目标是六路各自有效 **10 fps**，合计 **60 fps**；每路目标采样周期为 100 ms。每路 4 fps（合计 24）和 6 fps（合计 36）只作为逐级调试与定位容量拐点的阶段，不降低最终 10 fps 要求。每一路都需达到有效帧率，不能用六路总吞吐或均值掩盖慢工位，也不能仅把绑定 `sampleFps` 设为 10 就宣布完成。响应 P95、结果年龄及最大允许无结果间隔还需依据工序另行定义；100 ms 采样周期不自动等于已约定的响应 P95 限额。工具默认 95% 的 FPS 通过容差只能作为压测判据，不能替代用户的正式 10 fps 验收要求。

本轮代码范围、226 项相关回归、两模型真实 CUDA 对照及未部署边界见[六台工位机优化记录](../history/six-workstation-pipeline-2026-11-07.md)。

后续整体测试已完成 457 帧人体/专项重叠等价检查和 337 项合并回归，详见[系统吞吐优化记录](../history/system-throughput-2026-11-07.md)。本机 RTX 5070 Ti 的 pose/螺丝/贴条各两路、30 秒重复 JPEG 测试，在同样 3/3/2 并发/人体池/专项池下，基线合计 27.333 fps、候选 28.834 fps；候选 4 并发为 32.266 fps，6 并发为 35.700 fps，但最慢一路仅 3.233 fps。没有达到六路各 10 fps。6 并发队列 P95 已为 0，专项仍耗时较长，应继续剖析实际模型并发服务内部；不能仅继续扩大排队和副本数。

螺丝与贴条每帧在同一解码图上并行人员检测和专项检查，两者成功后再更新工序证据。`timingsMs.person` 此时是专项完成后的剩余等待，完整人员分支耗时见 `personProcessingMs`，不得与旧版串行 person 阶段直接比较。通用场景的计时同步替换实验没有收益，仍保留原执行路径。浏览器和六路 API 的测量范围、窗口与背景 GPU 负载限制均见上述报告。

双路至强和 RTX4090 的型号不能保证六个尚未确定的模型达到上述目标，现有单路贴条浏览器实测及本机其他 GPU 数据也不能替代六机目标服务器验收。应在确认模型后逐级测量并定位计算、重载、串行锁或采集瓶颈；是否需要多 GPU 以这些实测结果为依据，当前不作硬件扩容承诺或购买建议。

`OPENCV_THREADS=4`、`TORCH_THREADS=4` 作为 CPU 线程起点，PyTorch inter-op 保持 1。不要因双路至强具有很多核心就把线程池直接开到 40；同时记录采集端编码、服务 CPU 时间、GPU 利用率和结果延时，再逐项调整。NUMA 绑核/绑内存需在目标机核对 CPU、GPU 和网卡拓扑并完成对照测试后再决定，本候选配置不预设绑核方案。生产期间保留已实现的离线分析/训练禁领开关。同模型跨流合批和人体检测降频属于后续方案，当前不能计为已完成优化。

## 2. 登记六台工位机

1. 管理员打开 `/terminals`，为 `station-1` 到 `station-6` 逐一保存绑定。例如工位 1 使用 `pc-01`、`cam-01`，工位 6 使用 `pc-06`、`cam-06`。
2. 选择接入方式、已验证的检测任务、目标 FPS 和最大允许有效采样间隔。调试可以从每路 4 fps、6 fps 逐级推进；本次部署最终要求每路有效 10 fps，需在目标 GPU 和真实六机采集链路验收。
3. 保存后页面只显示一次设备凭据，将它交给对应工位机或对应常驻采集服务。服务器仅保存凭据摘要。
4. 每台工位机打开 `/terminal` 输入自己的凭据。服务器返回固定工位，不依赖 URL、相机数组顺序、浏览器编号或客户端自报工位决定权限。
5. 更新绑定必须使用当前版本号。仅调目标 FPS 或最大采样间隔时，保留原凭据及当前登录，新版本对下轮检测生效；其他配置变化会轮换凭据并使旧会话失效。配置未变化时保留版本和原凭据。只需换密钥时点击“重新签发凭据”。有有效采集流时拒绝变更和重签，应先停止采集；单纯暂停发帧不等于停止。
6. 刷新状态或切换工位会保留尚未保存的配置草稿。出现版本冲突时，核对修改后点击“重新载入配置”；该按钮会丢弃当前工位草稿。凭据标注所属工位，可复制；退出管理会话时立即从页面清除。

采样参数修改后，已打开的独立 `/terminal` 页面需刷新以重新读取配置，登录仍可保留。常驻采集器在启动时读取绑定，需停止并重新启动对应进程；显式 `--fps` 是本机上限，例如仍传 `--fps 4` 时，绑定提高到 6 也只采 4 fps。省略 `--fps` 可在该次启动时使用服务器绑定值，当前没有运行中热更新采样参数。

工位机/相机逻辑编号不可重复。逻辑绑定不等于物理设备认证：现场仍需核对镜头画面、接线和机位；浏览器 USB `deviceId` 仅用于本机选相机，设备更换或浏览器清理后可能需要重选。RTSP 地址只配置在采集服务，不下发到操作员网页。

六个工位默认均为“人工检查”，不会自动套用螺丝模型。螺丝、贴条专项仅适用于已标定的相机视角和工装；自定义场景从下拉列表选择已发布且模型可用的场景，服务端保存时也会校验。人体姿态观察不代表工序通过。六工位完整算法需按各自工艺与现场样本单独验证。状态卡分别说明未登记、停用、人工检查、等待首帧、采集离线、结果过期，以及运行满 10 秒后的帧率不足和本轮采样缺口。

## 3. USB 浏览器接入

终端登录后点击“授权摄像头”，核对设备，再点击“开始检测”。默认按工位独立检测，无需工件号或人工建单。工件选择入口、查询和关联校验已移除，不需要附加 URL 参数。检测结果直接同步大屏。远程工位机使用 HTTPS；本机开发可访问 `http://127.0.0.1:8000/terminal`。

完成本轮时点击“结束并归档”：停止本机采样、等待在途请求、提交最终算法结果到数据库，再释放采集流。归档失败可以使用同一轮重试，不能继续添加采样；归档成功而释放失败时，保留归档结果，并通过“停止采集”重试释放。归档后可直接开始下一轮检测。

异常、换班或调试结束时，选择停止原因再点击“停止采集”。未经正常归档的轮次保存为“中断 · 待复核”，不复用最后一帧 PASS 作为最终结果。终端下方展示本工位最近 20 轮，展开可查看结果、原因、检测轮次和设备版本；刷新或服务重启后仍可查询。

首页工位面板按目标时间间隔独立采图/编码，在上一请求进行时准备下一帧；最多一个上传请求和一张最新待送帧，采图至待送超过 500 ms 就丢弃。结果叠加使用对应采样的 bitmap，停止/归档先停止采图，再等在途请求结束。独立 `/terminal` 页仍按单请求完成后安排下一次采样。JPEG 不超过 1MiB；高像素小零件需结合现场 ROI 和清晰度验证，不能仅靠缩图保证识别。

贴条和自定义场景按长边1920、总像素1920×1080限制，支持保留1080×1920竖屏输入；浏览器、常驻采集器和压测器采用同一像素边界。螺丝仍保持宽1920、高1080以内，姿态仍保持宽1280、高720以内。更高细节可能增加编码和推理时间，需连同有效采样率评测。

停止失败时会保留流编号，可再次点击“停止采集”，不允许误开新流。页面进入后台会暂停采样；恢复时等待新帧，缺口可能使本轮判定转为复核。摄像头断开、身份不匹配、状态查询超时都会清除旧判定。页面退出未能释放的流在 30 秒无有效续租后可重新建立。

浏览器采集依赖页面持续运行。生产长时间采集使用下一节带 `--controlled` 的常驻进程，并在首页选择“后台采集（正式）”；该模式刷新页面不会停止采集，详见[正式后台采集与逐件控制](controlled-station-collection.md)。

## 4. RTSP / USB 常驻采集

一台相机对应一个进程。网络相机可由服务器或采集主机运行六个进程；USB 相机在对应工位机运行。需要现有 Python 环境、OpenCV/FFmpeg 和可达的 API，不需要在工位机加载推理模型。

PowerShell 示例（占位值需替换，在受限的服务环境配置中保存真实值）：

```powershell
$env:AISOP_TERMINAL_KEY = '<本工位设备凭据>'
$env:AISOP_CAMERA_SOURCE = '<现场确认的完整RTSP地址>'
# USB 调试时可将 source 设为本机设备索引，如 '0'
# 省略 --fps，启动时读取管理员配置的目标采样率。
node tools/python.cjs tools/station-collector.py --controlled --server https://aisop.example.internal --report runtime-data/station-1-collector.json
```

六进程可分别用 `--key-env AISOP_TERMINAL_KEY_1 --source-env AISOP_CAMERA_SOURCE_1` 至 `_6` 指定不同环境变量。使用现有系统服务管理器启动，配置工作目录、私有环境文件、异常退出后退避重启、日志轮转。程序前台运行，退出码 0 为正常结束、2 为配置/采集故障、3 为需要人工复核的检测流错误。不要将凭据直接放进命令行。

采集在子进程中解码，队列最多一张编码帧；500ms 以上的积压帧被丢弃，HTTP 单次超时 15 秒。`--controlled` 模式在空闲时不创建轮次，收到首页开始命令才送检；服务重启、相机断开或重连会中断原轮，恢复后须显式开始下一件。省略 `--controlled` 仍保留旧版自动连续开流的兼容行为。已移除 `--cycle-id` 参数。

本轮常驻采集器复用同源 HTTP/1.1 持久连接，成功和错误响应都先读完再复用；同一客户端实例的请求串行发送。连接关闭或中断后，下次调用可以重建连接，但不会自动重发已经提交的 POST，避免服务器已接受而响应丢失时重复提交。帧失败后继续发送更新的采样及序号；设备凭据不会随重定向发送到其他地址。连接复用减少重复建连开销，实际网络收益仍需在六台工位机上测量。

`--duration 60` 可运行 60 秒检查采集链路，`--report` 写提交、完成、拒绝、丢弃和重连计数。应用日志不主动打印源地址和密钥，但本机相机驱动/原生 FFmpeg 可能另行记录，服务日志应限制访问。USB 数字索引在拔插后需要重新核对。

常驻采集器的定时退出、信号退出或故障退出会中断未归档轮次，不能据此宣布本轮检测完成。首页“后台采集（正式）”提供逐件开始、结束归档和中断控制；命令持久保存，采集器在停止送帧、完成 `finish` 与 `DELETE` 后才回执。独立网络终端页继续提供预览、实时判定和检测记录查询，其采集/结束按钮仍用于 USB 浏览器采集。

网络终端默认显示服务器最新成功检测的采样画面，按工位鉴权，并核对流 ID/代次/帧号；过期画面立即隐藏。这是低帧率的“检测采样预览”，用于核对相机与判定，不是原始视频。如果需要流畅预览，可配置同源 `/media/...` 地址并另行部署、鉴权媒体网关。本项目目前不提供 RTSP 转 WebRTC/HLS，也不提供原始 60fps 连续录像；既有告警采样证据不等于全量回溯录像。

## 5. 协议与恢复

| 接口 | 行为 |
| --- | --- |
| `POST /api/v1/terminal/login` | 设备凭据换取 HttpOnly 会话及 CSRF token |
| `GET /api/v1/terminal/context` | 读取固定绑定 |
| `GET /api/v1/stations/{station}/context` | 只读取本工位设备绑定 |
| `GET /api/v1/stations/{station}/collector` | 读取受控采集器、当前轮次、命令及执行状态；页面刷新只恢复观察 |
| `POST /api/v1/stations/{station}/collector/commands` | 持久提交 UUID 标识的 `start`、`finish` 或 `stop` 命令，每工位一个待执行命令 |
| `GET /api/v1/stations/{station}/collector/commands/{requestId}` | 精确核对原控制请求，不能用其他标签页的最近命令代替 |
| `POST /api/v1/stations/{station}/collector/heartbeat` | 设备凭据专用；采集实例心跳和故障状态 |
| `POST /api/v1/stations/{station}/collector/commands/{requestId}/ack` | 设备凭据专用；服务器验证实际轮次状态及释放完成后接受执行回执 |
| `GET /api/v1/stations/{station}/inspections` | 按工位查询持久检测记录；`limit` 为 1–100，`offset` 分页；不再接受 `cycle_id` |
| `GET /api/v1/station-streams/{id}/inspection` | 精确读取轮次、服务器实际结论、`reviewAllowed` 和当前认证的 `reviewActor`，用于断线后的确认核对 |
| `POST /api/v1/station-streams/{id}/manual-review` | 已归档的 FAIL/REVIEW 装配轮次人工合规/不合规确认；UUID 幂等，保留算法原判定，拒绝运行中、中断和纯姿态轮次 |
| `POST /api/v1/admin/terminals/{station}/credential` | 传入当前 `revision`，单独轮换凭据；有有效采集流时拒绝 |
| `POST /api/v1/station-streams` | `requestId` UUID 幂等开流，可选 `replaces` 替换原流；终端工位由服务端确定；不再接受 `cycleId` |
| `POST /api/v1/station-streams/{id}/frames` | JPEG；帧序号、相对时间、流代次必填 |
| `DELETE /api/v1/station-streams/{id}` | 可传 JSON `reason`；未归档则保存中断原因并释放，已归档则保留结果；在途采样返回 429 |
| `POST /api/v1/station-streams/{id}/finish` | 持久归档本轮算法判定，成功返回 `archived=true`；同流重试可跨重启返回原结果，之后需 DELETE |
| `GET /api/v1/stations/{station}/live-status` | 当前有效结果、近 10 秒有效 FPS、结果年龄 |
| `GET /api/v1/stations/{station}/preview?stream_id=...&frame_seq=...` | 当前有效检测采样 JPEG；只允许所属工位，旧帧返回 409、过期返回 404 |
| `GET /api/v1/admin/station-metrics` | 六工位状态、排队和处理延迟、替换/过期/失败计数 |

采集端用 `X-Terminal-Key`；浏览器用会话 cookie 及 `X-CSRF-Token`。帧头为 `X-Frame-Seq`、`X-Frame-Time`（0–86400 秒、严格递增）、`X-Stream-Epoch`，可附 `X-Station-Id`、`X-Camera-Id` 进行一致性检查。服务器返回工位、工位机、相机、绑定版本、流 ID、流代次、帧号、模型版本。流应在 24 小时内计划换班重建。

每路只保留一张待处理新帧，更新帧替换旧帧不改变工位排队位置。被替换的请求返回 409，排队超时返回 429；生产客户端始终下一次提交新采样，不补传旧帧。HTTP 客户端取消不提前释放仍在运行的 GPU 工作。样本时间或服务器接收间隔超限会累积采样缺口，PASS 转为 REVIEW。

`live-status` 的结果年龄从服务器收到 JPEG 请求时开始计算，状态页另保守计入查询耗时，不包含相机曝光、RTSP 缓存或发送前编码耗时。首页直接采集时使用本地浏览器采图时间计算结果有效期，覆盖编码及发送前等待。两种计时均不能宣称为曝光至屏幕延迟；超过 1 秒的即时结果不再显示。

检测与人工工件周期独立。开流传入 `cycleId`（包括 null）或记录查询传入 `cycle_id` 均返回 422，避免旧客户端误以为仍有关联。接口不再返回工件字段；历史 SQL 数据保留。检测记录状态为 `RUNNING`、`FINISHED` 或 `INTERRUPTED`。租约失效的未归档记录，在查询记录、重新开流或更改配置时持久标为中断；已中断轮次不能再补记为完成。服务重启不恢复时序算法状态，最多等待旧租约 30 秒过期后重新开流。旧版本从未持久保存的最终结果无法追溯补建。

最终记录冻结工位、工位机、相机、绑定版本、流代次、模型版本。数据库提交失败时冻结算法结果，阻止继续送帧；重试不会重复结束算法，也不会把等待数据库的时间算作新的采样缺口。检测结果与告警证据按各自记录保存；待复核结果保留原因供现场复核。

### NG 确认与断线恢复

检测首页的人工确认使用 `review-outbox.js`，先将工位、工位机、轮次、UUID、结论、原因和已认证身份保存到 IndexedDB，再发请求。写事务请求 `strict` durability，等待事务完成后才允许联网提交；本地写入失败会直接显示错误。一次轮次只保留一个原请求，未明确核对前不能改选结论或原因；多标签页使用 IndexedDB 事务和 Web Locks 协调，并通过 BroadcastChannel 同步保存后的状态。

提交时显示“提交中”。超时、网络错误或服务器暂不可用时显示“待核对”，不将其解释为保存失败。恢复连接、重新打开页面或点击“立即核对”时，先按轮次查询服务端：请求编号、结论、原因和确认身份均相同则完成；没有人工确认、仍允许复核且身份一致才重发原请求。发现他人确认、请求内容冲突、身份/设备凭据版本变化或权限拒绝后，保存实际服务器记录并停止自动提交；“刷新服务器结果”仅查询。待核对记录可从工位页“核对人工确认”查看。

人工最终结论保存在 `manualReview`，算法 `compliance`、位置与动作证据保留。告警“标记已读”仅表示阅读状态，不能改变人工结论。设备凭据对应工位设备，`reviewer` 为 `station-N`；管理登录使用服务器认证账号。`expectedActor` 是原请求身份的前置条件，服务器始终自己确定审计身份，并在写事务内再次检查设备绑定。客户端填写姓名不能成为审计身份；若需追溯具体员工，应接入服务器认证的员工登录。

浏览器存储以站点 origin 和浏览器配置目录为边界。页面会申请并展示长期存储保护状态；未获保护时仍保存到 IndexedDB，但明确提示生命周期限制。验收已覆盖相同浏览器配置目录下关闭并重新启动浏览器进程；这不等于清除站点数据、隐私模式退出、配置目录丢失或设备损坏后的恢复保证。部署应固定访问地址和浏览器配置，保留站点数据；如要求独立于浏览器生命周期的保证，应使用安装式本地服务及 SQLite 持久队列。当前仓库实现为网页客户端。

实时帧不会进入确认队列：只发送最新采样，发送前过期即丢弃。中央推理失联时显示“无法检测”，清除旧判定和识别框；恢复后核对原租约、流 ID/代次、设备和模型身份，匹配后仍须取得新采样结果。旧状态查询中的 PASS 不会变成新轮结果。证据由现有服务端取证模块保存，没有将证据文件混入业务确认队列；如增设离线上传，应另设容量、期限及失败处置有界的证据队列。

恢复回归（真实 API/SQLite、持久浏览器配置；推理结果为测试夹具）：

```powershell
node tools/python.cjs -m pytest tests/test_station_review.py -q
node tools/python.cjs tools/test-pilot-ui.py --registration-paused --browser-script tools/test-review-recovery.cjs
node tools/python.cjs tools/test-pilot-ui.py --registration-paused --browser-script tools/test-station-review-ui.cjs
```

## 6. 验证与性能验收

```powershell
npm run test:terminals
npm run test:terminal-ui
```

第一项包含六路并发隔离、租约恢复、旧流结果拦截、权限、调度、常驻采集子进程以及 fixed/serial 压测记账。第二项使用隔离 SQLite、真实鉴权 API、浏览器虚拟相机和模拟 AI 结果，不占用生产数据，不代表 GPU 实测。

目标 GPU 上先停止同工位采集及同卡离线任务，准备六张代表性 JPEG，在维护窗口运行：

```powershell
# 环境变量 AISOP_TERMINAL_KEY_1 ... AISOP_TERMINAL_KEY_6 分别属于对应工位。
npm run benchmark:stations -- --server https://aisop.example.internal --arrival-mode fixed --fps 4 --duration 60 --images cam1.jpg cam2.jpg cam3.jpg cam4.jpg cam5.jpg cam6.jpg --report runtime-data/six-station-4fps-fixed.json
# 上一轮完全结束后，用同样模型与图片核对单在途串行节奏。
npm run benchmark:stations -- --server https://aisop.example.internal --arrival-mode serial --fps 4 --duration 60 --images cam1.jpg cam2.jpg cam3.jpg cam4.jpg cam5.jpg cam6.jpg --report runtime-data/six-station-4fps-serial.json
```

脚本使用真实配置模型，预热时间单列。默认 `fixed` 按固定时刻产生候选采样，上次请求未完成会记录跳过，不降低候选数来掩盖不足；`serial` 每路独立维持一个在途请求，完成后等待采样周期剩余时间，候选数为真实请求次数，跳帧计数为 null。两种模式需要分别注明，不能直接混比。报告逐路候选/提交/完成/跳过/错误、有效 FPS、p95/p99、排队 p95、最长无结果间隔及 500ms deadline 内完成数。退出码 1 表示至少一路未满足约定的吞吐（目标 95%）或延迟门限，退出码 2 表示配置/准备失败。

该工具重复预编码代表图片，验证 HTTP/模型服务容量，不含真实采集、视频解码、浏览器采图/编码、录像和屏幕显示，也不能验证动作正确性；即使选择 `serial` 也不是摄像头或网页端到端 FPS。现场仍需六路真实任务、已部署录像链路开启时的负载、断网重连、8 小时及 72 小时稳定性测试，并以独立验证集检验漏步骤、漏装及误放行。先以每路 4、6 fps 调试，再按每路有效 10 fps 的最终要求验收，逐路检查 P95、结果年龄和最大无结果间隔。调度优化不能代替模型正确性验收。

本机也可以创建隔离数据库和临时 API，使用项目已有图片与真实模型做配置对比，不改正式工位绑定：

```powershell
npm run benchmark:stations:local -- --profile mixed --arrival-mode fixed --fps 4 --duration 60 --concurrency 3 --pose-pool 3 --specialist-pool 3 --opencv-threads 4 --torch-threads 4
npm run benchmark:stations:local -- --profile mixed --arrival-mode serial --fps 4 --duration 60 --concurrency 3 --pose-pool 3 --specialist-pool 3 --opencv-threads 4 --torch-threads 4
# 4 fps 逐路达标后再试 6 fps；仍须在真实六机采集链路复核。
npm run benchmark:stations:local -- --profile mixed --arrival-mode serial --fps 6 --duration 60 --concurrency 3 --pose-pool 3 --specialist-pool 3 --opencv-threads 4 --torch-threads 4
# 最终每路 10 fps / 六路 60 fps；本命令只测示例模型配方的 API 容量。
npm run benchmark:stations:local -- --profile mixed --arrival-mode serial --fps 10 --duration 60 --concurrency 3 --pose-pool 3 --specialist-pool 3 --opencv-threads 4 --torch-threads 4
```

`mixed` 默认是姿态、螺丝、贴条各两路的服务容量实验，不等于现场六工位任务配方；确认现场分工后可用 `--tasks` 按实际顺序指定 1–6 个任务。结果和硬件/参数快照在 `test-results/station-gpu-benchmarks/`。各轮需依次运行，避免互相争抢 GPU；最新本机实测、逐帧对照和限制见 [帧率与 GPU 优化记录](../history/throughput-optimization-2026-11-07.md)。

压测先逐路加载模型，再执行三轮并发预热以覆盖专项副本，预热结果在 `parallelWarmup` 单独记录。正式计量采用指定的 `--fps` 与 `--arrival-mode`；报告的 `stagesP95Ms` 可区分解码、人体、专项定位、分类及服务校验。隔离脚本保存 `metrics.json`、`scheduler.json`、硬件快照及 `gpu.jsonl`，GPU 统计应只取 `measurementStartedAt` 至 `measurementFinishedAt` 测量窗口。整卡利用率包含其他进程或游戏，不能当作检测进程专属占用。出现超时、明显背景负载变化时，不应把两轮差值全部归因于优化。`--profile pose|screw|sticker` 可以核对六路同类任务；模型副本上限需要按实际任务数量和显存预算设置。

`tools/summarize-station-benchmark.py <运行目录> --output <汇总.json> --markdown <汇总.md>` 可离线汇总逐路结果与测量窗口内整卡 GPU 样本。真实首页工位浏览器探针 `tools/test-station-throughput-ui.cjs` 支持环境变量 `STATION_THROUGHPUT_FPS=10`；隔离启动器 `tools/test-pilot-ui.py --source-root <源码快照>` 供同输入前后对照。浏览器测量窗口由 `throughput-measurement-windows.json` 给出，可交给 `tools/summarize-browser-gpu.py` 对齐外部 GPU 日志。
