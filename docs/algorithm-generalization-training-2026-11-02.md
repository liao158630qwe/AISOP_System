# 多场景算法优化、训练与适配

日期：2026-11-02。以当前工作区实现为准。

目标是复用训练、证据和评测流程，让不同场景选用合适的模型。准确率和实时性都是准入条件；只有同时满足场景要求的候选才进入部署比较。新模型在独立数据上的指标需要实测，本次没有宣称现场准确率提升。

## 1. 本次已落地的改进

| 改进 | 实现 | 作用与边界 |
| --- | --- | --- |
| 资源快照缓存 | `algorithms/asset_snapshot.py`，接入螺丝、贴条 | 每帧检查配置及所有依赖的文件签名，变化时重新完整哈希；覆盖多机位参考图、局部专用权重及材质分类器；返回深拷贝，避免调用方污染缓存 |
| 有限时间证据窗 | `algorithms/evidence.py`，接入螺丝历史 | 未确认候选受最大相邻间隔与总时窗约束；未知不投票；保留本轮已确认历史；类别名称不写死在证据模块中 |
| 动作结束/开始区间核验 | `event_precedence()`，接入贴条动作 | 记录最后接触、首次释放及起点下界；明确重叠为 FAIL，采样区间无法确定先后为 REVIEW，可靠先后才允许 PASS |
| 特征复用 | `algorithms/sticker_actions.py` | 预存参考灰度、参考 ROI 和 HOG；前景掩码每 ROI 只算一次；不改变特征定义与已有模型输入 |
| 分段耗时 | 两个专项实时服务 | 新增 `totalProcessingMs` 和 `timingsMs`，覆盖解码、快照、模型加载、流状态、人体、专项检测、规则；保留旧 `processingMs` 字段 |
| 通用训练工具 | `algorithms/training.py`、`tools/train-model.py` | 配方选择模型/任务；独立数据审计、候选训练、显式测试集评估；生成权重/数据/配方哈希与模型卡；不会自动替换生产权重 |
| 准确率与延迟共同约束 | `select_candidate()` / `select` 子命令 | 先检查缺陷召回、整件误放行、端到端 P95 三个约束，再按召回、误放行、延迟排序；无合格候选时明确返回空结果 |

此处的通用化已经覆盖训练与部分算法组件。网页任务路由仍保留 `pose/screw/sticker`，新任务的实时服务、输出字段、规则和 UI 尚需接入；不能把“训练工具支持”理解为“任意场景已可直接在页面上线”。

## 2. 为什么优先做这些优化

当前专用模型并非唯一瓶颈。每帧对约 74–85 MiB 资产重算哈希，消耗了大量帧预算；这些工作不增加图像证据，可以消除重复计算。相反，直接减少输入分辨率、减少检测频率或换更小模型，会影响小目标与短动作，需要单独验证。

当前部分旧训练脚本按同一视频的帧进行留出。新工具要求按完整来源与采集组隔离，防止相邻帧、同一动作裁剪和增强副本跨集合。旧脚本保留作历史复现，新场景使用新入口；历史开发结果不能重新标为独立测试结果。

已有“一次接触”和“接触后离开”只是动作证据。螺丝头可见不能证明扭矩，贴放动作不能证明贴片存在或粘附。新工艺应分别定义可观测的视觉结果与需要工具/设备信号确认的质量要求。

## 3. 场景与模型适配表

