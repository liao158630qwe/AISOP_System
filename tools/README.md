# 工具与脚本

所有脚本从项目根目录运行。Python 脚本推荐通过 `node tools/python.cjs` 调用，以共用项目的解释器选择规则。

| 用途 | 入口 |
| --- | --- |
| 本机启动与恢复 | `start-local.py`、`restart-local.ps1`、`python.cjs` |
| PostgreSQL 实库测试 | `npm run test:postgres`，自动启动独立测试库、运行后关闭；见数据库测试指南 |
| 登录与会话回归 | `npm run test:login`、`npm run test:login-ui`，隔离数据库与浏览器服务，覆盖并发限流、慢 SQL、CSRF 同步、迟到响应、重复提交和跨标签页登录/退出 |
| 异常媒体与人工确认恢复 | `npm run test:station-media-ui`、`npm run test:review-recovery`，隔离服务验证无法解码/零尺寸/加载超时、切换素材取消、相机释放，以及本地确认库升级、登录过期恢复、历史积累与跨标签页去重 |
| 通用模型训练 | `train-model.py`，配置示例见 `config/training/` |
| 视频数据准备链路验证 | `npm run test:datasets`、`npm run test:datasets-ui`，隔离 API/Worker 与合成视频 |
| 螺丝与贴条训练 | `train-screw-*.py`、`train-sticker-*.py` |
| 候选评估、检查与发布 | `evaluate-*.py`、`check-*.py`、`activate-*.py`、`promote-*.py` |
| 性能与精度 | `benchmark-*.py`、`evaluate-accuracy.py`、`evaluate-realtime-fps.py` |
| 工位1、2模型优化对照 | `compare-station-models.py --baseline 改动前源码目录 --kind screw或sticker --video 视频路径 --output 新目录`；以相同JPEG交替执行原版与优化版，记录逐帧状态、事件、合规结论、数值偏差和耗时，不写业务记录；默认4FPS覆盖完整视频 |
| 单视频实时端到端实测 | `evaluate-video-realtime.py 视频路径 --output 新目录`，独立 API 的 4/10fps 送检及原页面文件模拟摄像头；保存接口原始结果、页面对照与截图 |
| 视频到大屏与终端实测 | `evaluate-board-video.py 视频路径 --output 新目录`，独立 API 和数据库，按原速测单路及六路工位推理；真实 Board 与默认 `/terminal` 页面同步核对并截图，额外检查工件记录及关联字段均为 0。六路共享视频解码，各自推理及保存历史；`--runs six` 可只测六路；`--station-only` 排除大屏/终端浏览器。可用 `--concurrency`、`--pose-pool`、`--specialist-pool`、`--opencv-threads`、`--torch-threads` 做明确参数对照；使用视频首帧及各视角标定图预热临时流，记录实际预热结果，避免空画面未触发专项模型加载。报告有效 fps、P95/P99、跳过帧和最长无结果间隔 |
| 数据和视频准备 | `download-ha4m.py`、`prepare-screw-video.py`、`analyze-workers.py` |
| 故障分析与报告 | `diagnose-*.py`、`review-*.py`、`report-*.py`、`build-*.py` |
| 当前页面验证 | `test-*.py`、`test-*.cjs`、`verify-main-service.cjs` |
| 大屏高级清空功能 | `npm run test:board-reset-ui`、`node tools/python.cjs -m pytest tests/test_board_reset.py -q`；使用隔离数据库，验证全日期与回收站清空、权限、密码、运行中阻止、变化范围保护、跨标签页确认队列和断线核对 |
| 大屏选择记录删除 | `npm run test:board-selected-ui`；使用隔离数据库验证日期/工位/结论/回收站筛选、跨页勾选、确认取消、只删除所选 ID、保留新记录、确认队列同步、断线不重发与手机布局 |
| 大屏统计范围 | `npm run test:board-ranges-ui`；隔离数据库验证总体/当日/指定日期的数量与告警、筛选记忆、每日历史和高级功能日期联动、迟到响应隔离及手机布局；`tests/test_board_detection.py` 验证 UTC+08:00 边界、跨零点、各范围缓存与删除后刷新 |
| 后端故障恢复与六路采样 | `npm run test:stability`，隔离数据库与 API/Worker，真实 GPU 推理 |
| 六工位接入 | `station-collector.py --controlled`，每相机一个常驻采集进程，首页按件开始/结束；刷新页面不停止后台采集。见[正式采集指南](../docs/guides/controlled-station-collection.md) |
| 六路固定到达率压测 | `benchmark-stations.py`，逐路报告吞吐、丢弃及延迟，输入代表性 JPEG |
| 本机隔离 GPU 配置对照 | `benchmark-stations-local.py`，临时数据库及 API，支持串行/并发与人体池组合 |
| 六工位终端验证 | `npm run test:terminals`、`npm run test:terminal-ui`，隔离测试数据 |
| 检测异常与人工确认 | `node tools/python.cjs -m pytest tests/test_station_review.py tests/test_board_detection.py -q`；界面验证：`node tools/python.cjs tools/test-pilot-ui.py --registration-paused --browser-script tools/test-station-review-ui.cjs`。使用隔离数据库及推理/归档样例，真实验证人工合规/不合规、原因选填、确认账号与时间、保存重试、历史导出和大屏统计 |
| 六工位无需登记的首页与大屏联动验证 | `node tools/python.cjs tools/test-pilot-ui.py --registration-paused`，隔离 API、真实模型，验证六工位 F8、完整视频自动归档、历史导出、下一轮重置、停止中断、零工件创建及大屏计数一致；另以六个浏览器页面独立解码同一视频，短时并发检测并核对归档。报告分别记录采样到检测结果、大屏显示的延迟并保存截图。可通过 `STATION_DASHBOARD_VIDEO` 指定视频。省略参数则显式启用登记并回归原人工流程 |
| 历史演示验证 | `preview-test.cjs`、`smoke-test.cjs`、`display-regression.cjs`、`scene-flow-test.cjs` |

优先使用 `package.json` 中的 npm 命令。涉及模型激活、现场数据和历史实验的脚本，请先阅读脚本参数与对应 [文档](../docs/README.md)。这些脚本保留原路径，便于复现已有训练流程。

当前浏览器测试、基准验证统一输出到 `test-results/`；专用训练与评估脚本输出到 `outputs/`。早期测试中已有 `.runtime/` 输出约定的脚本仍保留该位置。
