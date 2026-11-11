# 视频训练工作流验证 · 2026-11-06

本次实现视频上传、独立后台抽帧、检测/分类网页标注、覆盖检查、工业审计、冻结版本及训练页联动。使用方式见 [视频训练指南](../guides/video-training-workflow.md)。

## 已执行

- 相关回归批次：`tests/test_datasets.py tests/test_training_service.py tests/test_scene_management.py tests/test_static_assets.py tests/test_backend_stability.py tests/test_cleanup.py tests/test_postgres.py`，98 通过、4 跳过（该批次运行后增加了两项数据集回归及一项 PostgreSQL 测试）。
- 新增数据集回归复测：`tests/test_datasets.py tests/test_postgres.py`，11 通过、5 跳过。包括数据库提交应答丢失后的文件保留，以及审核租约过期后的解锁和旧结果拦截。
- API、工作器和页面资源：`tests/test_api.py tests/test_worker.py tests/test_static_assets.py`，34 通过。
- 浏览器与真实 API/Worker 联调成功两次。最后一轮产物在 `test-results/datasets-20261106-222116/`，包含桌面、手机和画框截图，隔离数据库、API/Worker 日志及训练产物。
- 浏览器验证：实际上传 6 段生成视频；CPU 子进程抽帧；网页绘制两个类别的矩形框；确认负样本；复核分类图片；24 张分页；生成通过审计的版本；自动带入训练页；完成 1 轮真实 CUDA 检测训练并按预期被准入门槛拒绝；检查手机无横向溢出、退出后清除图片及浏览器无脚本错误。
- 更新本机数据库结构，重启 API/Worker 后验证 `/datasets`、脚本/样式、`/training`、数据集列表和服务就绪接口均返回 200。重启前确认训练/分析无活跃任务、工位无有效流租约、四类实时流均为 0。

## 边界

以上数据是随机生成的视频与图片，用于验证工程链路，不是现场效果或准确率证据。没有替用户创建正式训练数据。

未配置 `TEST_POSTGRES_URL`，PostgreSQL 并发和服务器部署验证未执行。已加入独立数据库 schema 下的数据集创建/任务领取并发、租约恢复和旧结果拦截测试，部署环境可执行。Nginx 配置已调整，未在本机启动 Nginx/Docker 验证。

当前工作流只支持目标检测与整图分类，不包括动作片段、时序或 SOP 模型训练。近似重复视频、真实批次和标注质量仍需人工复核。
