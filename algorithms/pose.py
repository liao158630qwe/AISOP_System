import math
import numpy as np

from .skeleton import LIMB_EDGES, VISIBILITY_THRESHOLD

LABELS = {'standing':'站立 / 原地作业','walking':'位置移动（行走候选）','raised':'抬手','reaching':'手臂伸出','bending':'躯干前倾','unknown':'姿态不确定'}
COLORS = {'standing':'#3fcf9b','walking':'#5aa7ff','raised':'#b17bfd','reaching':'#efb956','bending':'#ed8396','unknown':'#7890ac'}
# 兼容旧调用方：四肢躯干连线；完整可绘制骨架（含头部连线）见 skeleton.BODY_EDGES。
EDGES = LIMB_EDGES


def smooth_keypoints(current, previous, dt, body_height, aspect=1.):
    """Damp small jitter, follow fast motion, never restore occluded joints."""
    result = current.copy()
    if previous is None or dt is None or not 0 < dt <= .35:
        return result
    visible = (current[:, 2] >= VISIBILITY_THRESHOLD) & (previous[:, 2] >= VISIBILITY_THRESHOLD)
    distance = np.linalg.norm((current[:, :2]-previous[:, :2])*[aspect, 1], axis=1)
    motion = np.clip(distance/max(.05, body_height)/.12, 0, 1)
    # Fast motion reduces the filter time constant to avoid dragging the skeleton.
    tau = .09*(1-motion)+.015*motion
    alpha = 1-np.exp(-dt/tau)
    result[visible, :2] = previous[visible, :2]+alpha[visible, None]*(current[visible, :2]-previous[visible, :2])
    return result

def iou(a,b):
    x1,y1,x2,y2=max(a[0],b[0]),max(a[1],b[1]),min(a[2],b[2]),min(a[3],b[3])
    intersection=max(0,x2-x1)*max(0,y2-y1)
    return intersection / max(1e-8,(a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-intersection)

def action_rule(k,box,history,t):
    def visible(*ids):return all(k[i][2]>=VISIBILITY_THRESHOLD for i in ids)
    if not visible(5,6,11,12):return 'unknown', '肩部或髋部关键点置信不足，保留为不确定'
    shoulder=(k[5][:2]+k[6][:2])/2;hip=(k[11][:2]+k[12][:2])/2
    torso=max(float(np.linalg.norm(hip-shoulder)),.06)
    for wrist,shoulder_id in [(9,5),(10,6)]:
        if visible(wrist,shoulder_id) and k[wrist][1]<k[shoulder_id][1]-.10*torso:
            return 'raised','可见手腕高于同侧肩部 0.10 倍躯干长度以上；不等同于取物'
    tilt=math.degrees(math.atan2(abs(float(shoulder[0]-hip[0])),max(.01,float(hip[1]-shoulder[1]))))
    if tilt>32:return 'bending',f'肩髋连线偏离竖直约 {tilt:.0f}°，仅为画面姿态判断'
    for wrist,shoulder_id in [(9,5),(10,6)]:
        if visible(wrist,shoulder_id) and abs(k[wrist][0]-k[shoulder_id][0])>.5*torso and k[wrist][1]<hip[1]:
            return 'reaching','手腕横向离肩超过 0.5 倍躯干长度且位于髋部上方；未验证物体交互'
    previous=[item for item in history if .55<=t-item[0]<=1.2]
    if previous:
        old=previous[0];speed=abs(float(hip[0])-old[1])/max(.1,t-old[0])/max(.15,box[3]-box[1])
        if speed>.12:return 'walking',f'髋部横向位移速率 {speed:.2f} 身高/秒，高于 0.12 阈值；可能含检测抖动'
    return 'standing','肩髋可见，未达到抬手、伸臂、前倾或位移阈值；不能据此确认具体工序'


def action_observation(k, box, history, t):
    """Aspect-corrected geometry; independent signals preserve overlapping actions."""
    scores = {name: 0. for name in LABELS if name != 'unknown'}
    if not np.isfinite(k).all() or min(k[i, 2] for i in (5, 6, 11, 12)) < VISIBILITY_THRESHOLD:
        return scores, {'valid': False, 'reason': '肩髋关键点置信不足', 'signals': {}}
    shoulder = (k[5, :2] + k[6, :2]) / 2
    hip = (k[11, :2] + k[12, :2]) / 2
    torso = max(float(np.linalg.norm(hip-shoulder)), .03)
    def ramp(value, low, high):
        return float(np.clip((value-low)/(high-low), 0, 1))
    # Detector confidence only weights visible evidence; it is not action truth.
    quality = min(1., min(k[i, 2] for i in (5, 6, 11, 12))/.6)
    raised, reach = 0., 0.
    arms = 0
    for wrist, side in ((9, 5), (10, 6)):
        if min(k[wrist, 2], k[side, 2]) < VISIBILITY_THRESHOLD:
            continue
        arms += 1
        reliability = min(1., k[wrist, 2]/.6)
        raised = max(raised, reliability*ramp((k[side, 1]-k[wrist, 1])/torso, .05, .2))
        if k[wrist, 1] < hip[1]:
            reach = max(reach, reliability*ramp(abs(k[wrist, 0]-k[side, 0])/torso, .4, .75))
    tilt = math.degrees(math.atan2(abs(float(shoulder[0]-hip[0])), max(.01, float(hip[1]-shoulder[1]))))
    bending = ramp(tilt, 25, 42)*quality
    # Median pairwise slope rejects individual hip-jitter outliers.
    samples = [(ts, x) for ts, x in history if 0 < t-ts <= 1.25] + [(t, float(hip[0]))]
    slopes = [(b[1]-a[1])/(b[0]-a[0]) for i, a in enumerate(samples) for b in samples[i+1:]
              if .2 <= b[0]-a[0] <= 1.25]
    speed = abs(float(np.median(slopes)))/max(.1, box[3]-box[1]) if len(samples)>=3 and t-samples[0][0]>=.4 and slopes else 0.
    walking = ramp(speed, .09, .2)*quality
    signals = dict(raised=raised, reaching=reach, bending=bending, walking=walking)
    # Standing needs arm evidence too: missing wrists cannot prove inactivity.
    signals['standing'] = quality*max(0., 1-max(signals.values())) if arms == 2 else 0.
    scores.update(signals)
    return scores, {'valid': True, 'reason': '尺度归一化姿态与稳健位移',
                    'torsoTiltDegrees': round(tilt, 2), 'hipSpeedBodyHeights': round(speed, 4),
                    'signals': {name: round(value, 4) for name, value in signals.items()}}
