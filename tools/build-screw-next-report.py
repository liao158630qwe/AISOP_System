"""Write the final upgrade summary only from completed measured artifacts."""
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
BASE=ROOT/'outputs/screw-inspection-2026-10-17'
OUT=BASE/'next-models/yolo26m'


def main():
    training=json.loads((OUT/'training-result.json').read_text(encoding='utf-8'))
    evaluation=json.loads((OUT/'comparison.json').read_text(encoding='utf-8'))
    run=json.loads((BASE/'api-test.json').read_text(encoding='utf-8'))
    if run['model_sha256']!=training['model_sha256']:
        raise RuntimeError('The full video has not completed with this candidate hash')
    prior=evaluation['summary']['yolo11s-v2'];current=evaluation['summary']['yolo26m-v3']
    labels={'native':'原始1080p采样','jpeg40':'JPEG质量40（合成）','dim70':'亮度70%（合成）','720p':'缩为720p（合成）','blur06':'轻微模糊（合成）'}
    rows='\n'.join(f'| {labels[k]} | {prior[k]["visible_heads_found"]}/49 | {current[k]["visible_heads_found"]}/49 | {current[k]["empty_holes_called_heads"]} | {current[k]["median_ms"]:.1f} ms |' for k in labels)
    s=run['summary']
    text=f'''# YOLO26m 螺丝增强 v3 升级结果

已将本机实时与新离线任务切换到YOLO26m螺丝专用模型，保留YOLO11s权重与此前配置。原界面、当前帧画框、稳定明细、手动历史回放、刷新空工作区和结束判定均保留。

模型约{training['parameters']/1e6:.2f}M参数，实际训练{training['epochs']}轮，输入{training['input_size']}，batch{training['batch']}，设备{training['device']}，训练及测试约{training['seconds']:.1f}秒。官方基础权重与协议见上级protocol.md。沿用同一修正后的237/17/42张裁剪；这不是新增独立数据，也不是纯架构消融。

对比使用相同当前帧检测与位置关联规则、置信阈值0.45。

| 画面 | YOLO11s v2可见螺丝检出 | YOLO26m v3 | 空孔误报为螺丝 | v3核心处理中位 |
|---|---:|---:|---:|---:|
{rows}

这49个可见螺丝与9个空孔位置来自已使用视频的开发复核，含训练时段。合成压缩、变暗、缩小和模糊仅用于压力检查，不能当作真实独立漏打检出率。原始画面检出持平；压缩和轻微模糊各追回1处。模型更大，核心延迟比旧版略有增加，不能宣称所有指标都提升。裁剪测试mAP@50为{training['metrics']['metrics/mAP50(B)']:.3f}，mAP@50:95为{training['metrics']['metrics/mAP50-95(B)']:.3f}。

原始4K视频完整实测：{s['analyzedFrames']}个采样，可核对{s['registeredFrames']}帧，处理{s['processingSeconds']:.2f}秒；最终可见螺丝{s['present']}/{s['expected']}、空孔{s['empty']}、未知{s['unknown']}，末次有效工件{s['assessmentTime']}秒。任务编号`{run['run']['id']}`。

上一版完整视频可核对119帧、约152秒；本次V3为{s['registeredFrames']}帧、约{s['processingSeconds']:.0f}秒，最终12/12持平。可核对帧数不是人工真值准确率；本次升级主要改善所测压缩／模糊开发画面，不能声称每个画面和每项统计都优于旧版。

真实API正确采样PASS、空白REVIEW且无框、局部合成空孔FAIL；工件移走的4个已复核时刻在原图及压力变体下均未报螺丝。120项自动化通过、1项跳过（未配置PostgreSQL），桌面/手机、实时模拟摄像头、当前帧过期清框、特写清空、面板稳定、历史手动打开和片尾判定通过。摄像头链路使用原视频Y4M模拟输入，未实测物理摄像头。

刷新页面后不会自动打开视频；如需新版回放，从检测记录选择最新 **YOLO26m 螺丝增强 v3**。旧记录保留旧模型结果。合规结论仅限螺丝安装视觉证据，不验证扭矩。

权重：`{training['model_file']}`。SHA256：`{training['model_sha256']}`。
'''
    (OUT/'results.md').write_text(text,encoding='utf-8')
    print('Saved upgrade results.md')


if __name__=='__main__':main()
