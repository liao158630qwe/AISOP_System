# 六台工位机集中推理优化记录（2026-11-07）

用户确认的部署为六台工位机采集、双路至强＋RTX4090 集中推理，模型尚未确定，不能预设为三路螺丝加三路贴条。用户回复“10”按每工位有效 10 FPS、总计 60 FPS 作为目标；接入六路或设置采样率不等于已达标。

## 本轮代码

- `backend/scene_realtime.py`、`backend/scene_model_cache.py`：将自定义场景的全程全局锁改为短缓存锁、模型独占租用和流独占保护。模型键包含所属用户、模型 ID、权重 SHA、规范路径、任务与设备。不同模型使用独立工作线程与 CUDA stream；同一模型实例的加载、可变 predictor 和执行互斥。缓存只驱逐空闲条目，忙碌时返回 429，不新增无界模型或推进流时间戳。失败清理、在途流释放与服务关闭均有回收路径。
- `backend/station_scheduler.py`、`backend/terminals.py`：工位开流冻结模型资源键。同模型在调度层排队，忙模型不阻挡其他可执行模型；每工位仍只保留一张最新待处理帧，替换保留排队位置。模型版本和租约前后校验保持生效。
- `backend/transport.py`、`backend/app.py`：工位流与通用实时入口共享总请求容量，同时保留每类容量限制。总容量为 `min(STATION_INFERENCE_CONCURRENCY, REALTIME_POOL_SIZE)`，场景容量再受 `SCENE_MODEL_CACHE_SIZE` 限制。已接纳的通用请求独立持有名额，HTTP 调用者取消不会提前释放仍在运行的推理；关闭等待请求排空后回收模型。
- `tools/station-collector.py`、`tools/benchmark-stations.py`：原生采集端和压测端复用同源 HTTP(S) 连接，HTTPS 保留证书验证，完整读取响应以便复用；连接异常不自动重放 POST，下一次调用可重建连接。退出与初始化失败均关闭连接。浏览器已有自己的连接复用机制，不能把此项作为浏览器吞吐提升证据。
- `algorithms/screw_refined.py`：主体锚点校验只变换三个实际需要的搜索块，保留原有坐标、插值、边界、阈值与业务规则。GPU 显存显示继续保持移除。

## 验证

相关服务回归 **226 passed，59.08 秒**，含场景、调度、真实 HTTP 接口、流隔离、取消/关闭、采集器和既有实时功能。测试结果保存在 `test-results/six-workstation-4090-prep-20261107/regression.xml`。

```powershell
node tools/python.cjs -m pytest tests/test_scene_concurrency.py tests/test_scene_logic.py tests/test_scene_management.py tests/test_scene_station_integration.py tests/test_station_model_scheduling.py tests/test_station_terminals.py tests/test_specialist_pool.py tests/test_transport.py tests/test_transport_cancellation.py tests/test_station_tools.py tests/test_realtime.py -q --junitxml=test-results/six-workstation-4090-prep-20261107/regression.xml
```

真实 HTTP 集成包含：工位 1、2 使用同模型不同规则，工位 3 使用另一模型；工位 1 阻塞时工位 2 排队、工位 3 并行，三请求都成功；两个用户与场景证据互相隔离。另验证两入口共享容量、模型变更拦截、异常释放、取消后真实工作完成才释放名额。测试使用模型替身，证明执行与状态约束，不测 GPU 吞吐。

本机 **RTX5070Ti** 两个真实模型（`yolo11n.pt`、`screw-head-v1.pt`）并行冷加载及两轮缓存复用，共 **6/6 非计时输出与改造前串行实现完全一致**；独立线程和 CUDA stream、仅加载两次、无驱逐和忙碌拒绝、关闭后驻留数为零。螺丝模型在该 smoke 图片上无观测并返回 REVIEW，此项不是准确率验证。报告与复现脚本分别为 `scene-cuda-smoke.json`、`smoke_scene_cuda.py`，位于上述结果目录。

螺丝局部变换验证：**23 passed、1 skipped**（双卡事件用例因仅有单 GPU 跳过）；完整开发视频 **215/215 帧非计时输出一致**，原版/候选最终均 PASS。两套真实标定、遮挡/全黑样本及 NumPy/CudaFrame 路径均对照。本机 GPU 单路检测耗时中位数 **123.554 → 120.328 ms，降低约 2.61%**，P95 **142.222 → 138.629 ms**。结果见 `screw-anchor-validation.json` 和 `screw-anchor-comparison/report.json`，不能外推到六路或目标 4090。

`git diff --check` 对本轮相关文件通过。工作区含大量既有修改，本轮没有重置、提交或清理无关文件。

## 部署和性能边界

本轮未重启本机 8000 服务，也未连接或部署目标 RTX4090 服务器。[候选环境配置](../../deploy/rtx4090-six-stations.env.example)没有自动应用；它保留全局并发 3、场景缓存 1，默认场景并发仍为 1。实际六模型合计显存允许时才设置缓存 6；全局并发 3 时代表最多驻留 6、执行 3。

当前通用场景仍调用 Ultralytics `model.predict`，其计时路径包含设备同步。因此独立 CUDA stream 和请求并发不保证 GPU 内核全程重叠；本轮未替换通用场景的预处理、后处理和精度设置。专项 `cuda_yolo` 的路径不同。未实施跨流合批、人体检测降频或 TensorRT。

原生采集器仍在同一循环读取与编码，当前最新帧时间戳不包含相机曝光前后的驱动缓存时间；调整采样率需重启采集器或刷新独立终端页。原生持续采集还需现场明确每轮检测的开始、结束与归档协议。

下一步需要实际六模型、相机输入和目标服务器：先逐模型确认正确性、耗时与显存，再测完整组合及六机端到端链路。逐路验收有效 10 FPS，记录 P95、结果年龄和最长无结果间隔；压测工具 95% 的帧率容差不代表正式达标。详细配置及验收步骤见[六工位指南](../guides/six-station-realtime.md)。
