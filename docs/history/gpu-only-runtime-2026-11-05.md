# 服务器 GPU-only 模型执行

模型推理和训练必须在服务器 CUDA GPU 上执行。现场不承担模型计算。此约束不代表服务器可以没有 CPU：操作系统、HTTP、数据库、视频编解码、OpenCV 配准/特征提取、业务规则与结果传输仍运行在服务器主机上。

## 执行约束

- API 和 Worker 启动时执行真实 CUDA 运算与同步自检。没有可用 GPU、设备编号错误、驱动初始化失败时拒绝启动，不以健康状态接收推理任务。
- 人体姿态、螺丝、贴片外观、通用场景和专用目标检测均显式选择 CUDA。旧场景 `auto` 只表示使用配置的 GPU；`cpu` 请求被拒绝，历史 CPU 场景需重新保存为 CUDA。
- 贴条动作与材质分类保留已有 SVC 权重、标准化参数及概率校准参数，使用 PyTorch CUDA 执行核计算、类别决策及概率耦合，不调用 sklearn 的推理方法。特征提取仍是 OpenCV 辅助处理。
- 后台视觉训练、独立测试评估和特征分类训练均使用 CUDA。特征分类新配方使用 `backend: torch`；历史 `sklearn` 配方兼容映射到 CUDA 训练器，旧CPU训练产物不会被偷偷用于CPU评估。
- 训练仍是数据库约束的单任务、独立子进程，保留GPU可见性。加载辅助线程为2，数据加载workers为0；默认训练PyTorch分配器上限为GPU总显存25%。超过限制任务失败，不改用CPU。该限制不涵盖CUDA上下文及第三方分配，也不是实时推理的显存保留或GPU算力配额。实时与训练共享显卡仍需混合负载验收。
- 原 `tools/train-sticker-actions.py`、`train-sticker-actions-v2.py`、`train-sticker-material.py` 的历史CPU SVC训练命令已禁止执行；后续训练使用GPU训练入口，候选必须重新通过独立测试及现场验收。已有权重仍可通过CUDA适配器推理。

## 后端兼容性

默认人体后端统一为YOLO，基础镜像已包含其依赖。RTMO/RTMPose仅在整个ONNX图可由CUDA执行时可用：会设置 `session.disable_cpu_ep_fallback=1` 并禁用运行时provider回退，逐个模型检查provider。当前本机缓存的RTMO模型包含被ORT分配到CPU的节点，因此明确拒绝加载；不要直接切换该后端。不会用另一个模型掩盖加载失败。

可选MediaPipe手部识别显式请求GPU delegate，缺少GPU支持时报告失败，不创建CPU手部推理器。该路径需要在目标Linux GPU服务器验证；本机Windows不能代表其GPU兼容性。

模型权重加载到主机、将少量结果 `.cpu()` 后返回JSON、保存权重到磁盘都是数据传输，不是模型CPU推理。研究目录下未接入服务的历史脚本不属于Compose运行入口；不要将其作为生产工作器启动。

## 部署

`.env.example` 与Compose包含：

```dotenv
POSE_BACKEND=yolo
REALTIME_BACKEND=yolo
INFERENCE_DEVICE=cuda:0
TRAINING_DEVICE=cuda:0
TRAINING_GPU_MEMORY_FRACTION=0.25
```

GPU编号是容器内可见编号。当前Compose为API与Worker各申请1张GPU，默认使用可见设备0。需要多GPU隔离训练时，应同步调整容器GPU可见性与设备编号，重新做实际训练及实时检测验收。

更新代码后在目标服务器重建API和Worker镜像。数据库初始化服务只建立数据库结构，不执行GPU推理。登录后的 `/api/v1/metrics` 中 `inferenceRuntime` 应包含 `policy: cuda-only`、`device: cuda:0`、真实GPU名称及 `cpuFallback: false`。

```sh
docker compose up --build -d
docker compose logs --tail=100 api worker
docker compose exec api python -c "from algorithms.gpu import check_cuda; print(check_cuda())"
docker compose exec worker python -c "from algorithms.gpu import check_cuda; print(check_cuda())"
```

这次修改只在本机GPU上验证，没有连接或部署到现场服务器；目标服务器的驱动、显存、摄像头路数和训练并发仍需验收。

## 2026-11-06 本机验证

- RTX 5070 Ti：18项GPU策略测试通过，129项针对性回归测试通过；灯具训练/模型及传输等45项测试通过。CUDA缺失、CPU设备请求、无CPU节点回退等路径有明确测试。
- 浏览器实际创建GPU特征训练和YOLO视觉训练：特征夹具成为待验收候选；随机图像视觉夹具按预期被精度门槛拒绝。两者模型卡均记录 `cuda:0`、`cpuFallback: false`，没有发布测试模型。
- 四组录制视频业务用例与迁移前基线的输入哈希和逐帧业务输出一致（排除时间诊断和设备名称）。其中“正确顺序”视频在基线和本次都得到FAIL，因此业务验收仍未全部通过，没有将此项记为成功。
- 人体/螺丝/贴条混合请求可处理，限流后恢复通过。短测试和其他本地验证存在重叠，耗时不能当作服务器吞吐承诺。
- 详细记录：`test-results/gpu-only-runtime-20261106/verification.json`；实际训练记录：`test-results/training-verification-20261106-000103/verification.json`。