| 场景需求 | 训练任务/候选方案 | 标注与训练方法 | 当前接入程度 |
| --- | --- | --- | --- |
| 零件有无、数量、工具定位 | `ultralytics` / `detect` | 框标注；预训练微调；补真实空位、相似件、遮挡与难负样本；按目标像素尺寸选择 ROI 与分辨率 | 新统一入口可训练；现有检测器需显式配置类别和输出映射 |
| 胶路、涂覆、轮廓、覆盖面积 | `ultralytics` / `segment` | 多边形标注；测量边界与覆盖率；小缺口单独验收；物理尺寸需标定 | 新统一入口可训练；分割结果到测量/规则的适配器待接入 |
| 端子、长条零件方向 | `ultralytics` / `obb` | 四角旋转框；加入错向/翻转样本；定义角度周期和允许误差 | 新统一入口可训练；业务方向判定待接入 |
| 人体/手部/工件关键点 | `ultralytics` / `pose` | 关键点、可见性；固定骨架拓扑；缺失点使用 mask；按机位验证 | 新统一入口可训练；自定义关键点不能直接当作现有 COCO-17 |
| 已定位 ROI 的有限状态分类 | `ultralytics` / `classify` | 整图类别；包含遮挡/未知/易混淆状态；保持训练和推理裁剪一致 | 新统一入口可训练；类别按模型卡索引映射 |
| 少量结构化视觉特征分类 | `sklearn` / `feature-classify` | 一样本一个 `.npy` 向量；仅 train 拟合标准化；类别平衡逻辑回归；验证集比较正则强度 | 新统一入口可训练；须实现相同 `feature_schema` 的在线提取器 |
| 接触、持续操作、释放、短动作 | 已有因果 TCN / 关系特征 TCN，或新增时序适配器 | 完整周期序列；动作/位置/边界/质量标签；因果左侧上下文；按周期、人员分组；按事件评价 | 项目保留 `experiments/lamp_assembly` 研究训练路径；新工艺需调整特征、动作语义与事件解码，未纳入新图像 CLI |
| 字符/OCR | 文字检测 + 识别模型 | 行/字符标注；字符集、码制、字典和校验规则；按整串正确率验收 | 本次未实现 OCR 训练/运行适配器 |
| 未知外观缺陷、少样本异常 | 正常特征库/异常定位，再按需监督微调 | 正常域建模；真实缺陷独立验证；分产品建立阈值；异常分数不能直接当类别概率 | 本次未实现异常检测适配器 |

同一模型族新增对象类别时，主要改变数据、类别表和场景配置。同类工位换光照/相机时先评测标定与视觉域变化；新模态或新任务才增加适配器。ROI、分辨率、颜色空间、归一化、关键点顺序、特征顺序必须随模型版本冻结。

不同场景可以分别使用不同规模的模型。共享的是接口和评测协议，不要求把所有零件、动作与缺陷塞进同一个分类头。

## 4. 数据与训练流程

### 4.1 先定义标签、采集组和验收口径

对每个新场景记录：产品/工位、相机、最短有效事件、目标尺寸、正常/缺陷/不可观察状态、误放行代价、并发路数、目标硬件、延迟预算。

采集完整产品周期，覆盖人员、批次、机位、光照和遮挡；优先补会导致错误通过的难负样本。将整个视频归入同一 split，采集批次也不能跨 split。需要验证跨人员或跨机位时，将 `operator` 或 `camera` 加入 `isolation_keys`。这可能要求补采新人员/新机位，而不能通过重新命名组 ID 满足约束。

可先按组做约 60/20/20 或 70/15/15 划分，但比例不是保证。每类都须有训练支持、真实验证样本和真实测试样本。样本不足时工具会拒绝训练，不能通过相邻帧凑出三个“独立”集合。

人工复核预标注框、分割边界、动作起止和负例；派生裁剪、增强与合成样本沿用来源组，提供 `parent_ids`。合成样本只进入训练。工具检查声明的来源、组、父样本、文件路径和图像字节哈希；不同来源可能产生相同的低维特征向量，特征哈希只校验完整性，不单独据此判定泄漏。工具无法替代人工审计虚假来源信息或经过重新编码的重复视频。

### 4.2 数据清单格式

所有文件路径相对清单所在目录。完整清单需在三个集合中覆盖全部类别；下面只展示格式：

```json
{
  "schema_version": 1,
  "names": ["connector_locked", "connector_unlocked"],
  "isolation_keys": ["operator"],
  "samples": [
    {"id":"a-001", "source":"video-a", "group":"batch-a", "operator":"worker-a", "split":"train", "file":"images/a-001.jpg", "label":"labels/a-001.txt"},
    {"id":"b-001", "source":"video-b", "group":"batch-b", "operator":"worker-b", "split":"val", "file":"images/b-001.jpg", "label":"labels/b-001.txt"},
    {"id":"c-001", "source":"video-c", "group":"batch-c", "operator":"worker-c", "split":"test", "file":"images/c-001.jpg", "label":"labels/c-001.txt"}
  ]
}
```

