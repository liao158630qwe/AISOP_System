# 文档索引

先从 [项目首页](../README.md) 查看启动方式和目录说明。历史报告记录当时的实现与测试边界，当前行为以源码和使用指南为准。

## 使用与运行指南

- [工位1与工位2：模型训练、优化与实时检测方法](guides/工位1与工位2模型训练与实时检测方法.md)
- [六工位设备接入、集中推理与压测](guides/six-station-realtime.md)
- [数据库与告警录像存储](guides/database-storage.md)
- [数据库 v2：约束、迁移、备份与恢复](guides/database-architecture.md)
- [本机 PostgreSQL 配置与实库测试](guides/postgres-testing.md)
- [后端稳定性与技术协议对应说明](guides/backend-stability.md)
- [现场识别评测](guides/accuracy-evaluation.md)
- [通用动作顺序检测](guides/action-sequence.md)
- [动作时序解码](guides/action-temporal.md)
- [多场景算法优化、训练与适配](guides/algorithm-generalization-training-2026-11-02.md)
- [可选择的算法测试工位](guides/algorithm-test.md)
- [检测开始按键](guides/inspection-trigger.md)
- [8000 统一入口与本机服务恢复](guides/local-runtime.md)
- [六工位人工检查流程](guides/manual-stations.md)
- [服务器模型导入（2026-11-05）](guides/model-import-server.md)
- [SOP 与动作时序模型：公共数据集筛选及训练路线](guides/public-datasets-training.md)
- [模型与场景任务管理](guides/scene-management.md)
- [训练与场景管理：性能、并发和服务器部署](guides/management-performance.md)
- [只有视频时：上传、抽帧、标注与训练版本](guides/video-training-workflow.md)
- [服务器首版运行与验收](guides/server-quickstart.md)
- [灯具六工位检测配置与算法边界](guides/station-algorithms.md)
- [工位1人工流程试点](guides/station-one-pilot.md)
- [TCN／Transformer 时序模型升级评估](guides/temporal-model-upgrade.md)

## 研究与方案

- [六工位 Windows 触摸终端优化方案（2026-11-08）](research/six-station-terminal-optimization-2026-11-08.md)
- [六工位六路视频接入与 GPU 集中推理优化方案（2026-11-06）](research/six-station-realtime-plan-2026-11-06.md)
- [AI-SOP 算法详解与优化研究路线](research/algorithm-explanation-and-research-2026-10-14.md)
- [目标检测＋动作时序模型重训练详细方案](research/detection-temporal-retraining-plan-2026-10-25.md)
- [灯具模型训练与验证集诊断](research/lamp-model-training-diagnostics-2026-10-14.md)
- [工厂多场景模型通用化方案](research/model-generalization-plan-2026-10-18.md)
- [AI-SOP 项目模型与算法梳理](research/models-and-algorithms-2026-10-14.md)
- [人体、骨架、工人跟踪与员工动作归属接入方案](research/worker-attribution-plan-2026-10-25.md)

## 实现与验收历史

- [工位1、2模型推理优化与逐帧对照（2026-11-09）](history/station-model-optimization-2026-11-09.md)
- [整体审查：上传并发、请求恢复、确认队列与媒体输入（2026-11-08）](history/overall-review-2026-11-08.md)
- [首页工位采样、结果新鲜度与预览优化（2026-11-08）](history/capture-preview-optimization-2026-11-08.md)

- [六路螺丝：API 与六个工位页面并发实测（2026-11-08）](history/six-screw-terminal-performance-2026-11-08.md)
- [数据库 v2 本机上线与验收（2026-11-08）](history/database-v2-deployment-2026-11-08.md)
- [六路实时检测：专项并发、分类合并与对照验证（2026-11-07）](history/six-video-optimization-2026-11-07.md)
- [服务器整体审查、六路性能与实时判定修复（2026-11-07）](history/server-review-2026-11-07.md)
- [场景规则、人物计数边界与缓存性能（2026-11-06）](history/scene-logic-performance-2026-11-06.md)
- [视频训练数据工作流验证（2026-11-06）](history/video-training-2026-11-06.md)
- [六工位实时接入、恢复与 GPU 压测（2026-11-06）](history/six-station-realtime-2026-11-06.md)
- [六工位工件关联、归档与管理业务优化（2026-11-06）](history/six-station-business-2026-11-06.md)
- [数据库与取证存储优化验收（2026-11-06）](history/database-storage-2026-11-06.md)
- [项目结构整理记录](history/project-structure-cleanup.md)

- [现有视频核查与界面优化（2026-10-10）](history/action-review-2026-10-10.md)
- [服务器 GPU-only 模型执行](history/gpu-only-runtime-2026-11-05.md)
- [工业训练与 8000 主服务联动](history/industrial-training-2026-11-05.md)
- [时序模型与目标检测推理优化](history/inference-optimization-2026-10-10.md)
- [推理线程优化与续验记录](history/inference-runtime-2026-11-05.md)
- [AI-SOP 视频行为分析平台](history/legacy-preview.md)
- [综合优化验收记录](history/optimization-2026-10-08.md)
- [人体、螺丝与贴条视觉检测](history/project-notes-before-cleanup.md)
- [公共数据下载、训练与测试实测报告](history/public-training-results-2026-10-10.md)
- [10 FPS 检测对照实测](history/realtime-10fps-evaluation-2026-11-05.md)
- [RTMPose / RTMO 姿态后端（rtmlib）接入记录（2026-10-10）](history/rtmpose-rtmo-backend-2026-10-10.md)
- [基于正确工序视频的螺丝专项检测](history/screw-inspection-2026-10-17.md)
- [服务器性能与可运行部署](history/server-performance-2026-11-05.md)
- [8000 主服务与实时交互优化验收](history/service-optimization-2026-11-05.md)
- [装配 SOP 全流程测试与优化](history/simple-sop-flow-results-2026-10-10.md)
- [桌面装配收尾：四步 SOP 与时序识别](history/simple-sop-results-2026-10-10.md)
- [四步装配时序模型 V2 优化结果](history/simple-sop-v2-results-2026-10-10.md)
- [装配 SOP V2.2：置信度累积与事件误差验证](history/simple-sop-v22-results-2026-10-10.md)
- [装配 SOP V2.3：子集集成与衰减证据滤波](history/simple-sop-v23-results-2026-10-10.md)
- [骨骼框架统一（2026-10-12）](history/skeleton-framework-2026-10-12.md)
- [螺丝与贴条独立任务：工位使用与部署准备](history/sticker-inspection-station-2026-10-17.md)
- [下载视频模拟实时输入演示](history/video-stream-demo-2026-10-10.md)

## 参考资料

- [灯具装配文献调研](research/exploration/灯具装配场景的行为识别与工序时序模型-3968c13a/2026-10-05_153002/research_exploration.md)：保留完整的有日期版本，删除与其逐文件相同的 `latest` 副本。
- [单工位最小验证方案](research/single-station-poc/单工位最小验证方案.md)
- [单工位数据约定](research/single-station-poc/data_contract.md)
- [原型资料与素材来源](history/legacy-preview.md)，配图保存在 `reference/`。
