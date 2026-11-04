"""Report measured selected-task training, full-video and live API evidence."""
import argparse
import json
import re
from pathlib import Path
import numpy as np
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'outputs/sticker-inspection-2026-10-17'

active=json.loads((ROOT/'config/sticker-inspection.json').read_text(encoding='utf-8'))
if active.get('mode')=='action':
    raise SystemExit('当前贴条任务为动作模式，请运行 tools/report-sticker-actions.py')

def read(path):return json.loads(path.read_text(encoding='utf-8'))

parser=argparse.ArgumentParser();parser.add_argument('--run-folder',default='full-video-v5');args=parser.parse_args()
data=read(OUT/args.run_folder/'full-video-result.json');api=read(OUT/args.run_folder/'api-test.json')
live=read(OUT/'realtime-api-test.json');ui=read(OUT/'task-ui-result.json')
training=read(OUT/'training-result.json');provenance=read(OUT/'dataset-provenance.json')
assert data['analysisType']=='sticker' and data['verdict']=='PASS' and data['sequence']['status']=='PASS'
assert data['model']['sha256']==training['model_sha256']
assert data['source']['sha256']==provenance['source_sha256']
assert not live['failures'] and live['model_assets']==data['calibration']['assets']
assert ui['run']==data['id'] and not ui['errors']
counts={s:len(list((OUT/'dataset/images'/s).glob('*.jpg'))) for s in ['train','val','test']}
s=data['summary'];sequence=data['sequence']
table='\n'.join(f'| {v["case"]} | {v["result"]["compliance"]["label"]} | {"合成对照" if "synthetic" in v["provenance"] else "录像帧模拟"} |' for v in live['cases'])
names={t['id']:t['name'] for t in data['targets']}
events='\n'.join(f'| {e["id"]} {names[e["id"]]} | {e["time"]:.2f}s | {e["confirmedAt"]:.2f}s |' for e in sequence['events'])
latency=next(v['wall_ms'] for v in live['cases'] if v['case']=='recorded-correct-order')
tests=(OUT/'unit-tests-final.log').read_text(encoding='utf-8',errors='replace')
match=re.search(r'\d+ passed, \d+ skipped[^\n]*',tests)
report=f'''# 螺丝与贴条独立任务：训练及验证报告

原深色监控界面新增“检测任务”选择：螺丝安装检测、贴条工序检测。两者使用独立权重、标定、历史筛选和实时轮次。切换任务清除当前观察与视频；刷新仅保留任务选择，不恢复历史视频。

按用户最新要求，标注、训练执行和审核均由当前主模型直接完成；此前指定的训练子代理未产出训练模型。贴条仅使用更正后的 `lv_0_20261017162449.mp4`（60.47秒、2160×3840）；上一段161554视频未进入贴条数据集。

## 已启用的规则与模型

当前模型：{data['model']['name']}。核对两个三角、六个方形（含长方形）贴合位：先完成两处三角，再贴后续方形，组内不额外规定先后。根据真实当前图像检出贴片、裸露位置或保留未知，观察到裸位→已贴后才记录安装事件。视频时间表只用于辅助训练标签，没有进入运行时推理逻辑。

工序事件需三次实际一致观察，允许1.5秒以内无相反证据的短遮挡间隔；未知帧不增加观察票数，也不绘制检测框。瞬时贴片外观闪变不能单独完成步骤。成品直接入镜没有安装顺序证据，保持无法判定。

定位模板与工件需有共同的当前图像特征支持；模板抬起或关键定位区域不可核对时不沿用旧位置。局部候选仅用于其对应贴合位，避免相邻目标干扰。

## 完整视频实测

| 项目 | 实测 |
| --- | --- |
| 模型版本 | {data['model']['name']} |
| 采样 | {s['sampleFps']:.1f} FPS，共 {s['analyzedFrames']} 帧 |
| 可核对工件帧 | {s['registeredFrames']} |
| 最终对应贴条 / 未贴 / 形状不符 / 未知 | {s['present']} / {s['empty']} / {s['wrong']} / {s['unknown']} |
| 两处三角最后的首次观察时刻 | {sequence['trianglesCompleteAt']:.2f}s |
| 第一处方形首次观察时刻 | {sequence['firstRectangleAt']:.2f}s |
| 最终顺序与外观判定 | {data['compliance']['label']} |
| 末次可核对时刻 | {s['assessmentTime']}s |
| 完整处理耗时 | {s['processingSeconds']}s，含解码与回放编码 |
| 检测记录编号 | {data['id']} |

| 位置 | 首次可见证据 | 多次观察确认 |
| --- | --- | --- |
{events}

这些时间是实际采样中的视觉事件，不是贴合接触或压紧时刻的精确测量。最终范围不包含胶粘强度和毫米级贴合偏移。

## 实时接口与缺陷对照

真实API以4FPS顺序提交视频中14–49.5秒的连续采样，保留手遮挡与图像变化，未用单张成品图冒充整段实时流程。竖屏输入为1080×1920 JPEG94，长边与像素数受限；物理摄像头和现场工位尚未实测。

热身后本机HTTP请求延迟中位 {float(np.median(latency[2:])):.1f}ms、P95 {float(np.percentile(latency[2:],95)):.1f}ms，不含真实摄像头采集和客户端JPEG编码。首次模型加载另计。

| 输入场景 | 结果 | 来源 |
| --- | --- | --- |
{table}

合成漏贴使用真实贴前裸位像素替换成品中的同一位置；合成错序在三角仍未贴的图像中加入方形贴片。两者是明确的合成控制，不是真实缺陷成品。训练中也加入了不同源帧组合的64张合成漏贴裁剪，训练组合和本次控制组合分开；仍不能据此声称现场漏贴或错序检出率。

## 标注、训练和数据边界

最终裁剪为{counts['train']}张训练、{counts['val']}张验证、{counts['test']}张测试。由当前模型逐位置视觉复核，使用SIFT辅助对齐与传播、贴前贴后保守时间段和遮挡剔除；没有独立人工标注团队。验证、测试按整帧时间段留出，相邻时间设保护间隔，增强样本来自训练时段。数据仍来自同一正确工件视频，具有时间与场景相关性。

实际进行了100、40、30、20、20轮的迭代训练，分别加入放模板/阴影难样本、实时JPEG输入、相邻已贴与目标未贴的联合样本及合成漏贴对照。各版权重、失败对照和标注审计均保留。裁剪mAP等原始指标见 `training-result.json`，它们不是独立场景或现场缺陷准确率。

代码测试：{match.group(0) if match else '见 unit-tests-final.log'}。原螺丝界面回放、实时模拟和过期清框回归通过；任务选择、真实文件上传、明细与工序计数随回放同步、任务历史隔离、刷新空白及移动布局均已在原界面核对。

## 使用与后续工位部署

打开页面，选择“贴条工序检测”，再上传视频或从贴之前开始摄像头新轮次；每件结束并判定，下一件用新轮次。可收藏 `http://127.0.0.1:8000/?task=sticker`，螺丝工位使用 `?task=screw`。这仅选择任务，不自动开启摄像头或旧视频。

工位部署说明：`docs/sticker-inspection-station-2026-10-17.md`。当前模型是这段已参与训练视频的开发模型，尚未接入PLC或通过现场验收；新机位、光照、料件与真实漏贴/错序成品需要独立标定和验证。

模型SHA256：`{data['model']['sha256']}`。

正确视频SHA256：`{data['source']['sha256']}`。

可核查文件包括 `dataset-provenance.json`、各 `*-audit.json`、`promotion.json`、`candidate-evaluation*.json`、`realtime-api-test.json`、`task-ui-result.json` 及 `{args.run_folder}/`。先前误检与漏贴控制失败结果保留用于对照，不覆盖为成功成绩。
'''
(OUT/'report.md').write_text(report,encoding='utf-8')
print(json.dumps(dict(run=data['id'],verdict=data['verdict'],counts=counts,live_failures=live['failures']),ensure_ascii=False))
