# 推理线程优化与续验记录

日期：2026-11-05，Asia/Singapore。本次接续网络中断前的性能优化，完成启动配置、真实模型对照、混合负载与业务链路检查。识别模型、阈值、分辨率和动作规则保持原配置。

## 实现范围

- `backend/inference_runtime.py` 在单个进程启动时导入依赖，再设置OpenCV线程预算，避免Ultralytics初始化覆盖设置。并发初始化受锁保护，重复使用同一预算可复用，修改预算需要重启。
- `Settings`、`.env.example` 和Compose传入 `OPENCV_THREADS`，默认4，接受1–16，按系统CPU数封顶。API在lifespan、后台Worker在开始轮询前配置；预热在配置之后启动。
- `/api/v1/metrics` 返回API实际线程数。OpenCV预算不等于进程总线程数；多个服务和独立训练仍需分别评估资源竞争。
- `tools/benchmark-runtime.py` 用隔离SQLite库运行真实模型，记录P50/P95/P99、分阶段耗时、混合请求200/429、健康响应、RSS、PyTorch已分配/保留显存、负载后恢复及流释放。`--baseline` 验证输入哈希和全部非诊断输出，业务期望失败仍返回非零退出码。

## 优化前后对照

硬件为本机RTX 5070 Ti；每组使用新进程，YOLO姿态池1，线程数分别1和4。人体输入为录像中的10帧，螺丝和贴条输入为参考图及平移变体；这些样本验证运行一致性，不能用于估算现场准确率。每类33次请求，前3次不计入稳定阶段，下面各30个样本。

计时包含TestClient、API鉴权、解码、模型和规则处理，不含网络、物理相机及浏览器显示。

| 任务 | 1线程中位ms | 4线程中位ms | 中位下降 | 1线程P95 ms | 4线程P95 ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| 人体 | 20.25 | 17.06 | 15.8% | 23.99 | 20.55 |
| 螺丝 | 196.31 | 126.40 | 35.6% | 205.73 | 135.78 |
| 贴条 | 391.96 | 240.83 | 38.6% | 420.62 | 254.82 |

原始证据：`test-results/performance-implementation-20261105/baseline-1/`、`optimized-4/`。早期响应保留了 `streamEpoch` 和 `personProcessingMs`，因此原记录中的行为哈希不能直接互比；续验比较器会对两边都移除诊断字段。归一化后的278帧和最终结果逐字段一致，包含分数、坐标、动作状态和工序判断。

早期 `baseline-1`、`optimized-4` 的混合部分始终让人体请求先到，螺丝和贴条全被拒绝，不能作为有效混合负载。已改为轮换先到的任务并错开25ms，且要求每种任务都有成功推理。

修正后的60轮记录位于 `mixed-1/`、`mixed-4/`。1线程的180个请求成功169次、429拒绝11次；4线程成功175次、拒绝5次。三类请求均有成功推理，负载后均恢复，健康接口P95分别4.93ms和3.85ms。这是客户端每轮等待完成后再发下一轮的短时竞争测试，不是固定到达率的容量认证。

## 1,000轮续验

2026-11-05 21:36–21:42（Asia/Singapore）使用当前代码、4线程和同一份输入复测；16个逻辑CPU，OpenCV 4.11.0，PyTorch 2.8.0+cu128。单类稳定阶段中位值为人体20.12ms、螺丝136.18ms、贴条238.38ms。自动对比仍通过全部278帧与最终结果，输入哈希一致。

混合部分耗时280.88秒，三类各1,000次请求，合计2,905次成功、95次429、无5xx或未预期状态。下表时延只统计成功请求，拒绝单列，避免将快速拒绝算成推理加速。

| 任务 | 成功 | 429 | 中位ms | P95 ms | P99 ms | 最大ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 人体 | 929 | 71 | 22.22 | 29.94 | 37.76 | 188.25 |
| 螺丝 | 994 | 6 | 139.21 | 170.95 | 198.94 | 279.99 |
| 贴条 | 982 | 18 | 254.53 | 283.53 | 305.25 | 418.18 |

1,000次健康请求均成功，P95 3.91ms，最大28.62ms。RSS从1,947.52MB到1,982.68MB，峰值1,994.55MB；第100轮之后范围1,971.43–1,994.55MB。PyTorch已分配显存从180.90MB到212.90MB，保留显存从532MB到612MB；第100轮之后两者均未继续增长。这些是当前进程的PyTorch分配器统计，不是GPU总占用，也不能证明长期无泄漏。

