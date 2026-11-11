# 系统吞吐与业务链路优化记录（2026-11-07）

本轮目标是提高及时返回的有效检测帧数，并缩短采集、编码、调度、人员检测和专项检测之间的空档。GPU 占用率是排查指标；是否改善仍以有效帧率、结果延迟、最长无结果时间及业务结果一致性判断。

本文是本轮证据记录。此前一轮局部算子优化和 4/6 fps 浏览器实验另见 [throughput-optimization-2026-11-07.md](throughput-optimization-2026-11-07.md)，不能与本轮结果合并为一个 A/B 实验。

**系统修改与业务边界。**

| 环节 | 本轮处理 | 业务边界 |
| --- | --- | --- |
| 浏览器采样与编码 | 请求处理期间准备下一张采样图，只保留最新待送帧；编码和上传可以重叠，减少服务器空等 | 每工位最多一个上传请求在途；待送帧从采图开始计时，超过 500 ms 丢弃；停止、切换、自动结束时释放 bitmap、worker 和媒体轨道 |
| 人员与专项检测 | 螺丝、贴条在同一解码帧上启动一次人员检测，与专项检查重叠执行；两者完成后才推进证据和工序规则 | 不减少人员检测频率，不改变阈值、精度或输入尺寸；异常路径也等待人员任务收尾，防止下一帧覆盖 tracker 或跨流结果 |
| 服务生命周期 | 专项服务停止接收新请求后，先排空实际在途调用，再同步所属 CUDA stream、关闭线程和人员 worker | 工位释放/结束时不遗留任务；关闭所有副本时先停止全部副本接收请求，再逐个回收 |
| 场景模型并发 | 短缓存锁、按模型独占租用、固定模型线程和 CUDA stream；活动模型数受限；只淘汰空闲模型 | 相同模型实例不会被并发调用；满载时在时间戳状态更新前返回忙；释放流后不发布旧结果 |
| 压测链路 | 同源 HTTP 长连接；空闲连接在请求前更新；增加明确失败报告、源码与输入指纹、窗口内 GPU 汇总 | 帧 POST 不自动重放，不跟随重定向转发凭证；预热失败不冒充完成测量 |

人员并行实现见 [person_overlap.py](../../backend/person_overlap.py)、[specialist_lifecycle.py](../../backend/specialist_lifecycle.py)、[screw_realtime.py](../../backend/screw_realtime.py) 和 [sticker_realtime.py](../../backend/sticker_realtime.py)。场景并发实现见 [scene_realtime.py](../../backend/scene_realtime.py) 与 [scene_model_cache.py](../../backend/scene_model_cache.py)。

只读检查还发现每帧存在资产快照检查、终端认证、推理前后租约及版本校验。其中快照缓存命中主要执行依赖文件 stat 和结果拷贝，不是每帧重新散列整个权重；已有阶段数据中它远小于人员检测、专项检测和定位耗时。租约及版本检查承担结果归属、过期拒绝和配置一致性职责，本轮没有通过降低这些检查或数据库持久化强度换取吞吐。

**人员并行：完整视频逐帧等价与时间测量。**

证据为 [person-overlap/report.json](../../test-results/system-throughput-20261107/person-overlap/report.json) 及同目录逐帧 JSONL；脚本为 [compare_person_overlap.py](../../test-results/system-throughput-20261107/compare_person_overlap.py)。本机 GPU 为 RTX 5070 Ti。两段视频完整读取，以 4 fps 取样，按任务原有尺寸限制编码为质量 82 的 JPEG。每帧使用相同 JPEG 和时间戳，优化前后调用顺序逐帧交替，完整调用前后同步 CUDA。

| 工序 | 比较帧数 | 非计时输出差异 | 定位成功帧 | 检出人员帧 | 最终结果（前后相同） |
| --- | ---: | ---: | ---: | ---: | --- |
| 螺丝 | 215 | 0 | 141 | 215 | PASS |
| 贴条 | 242 | 0 | 127 | 234 | FAIL |
| 合计 | 457 | 0 | 268 | 449 | 两工序分别一致 |