YOLO 标签采用对应任务格式，坐标归一化到 `[0,1]`：

- detect：`class cx cy width height`；真实背景图使用显式空标签文件。
- segment：`class x1 y1 x2 y2 x3 y3 ...`。
- obb：`class x1 y1 x2 y2 x3 y3 x4 y4`。
- pose：框后附每个关键点；清单声明 `kpt_shape: [K, 3]` 或 `[K, 2]`；三通道可见性取 0/1/2。启用镜像增强时提供与拓扑相符的 `flip_idx`。
- classify：`file` 指向图像，`label` 为从 0 开始的类别整数。适配器使用补零的数字目录保证类别顺序，业务显示名称读取模型卡 `class_index_to_name`，不要把数字目录当业务名称。
- feature-classify：`file` 指向一维 `.npy`，`label` 为类别整数；所有样本维度一致且无 NaN/Inf。只可加载本地可信训练得到的 joblib 模型。

### 4.3 可执行命令

复制 `config/training/detector.example.json`，修改数据清单、已有预训练权重、任务和训练参数。所有配方路径相对配方文件；预训练文件必须已在本地。默认 `amp=false`，可在验证硬件与精度后启用；底层库的字体和可选 AMP 检查资源按其配置管理。`features.example.json` 演示特征分类配方。

```powershell
# 先查数据隔离、重复内容、标签、类别覆盖；不会启动训练
node tools/python.cjs tools/train-model.py validate --recipe config/training/my-scene.json

# 候选目录必须尚不存在；train 只把 train/val 送入训练器
node tools/python.cjs tools/train-model.py train --recipe config/training/my-scene.json --output outputs/my-scene/run-001

# 完成验证集选模和阈值/事件规则冻结后，显式运行一次独立测试
node tools/python.cjs tools/train-model.py evaluate --run outputs/my-scene/run-001 --output outputs/my-scene/test-001
```

输出包括 `recipe.json`、`dataset-audit.json`、`model-card.json`、候选权重及独立的 `test-evaluation.json`。模型卡记录种子、依赖版本、数据/模型/配方哈希、训练耗时和验证结果。模型或数据变化后拒绝复用旧 run 评估。

`train` 会对全部来源做完整性审计，但测试样本不参与梯度、归一化、模型选择和训练数据目录。测试命令为满足上游数据检查器，会在独立测试目录建立指向测试内容的 train/val 条目；只调用模型 `val(split='test')`，不调用训练，也不混入原训练图像。

### 4.4 建议训练方法

1. **先做同协议基线**：固定验证组，比较一到两种预训练模型、两个输入尺寸。报告各缺陷类别、各工位以及全链路延迟，避免仅用总体 mAP/准确率选模型。
2. **迁移学习**：数据较少时先训练头部/浅层适配，再用较低学习率全模型微调；是否冻结、冻结哪些层由所选模型结构决定。配方支持 `freeze`；不要把某个模型的层编号复制给所有架构。
3. **处理不平衡**：优先补真实少数类与难负样本；按完整周期采样，避免重复正常帧主导。特征基线已用 `class_weight='balanced'`。对检测/时序损失的重加权或 focal loss 应在对应训练适配器内实现并验证，本次未自动改写底层损失。
4. **增强按工艺设置**：默认关闭镜像和 Mosaic/MixUp，避免破坏左右安装方向、文字和几何关系。亮度、色温、轻微透视、模糊等增强需匹配现场变化。ROI 裁剪保留目标边缘及必要上下文。
5. **动作时序模型**：输入可组合归一化关键点、手/工具到操作位的相对距离、速度、目标状态、可见度和证据年龄；缺失值通过 mask 表示。训练因果 TCN/轻量时序模型，输出动作/位置/边界；SOP 顺序仍由显式规则核对。训练回放必须按线上采样率和缺帧模式重放，不能使用未来帧提高离线分数。
6. **阈值与拒识**：仅在验证集选择每类阈值、证据时窗及不确定区域；对遮挡、模糊、定位失效和分数接近的结果保留 UNKNOWN/REVIEW。模型 softmax/SVC 分数不等于可靠校准概率；若做校准，另留按组隔离的校准数据。
7. **冻结与验收**：冻结模型、预处理、阈值和规则后用独立测试周期评估；如果根据测试错误调参，这批数据随后只能作开发回归，需另留新的最终测试。

