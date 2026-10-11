"""Shared online/offline visual candidates, not assembly-completion assertions."""
import cv2
import numpy as np
from .stations import Station, asset_path, verify_assets


class Inspector:
    YOLO_BATCH_SIZE = 8

    def __init__(self, frozen):
        verify_assets(frozen)
        self.frozen = frozen
        self.station = Station.model_validate(frozen['config'])
        self.cache = {}

    def resource(self, target):
        cache_key = (target.method,target.asset)
        if cache_key not in self.cache:
            if target.method == 'template':
                template = cv2.imread(str(asset_path(target.asset)), cv2.IMREAD_GRAYSCALE)
                if template is None or np.std(template) < 1:
                    raise ValueError('模板不可解码或纹理不足')
                self.cache[cache_key] = template
            else:
                from ultralytics import YOLO
                model = YOLO(str(asset_path(target.asset)))
                if model.task != 'detect':
                    raise ValueError('必须配置灯具目标检测权重，不能使用人体姿态权重')
                self.cache[cache_key] = model
        return self.cache[cache_key]

    def candidate(self, crop, target):
        if target.method == 'hsv':
            mask = cv2.inRange(cv2.cvtColor(crop, cv2.COLOR_BGR2HSV),
                               np.array(target.hsv_low), np.array(target.hsv_high))
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3,3), np.uint8))
            _, _, stats, _ = cv2.connectedComponentsWithStats(mask)
            area = max(stats[1:, cv2.CC_STAT_AREA], default=0) / mask.size
            return target.min_area <= area <= target.max_area, float(area), '黄色连通域面积比例'
        resource = self.resource(target)
        if target.method == 'template':
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            if any(a > b for a,b in zip(resource.shape, gray.shape)):
                raise ValueError('模板比目标ROI大')
            score = float(cv2.minMaxLoc(cv2.matchTemplate(gray, resource, cv2.TM_CCOEFF_NORMED))[1])
            return score >= target.threshold, score, '归一化模板相关分数'
        if target.class_name not in resource.names.values():
            raise ValueError('专用权重不包含指定训练类别')
        result = resource.predict(crop, imgsz=640, conf=target.threshold, max_det=30, verbose=False)[0]
        scores = [float(conf) for cls,conf in zip(result.boxes.cls.tolist(), result.boxes.conf.tolist())
                  if resource.names[int(cls)] == target.class_name]
        return bool(scores), max(scores, default=0), '专用目标检测分数'

    @staticmethod
    def apply_candidate(result, detected, score, metric):
        result.update(state='CANDIDATE' if detected else 'UNKNOWN', score=round(score, 4), metric=metric,
            reason='检测到可见候选，未验证安装质量' if detected else '未检出可靠候选；遮挡与缺件尚不可区分')

    def detect_regions(self, pending):
        # Equal shapes retain YOLO's single-image rectangular letterboxing. Keep
        # thresholds separate so confidence filtering and max_det remain identical.
        groups = {}
        for crop, target, observation in pending:
            try:
                resource = self.resource(target)
                if target.class_name not in resource.names.values():
                    raise ValueError('专用权重不包含指定训练类别')
            except (ValueError, OSError) as exc:
                observation['reason'] = str(exc)
                continue
            key = (target.asset, target.threshold, crop.shape)
            groups.setdefault(key, []).append((crop, target, observation))
        for group in groups.values():
            resource = self.resource(group[0][1])
            for start in range(0, len(group), self.YOLO_BATCH_SIZE):
                batch = group[start:start+self.YOLO_BATCH_SIZE]
                try:
                    predictions = resource.predict([item[0] for item in batch], imgsz=640,
                        conf=batch[0][1].threshold, max_det=30, rect=True, verbose=False)
                    if len(predictions) != len(batch):
                        raise ValueError('目标检测返回数量与ROI数量不一致')
                    for (_, target, observation), prediction in zip(batch, predictions):
                        scores = [float(score) for cls, score in zip(
                            prediction.boxes.cls.tolist(), prediction.boxes.conf.tolist())
                            if resource.names[int(cls)] == target.class_name]
                        self.apply_candidate(observation, bool(scores), max(scores, default=0), '专用目标检测分数')
                except (ValueError, OSError) as exc:
                    for _, _, observation in batch:
                        observation['reason'] = str(exc)

    def inspect(self, frame):
        station = self.station
        h, w = frame.shape[:2]
        checks = []
        pending = []
        for check in station.checks:
            observations = []
            for target in check.targets:
                result = {'id':target.id, 'category':target.category, 'roi':list(target.roi),
                          'state':'UNKNOWN', 'method':target.method, 'score':None}
                x1,y1,x2,y2 = [round(v*s) for v,s in zip(target.roi,[w,h,w,h])]
                crop = frame[y1:y2,x1:x2]
                if not station.approved or not station.fixed_fixture:
                    result['reason'] = '工位配置未批准或固定治具未确认'
                elif crop.size == 0 or min(crop.shape[:2]) < 8:
                    result['reason'] = '检测区域像素不足'
                elif cv2.Laplacian(cv2.cvtColor(crop,cv2.COLOR_BGR2GRAY),cv2.CV_64F).var() < station.min_sharpness:
                    result['reason'] = '检测区域清晰度不足'
                elif target.method == 'yolo':
                    pending.append((crop, target, result))
                else:
                    try:
                        detected, score, metric = self.candidate(crop,target)
                        self.apply_candidate(result, detected, score, metric)
                    except (ValueError, OSError) as exc:
                        result['reason'] = str(exc)
                observations.append(result)
            checks.append({'id':check.id,'name':check.name,'state':'REVIEW',
                           'expected':check.expected,'requiredEvidence':check.required_evidence,'targets':observations})
        self.detect_regions(pending)
        for check, output in zip(station.checks, checks):
            observations = output['targets']
            found = sum(o['state'] == 'CANDIDATE' for o in observations)
            reason = ('需要动作时序及设备完成证据' if check.kind == 'process' else
                      '尚未配置现场位置ROI及专用检测资源' if not check.targets else
                      f'本帧可见候选 {found}/{len(observations)}；安装质量待复核')
            output['reason'] = reason
        return {'stationId':station.id,'stationName':station.name,'version':station.version,
                'configHash':self.frozen['sha256'],'approved':station.approved,
                'verdict':'REVIEW','checks':checks}
