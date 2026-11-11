# 正式后台采集与逐件控制

首页工位 1–6 的“后台采集（正式）”由独立常驻进程读取 USB/RTSP 相机。网页只提交逐件控制请求、读取当前状态和检测采样画面。刷新、关闭网页、切换工位或退出网页登录，不会停止该进程，也不会结束正在检测的工件。

“页面摄像头（刷新会中断）”、视频和图片仍用于页面采集或联调。浏览器无法在页面销毁后继续持有本地摄像头，不能用页面采集代替正式后台模式。

## 启用

本次数据库版本为 v3。按现有部署流程先停止旧 API/Worker，再使用相同数据库配置执行迁移并启动新版服务。迁移只新增采集器状态和控制请求表，保留原检测与复核记录：

```powershell
node tools/python.cjs -m backend.init_db
```

在 `/terminals` 配置工位、相机与检测模型，取得该工位的设备凭据。在相机可达的机器上，通过服务环境变量提供凭据和相机源：

```powershell
$env:AISOP_TERMINAL_KEY = '<本工位设备凭据>'
$env:AISOP_CAMERA_SOURCE = '0' # 本机 USB 索引，或现场确认的完整 RTSP 地址
node tools/python.cjs tools/station-collector.py --controlled --server https://aisop.example.internal --report runtime-data/station-1-collector.json
```

一个相机运行一个采集器。六工位可分别使用 `--key-env AISOP_TERMINAL_KEY_1 --source-env AISOP_CAMERA_SOURCE_1` 至 `_6`。由现有系统服务管理器托管进程、日志轮转和故障后退避重启；设备凭据不放在命令行参数中。`--controlled` 是正式逐件模式，省略该参数仍为兼容旧脚本的连续采样模式。

打开首页对应工位。检测到在线的受控采集器时自动选择“后台采集（正式）”；也可在“图像来源”中手动选择。相机保持就绪，空闲时不会自动创建检测轮次。

## 每件的操作边界

1. 工件到位后按 F8 或“开始检测”。页面先持久保存请求编号，服务器再保存开始命令；采集器使用同一编号创建一条新轮次。开始前缓存在队列里的画面不会作为本件首帧。
2. 检测过程中可刷新或关闭页面。重新打开后读取服务器上的原轮次，继续显示该轮结果，不新开轮次。预览只显示与当前流、代次和帧号相符的检测采样图。
3. 工件完成后点击“结束并归档”。采集器停止提交新帧，核对最终结果已经保存，再释放检测流。界面收到执行回执后显示归档结果；仅收到命令不代表已归档。
4. 出现异常时选择中断原因并点击停止。未正常归档的轮次保存为 `INTERRUPTED`，不会沿用最后一帧 PASS 作为最终结论。
5. 下一件需要再次开始，使用新的流编号和时序状态。采集器空闲不会自行把同一工件反复建轮。

已归档的 FAIL/REVIEW 仍使用现有“人工确认”与持久核对队列。算法原判定与人工复核结果分别保存。

## 超时与恢复

开始、结束、停止命令均有固定 UUID。页面在 IndexedDB 严格事务完成后才提交；响应丢失显示待核对，刷新或重开浏览器后按原编号精确查询。服务器已经保存的命令不会再次执行；未找到的请求仅通过“重试原请求”明确重试，不因轮询自动开始新件。原账号、设备实例、配置版本或服务代次变化时，旧请求不能用于控制新的设备或工件。

同一工位只允许一个有效采集器和一个待执行命令。采集器心跳租约为 45 秒，检测流继续使用原有 30 秒租约；失联后旧进程不能继续发送有效结果。相机断开/重新连接、API 重启、设备配置变更、进程退出或采集故障均中断旧轮次。恢复设备后需操作员显式开始新件，不自动续用中断前的时序证据。

已保存归档、释放响应或执行回执丢失时，采集器先查询记录，再继续未完成的步骤；不会重新计算已经持久保存的最终判定。已中断轮次不能补记为正常完成。

## 验证与范围

```powershell
node tools/python.cjs -m pytest tests/test_collector_control.py tests/test_station_collector_control.py tests/test_controlled_collection_integration.py -q
node tools/python.cjs tools/test-pilot-ui.py --registration-paused --browser-script tools/test-station-collector-ui.cjs
node tools/python.cjs tools/test-controlled-collection-ui.py
```

控制接口测试使用隔离数据库。联动测试运行真实 API、数据库和生产采集器状态机，替代相机帧与模型推理；浏览器恢复测试运行真实浏览器、会话、IndexedDB 和刷新生命周期，并明确记录所使用的模拟接口。`test-controlled-collection-ui.py` 进一步将真实浏览器、HTTP API、SQL 和独立采集循环连在一起，验证关闭/刷新页面后的同流新帧、丢回包、归档及下一件。`npm run test:collector` 和 `npm run test:collector-ui` 分别运行控制测试与真实接口浏览器联动。这些测试不代替物理相机、六路 GPU 吞吐和识别准确率验收。独立 `/terminal` 的浏览器采集入口保持原行为，正式逐件控制从首页工位页使用。
