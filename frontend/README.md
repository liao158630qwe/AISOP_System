# 前端文件

全部 HTML、CSS 和浏览器 JavaScript 集中在本目录，无构建步骤。API 从本目录提供白名单文件，URL 不带 `frontend/`；公共素材继续通过根目录 `assets/` 提供。

| 页面/模块 | 文件 | 职责 |
| --- | --- | --- |
| 主界面 | `index.html`、`app.js`、`styles.css` | 工作区、视频、基础界面和静态演示 |
| 检测控制台 | `screw-dashboard.js`、`screw-dashboard.css` | 上传、实时检测、任务选择与结果显示 |
| 场景任务 | `scene-dashboard.js`、`scene-console.*` | 自定义场景接入与控制台 |
| 模型与场景管理 | `admin.html`、`admin.js` | 模型库、规则和场景发布 |
| 训练管理 | `training.html`、`training.js`、`training.css` | 训练提交、状态与报告 |
| 视频训练数据 | `datasets.html`、`datasets.js`、`datasets.css` | 视频上传、后台抽帧、网页标注、覆盖检查与冻结版本 |
| 人工工位 | `station-one.*`、`pilot-dashboard.*` | 六工位人工确认、证据与复核 |
| 设备与采集终端 | `terminal.*`、`terminals.*` | 六工位绑定、独立凭据、采样与实时结果 |
| 检测按键 | `inspection-trigger.*`、`settings.html`、`settings.js` | 启动按键配置和触发 |
| 集中大屏 | `board.html`、`board.css`、`board.js` | `/board` 六工位总览、实时告警、趋势与轮播 |
| 独立螺丝页面 | `screw-inspection.*` | 专用检测页面 |
| 共用界面 | `workspace-api.js`、`workspace.css` | API 访问与后台页面样式 |
| 早期适配 | `server-ui.js` | 历史服务器界面适配器 |

运行方式见 [项目首页](../README.md)。请通过服务打开页面；直接双击 HTML 不会建立 API 服务。