已有灯具时序研究入口示例（清单采用该实验模块的 NPZ 周期协议，不是上面的图像清单）：

```powershell
node tools/python.cjs -m experiments.lamp_assembly.train --manifest datasets/my-actions/manifest.json --kind relation --output-dir outputs/my-actions/candidate --epochs 80 --patience 10 --device cuda
```

这一路径仍包含灯具动作/位置的既有语义，新工艺要审查 `data.py`、模型头和 `decode_operation_events()`；不能仅更换名称就宣称通用动作识别已完成。

## 5. 实时性优先级与误差控制

建议按收益和精度风险依次处理：

1. 缓存静态资源与模型实例，复用预处理，按模型/尺寸/阈值合批 ROI；已有 `Inspector.detect_regions()` 可以复用。
2. 定位工件后对小目标裁剪放大；必须以当前帧定位为依据，定位失效进入未知。避免把整幅 4K 图缩得过小导致螺丝或端子消失。
3. 根据最短事件设置采样率。例如持续约 200 ms 的事件，如要求约三次观测，15 Hz 只可作为起步估算；还要为采样相位、丢帧和遮挡留余量，并测量最坏确认延迟。相机预览 30 FPS 不代表算法处理 30 FPS。
4. 多模型调度只降低非关键观察频率；低频结果带时间戳与有效期，不能用旧结果给当前工件投票。出现超载时丢弃等待处理的旧帧并记录覆盖率，不堆积延迟，也不把未观测动作当正常。
5. 对满足精度基线的候选，再试 FP16、ONNX/TensorRT/OpenVINO 或蒸馏；INT8 使用代表性训练/校准数据，并复测小目标、阈值附近样本和事件误放行。部署加速后需重新验收，不能由权重相同推导输出完全一致。

证据时窗目前可通过螺丝配置的可选字段调整，线上与离线共用：

```json
{"confirmation": {"frames": 2, "max_gap_seconds": 2.0, "window_seconds": 4.0}}
```

这些是默认值，不是所有工艺的推荐值。低采样率、短事件或不稳定网络须在验证集上重新确定。确认历史按工件轮次隔离；工件更换需开始新轮次。

`totalProcessingMs` 只包含服务端此次调用，不包含相机曝光、采样等待、上传网络与浏览器绘制。端到端 P95 应从采集时间测到结果可用时间，覆盖预热、稳态、多路竞争和事件确认延迟；单独报告冷启动。

## 6. 用准确率与延迟共同选模

每个场景至少评估：关键缺陷召回、正常件误拒收、整件误放行、UNKNOWN/REVIEW 比例、事件 precision/recall/F1、顺序错误检出、确认延迟，以及端到端 P50/P95/P99。按人员/批次/相机/光照分别报数；工件级误放行不能用框级 precision 替代。

示例 `candidates.json`：

```json
[
  {"id":"detector-s-640", "protocol_id":"sceneA-val-v1-gpuX-2cams-e2e", "defect_recall":0.992, "false_accept_rate":0.003, "end_to_end_p95_ms":85},
  {"id":"detector-m-960", "protocol_id":"sceneA-val-v1-gpuX-2cams-e2e", "defect_recall":0.997, "false_accept_rate":0.001, "end_to_end_p95_ms":180}
]
```

```powershell
node tools/python.cjs tools/train-model.py select --candidates candidates.json --min-recall 0.99 --max-false-accept 0.005 --max-p95-ms 100
```

