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
