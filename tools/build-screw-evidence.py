"""Build a reviewable contact sheet from published per-location evidence times."""
import json
from pathlib import Path
import cv2
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'outputs/screw-inspection-2026-10-17'


def main():
    result=json.loads((OUT/'full-video-result.json').read_text(encoding='utf-8'))
    source=json.loads((OUT/'source.json').read_text(encoding='utf-8'))
    tiles=[]
    for target in result['targets']:
        if target['lastSeen'] is None:continue
        frame=min(result['frames'],key=lambda f:abs(f['time']-target['lastSeen']))
        entry=min(source['samples'],key=lambda f:abs(f['time']-target['lastSeen']))
        image=cv2.imread(str(OUT/'frames'/entry['file']))
        observation=next(t for t in frame['inspection']['targets'] if t['id']==target['id'])
        a,b,c,d=observation['bbox'];x,y=round((a+c)/2*1920),round((b+d)/2*1080)
        crop=image[max(0,y-30):y+30,max(0,x-30):x+30]
        tile=np.full((250,240,3),248,np.uint8)
        tile[40:240,20:220]=cv2.resize(crop,(200,200))
        cv2.putText(tile,f'{target["id"]}  {target["lastSeen"]:.2f}s  {target["score"]:.2f}',(10,25),0,.48,(70,95,50),1)
        tiles.append(tile)
    if tiles:
        tiles+=[np.full_like(tiles[0],248)]*((-len(tiles))%4)
        cv2.imwrite(str(OUT/'twelve-location-evidence.jpg'),np.vstack([np.hstack(tiles[i:i+4]) for i in range(0,len(tiles),4)]))
    summary=result['summary']
    rows='\n'.join(f'| {t["id"]} | {t["name"]} | {t["state"]} | {t["lastSeen"]} | {t["score"]} |' for t in result['targets'])
    report=f'''# 螺丝专项训练与视频测试结果

日期：2026-10-17。旧六工位和动作示例 SOP 已从启用配置移除，新页面运行于 http://127.0.0.1:8000 。

源视频 `VID_20261017_103101.mp4` 约 53.66 秒，用户确认为正确工序。成品标注12处可见螺丝头，其中包含2处预装件，不代表本工序拧紧次数。

实际微调 YOLO11n 120轮，172/12/42 张训练/验证/测试裁剪，内部同视频 mAP@50 为97.5%。标签由参考点和图像匹配辅助生成，参考图也参与测试标签标定；这不是独立缺陷测试。真实漏打检出率尚未验证。

完整原视频经API和独立Worker实测成功。采样 {summary['analyzedFrames']} 帧，主体与定位同时满足检查条件 {summary['registeredFrames']} 帧，处理 {summary['processingSeconds']} 秒。最后可定位工件为 {summary['assessmentTime']} 秒；本轮累计历史中，12处均取得至少两帧可见螺丝头证据，空孔0，证据不足0。S02最后一帧被导线部分遮挡，使用50.53和50.76秒的一致证据，不调低阈值强行判定。

| 位置 | 名称 | 状态 | 最近证据秒 | 保守分数（两帧取低） |
|---|---|---|---:|---:|
{rows}

![逐位置原视频证据](twelve-location-evidence.jpg)

空白图、人工遮挡图和原视频工件移走帧均未被判为装好。把一个真实空孔裁剪贴到S01的合成压力测试会报告EMPTY；它只证明检测链路能响应，不代表真实缺陷检出率。

自动化测试112通过、1跳过（未配置PostgreSQL测试库）。桌面和390px手机页面、登录、真实任务回放、逐位置证据定位、报告访问已通过。

任务编号：`{result['id']}`。源视频SHA256：`{result['source']['sha256']}`。模型SHA256：`{result['model']['sha256']}`。

详细来源、协议、边界与复现命令见 `docs/screw-inspection-2026-10-17.md`。螺丝头可见不代表扭矩合格，需独立合格与漏打视频继续验证。
'''
    (OUT/'results.md').write_text(report,encoding='utf-8')
    print('Evidence sheet and results.md saved')


if __name__=='__main__':main()
