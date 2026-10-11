# AI-SOP 视频行为分析平台

根据提供的约 50 秒录屏复刻的中文工业检测大屏。深蓝底色、蓝色状态栏、紫色模块标题、实时画面、检测表、动作时间轴、SOP 分步判定和告警中心均按参考制作，支持桌面与移动端。

## 启动

安装 Node.js 18 或更新版本，然后在项目目录运行：

```sh
npm run preview
```

浏览器打开 http://localhost:8080。无需安装依赖。可通过 `PORT` 环境变量调整端口。

行人测试视频入口：http://localhost:8080/?video=people 。这是 1280×720、5 秒循环的行人过闸机示例；“上传视频检测”弹窗内也可切换，或恢复原工位演示。刷新测试链接仍播放该视频。

工厂测试视频入口：http://localhost:8080/?video=factory 。这是 1280×720、5 秒循环的自动化玻璃瓶生产线场景，可在“上传视频检测”弹窗中选择。素材来自 [Ultralytics 公开工厂示例](https://github.com/ultralytics/assets/releases/download/v0.0.0/manufacturing-bottles-monitoring-compressed.mp4)，画面中的瓶子检测框和生产计数是原视频自带的标注，并非当前系统推理输出。已验证播放、拖动进度、刷新保留与行人/工厂场景切换。

工厂工人实际分析入口：http://localhost:8080/?video=workers 。这是 1280×720、15 秒的电子装配/检验车间俯拍视频，来源为 [Mixkit Engineering staff working in a factory](https://mixkit.co/free-stock-video/engineering-staff-working-in-a-factory-22032/)，来源页面标记为 Mixkit Restricted License；复用、发布或商业使用前应核对现行授权条款。项目使用本机 YOLO11s-pose 在 5 fps 采样下实际分析，页面的青色人体框、骨架、检测明细和动作时序由本次模型输出生成。

该素材的实际分析结果：75 个采样帧均检出人员，最多同时 6 人，人体检测平均置信度约 76.7%，产生 72 个姿态规则片段。24 是轨迹段数量，不是独立工人数；俯拍、遮挡和轨迹重连会造成编号变化。动作标签来自关键点几何规则，尚未用人工真值验证。页面不填写虚构的工序合格率；工具、工件和设备状态缺失时，SOP 步骤显示“待人工复核”。“详细分析报告”提供关键帧、完整时间轴、质量事件、模型与源文件哈希、阈值、性能和适用范围，并支持打印为 PDF、下载 JSON 和导出动作 CSV。

测试素材来自 [Ultralytics 公开示例 People-counting-compressed.mp4](https://github.com/ultralytics/assets/releases/download/v0.0.0/People-counting-compressed.mp4)，检测框和计数已嵌入原视频，并非当前系统生成。项目保留素材中的原有署名。

## 已实现

- 从用户提供的录屏中裁切工位区域并连续回放，保留原片中的识别框。可暂停、拖动进度、全屏。
- 按回放进度变化的演示检测数据、动作时间轴和 SOP 步骤。
- 流程名称、所属区域、2–8 个步骤与演示阶段时长编辑，支持本机持久保存。
- 告警等级筛选、文本搜索、详情、工位回放、确认、批量确认与重新打开；处置记录在本机保存。
- 检测结果与筛选后的告警记录导出为带 UTF-8 BOM 的 CSV，兼容 Excel 中文显示。
- 本地视频选择和拖放回放。文件不上传；可恢复参考演示。
- 系统信息、浏览器能力自检、键盘快捷键 `Ctrl+Shift+G`、移动端布局。

## 数据与能力边界

这是可运行的前端复刻，不包含人体检测、姿态估计或 SOP 自动判定模型。工位素材来自用户给定视频；演示指标与历史告警用于还原参考界面，并非本机真实测量。识别框是参考视频自带的图像内容。上传自有视频时清空演示识别结果，避免将模拟输出当成真实检测。

流程配置仅改变演示步骤切换，不改变视频中原有的识别框。告警详情的图片是同工位参考画面，不是历史事件的精确快照。历史告警为独立演示记录，切换到测试或上传视频时不显示，恢复原工位演示后可查询。

若要接入真实生产检测，需要提供摄像头/视频流、检测与姿态模型、动作标签、区域定义及 SOP 判定规则，并替换 `app.js` 中的演示数据源。当前服务仅监听本机，适合本地预览；没有用户管理、共享数据库或远程部署配置。

## 文件

- `index.html` / `styles.css` / `app.js`：页面、响应式样式、交互与状态。
- `server.cjs`：无依赖本地服务，支持视频 Range 请求。
- `assets/reference.mp4`：用户提供的参考录屏。
- `assets/factory.jpg`：参考工位画面。
- `tools/smoke-test.cjs`：浏览器交互验收脚本，使用 Playwright。默认自动发现本机 Codex 附带运行时，也可通过 `PLAYWRIGHT_MODULE` 指定 Playwright 模块路径。
- `reference/`：复刻时提取的录屏关键帧与验收截图。

## 验收

先启动服务，再运行 `npm test`。测试需要 Playwright 和 Microsoft Edge，或通过 `BROWSER_CHANNEL` 指定已安装的 Chromium 通道。