这些数字只演示格式。`protocol_id` 必须绑定相同冻结数据、硬件、并发数和延迟统计范围，并记录每个候选的输入尺寸/ROI 策略；不能混合单帧 GPU 推理时间与全链路时间。工具检查 ID 一致，但真实性需要评测记录支持。无合格候选时退出码为 2，不自动放宽指标或启用大模型。

通用训练 CLI 输出的检测指标仍是上游库的任务指标，特征分类器输出帧/样本分类指标；这些不能直接填为 `defect_recall` 和 `false_accept_rate`。应再用完整周期回放产生场景级报告，按验证报告选候选，独立测试作最终验收。

如果没有足够真实缺陷，必须报告样本数和不确定性。零误放行不等于真实误放行率为零；不能仅凭一条正确视频承诺产线准确率。

## 7. 新模型的工程适配边界

新 Ultralytics 同族模型通过配方中的本地 `.pt` 或模型 `.yaml`、`task` 和训练参数切换。预训练任务不匹配时拒绝训练；不假定所有模型有同一输入尺寸、类别或关键点拓扑。

其他模型框架在代码中实现 `validate(recipe)`、`train(recipe, audit, output)` 和 `evaluate(recipe, audit, output, model_path)`，注册到 `ADAPTERS`。检测框架可复用检测标签协议；OCR、异常、音频或时序等新任务还需自己的数据校验/准备器，不能将其错误映射为 `detect`。

运行端适配器另行负责：输入预处理、模型加载与设备选择、类别/坐标/关键点映射、质量/拒识、每流状态、输出证据及计时。建议统一输出 `target_id/class/state/score/bbox/timestamp/model_hash/quality`，再接各场景规则。当前接口仍是既有专项结构，本次没有完成全平台场景插件迁移。

验收至少覆盖：新增第三类零件、不同比例图像、未知类别/遮挡、资产缺失/热更新、多路流隔离、输入合同不一致，以及同一批冻结数据上的新旧版本回归。训练成功的模型卡只表示候选产物完整。

## 8. 本机验证与复测

完整执行范围、测试结果、真实训练库冒烟和已有证据回放限制见 [验证记录](../outputs/algorithm-generalization-2026-11-02/verification.md)。

微基准见 `outputs/algorithm-generalization-2026-11-02/benchmark.json`：

| 环节 | 未复用 P50 | 复用后 P50 | 未复用 P95 | 复用后 P95 |
| --- | ---: | ---: | ---: | ---: |
| 螺丝资源快照 | 359.19 ms | 1.42 ms | 394.04 ms | 2.03 ms |
| 贴条资源快照 | 307.36 ms | 1.13 ms | 333.11 ms | 1.22 ms |
| 单 ROI 动作特征及前景比例 | 9.29 ms | 4.49 ms | 9.53 ms | 4.97 ms |

快照使用当前真实资产，未缓存 8 次、缓存 80 次，文件系统已热；特征使用 180×180 合成输入各 80 次并逐值校验相等。测试不含模型推理、摄像头与网络，不能换算为生产 FPS，也不证明精度提高。

```powershell
node tools/python.cjs -m pytest tests/test_generalization.py tests/test_screws.py tests/test_stickers.py tests/test_screw_people.py tests/test_sticker_people.py tests/test_config_cache.py -q
node tools/python.cjs tools/benchmark-algorithm-core.py --iterations 8 --output .runtime/algorithm-benchmark.json
```

本次规则边界修正可能将旧的 PASS 改为 REVIEW/FAIL，这是为了消除不充分的时序证据；仍须对现场完整视频重新回放确认业务影响。未重训或替换当前生产模型；新场景精度和端到端实时性需通过上述独立协议实测。

实现参考：项目锁定的 Ultralytics 8.4.155 本地 `engine/model.py`、`data/utils.py` 与既有 `experiments/lamp_assembly` 训练协议。外部说明入口：Ultralytics Train/Val/Export 文档、scikit-learn 分组交叉验证文档、ONNX Runtime 量化及精度诊断文档；使用时以实际安装版本与实测为准。
