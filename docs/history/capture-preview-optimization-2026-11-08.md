# 首页工位采样、结果新鲜度与预览优化

日期：2026-11-08。范围：首页工位 1–6 的 `station-detection.js`，按当前要求暂不使用独立终端。

## 实现

- 延续已有编码与上传重叠的有界流水线：最多一个在途请求、一张最新待送帧；发送前超过 500 ms 的采样丢弃；忙碌响应按 Retry-After 退避。
- 即时判定与识别框的有效期从浏览器采图时刻起算 1 秒。已经过期的响应不会显示为当前结果，也不会触发自动归档；最终归档仍使用服务器保存的结论。
- 帧响应核对工位、工位机、相机、绑定版本、流 ID、代次、检测类型、场景、模型版本与帧号。页面切后台、断网、视频暂停、相机静音均使已有结果和待送帧失效，恢复后等待新采样。
- 停止和归档先阻止新采样，关闭编码并等待在途工作收尾。收尾响应不能重新显示画面、覆盖相机故障提示或触发自动归档。归档响应须通过身份、帧号范围和 final/archived 标记检查，之后才释放流，失败可重试。
- 摄像头及视频支持“检测采样与识别框”和“实时视频（无识别框）”两种显示。实时视频下明确标注文字判定来自最近有效采样，图片联调保留原显示方式。
- 页脚和运行详情分别展示视频呈现帧率、近 10 秒有效检测帧率。停止时冻结检测帧率，在途响应收尾不抬高该统计；运行详情增加替换、丢弃、失败及迟到结果计数。

## 验证

所有浏览器验证使用临时 API 和 SQLite，不改正式工位绑定或生产数据库。浏览器实际执行合成视频、采图、JPEG 编码与 Worker 生命周期；模型响应及最终算法归档使用明确的测试样例。人工复核验证中的保存、历史、导出与大屏查询使用真实 API。

| 验证 | 结果 | 证据 |
| --- | --- | --- |
| 采样与画面流水线 | 23/23 场景通过；最大在途请求为 1；停止后编码、待送帧、相机轨道及位图均释放 | [完整报告](../../test-results/pilot-browser-tests/run-fb75jerb/station-pipeline-ui.json) |
| 六工位完成与归档回归 | 通过，覆盖自动完成、释放失败重试、最终判定保护和下一轮 | [报告](../../test-results/pilot-browser-tests/run-2kuc7gpa/station-completion.json) |
| 人工复核回归 | 通过，3 轮、4 次请求；原因选填、同请求重试、审计、导出、历史及大屏计数正常 | [报告](../../test-results/pilot-browser-tests/run-nuw4qc0f/station-review.json) |
| 静态页面与资源 | 17 项通过 | `node tools/python.cjs -m pytest tests/test_static_assets.py -q` |
| JavaScript 语法与改动格式 | 两个修改的 JavaScript 文件通过 node --check，改动格式检查通过 | [源码与验证摘要](../../test-results/capture-optimization-20261108/verification.json) |

流水线新增覆盖慢响应 PASS、从采图起算的过期清除、后台/离线/暂停前的响应在恢复后迟到、相机断开时仍有在途 PASS、不同工位机/绑定/模型身份、实时视频无框、停止帧率冻结，以及未确认保存或身份错误的归档响应不得释放流。原有编码回退、过载退避、停止时仍在编码/创建位图、像素与帧身份一致性继续通过。

复现：

```powershell
node tools/python.cjs tools/test-pilot-ui.py --registration-paused --browser-script tools/test-station-pipeline-ui.cjs
node tools/python.cjs tools/test-pilot-ui.py --registration-paused --browser-script tools/test-station-completion-ui.cjs
node tools/python.cjs tools/test-pilot-ui.py --registration-paused --browser-script tools/test-station-review-ui.cjs
node tools/python.cjs -m pytest tests/test_static_assets.py -q
```

## 边界

本轮没有测量真实六机吞吐或 GPU 性能，不能据此宣称每路 10 fps 达标或识别精度改善。浏览器采图计时也不包括曝光、相机固件及驱动缓存。常驻采集器的远程轮次控制、SSE 和 RTSP 媒体网关保留为后续需求；未修改独立 /terminal 页面。
