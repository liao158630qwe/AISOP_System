"""Model-independent ROI rules over normalized Ultralytics observations."""
from collections import Counter, defaultdict
import csv
import html
import json
import math
from pathlib import Path
import time

import cv2
import numpy as np

from .evidence import EvidenceWindow
from .video import AnalysisError, file_hash, probe, validate_options


def verify(frozen):
    path = Path(frozen['model_path'])
    if not path.is_file() or file_hash(path) != frozen['model']['sha256']:
        raise AnalysisError('MODEL_CHANGED', '已注册模型改变或缺失，请重新注册并开始检测')


class SceneDetector:
    def __init__(self, frozen, model=None):
        self.frozen, self.config = frozen, frozen['config']
        if model is None:
            verify(frozen)
            from ultralytics import YOLO
            model = YOLO(frozen['model_path'])
        self.model = model
        if model.task != frozen['model']['task']:
            raise AnalysisError('MODEL_TASK_MISMATCH', '模型任务类型与注册信息不一致')
        import torch
        requested = self.config['device']
        if requested == 'cuda' and not torch.cuda.is_available():
            raise AnalysisError('DEVICE_UNAVAILABLE', '当前服务器不可用 CUDA，请修改任务的运行设备')
        self.device = 0 if requested != 'cpu' and torch.cuda.is_available() else 'cpu'

    def inspect(self, frame):
        started = time.perf_counter()
        height, width = frame.shape[:2]
        groups, regions, observations = defaultdict(list), {}, []
        for rule in self.config['rules']:
            key = tuple(rule['roi'])
            if key in regions:
                continue
            x1, y1, x2, y2 = [round(v*s) for v, s in zip(key, (width, height, width, height))]
            crop = frame[y1:y2, x1:x2]
            sharpness = float(cv2.Laplacian(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()) if crop.size else 0
            valid = min(crop.shape[:2], default=0) >= 8 and sharpness >= self.config['min_sharpness']
            regions[key] = dict(valid=valid, sharpness=round(sharpness, 2), objects=[])
            if valid:
                groups[crop.shape].append((key, crop, (x1, y1, x2, y2)))
        confidence = min((r['threshold'] for r in self.config['rules']), default=.6)
        for group in groups.values():
            for start in range(0, len(group), 8):
                batch = group[start:start+8]
                predictions = self.model.predict([item[1] for item in batch], imgsz=self.config['imgsz'],
                    conf=confidence, iou=.45, max_det=300, device=self.device, half=False, verbose=False)
                if len(predictions) != len(batch):
                    raise AnalysisError('PREDICTION_INVALID', '模型返回数量与检测区域不一致')
                for prediction, (key, crop, rect) in zip(predictions, batch):
                    items = self._objects(prediction, rect, width, height)
                    regions[key]['objects'] = items
                    for item in items:
                        if not any(item['class_id'] == old['class_id'] and _iou(item['bbox'], old['bbox']) > .8 for old in observations):
                            observations.append(item)
        checks = []
        for rule in self.config['rules']:
            region = regions[tuple(rule['roi'])]
            matched = [o for o in region['objects'] if o['class_id'] == rule['class_id'] and o['score'] >= rule['threshold']]
            count = len(matched)
            value = 'REVIEW'
            if not region['valid']:
                reason = '检测区域不够清晰或像素不足'
            elif rule['kind'] == 'forbidden':
                value = 'FAIL' if count else 'CLEAR'
                reason = f'检出禁用类别 {count} 个' if count else '当前未检出禁用类别'
            elif count < rule['min_count']:
                reason = f'可靠检出 {count} 个，至少需要 {rule["min_count"]} 个；遮挡与缺件待核实'
            elif rule['max_count'] is not None and count > rule['max_count']:
                value, reason = 'FAIL', f'检出 {count} 个，超过允许数量 {rule["max_count"]}'
            else:
                value, reason = 'PASS', f'可靠检出 {count} 个，满足配置数量'
            checks.append(dict(id=rule['id'], name=rule['name'], kind=rule['kind'],
                class_id=rule['class_id'], class_name=self.frozen['model']['classes'][str(rule['class_id'])],
                state=value, count=count, score=min((o['score'] for o in matched), default=None),
                reason=reason, roi=rule['roi'], sharpness=region['sharpness']))
        return dict(observations=observations, checks=checks,
                    inferenceMs=round((time.perf_counter()-started)*1000, 2))

    def _objects(self, prediction, rect, width, height):
        x1, y1, x2, y2 = rect
        def points(values):
            return [[float(np.clip((x1+p[0])/width, 0, 1)), float(np.clip((y1+p[1])/height, 0, 1))] for p in values]
        task = self.frozen['model']['task']
        if task == 'classify':
            probs = prediction.probs
            categories = [(int(probs.top1), float(probs.top1conf), [0, 0, x2-x1, y2-y1])]
        elif task == 'obb':
            boxes = prediction.obb
            categories = zip(boxes.cls.cpu().tolist(), boxes.conf.cpu().tolist(), boxes.xyxy.cpu().tolist()) if boxes is not None else []
        else:
            boxes = prediction.boxes
            categories = zip(boxes.cls.cpu().tolist(), boxes.conf.cpu().tolist(), boxes.xyxy.cpu().tolist()) if boxes is not None else []
        result = []
        for index, (cls, score, box) in enumerate(categories):
            cls = int(cls)
            if str(cls) not in self.frozen['model']['classes'] or not math.isfinite(score):
                continue
            a, b = points([[box[0], box[1]], [box[2], box[3]]])
            item = dict(class_id=cls, name=self.frozen['model']['classes'][str(cls)],
                        score=round(float(score), 5), bbox=[*a, *b])
            if task == 'segment' and prediction.masks is not None:
                polygon = prediction.masks.xy[index]
                # Bound response payload while retaining contour geometry.
                item['polygon'] = points(polygon[::max(1, math.ceil(len(polygon)/160))])
            elif task == 'obb':
                item['polygon'] = points(prediction.obb.xyxyxyxy[index].cpu().tolist())
            elif task == 'pose' and prediction.keypoints is not None:
                keypoints = prediction.keypoints.data[index].cpu().tolist()
                item['keypoints'] = [[*points([p])[0], float(p[2]) if len(p)>2 else 1.] for p in keypoints]
            result.append(item)
        return result


def _iou(a, b):
    intersection = max(0, min(a[2], b[2])-max(a[0], b[0])) * max(0, min(a[3], b[3])-max(a[1], b[1]))
    area = (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - intersection
    return intersection/area if area > 0 else 0


class SceneEvidence:
    """A current-frame decision; confirmed observations never certify a later blank frame."""
    def __init__(self, config):
        self.config = config
        self.gap = max(1., 3/config['sample_fps'])
        self.votes = {r['id']: EvidenceWindow(config['confirmation_frames'], self.gap,
            max(4., self.gap*config['confirmation_frames'])) for r in config['rules']}
        self.confirmed, self.last = {}, None

    def update(self, timestamp, inspection):
        if not math.isfinite(timestamp) or (self.last is not None and timestamp <= self.last):
            raise ValueError('采样时间必须有限且严格递增')
        if self.last is not None and timestamp-self.last > self.gap:
            self.confirmed.clear()
            for votes in self.votes.values():
                votes.reset()
        self.last = timestamp
        checks = []
        for raw in inspection['checks']:
            item = dict(raw)
            identity, value = raw['id'], raw['state']
            votes = self.votes[identity]
            if value in ('PASS', 'FAIL'):
                if self.confirmed.get(identity) != value:
                    self.confirmed.pop(identity, None)
                    if votes.observe(timestamp, value, raw['score']) is not None:
                        self.confirmed[identity] = value
                else:
                    votes.reset()
                    votes.observe(timestamp)
                item['state'] = self.confirmed.get(identity, 'REVIEW')
                if item['state'] == 'REVIEW':
                    item['reason'] = f'候选证据确认中（至少 {self.config["confirmation_frames"]} 帧）'
            else:
                self.confirmed.pop(identity, None)
                votes.reset()
                votes.observe(timestamp)
            checks.append(item)
        required = [c for c in checks if c['kind'] == 'required']
        status = 'FAIL' if any(c['state'] == 'FAIL' for c in checks) else 'PASS' if required and all(
            c['state'] == ('PASS' if c['kind'] == 'required' else 'CLEAR') for c in checks) else 'REVIEW'
        reason = next((c['reason'] for c in checks if c['state'] == status),
                      '当前采样满足全部已配置视觉规则' if status == 'PASS' else '等待可靠视觉证据')
        return dict(**{k:v for k,v in inspection.items() if k != 'checks'}, checks=checks,
            compliance=dict(status=status, label={'PASS':'视觉规则通过', 'FAIL':'检出异常', 'REVIEW':'待复核'}[status],
                reason=reason, scope='仅核对已配置类别、数量与区域；未检出不直接证明缺件'))


def analyze(video, model_path, out, *, run_id, options, progress=None, max_seconds=600, detector=None, **unused):
    started = time.perf_counter()
    frozen = options['scene_config']
    verify(frozen)
    source_hash = file_hash(video)
    config = frozen['config']
    detector = detector or SceneDetector(frozen)
    meta = probe(video, max_seconds)
    start, end, rate, _, _ = validate_options(meta, options.get('start', 0), options.get('end'),
        min(config['sample_fps'], meta['fps']), None, config['imgsz'])
    evidence = SceneEvidence(dict(config, sample_fps=rate))
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(video))
    fps = float(cap.get(cv2.CAP_PROP_FPS))
    width, height = int(cap.get(3)), int(cap.get(4))
    scale = min(1., 1280/max(width, height))
    size = (max(2, round(width*scale)//2*2), max(2, round(height*scale)//2*2))
    writer = cv2.VideoWriter(str(out/'video.webm'), cv2.VideoWriter_fourcc(*'VP80'), fps, size)
    frames, next_sample, index = [], start, 0
    try:
        if not writer.isOpened():
            raise AnalysisError('ENCODER_FAILED', '回放视频编码器不可用')
        while index/fps < end:
            ok, frame = cap.read()
            if not ok:
                if index/fps < end-1/fps-.01:
                    raise AnalysisError('DECODE_FAILED', '视频解码提前结束')
                break
            timestamp = index/fps
            index += 1
            if timestamp < start:
                continue
            writer.write(cv2.resize(frame, size))
            if timestamp+1e-6 >= next_sample:
                next_sample += 1/rate
                result = evidence.update(timestamp, detector.inspect(frame))
                frames.append(dict(time=round(timestamp-start, 6), **result))
            if progress and index % max(1, round(fps)) == 0:
                progress(min(.98, (timestamp-start)/max(.001, end-start)))
    finally:
        cap.release()
        writer.release()
    if not frames:
        raise AnalysisError('NO_FRAMES', '所选范围没有可用采样')
    verify(frozen)
    if file_hash(video) != source_hash:
        raise AnalysisError('MEDIA_CHANGED', '处理期间视频内容发生变化')
    counts = Counter(f['compliance']['status'] for f in frames)
    result = dict(version=1, analysisType='scene', id=run_id, sceneId=frozen['scene_id'],
        sceneName=config['name'], sceneRevision=frozen['revision'], configHash=frozen['sha256'],
        model=frozen['model'], config=config, frames=frames, compliance=frames[-1]['compliance'],
        source=dict(title=options.get('title', Path(video).name), width=width, height=height,
                    fps=fps, duration=end-start, start=start),
        summary=dict(analyzedFrames=len(frames), sampleFps=rate, passFrames=counts['PASS'],
            failFrames=counts['FAIL'], reviewFrames=counts['REVIEW'],
            processingSeconds=round(time.perf_counter()-started, 3),
            p95InferenceMs=float(np.percentile([f['inferenceMs'] for f in frames], 95))))
    (out/'analysis.json').write_text(json.dumps(result, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    with (out/'segments.csv').open('w', newline='', encoding='utf-8-sig') as handle:
        rows = csv.writer(handle)
        rows.writerow(['采样秒', '规则', '状态', '数量', '说明'])
        for frame in frames:
            for check in frame['checks']:
                def safe(value):
                    value = str(value)
                    return "'"+value if value.startswith(('=', '+', '-', '@')) else value
                rows.writerow([frame['time'], safe(check['name']), check['state'], check['count'], safe(check['reason'])])
    report = '<!doctype html><meta charset="utf-8"><title>检测报告</title><style>body{font:16px sans-serif;max-width:900px;margin:50px auto;line-height:1.8}td,th{padding:10px;border-bottom:1px solid #ddd}</style>'
    report += f'<h1>{html.escape(config["name"])} · 检测报告</h1><p>任务版本 {frozen["revision"]} · 模型 {html.escape(frozen["model"]["name"])} {html.escape(frozen["model"]["version"])}</p>'
    report += f'<p>采样 {len(frames)} 帧：通过 {counts["PASS"]}，异常 {counts["FAIL"]}，待复核 {counts["REVIEW"]}。这是逐采样视觉结果，不代表整个视频的工艺合格率。</p><table><tr><th>秒</th><th>状态</th><th>说明</th></tr>'
    report += ''.join(f'<tr><td>{f["time"]:.2f}</td><td>{f["compliance"]["label"]}</td><td>{html.escape(f["compliance"]["reason"])}</td></tr>' for f in frames)
    (out/'report.html').write_text(report+'</table>', encoding='utf-8')
    manifest = dict(model_hash=frozen['model']['sha256'], source_hash=source_hash,
        scene_hash=frozen['sha256'], files={name:file_hash(out/name) for name in ('analysis.json','segments.csv','report.html','video.webm')})
    (out/'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    return result
