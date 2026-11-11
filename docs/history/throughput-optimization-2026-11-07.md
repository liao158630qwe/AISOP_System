# 有效检测帧率与 GPU 链路优化（2026-11-07）

本轮已减少贴条检测中的重复像素计算、逐位置 GPU 同步、前端画布重建及服务线程切换，并允许管理员调整采样率时保留设备凭证和终端登录。最有说服力的算法对照是同一开发视频逐帧交替运行：242 帧输出严格一致，JPEG 解码加检测耗时中位数从 102.064 ms 降至 88.084 ms，下降 13.697%。真实 Edge 浏览器回放中，候选代码将贴条目标采样率从 4 调至 6 fps 后，页面实际接收帧率从 3.985797 升至 5.960805 fps。

这两项证据回答不同问题：逐帧交替对照验证本轮算法改动的性能和输出一致性；浏览器实验验证候选系统在该视频、该主机、该 30 秒片段上能够接近 6 fps。浏览器实验的两轮均使用候选代码，不能把约 49.55% 的帧率提升全部归因于代码优化。螺丝检测尚无本轮稳定提速的证据。GPU 占用率用于定位资源利用情况，业务目标仍是及时返回有用的检测结果。

**改动及原因。**

| 部分 | 实际改动 | 保留的边界 |
| --- | --- | --- |
| 贴条局部变换 | `algorithms/cuda_vision.py` 增加 `warp_regions`，只计算后续需要的局部区域；缓存坐标网格并处理 CUDA 流间等待及缓存生命周期 | 沿用完整变换的坐标与插值规则；没有缩小定位输入或调整模型阈值 |
| 贴条特征及观测 | `algorithms/cuda_actions.py` 将前景掩码、动作特征、前景比例和动作签名成批计算；`algorithms/sticker_actions.py` 在检测路径使用局部裁剪并合并小结果回传 | 每个位置的类别、动作时间、释放/确认规则保持原有语义；`locate` 默认仍返回完整对齐图 |
| 分类校验 | `algorithms/gpu_classifier.py` 将同设备张量有限性校验合并，减少逐位置等待 | 非有限输入仍拒绝；保留各位置独立模型参数和概率求解规则 |
| 服务处理 | `backend/terminals.py` 将推理前租约、模型版本校验和推理放在一次线程池任务中；续租复用同一次数据库时间读取；补充分段耗时 | 推理前后版本/租约检查、过期帧拒绝、工位串行、结果与预览同时发布继续有效 |
| 浏览器采样 | `frontend/station-detection.js`、`frontend/station-frame-worker.js` 复用画布及上下文，仅尺寸改变时重设宽高；每帧仍清除旧图及旧标注 | 保持单请求在途、画面与响应对应、结束后停采样；本轮未引入采样与上传重叠流水线 |
| 可观测性 | 页面运行详情展示采图缩放、JPEG 编码、HTTP 往返、服务器处理和目标间隔；服务返回 `serviceTimingsMs` | 服务器时间已包含在 HTTP 往返中，不能再次相加；不同计时器的精度和统计分位数也不应直接相加 |
| 采样配置业务 | 仅改变 `sampleFps` / `maxSampleGapSeconds` 时保留凭证 hash，将当前版本终端会话同步至新 revision，新轮使用新参数并清除旧 finished state | 活动流仍拒绝修改；旧版本会话不会复活；设备、来源、模型、预览或启用状态变化仍旋转凭证；no-op 行为保留 |
| 压测工具 | 增加固定到达与单在途串行两种模式、测量起止时间、GPU 逐秒记录、来源目录；补充真实浏览器回放探针 | API 测试使用预编码重复图，不能替代网页或摄像头端到端帧率 |

**贴条完整视频逐帧对照。**

原始证据位于 [video-full/report.json](D:/Project/AISOP/test-results/throughput-optimization-20261107/video-full/report.json)、[manifest.json](D:/Project/AISOP/test-results/throughput-optimization-20261107/video-full/manifest.json) 和同目录 `frames.jsonl`。输入为 `lv_0_20261017162449.mp4`，原视频 2160×3840、约 60.47 秒，以 4 fps 取 242 帧，统一按 1920 长边、2073600 像素上限、JPEG 质量 82 编码。原始视频 SHA-256 为 `06104b7e2081121418d777d7bfa0931edecc30870a176a6f173af51c90183e6e`。