每个工序首 3 帧只从时间统计中剔除，仍参与等价检查。时间覆盖解码、人员检测、专项检查及证据/业务规则，排除视频解码、JPEG 编码、HTTP 和浏览器。

| 工序 | 计时帧数 | 优化前中位数 | 优化后中位数 | 优化前 P95 | 优化后 P95 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 螺丝 | 212 | 46.349 ms | 41.913 ms | 53.264 ms | 48.551 ms |
| 贴条 | 239 | 60.655 ms | 55.613 ms | 69.892 ms | 63.520 ms |

螺丝中位耗时下降约 9.57%，贴条下降约 8.31%。这证明本次开发视频中有可重叠的工作，且没有改变所比对的逐帧非计时输出和最终判定；贴条原有 FAIL 也得到保留。这不是独立准确率评测，不能把耗时倒数称为网页帧率，也不能据此承诺 RTX 4090 六路容量。

| 输入 | SHA-256 |
| --- | --- |
| `VID_20261017_103101.mp4` | `aa408dd21297ae455dd75c830385d17fe6a388012712dee844dce3539d87088d` |
| `lv_0_20261017162449.mp4` | `06104b7e2081121418d777d7bfa0931edecc30870a176a6f173af51c90183e6e` |

报告同时保存优化前后两个服务模块的 SHA-256，避免把之后的文件内容误认为测量时源码。

**前端生命周期：模拟推理的 10 项验证。**

证据为 [run-6bvnatgy/station-pipeline-ui.json](../../test-results/pilot-browser-tests/run-6bvnatgy/station-pipeline-ui.json)，`passed=true`，`inferenceMocked=true`，`archiveMocked=true`。10 项均通过：`worker-stop`、`worker-finish`、`worker-auto-pass`、`worker-backpressure`、`worker-wrong-identity`、`canvas-fallback`、`worker-slow-encode-expiry`、`worker-stop-during-encode`、`worker-stop-during-bitmap`、`fallback-stop-during-encode`。

这些用例覆盖单请求在途、旧帧丢弃、429 退避、结果身份对应、自动归档、编码中停止及资源回收。正常提交用例的最大在途请求为 1；三个提前停止用例为 0。用例结束后媒体轨道关闭，无待送帧或打开的 bitmap。500 ms 有效期从采图开始，包含编码及等待上传时间。这些是浏览器行为验证，不提供真实 GPU 帧率证据。真实回放的统一方法见 [browser-pipeline-ab-plan.md](../../test-results/system-throughput-20261107/browser-pipeline-ab-plan.md)。

**未采用的实验：场景 CUDA 计时适配器。**

正式实验材料位于 [scene-profile-20261107-232511](../../test-results/system-throughput-20261107/scene-profile-20261107-232511/README.md)。环境为本机 RTX 5070 Ti、Torch 2.8.0+cu128、Ultralytics 8.4.155。已有 detect 与 pose 模型各使用固定线程和独立 CUDA stream，保持 FP32、原阈值及分辨率；以 batch=2 静态图重复推理，按原版/候选/原版/候选各测 10 秒。

原始输出 8 个案例、完整 SceneDetector 4 个案例逐值一致，最大绝对差为 0；segment、OBB、classify 仅经过 CPU 模拟，未做对应真实权重验证。四轮完成图像数分别为 282.2、272.6、298.0、281.0 图像/秒，原版均值 290.1、候选 276.8，候选下降约 4.58%。整卡 GPU 利用率均值分别为 29.28%、29.76%、31.67%、29.17%，没有改善。

该适配器没有接入生产。实验实现和 CPU 测试移至 [scene-profile-experiment](../../test-results/system-throughput-20261107/scene-profile-experiment/README.md)。上述是模型吞吐，排除 HTTP、工位调度、摄像头和页面；整卡利用率还包含后台桌面及游戏。此前 `scene-profile-20261107-232330` 使用低精度时钟，延迟出现 15/16 ms 量化，故不用于正式延迟结论；正式重跑使用 `perf_counter`。

**初次六路诊断不能用作本轮 A/B 基线。**

