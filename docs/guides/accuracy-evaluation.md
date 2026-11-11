# 现场识别评测

现场评测使用视频级隔离的 JSON 标注集，不在测试集上调阈值。每个视频必须声明 `split` 和条件标签，至少覆盖 `occlusion`、`lighting`、`station` 三类条件：

```json
{
  "dataset_version": 1,
  "videos": [
    {
      "id": "station-s5-camera-01",
      "split": "test",
      "prediction": "runtime-data/runs/<run>/analysis.json",
      "conditions": {"occlusion": "partial", "lighting": "low", "station": "s5"},
      "frames": [
        {"time": 12.4, "people": [{"action": "reaching"}]},
        {"time": 13.0, "people": []}
      ]
    }
  ]
}
```

运行：

```powershell
npm run evaluate:accuracy -- path/to/field-dataset.json --split test --out field-test-report.json
```

工具按最近分析采样帧匹配标注时间，输出人体存在 precision/recall/F1、动作宏 F1，以及遮挡、光照、工位等条件分组指标。输出中明确记录未做阈值调参；只有新增视频的独立 test split 才能用于报告模型或采样率改进。

`prediction` 可以使用绝对路径，或相对于标注 JSON 所在目录的路径。每个视频必须显式声明 `split`；同一视频 ID 或同一预测文件跨 split 复用会被拒绝。改名或复制文件不能被该检查完全识别，数据版本仍需按完整工件、拍摄批次与人员隔离。

报告同时给出 `annotatedFrames`、`matchedFrames`、`unmatchedFrames` 和 `coverage`。未匹配的标注点按空预测计入，有人或有动作时会增加漏检，不能通过丢帧抬高召回率。全部预测缺失也会生成零覆盖率报告。人体指标是“画面是否有人”，动作指标是“画面是否存在该动作类别”，不衡量人数、人员身份或每人动作归属；工件漏装和误放行须使用独立的工件级标注评估。
