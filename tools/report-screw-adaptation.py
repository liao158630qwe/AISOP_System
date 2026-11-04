"""Assemble only measured training, full-video, API and UI evidence."""
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'outputs/screw-adaptation-v4-2026-10-17'

def read(path):return json.loads(path.read_text(encoding='utf-8'))

training=read(OUT/'training-result.json')
comparison=read(OUT/'comparison.json')
data=read(OUT/'full-video-v4b/full-video-result.json')
baseline=read(ROOT/'outputs/screw-validation-lv-141529-2026-10-17/full-video-result.json')
realtime=read(OUT/'realtime-api-test.json')
assert data['source']['sha256']==baseline['source']['sha256']
assert data['calibration']['assets'][training['model']]==training['model_sha256']
assert data['model']['sha256']==data['calibration']['assets'][data['calibration']['config']['model']]
assert comparison['accepted']
s=data['summary'];c=comparison['summary']['v4']
counts=read(OUT/'dataset-provenance.json')['final_counts']
profiles={}
for frame in data['frames']:
    if frame['inspection']['registered']:
        p=frame['inspection']['calibrationId'];profiles[p]=profiles.get(p,0)+1
api_rows='\n'.join(f'| {v["case"]} | {v["result"]["compliance"]["label"]} |' for v in realtime)
report=f'''# 新视频适配训练与复测

已将 `lv_0_20261017141529.mp4` 加入标定与训练，并启用 {data['model']['name']}。用户确认该视频为完整正确工序。原始独立验证的失败结果保留，本次结果为训练后的开发复测。

## 完整视频结果

| 项目 | 结果 |
| --- | --- |
| 视频 | 3840 × 2160，{data['source']['duration']:.2f} 秒 |
| 采样 | 4 FPS，共 {s['analyzedFrames']} 帧 |
| 成功核对工件的帧 | 优化前 {baseline['summary']['registeredFrames']} → 优化后 {s['registeredFrames']} |
| 最终位置清单 | 当前匹配视角 {s['expected']} 处 |
| 最终有螺丝证据 / 空孔 / 证据不足 | {s['present']} / {s['empty']} / {s['unknown']} |
| 最后可核对工件时刻 | {s['assessmentTime']} 秒 |
| 最终结论 | {data['compliance']['label']} |
| 完整处理耗时 | {s['processingSeconds']} 秒（含解码与回放编码） |
| 检测记录编号 | {data['id']} |

最终理由：{data['compliance']['reason']}。范围：{data['compliance']['scope']}。完整视频并非按“曾经检出最多螺丝的帧”判定，而是按末次可核对工件附近连续片段、每处至少两次一致观察判定；不同标定视角不拼接证据。

## 改动和训练

定位增加新视角的工件参考和组件区域，原视角参考保留。每帧计算当前特征并匹配视角，再运行共享的局部YOLO模型。新视角的S02裁剪使用针对实际安装前后、原始像素重新训练的YOLO26m专用权重，其余位置使用复测未退步的基础V4权重；专用权重不替换其他位置。模型及两套参考图全部写入任务快照并校验哈希。没有旧框复用、光流补框或累计整段视频凑齐完成位置。

新视角逐处复核得到11处明确可见螺丝，原视角清单为12处。编号及名称以所选视角为准；清单不代表工序总拧紧次数。顶部连接件旁始终未使用的空孔列为负样本，未加入必装位置。两套视角的位置总数不同，不将总数变化当作识别准确率提升。

混合两段已知视频，最终共{counts['train']}张训练、{counts['val']}张验证、{counts['test']}张测试裁剪。助手视觉复核参考位置、安装前后差异及传播结果；标签由SIFT与模板辅助生成，模糊、遮挡、标签不完整的裁剪排除。增加30张真实安装前空孔难负样本，并加入120张S02原始分辨率安装前后裁剪，修正两张S02过早标为螺丝的辅助标签。新纳入训练的73秒后新视频裁剪已从验证和测试集中移出并归档；最终裁剪验证和测试仅余旧视频，另用新视频完整流程进行开发复测。先训练60轮、补难负样本40轮、补原始像素与纠错30轮，保留各轮最佳权重和审计记录。

原生4K清晰帧76秒：仅补标定的V3检出2/11处，V4检出11/11处；1080p清晰帧同样检出11/11处。旧视频49个已复核可见螺丝仍检出{c['old_heads']}/49，已复核集合中的确定错误{c['old_wrong']}个。空白和两段视频的离场控制均无框。新视频22秒已核对空孔未误报为螺丝，个别原生空孔仍保留未知。

热身后所选帧核心处理中位 {c['median_ms']:.1f}ms，P95 {c['p95_ms']:.1f}ms。实时API的新视角清晰静帧请求分别为 {next(v['request_wall_ms'] for v in realtime if v['case']=='new-view-correct-stills')}ms，包含请求与推理，不包含真实摄像头采集；接口测量来自模型热身后，首次加载耗时另计。原视频静帧、遮挡与离场图像用于模拟摄像头采样，不能声称是真实摄像头现场验收。

## 实时接口与界面

| 实时输入检查 | 最终结果 |
| --- | --- |
{api_rows}

其中空孔移植是明确的合成测试；“安装前工序”检查来自尚未完成安装的真实帧，不是独立真实漏打成品。

保持原深色监控界面。明细和判定过程按当前视角的位置数量显示，刷新仍为空工作区；需从检测记录手动打开新任务。当前帧遮挡、离场或过期时不显示旧检测框。自动化测试124项通过，1项跳过；原界面回放、实时模拟、稳定面板与过期清框回归通过。

## 结果的使用边界

这段视频已参与参考标定、标注和训练，本次不是独立泛化测试。`training-result.json`中的最终裁剪指标来自S02专用权重在旧视频已知裁剪上的开发评估，不能当作部署的组合流程、新视频或现场逐颗螺丝准确率、真实漏打召回率。专用权重若用于全部位置存在退步，因此只用于已复核的S02；该全局替换候选的失败结果也已保存。当前仍使用两套标定，新机位及真实漏打成品需要独立测试。螺丝头可见也不能证明扭矩达标。

基础模型 SHA256：`{data['model']['sha256']}`。

S02专用模型 SHA256：`{training['model_sha256']}`。

源视频 SHA256：`{data['source']['sha256']}`，与优化前完全一致。

原始失败基线：`outputs/screw-validation-lv-141529-2026-10-17/`。本目录 `comparison.json`、`dataset-provenance.json`、`hard-negative-audit.json`、`native-positive-audit.json`、`S02-label-corrections.json`、`realtime-api-test.json`、`full-video-v4b/` 保存可核查数据。`full-video/` 保留上一轮因S02证据不足而REVIEW的完整复测；`config-before.json` 保存旧配置，V3和上一轮V4权重保留可回退。
'''
(OUT/'report.md').write_text(report,encoding='utf-8')
print(json.dumps(dict(verdict=data['compliance']['status'],summary=s,registered_profiles=profiles),ensure_ascii=False))