[mixed-w4r3rn6s/report.json](../../test-results/station-gpu-benchmarks/mixed-w4r3rn6s/report.json) 使用 pose、screw、sticker 各两路，目标各 10 fps、窗口 30 秒，总并发/人体池/专项池为 2/2/1。成功响应总有效帧率为 15.633 fps。各路为 3.000、2.133、2.667、3.033、2.133、2.667 fps；队列等待 P95 为 375、328、235、375、313、235 ms。窗口内整卡 GPU 共 28 个读数，平均 75.929%、P95 为 96%。这能说明该运行中排队显著，不能证明整条检测链路还有多少独占算力余量。

该轮正式主服务仍驻留显存，而之后的对照安排暂停主服务。初始状态见 [main-before.json](../../test-results/system-throughput-20261107/main-before.json)。因此运行条件不一致，禁止把它与后续暂停主服务后的结果直接比较并宣称代码提速。诊断汇总见 [initial-benchmark-summary.md](../../test-results/system-throughput-20261107/initial-benchmark-summary.md)。

`mixed-i7oemmt5` 在并行预热/流释放期间遇到 429，未完成计量；`mixed-kf7zeo74` 在旧版空闲连接断开时退出，未获得有效报告。这两轮没有吞吐结论。后续工具分别增加有界释放重试和长时间空闲后的连接更新；帧 POST 仍不重试。

**同条件六路 API 对照与并发测试。**

本机 RTX 5070 Ti、主 API/Worker 暂停、同一组 pose/螺丝/贴条各两路、同一编码 JPEG、每路目标 10 fps、每轮 30 秒、serial 到达、500 ms 队列期限。基线是本轮人体重叠前的源码快照；候选启用人体重叠，其他输入与业务阈值一致。这组测试不包含摄像头、六台终端的网络、浏览器编码和显示。所有六轮均无请求错误，但均未实现六路各 10 fps。

| 版本/运行 | 全局/人体池/专项池 | OpenCV/Torch 线程 | 总有效 FPS | 最慢一路 FPS | 各路响应 P95 最大值 | 整卡 GPU 均值 / P95 |
| --- | --- | --- | ---: | ---: | ---: | --- |
| 基线 `mixed-oye7ra9j` | 3/3/2 | 4/4 | 27.333 | 3.433 | 385.28 ms | 57.036% / 82% |
| 候选 `mixed-djjgex74` | 3/3/2 | 4/4 | 28.834 | 3.500 | 370.96 ms | 60.448% / 81% |
| 候选 `mixed-sxez36ug` | 2/2/2 | 4/4 | 25.834 | 3.667 | 342.54 ms | 60.357% / 74% |
| 候选 `mixed-1nzf9rgx` | 4/4/2 | 4/4 | 32.266 | 3.500 | 341.48 ms | 58.643% / 83% |
| 候选 `mixed-44ghq1t6` | 4/4/2 | 1/1 | 31.666 | 3.467 | 354.38 ms | 63.821% / 89% |
| 候选 `mixed-hrt6zpnh` | 6/6/2 | 4/4 | 35.700 | 3.233 | 388.05 ms | 62.036% / 85% |

相同 3/3/2 参数的一对对照，总吞吐增加约 5.49%，各路响应 P95 最大值降低约 3.72%。这是单次配对结果，未给出重复实验置信区间，不能承诺固定提升比例。并发调参是在候选代码上的容量探索，不是同参数代码 A/B。

| 代表流 | 基线 3 并发 FPS / P95 ms | 候选 3 并发 FPS / P95 ms | 候选 4 并发 FPS / P95 ms | 候选 6 并发 FPS / P95 ms |
| --- | --- | --- | --- | --- |
| pose 1 | 5.867 / 228.80 | 5.967 / 229.46 | 7.667 / 193.16 | 9.667 / 117.02 |
| 螺丝 1 | 3.433 / 385.28 | 3.500 / 360.42 | 3.533 / 334.62 | 3.300 / 373.79 |
| 贴条 1 | 4.467 / 302.43 | 4.867 / 278.14 | 4.900 / 245.90 | 4.867 / 233.27 |
| pose 2 | 5.733 / 227.89 | 6.067 / 220.34 | 7.733 / 200.20 | 9.533 / 130.03 |
| 螺丝 2 | 3.433 / 382.95 | 3.500 / 370.96 | 3.500 / 341.48 | 3.233 / 388.05 |
| 贴条 2 | 4.400 / 297.62 | 4.933 / 272.69 | 4.933 / 247.18 | 5.100 / 240.82 |