竞争结束后三类请求逐一返回200，所有推理槽位空闲，人体、螺丝、贴条、场景的剩余流均为0；所有模型加载后OpenCV仍为4线程。`continued-4/benchmark.json` 保存完整统计，`responses.json` 保存逐帧结果。

本轮退出码1来自保留的R05业务期望失败；`baselineComparison.passed=true`，`businessCasesPassed=false`。自动验收结果分别呈现，未把识别失败标成全通过。

## 贴条准确性问题

录像 `lv_0_20261017162449.mp4` 的14–49.5秒片段以4fps采样，共142帧。现有测试将它标为正确顺序并期望PASS，但1线程和4线程均判FAIL，缺少R05；本次不能宣称全部业务样本通过。

R05在录像41–47秒范围内只在46.25秒出现一次CONTACT，其余多为CLEAR或UNKNOWN，没有形成当前规则所需的确认事件。该现象需结合现场标注及更密采样进一步验证；没有通过降低规则阈值或替换期望结论来消除测试失败。逐帧证据保存在 `r05-baseline-observations.json`。

静态成品、空白画面均为REVIEW，重排动作片段为FAIL，均符合现有测试期望。优化前后所有四个片段结果一致。

## 复现

在项目目录用同一份模型、配置和录像运行，分别启动新进程：

```powershell
$video = 'C:\Users\Administrator\Downloads\lv_0_20261017162449.mp4'
node tools/python.cjs tools/benchmark-runtime.py --opencv-threads 1 --samples 30 --mixed-rounds 60 --sticker-video $video --test-results test-results/runtime-check/baseline-1
node tools/python.cjs tools/benchmark-runtime.py --opencv-threads 4 --samples 30 --mixed-rounds 1000 --sticker-video $video --baseline test-results/runtime-check/baseline-1 --test-results test-results/runtime-check/optimized-4
```

存在上述R05问题时，两组命令会在保存JSON后返回非零退出码。查看 `businessCasesPassed` 与 `baselineComparison.passed`，分别判断业务期望和优化前后一致性；不得把前后一致当作识别正确。

后端续验：301通过、1跳过，跳过项需要独立PostgreSQL测试库；管理目录9项Node测试通过。后端JUnit记录位于 `test-results/server-performance-20261105/qa/continued-regression.xml`。

## 实际服务与浏览器验证

独立API与Worker的螺丝上传测试通过，覆盖历史打开、连续两次上传、原生文件选择、失效列表节点修复、真实模型分析、回放、无效输入和忙时反馈。输入仍为参考图组成的合成视频，只验证链路；证据位于 `test-results/screw-upload-tests/run-uf3te_ms/`。实时传输前端15项回归通过，页面异常0；结果位于 `test-results/realtime-transport-20261105/`。

确认8000主服务没有排队/运行中的分析、训练和在途实时推理后，已重启本项目API和Worker。API指标显示 `opencvThreads=4`，Worker启动日志也确认4线程；本地原有姿态池2保持原配置，与上面的池1隔离基准不同。

重启后通过浏览器依次验证人体、螺丝、贴条，每类正式采样51帧，准备时间另列，稳定统计采用后50帧。计时为浏览器采样开始到DOM结果更新，不包含物理相机曝光、驱动或屏幕扫描；输入是录像驱动的浏览器摄像头。

| 任务 | 准备ms | 正式首帧ms | 稳定P95 ms | 稳定P99 / 最大ms |
| --- | ---: | ---: | ---: | ---: |
| 人体 | 1441.5 | 285.0 | 42.3 | 63.9 |
| 螺丝 | 2870.4 | 166.9 | 178.7 | 191.2 |
| 贴条 | 1242.8 | 324.9 | 288.8 | 318.7 |

153帧正式采样均小于500ms，无证据缺口、无页面异常，全部API请求使用8000同源入口，结束后释放流和摄像头。冷启动准备耗时不计入正式帧延迟。JSON及截图保存在 `test-results/performance-implementation-20261105/main-service/`，API配置记录为 `main-runtime.json`；完成验证后记录的14个模型、配置和源码哈希保存在 `verified-files.json`。

本环境尚未验证Linux Docker镜像、PostgreSQL/Nginx HTTPS、15路现场采集或72小时持续运行；当前结果不能外推为这些场景已达标。