`original` 加载本轮备份 `before-vision/cuda_vision.py` 及 `before-gpu/` 下三个模块；`candidate` 加载工作区模块。manifest 保存双方实际模块路径和 SHA-256。每一帧使用相同 JPEG，原版/候选先后次序交替，计时前后同步 CUDA。测试检查结束时模型资产和代码均未变化。

| 指标 | original | candidate |
| --- | ---: | ---: |
| JPEG 解码＋检测中位数 | 102.064 ms | 88.084 ms |
| JPEG 解码＋检测 P95 | 125.176 ms | 106.392 ms |
| 检测本身中位数 | 100.783 ms | 86.219 ms |
| 定位成功的 127 帧检测中位数 | 110.364 ms | 93.316 ms |
| 匹配帧数 | 242/242 | 242/242 |

定位、目标、展示目标、inspection、动作时序、累计结果和合规判定均为零差异；比较仅排除计时字段，保留动作时间戳。两者最终均为 FAIL，缺少 R01、R05 动作。这说明优化没有改变该样本的既有判断，不能作为独立准确率验证，也未解决原有漏观察问题。计时不包括原视频解码、缩放、JPEG 编码、时序更新、HTTP 或页面显示，不能取倒数直接宣称网页 FPS。

另保留了 [video-short/report.json](D:/Project/AISOP/test-results/throughput-optimization-20261107/video-short/report.json)：前 20 帧输出也一致，但均未成功定位，中位数为 70.247→71.788 ms，未显示提速。完整视频结果不能外推为每一帧、每种工况都获得相同收益。

**两路 API 压测全部记录。**

以下覆盖本轮所有带 `measurementStartedAt` 的 `mixed-*` 报告，位于 `D:/Project/AISOP/test-results/station-gpu-benchmarks/`。每轮 30 秒，两路依次为螺丝、贴条，硬件为本机 RTX 5070 Ti；总并发/人体池/专项池均为 3/3/3，OpenCV/Torch 线程均为 4，队列等待上限 500 ms。先顺序加载，再执行三轮并行预热，重新建立流后开始计量。各目录保存 `report.json`、`settings.json`、`gpu.jsonl`、`scheduler.json` 等原始材料。

表中 `original` 的 `settings.sourceRoot` 为 `D:/Project/AISOP/test-results/throughput-optimization-20261107/baseline-source`，`candidate` 为 `D:/Project/AISOP`。这些 API 报告记录的是相应时点的来源目录，不能替代 video-full 中逐模块 hash 固定的交替对照。

| 测试目录 | 版本 | 模式 | 每路目标 FPS | 螺丝/贴条有效 FPS | 螺丝/贴条响应 P95 ms | 螺丝/贴条错误 | 整卡 GPU 均值/P95 |
| --- | --- | --- | ---: | --- | --- | --- | --- |
| `mixed-5keooah0` | original | fixed | 8 | 1.967 / 3.967 | 219 / 203 | status 0 ×1 / 0 | 57.75% / 77% |
| `mixed-k5ofgnjm` | candidate | fixed | 8 | 3.967 / 3.967 | 219 / 203 | 0 / 0 | 62.75% / 89% |
| `mixed-xgj7h0mq` | original | serial | 8 | 4.467 / 4.633 | 265 / 234 | 0 / 0 | 77.96% / 94% |
| `mixed-_dfoayqx` | candidate | serial | 8 | 4.367 / 5.867 | 281 / 188 | 0 / 0 | 76.86% / 91% |
| `mixed-da0u_6lt` | candidate | serial | 6 | 4.833 / 5.733 | 266 / 188 | 0 / 0 | 77.29% / 93% |
| `mixed-_irkqnyg` | original | serial | 6 | 3.833 / 3.867 | 390 / 313 | 0 / 0 | 79.46% / 99% |

GPU 统计只选各报告 `[measurementStartedAt, measurementFinishedAt]` 时间窗内 `gpu.jsonl` 中 GPU 0 的成功读数，各轮均为 28 个样本；均值为样本算术平均，P95 采用 nearest-rank。完整日志包含预热期间读数，未把预热混入表中。上述 GPU 是 `nvidia-smi` 的整卡利用率，包含其他进程、桌面和游戏负载，不是 AISOP 进程专属占用，不能把它上升直接解释为检测效率提升。