6 并发的各路 queue P95 均为 0 ms，但螺丝 P95 仍约 374–388 ms，且其 FPS 比 4 并发下降；主要剩余耗时在并发模型服务内部，而不是等待调度名额。现有分段不足以继续区分 GPU 内核、主机调度与各模型相互争用比例，需要目标模型组合的 profiler 证据。4 并发本次更有利于控制最坏响应时间，6 并发则提高了总吞吐，不能把总数较高当作每工位达标。

线程 1/1 的整卡 GPU 均值更高但吞吐略低，故不采用“把 GPU 百分比调高”作为选择标准。本次局部最优值不能直接替换双路至强/4090 的部署参数；六个实际模型还未确定。完整输入/源码指纹、每路 P99、阶段 P95、最长无结果间隔及 GPU 样本见 [six-station-comparison.json](../../test-results/system-throughput-20261107/six-station-comparison.json) 和 [可读汇总](../../test-results/system-throughput-20261107/six-station-comparison.md)。

**有效帧率与耗时的精确口径。**

以下对应当前 [benchmark-stations.py](../../tools/benchmark-stations.py) 和 [summarize-station-benchmark.py](../../tools/summarize-station-benchmark.py)。每份报告的源码 manifest 决定其适用实现；旧报告可能缺少新增字段。

| 字段/行为 | 含义及限制 |
| --- | --- |
| `serial` | 每路一个独立 worker、最多一个在途请求；完成后等待目标周期剩余时间。`candidates` 等于实际 `submitted`，`skippedBusy` 和 `skippedLate` 为 null，不是 0 |
| `fixed` | 按固定时刻尝试提交；总候选为 `ceil(fps × duration)`，分别计入提交、忙跳过和迟到跳过 |
| `completed` | 所有成功响应，包括测量窗口结束后 drain 才完成的请求 |
| `completedWithinWindow` | 计时窗口内完成的成功响应；当前代码使用完成相对时刻 `<= duration` |
| `effectiveFps` | `completedWithinWindow / duration`，保留三位小数；合计是各路已舍入 FPS 的和，可能存在很小的舍入差 |
| 响应 P95/P99、队列 P95、阶段 P95、`withinDeadline` | 统计窗口内提交且最终成功的请求，包括 drain 中完成者；不包含失败请求耗时。与 FPS 的总体不同 |
| `targetMet` | 有效 FPS 达到目标的 95%，且成功响应 P95 不超过 deadline；`allTargetsMet` 要求所有工位成立 |
| `longestNoResultMs` | 以窗口内成功响应和窗口起止边界计算最长无结果间隔，用于补充成功响应 P95 无法反映的失败/停顿 |
| `service.beforeWorkerMs` | 已含请求接收后的上传/认证、调度等待及线程池等待等时间，不能再次加上 `queueWait` |
| `service.modelMs` | 完整模型服务调用阶段；内部的 `inspection`、`inspection.registration` 等属于嵌套阶段，不能把各自 P95 相加 |
| `timingsMs.person` | 人员并行版本中，表示专项检查结束后等待人员任务的剩余时间。旧串行版本是完整串行人员步骤，二者不能作为同一模型耗时直接比较 |
| `personProcessingMs` | 返回对象中的完整人员分支处理时间；当前 API 压测只收集 `timingsMs`、服务分段和 inspection 分段，尚未单独收入此字段 |
| 阶段时间与 GPU kernel 时间 | 阶段是应用墙钟区间，可能包含 CPU、排队及 CUDA 同步；异步 GPU 阶段不等价于纯 kernel 时间 |
| 计时钟 | 请求耗时、节奏及窗口采用 `perf_counter`；epoch 起点仅用于对齐 GPU 日志，结束 epoch 为起点加计划时长 |

