# 下载视频模拟实时输入演示

演示入口：[深蹲视频实时接口实测回放](http://127.0.0.1:8000/assets/demos/squat-stream-2026-10-10/demo.html)。视频来自 [Ultralytics 公开素材](https://github.com/ultralytics/assets/releases/download/v0.0.0/Squats.demo.video.mp4)，原片 7.4 秒、1920×1080、30 fps。下载日期 2026-10-10，源文件与 SHA-256 记录在对应目录的 `source.json`。

用原片模拟摄像头，以 5 Hz 向本机 `/api/v1/realtime/frame` 发送 640×360 JPEG。每次请求携带独立流 UUID 和递增时间戳，实际使用 YOLO11s-pose、连续流跟踪与现有姿态规则。未更改模型或阈值；测试结束后释放测试流、注销脚本创建的会话。

- 37 个采样帧全部检出人体，每帧最多 1 人；这不是有人工标注支持的检测准确率。
- 热身后的请求平均耗时 14.69 ms，P95 17.06 ms，服务端平均 10.67 ms。
- 本次热身请求 40.90 ms，模型已在此前测试中加载；不能当成冷启动耗时。
- 检测视频为 5 fps 的实测结果回放，页面支持切换原始采样画面与检测画面。骨架和青色框来自本次接口响应，没有预先写入检测结果。
- 页面播放、拖动、切换保持时间与暂停状态、手机布局检查通过。JSON、视频与桌面／手机截图均保存在 `assets/demos/squat-stream-2026-10-10/`。

此前尝试的 [OpenCV vtest 行人视频](https://github.com/opencv/opencv/blob/master/samples/data/vtest.avi) 在当前模型下前 20 秒的 100 个采样帧均未检出人体；未通过降低阈值或补写检测结果掩盖。原始视频、零检出结果与当时脚本快照保存在 `assets/demos/opencv-pedestrians-2026-10-10/`。当前展示素材是为便于演示挑选的单段视频，不能据此推断泛化效果。

现有姿态标签不包括“深蹲”类别；视频中的深蹲用于观察骨架变化，姿态规则可以输出未知。这不是 Kinect V2.2 四步装配模型的视频适配验证。

复现（先启动 `npm run dev:local`）：

```powershell
node tools/python.cjs tools/demo-video-stream.py
node tools/demo-video-stream-check.cjs
```

来源文件已下载，脚本会校验其哈希并重新实测。脚本从本机配置读取登录信息，不将口令或会话令牌写入演示页面。