`fixed` 在固定时刻尝试提交，上一请求未结束时记为 skippedBusy；`serial` 每路独立维持一个在途请求，完成后等待目标周期的剩余时间，相关跳帧计数为 null。有效 FPS 只计 30 秒窗口内完成的成功请求；响应 P95 统计成功请求，包含测量窗口结束后才收尾的成功请求，不包含失败耗时。两种请求节奏的 FPS 不能混为同一基准。

固定 8 fps 的 original 螺丝路出现一次 status 0 连接错误和 15281 ms 无结果区间，对应本轮遇到的约 15 秒请求超时，失去干净基线资格。因此不能将 1.967→3.967 fps 宣称为螺丝翻倍。成功请求 P95 仍为 219 ms，正说明只看 P95 会漏掉这次长停顿。

串行 8 fps 下，贴条从 4.633→5.867 fps、响应 P95 从 234→188 ms；螺丝则为 4.467→4.367 fps、P95 265→281 ms，未显示稳定改善。6 fps 复测先 candidate 后 original，original 的螺丝 inspection P95 从另一轮的 190.81 升至 274.97 ms，贴条 registration P95 从 129.82 升至 183.45 ms，整卡 GPU P95 达到 99%；多个阶段同时变慢，表明共享主机运行条件发生明显变化。不能把 6 fps 两轮差值全部归因于本轮代码。六份报告 `allTargetsMet` 均为 false；其中仅 candidate 串行 6 fps 的贴条路单独达到工具的 95% FPS 容差及 500 ms 延时要求。

这些测试发送的是两张重复的预编码 JPEG，包含真实 HTTP 和模型推理，但排除了相机采集、视频解码、浏览器采图/编码及页面显示。它们用于判断 API 容量和请求节奏，不能称为摄像头或网页的端到端帧率。

**真实浏览器回放。**

原始报告为 [station-throughput-ui.json](D:/Project/AISOP/test-results/pilot-browser-tests/run-ygpwvhwb/station-throughput-ui.json)，`passed=true`。使用真实 Edge 157.0.4341.42、同一开发视频、1×速度回放和候选 CUDA 模型，实际经历视频解码、canvas 采样缩放、JPEG 编码、HTTP、模型检测及 UI 接收。测试使用隔离临时数据库，先预热，再依次执行目标 4/6 fps；模型加载、预热和停止后的归档时间不计入运行窗口。

| 候选代码目标采样率 | 实际运行窗口 | 成功 HTTP/页面接收帧 | 页面有效 FPS | 定位成功帧/定位成功 FPS | 目标达成 |
| --- | ---: | --- | ---: | --- | --- |
| 4 fps | 30.1069 s | 120 / 120 | 3.985797 | 57 / 1.893254 | 是 |
| 6 fps | 30.0295 s | 179 / 179 | 5.960805 | 89 / 2.963752 | 是 |

定位成功 FPS 是 `registered=true` 的帧数除以完整运行窗口，不能与所有成功返回帧的 FPS 或工序 PASS 率混同。页面指标分别显示 3.99 和 5.96。两轮最大在途请求数均为 1，HTTP 错误、帧身份错配、页面错误和观测器错误均为零；停止采样后新增请求为零，释放流后继续观察未出现上传，最终已归档且页面无活动流。

| 前端阶段（中位数 / P95） | 目标 4 fps | 目标 6 fps |
| --- | ---: | ---: |
| 采图及缩放 | 0.2 / 0.3 ms | 0.2 / 0.3 ms |
| JPEG 编码 | 15.35 / 19.5 ms | 15.6 / 19.0 ms |
| HTTP 请求往返 | 136.9 / 157.1 ms | 127.3 / 151.7 ms |
| 服务器处理（已含于往返） | 125 / 157 ms | 125 / 141 ms |
| 采样至结果到达 | 153.1 / 177.5 ms | 143.6 / 168.9 ms |
| 服务队列等待 | 0 / 0 ms | 0 / 0 ms |