这里的成功响应可能为 REVIEW、定位未成功或工序 FAIL。API 有效 FPS 衡量有用身份校验通过的成功响应吞吐，不等于定位成功 FPS、准确率或工序通过率。真实浏览器报告应另外列出页面接收 FPS、HTTP 成功 FPS、定位成功 FPS、实际运行窗口及停止原因；自然播放结束或自动判定提前停止时，不能仍以计划 30 秒作分母。

GPU 汇总仅取 `[measurementStartedAt, measurementFinishedAt)` 内的成功采样，按物理 GPU 索引分别计算算术平均和 nearest-rank P95；无效/缺失读数不会替换成 0。`gpu.jsonl` 的 `at` 在 `nvidia-smi` 查询前记录，约每秒一次，不是逐帧时间戳；还应检查样本数量及最大采样间隔。整卡百分比包含所有进程，不能归因于 AISOP 独占。预热、模型加载和尾部 drain 的 GPU 样本从稳态统计中排除。

**闲置服务的显存驻留与 Worker 配置。**

主任务观察到暂停本机 API 与 Worker 后，整卡占用从约 10.5 GB 降至约 2.84 GB。这是两个服务共同退出后的整卡差值，尚未按进程、活动张量和分配器保留空间拆分；不能称为“Worker 泄漏 10 GB”，也不能把所有差值当作无用模型。暂停后的六路基线 [mixed-oye7ra9j/report.json](../../test-results/station-gpu-benchmarks/mixed-oye7ra9j/report.json) 为 27.333 fps，而早先主服务驻留时为 15.633 fps；运行配置和背景条件并非同一对照，不能据此计算代码收益，或认定全部差值由释放显存造成。

只读源码检查得到如下可证实的生命周期事实：

| 来源 | 明确持有的资源/退出路径 | 判断 |
| --- | --- | --- |
| 实时 API | `app.state.realtime` 保留人体模型槽；螺丝和贴条服务保留各专项 detector；场景模型缓存按上限保留。各服务 `release` 释放流历史，不卸载模型 | 这是供后续请求复用的正常缓存。模型按需加载；本机原有参数为人体池 3、专项池 3、总并发 3，不代表所有槽都已加载，也不能直接以池大小推断显存占用 |
| 离线分析 Worker | `backend/worker.py` 的模块全局 `_model_cache` 保留最后一个人员、pose 或场景模型；换模型 key 才清缓存。任务 `finally` 只停止并等待心跳线程 | 任务结束后存在明确模型强引用，空闲时也不会主动释放；缓存最多一个 key，并非随任务数量无限累积。专项离线分析的 detector 是函数局部对象，不在该全局字典中 |
| 共享图像辅助缓存 | `cuda_vision._region_grid` 最多保留 8 组 CUDA 网格，`cuda_actions._hog_weights` 最多 4 组设备权重 | 这些是有上限的常驻张量；仅凭源码不能把整卡数 GB 归因于它们 |
| 训练任务 | `training_service.execute_one` 启动独立训练子进程，正常路径等待退出；异常/取消路径终止并回收，子进程另有租约看门狗 | 没有证据表明已正常退出的训练模型继续由主 Worker 强引用。不能将离线分析缓存与训练资源释放混为一谈 |
| 禁领后的 Worker 初始化 | 两项 GPU 工作均禁用时，`worker.main` 仍调用 `check_cuda`，该检查会分配一个测试张量并同步 | 禁领阻止后续工作，不代表此进程完全不建立 CUDA 上下文；本轮保留该初始化行为，后续可单独调整并验证 |

证据入口为 [worker.py](../../backend/worker.py)（全局缓存、任务 finally、poll 和 main）、[training_service.py](../../backend/training_service.py)（子进程 supervisor 与租约看门狗）、[realtime.py](../../backend/realtime.py)（slot 和 release）、[cuda_vision.py](../../algorithms/cuda_vision.py) 及 [cuda_actions.py](../../algorithms/cuda_actions.py)。这次只读检查没有触发推理、GPU 探测或进程终止。

已有 `WORKER_ANALYSIS_ENABLED`、`WORKER_TRAINING_ENABLED` 默认均为 true。它们分别控制分析领取和训练 supervisor 启动；数据维护及健康心跳继续运行。检查时本机配置没有覆盖这两项，启动日志最后多次记录均为 `offline-analysis=True, training=True`。目标主机配置 [rtx4090-six-stations.env.example](../../deploy/rtx4090-six-stations.env.example) 已将两项设为 false，用于实时生产期间保留 GPU 资源，这个文件的设置不等于本机启动器已经应用。

