"""Summarize measured action-mode offline, live-API and promotion evidence."""
import json
import numpy as np
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'outputs/sticker-inspection-2026-10-17'


def read(path):return json.loads(path.read_text(encoding='utf-8'))


def main():
    config=read(ROOT/'config/sticker-inspection.json')
    promotion=read(OUT/'action-promotion.json')
    training=read(OUT/'action-training-result.json')
    full=read(OUT/'full-video-action-v2'/'full-video-result.json')
    live=read(OUT/'action-realtime-api-test.json')
    if config.get('mode')!='action':raise SystemExit('active sticker config is not in action mode')
    if not promotion.get('accepted') or full.get('verdict')!='PASS' or live.get('failures'):
        raise SystemExit('action evidence is incomplete or failing')
    sequence=full['sequence'];summary=full['summary'];names={t['id']:t['name'] for t in full['targets']}
    events='\n'.join(f'| {event["id"]} {names[event["id"]]} | {event["time"]:.2f}s | {event["confirmedAt"]:.2f}s | {event["evidence"]} | {event["score"]:.3f} |'
        for event in sequence['events'])
    cases='\n'.join(f'| {case["case"]} | {case["result"]["compliance"]["label"]} | {"通过" if case["matched"] else "失败"} | {case["provenance"]} |'
        for case in live['cases'])
    correct=next(case for case in live['cases'] if case['case']=='recorded-correct-order')
    latency=np.array(correct['wall_ms'][2:],dtype=float)
    checks='\n'.join(f'- `{key}`: {"通过" if value else "失败"}' for key,value in promotion['checks'].items())
    report=f'''# 贴条动作工序检测：核对记录

当前启用 `{config["model_name"]}`（`{config["model"]}`）。规则为：对应位置观察到贴放/按压并随后离开即计完成；先完成两处三角动作，再完成六处方形动作；不核验黑色贴片与黑色零部件的外观差异、贴片实际存在或粘附。

## 正确视频完整回放

| 项目 | 实测 |
| --- | --- |
| 源视频 | `{full["source"]["title"]}` |
| 源视频 SHA256 | `{full["source"]["sha256"]}` |
| 采样 | {summary["sampleFps"]:.1f} FPS，{summary["analyzedFrames"]} 帧 |
| 可定位工件帧 | {summary["registeredFrames"]} |
| 动作确认 | {summary["actionsConfirmed"]}/{summary["expected"]} |
| 两处三角最后动作 | {sequence["trianglesCompleteAt"]:.2f}s |
| 第一处方形动作 | {sequence["firstRectangleAt"]:.2f}s |
| 最终判定 | {full["compliance"]["label"]} |
| 处理耗时 | {summary["processingSeconds"]}s |
| P95 单帧推理 | {summary["p95InferenceMs"]:.2f}ms |

| 位置 | 动作开始 | 确认时刻 | 证据路径 | 最高接触分 |
| --- | --- | --- | --- | --- |
{events}

## 实时接口

使用真实 HTTP 帧接口提交 1080×1920 JPEG94 连续帧；正确视频 14–49.5 秒按 4FPS 提交。成品静置、空白画面和错序为独立轮次。

| 场景 | 判定 | 核对 | 来源 |
| --- | --- | --- | --- |
{cases}

正确视频场景热身后 HTTP 延迟中位 {float(np.median(latency)):.1f}ms、P95 {float(np.percentile(latency,95)):.1f}ms；不含摄像头采集和浏览器 JPEG 编码。默认实时采样为 4FPS，服务端满载会快速返回繁忙状态，不排队积压。

## 启用前检查

{checks}

## 边界

动作模型来自一条正确过程视频的开发训练，裁剪留出和整段复测仍属于同源验证，不代表独立工位准确率。当前不核验贴片实际存在、形状、外观、粘附强度或毫米级偏移；相似扶持、手部经过、短暂停留和不同手套颜色仍可能在现场造成误计或漏计。现场投用前应补录不同人员、真实漏动作、错序和遮挡样本，并重新标定定位模板。

训练记录：`{training["model"]}`，模型 SHA256 `{training["sha256"]}`；启用前核对：`action-promotion.json`；完整回放：`full-video-action-v2/`；实时接口：`action-realtime-api-test.json`。
'''
    (OUT/'action-report.md').write_text(report,encoding='utf-8')
    print(json.dumps(dict(verdict=full['verdict'],actions=summary['actionsConfirmed'],live_failures=live['failures']),ensure_ascii=False))


if __name__=='__main__':main()
