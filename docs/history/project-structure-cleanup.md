# 项目结构整理记录

本次整理按源码、配置、文档、实验、运行数据和验证产物区分目录；原有未提交的业务改动随文件保留。

## 文件归位

| 原位置 | 新位置 |
| --- | --- |
| 根目录 HTML、CSS、JS | `frontend/` |
| `docs/*.md` | `docs/guides/`、`docs/research/`、`docs/history/` |
| 原 README 完整内容 | `docs/history/project-notes-before-cleanup.md` |
| `reference/` | `docs/reference/` |
| `output/research-explorer/` | `docs/research/exploration/` |
| `output/single-station-poc/` | `docs/research/single-station-poc/` |
| 其余 `output/` | `test-results/` |
| `screw_action_training/` | `outputs/screw-action-training/` |

API、静态预览、Docker 静态文件复制、测试脚本、文档链接与数据集 YAML 已同步路径。网页 URL 和 npm 命令入口保持原有用法。

## 已删除内容

- `.wheels/`：已下载的 torch/torchvision 安装包缓存，共 1,997,174,448 字节；已安装环境保留。
- `.pytest_cache/` 与源码旁的 `__pycache__/`：可重新生成的测试和字节码缓存。
- `output/web/`：已无代码引用的旧前端构建。
- `output/web-test-temp/`：旧前端构建器的隔离测试夹具。
- `output/integrate-task-selector.py`：修改已落入当前源码的一次性补丁脚本。
- `启动本机服务.bat`：与保留的 `.cmd` 启动入口逐字节相同。
- 文献调研的 `latest/`：153 个文件逐个 SHA-256 与有日期目录一致，删除重复副本。
- 33 个 pytest `*current` 临时软链接：编号测试目录及其内容全部保留。

模型权重、参考图、训练数据、配置引用的训练结果和正式运行数据库均保留。只有编译文件的旧训练 `src/` 也保留，避免把唯一代码副本当缓存删掉。旧报告内部的运行时绝对路径保留为历史来源记录。

## 验证基线

整理前运行 `npm test -- --disable-warnings`：317 通过、2 失败、1 跳过。既有失败为：

- `tests/test_diagnostics.py::test_health_describes_configured_service_without_loading_model`：模型名称预期与当前默认后端不一致。
- `tests/test_transport.py::test_warmup_reserves_model_slot`：模拟模型未进入预热调用。

## 整理后验收

- `npm test -- --disable-warnings`：332 通过、1 跳过；跳过项需要独立 PostgreSQL 测试库。
- `npm run test:admin`：9 项通过。
- `npm run test:realtime-ui`：15 项通过，无页面异常。
- `npm run test:trigger-ui`：18 项通过，无页面异常。
- 新增 13 项页面资源与私有文件隔离回归，验证前端迁移后页面依赖及资源白名单。
- 前端 JavaScript 语法、Docker COPY 源目录和文档链接已检查；Docker 镜像未在本机实际构建。
- 8000 本机 API 和 Worker 已重启，主页面及静态资源地址保持可用。

核对历史资料时发现 7 处原有缺失文件链接，已改成明确的缺失说明。配置中的螺丝、贴条训练结果 JSON 也已缺失；对应输出目录未在本次删除范围内，模型权重和参考图保留。历史测试基线中的两项失败在最终工作区回归中均已通过；本次结构整理没有修改这两项业务测试。

`.runtime/pytest-*` 中另有 25 个旧测试夹具目录，删除请求被自动审批策略拦截，未执行，目录保留。

本机详细移动清单和整理前 Git 差异保存在 `.runtime/structure-cleanup/`，该目录不提交。