本轮补齐 [start-local.py](../../tools/start-local.py)：`local-settings.json` 的 `inference` 对象现在可以传入这两项开关，以及 `SCENE_MODEL_CACHE_SIZE`、`STATION_QUEUE_WAIT_MS`。启动器复用进程前会比较全部受支持参数的有效值；发现差异明确要求执行 `npm run restart:local`，防止修改配置后普通 start 继续沿用旧参数。缺失值按后端既有默认比较，布尔及数值的等价写法不会误报；配置没有提供的参数继续继承环境，不向配置文件写入默认值。本轮未修改实际 `local-settings.json`、默认领取行为或 Worker 模型缓存策略。

禁领开关在 Worker 启动时读取，既有进程不会因配置文件修改自动卸载缓存。要应用开关和释放先前模型，需要在任务已完成后重启对应服务；主任务会按实测参数执行最终恢复。API 的模型缓存继续为实时请求复用，不采用每帧或每次释放流就卸载模型的做法。

启动器 CPU 回归为 **32 passed（0.13 s）**，命令 `node tools/python.cjs -m pytest tests/test_local_runtime.py -q --junitxml=test-results/system-throughput-20261107/local-launcher-regression.xml`，证据见 [local-launcher-regression.xml](../../test-results/system-throughput-20261107/local-launcher-regression.xml)。测试使用临时配置和模拟进程，覆盖新增参数传给两个服务、配置不改写、继承环境、所有参数变更要求重启、缺省/显式默认等价及后端默认一致性，未操作真实服务或 GPU。

**真实浏览器 A-B-B-A。**

本机 RTX 5070 Ti、同一贴条视频正常速度回放、全局/人体/专项 3/3/3、OpenCV/Torch 4/4、目标 10 fps，每轮人工截取约 30 秒。两版都使用同一探针，基线 API/前端来自快照，候选来自当前源码；每轮使用隔离数据库与临时 API，主服务暂停。先基线/候选，再候选/基线，模型、视频和配置指纹在同版重复间一致。

| 窗口内合并指标 | 基线 | 候选 |
| --- | ---: | ---: |
| 页面接受帧数 / 实际秒数 | 597 / 60.1475 | 598 / 60.1299 |
| 有效 FPS | 9.9256 | 9.9451 |
| 定位成功 FPS | 4.8547 | 4.8562 |
| 采样到响应中位数 | 90.9 ms | 85.0 ms |
| 采样到响应 P95 | 100.8 ms | 94.6 ms |
| HTTP 往返中位数 | 76.7 ms | 71.1 ms |
| 整卡 GPU 样本均值 | 46.3559% | 44.6441% |
| 有效 GPU 样本数 | 118 | 118 |

四轮有效 FPS 依次为 9.9175、9.9307、9.9596、9.9337；都接近 10 fps 采样上限，合并约 0.197% 的帧率差不能解释为吞吐容量明显增加。延迟改善在正向与反向复测中方向一致，中位数降低约 6.49%、P95 降低约 6.15%。整体差异含后端人员重叠和前端改动，不可全部归因于前端；本次负载低于 100 ms 周期，候选仅 5 帧采图与上一响应重叠，慢响应下的流水行为由前述模拟用例验证。

四轮均为单请求在途，无 HTTP/身份错误，停止后无新增上传，成功归档。窗口在视频中途人工结束，最终 FAIL 不用于模型准确率评价。延迟仅纳入窗口内被页面接收的结果，排除停止后的在途响应；GPU 仅统计对应窗口，涵盖全部进程。详细数据与每轮来源见 [browser-ab-summary.json](../../test-results/system-throughput-20261107/browser-ab-summary.json)、[可读报告](../../test-results/system-throughput-20261107/browser-ab-summary.md)。

**验证与本机恢复。**