服务内部模型阶段中位/P95 分别为 121.929/142.659 ms 和 111.934/133.914 ms；推理前校验中位数约 3.6 ms，推理后约 3.6–3.8 ms。采图、编码和推理都有成本；原目标 4 fps 对应 250 ms 周期，即使一次结果约 150 ms 返回，仍会等待下个采样时刻，这会使 GPU 出现空档。提高目标采样率能利用该余量，本轮浏览器结果支持这台主机上的贴条 6 fps 设置，但不保证所有相机、视频和并发组合都达到相同表现。

两轮都在约 30 秒由测试主动点击结束，而完整视频约 60.49 秒；最终 FAIL 缺少 R01–R06 动作，是截断观察后的归档结果，不能解释成完整工序测试失败或准确率下降。实际完整视频的原版/候选输出一致性应看前述 242 帧对照。浏览器实验仅覆盖一段开发视频、一个浏览器及单路贴条，4→6 fps 顺序执行也包含主机负载变化；只读观测器和每轮一次截图的额外开销未做校正。真实摄像头、多路现场及完整工序仍需独立验证。

**回归及交付状态。**

| 验证 | 结果 | 说明 |
| --- | --- | --- |
| 本轮先前组合回归 | 117 passed，1 skipped | 采样配置业务调整前的组合结果；不与后续回归累加 |
| 采样配置及工位回归 | 46 passed，23.80 s | 原凭证与当前终端登录继续可用、新采样参数生效、活动流拒绝、其他配置旋转、旧 finished state 清理 |
| 压测工具回归 | 10 passed | 包含 fixed/serial 请求节奏及单在途行为；已包含在下列合并回归中 |
| 最新服务合并回归 | 103 passed，41.56 s | 工位 46、传输 15、工具 10、取证存储 19、专项池 13；XML 记录 testsuite 为 41.542 s，零失败/错误/跳过 |
| CUDA 等价性专项 | 22 passed，9.76 s | 掩码、特征、前景比例及签名精确一致；分类概率容差 1e-12；完整变换/局部裁剪及跨流缓存一致性 |
| 模拟推理 UI 回归 | 通过 | 自动完成、工位切换、计时展示及状态清理；不计为真实模型性能结果 |
| 真实浏览器贴条回放 | 通过 | 4/6 fps 两轮共 299 次成功响应均被 UI 接收；无并发上传或停止后继续上传 |

最新合并回归证据为 [service-regression.xml](D:/Project/AISOP/test-results/throughput-optimization-20261107/service-regression.xml)，CUDA 专项记录为 [gpu-batching-validation.json](D:/Project/AISOP/test-results/throughput-optimization-20261107/gpu-batching-validation.json)。这些测试组合存在重叠，不能相加为独立覆盖数量。代码改动前备份位于本轮 `before-frontend`、`before-gpu`、`before-vision`、`before-service`、`before-serial-bench` 和 `before-sampling-tune` 目录。

已在确认六工位全部离线、推理队列为空、无运行或排队的离线分析/训练后，使用项目启动器重启本机 API 和 Worker。上线记录见 [main-deployed.json](D:/Project/AISOP/test-results/throughput-optimization-20261107/main-deployed.json)：工位1继续使用螺丝模型、目标 6 fps、revision 5；工位2继续使用贴条模型，目标由 4 调至 6 fps、revision 1→2。采样调整返回 `credential=null`，保留原设备凭证和终端登录；模型及其余绑定参数保持原值。API、数据库、Worker 健康均为 ready。

上线后以独立临时实时流分别提交 3 帧螺丝、3 帧贴条参考图，6 帧均成功定位，CUDA-only 运行策略核验通过，随后释放临时流并退出临时登录；记录见 [main-smoke.json](D:/Project/AISOP/test-results/throughput-optimization-20261107/main-smoke.json)。贴条前两帧约 1.5 秒，包含冷启动开销，第三帧服务器耗时约 101 ms；这不是持续吞吐验证，前述预热后 FPS 不代表重启后的第一帧延迟。另通过真实绑定编辑页面回归（`test-results/pilot-browser-tests/run-q6hp3aqv`），覆盖保存、取消、版本冲突、活动流阻止修改及场景模型切换。GPU 显存显示继续保持移除，只展示 GPU 占用率。