最终合并回归 **337 passed，53.06 秒**，证据为 [final-regression-corrected.xml](../../test-results/system-throughput-20261107/final-regression-corrected.xml) 与同名 `.log`。覆盖场景缓存/并发、专项池、人物重叠、真实 HTTP 接口、租约与配置变更、取消排空、关闭异常、采集端与压测工具、启动器和 GPU 日志统计。包括模型替身与 CUDA 基础路径，不把它称作 337 次真实模型精度验证。

首次合并回归为 336 passed/1 failed：短请求中，分段使用高精度 `perf_counter`，总耗时却使用本机约 15/16 ms 粒度的 `monotonic`，导致分段 5.31 ms、总耗时 0 ms。现已为工位 HTTP 入口保存独立高精度起点，总耗时、beforeWorker 和 transport 耗时统一使用它；过期、租约和历史时间仍使用原来的单调时钟。测试显式冻结低精度时钟，验证模型实际等待仍计入总耗时。修复不改变推理、规则或采样期限；上述六路吞吐/请求延迟已使用高精度压测时钟，不受此显示修复影响。

关闭流程覆盖 scheduler、transport、专项、场景、共享人体池、证据存储和数据库；调用者取消仍等待实际工作结束，各阶段及多副本逐个尽力回收后再传播首异常。自建人体池由所属专项回收，共享人体池最后统一回收。故障注入测试包含多次取消、执行器已关闭、同步失败和推理与清理同时失败。

人员并行和工具的 CPU/模拟组合回归已记录为 50 passed（13.87 s），对应 `test_station_tools.py`、`test_person_overlap.py`、两个 same-frame 人员共享帧用例组及 `test_specialist_pool.py`。这里核查的是连接、计数、同帧、异常排空和关闭行为；与 457 帧真实模型等价验证的职责不同。之前场景并发回归与主任务其他组合存在重叠，不能累加成独立测试总数。

| 验证项目 | 当前状态 | 证据或边界 |
| --- | --- | --- |
| 统一条件六路 API A/B | 已完成，均未达六路各 10 fps | 见本文六路表及 six-station-comparison.json |
| 真实浏览器流水 A/B | 已完成 A-B-B-A，四轮通过 | 单路贴条约 9.93–9.95 fps；见 browser-ab-summary.json |
| 最终合并回归 | 已完成，337 passed | 见 final-regression-corrected.xml |
| 本机主服务恢复与冒烟 | 已完成 | API、Worker、数据库 ready；模型配置 CUDA-only，运行参数与工位目标见下文 |
| 目标主机六工位验收 | 未验证 | 双路 Xeon/RTX 4090 上六台真实终端、六个实际模型、真实视频/相机输入及同时运行条件；本机重复 JPEG 和单浏览器结果不能替代 |

本机六路重复 JPEG 的实测最高合计为 35.700 fps；每路 10 fps、合计 60 fps 尚未达标。人员并行减少了两段开发视频的服务耗时且逐帧业务结果一致，场景计时适配器没有吞吐收益而未采用。目标主机仍需六个实际模型与六台终端共同验收。

本轮从 2026-11-07 持续至 **2026-11-08 00:00（UTC+8）**。GPU 测试进程全部退出后，使用 `node tools/python.cjs tools/start-local.py` 恢复本机 8000 API 和 Worker，并验证 `/health`、`/ready`、零在途/排队与无运行中的离线或训练任务。主服务继续使用全局/人体/专项 3/3/3、场景缓存 1、OpenCV/Torch 4/4；保留原来的 Worker 任务策略。没有把示例六路调参结果直接套用为未知模型的最终配置，也没有连接目标 RTX4090 主机。

按用户的 10 fps 目标，2026-11-08 00:00:43（UTC+8）把已确定的工位 1 螺丝、工位 2 贴条的 `sampleFps` 从 6 调为 10。仅更新采样配置，模型与设备凭据保持有效，其他四个工位未修改。页面刷新后下一轮采用新目标；原生采集器需重启读取配置，显式 `--fps` 仍会限制其上限。此配置不是六路实际帧率达标证明。恢复检查和配置前后记录分别见 [main-restored.json](../../test-results/system-throughput-20261107/main-restored.json)、[local-target-update.json](../../test-results/system-throughput-20261107/local-target-update.json)。
