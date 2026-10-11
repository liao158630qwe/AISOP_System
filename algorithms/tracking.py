"""Motion-predicted global assignment with visibility-gated temporal evidence."""
from collections import deque
import math
import numpy as np
from scipy.optimize import linear_sum_assignment

from .pose import LABELS, action_observation, iou, smooth_keypoints
from .temporal import ActionDecoder


class PoseTracker:
    def __init__(self, max_gap=1.2):
        self.active = {}
        self.next_id = 1
        self.max_gap = max_gap
        self.last_time = None

    @staticmethod
    def break_evidence(track):
        track['history'].clear()
        track['decoder'].reset()
        track['filtered_keypoints'] = None

    @staticmethod
    def association(tr, box, keypoints, t, aspect):
        dt = min(t-tr['last'], .6)
        predicted = tr['box'] + np.tile(tr['velocity']*dt, 2)
        center = (box[:2]+box[2:])/2
        predicted_center = (predicted[:2]+predicted[2:])/2
        scale = max(.05, float((box[3]-box[1]+predicted[3]-predicted[1])/2))
        distance = float(np.linalg.norm((center-predicted_center)*[aspect, 1]))/scale
        overlap = iou(box, predicted)
        if overlap < .03 and distance > .65:
            return 1e6
        visible = (keypoints[:, 2] >= .45) & (tr['keypoints'][:, 2] >= .45)
        pose_distance = .3
        if visible.sum() >= 4:
            old = (tr['keypoints'][visible, :2]-tr['center'])*[aspect, 1]
            new = (keypoints[visible, :2]-center)*[aspect, 1]
            pose_distance = min(1., float(np.median(np.linalg.norm(old-new, axis=1)))/scale)
        return .45*(1-overlap)+.4*min(distance, 1.5)+.15*pose_distance

    def update(self, result, w, h, t):
        if not math.isfinite(t) or (self.last_time is not None and t <= self.last_time):
            raise ValueError('跟踪时间戳必须严格递增且有限')
        self.last_time = t
        self.active = {tid: tr for tid, tr in self.active.items() if t-tr['last'] <= self.max_gap}
        if result.keypoints is None or result.boxes is None:
            for track in self.active.values():
                self.break_evidence(track)
            return []
        boxes = result.boxes.xyxy.cpu().numpy()/np.array([w, h, w, h])
        points = result.keypoints.data.cpu().numpy()
        detections = []
        for box, conf, raw in zip(boxes, result.boxes.conf.cpu().numpy(), points):
            if raw.shape != (17, 3) or not np.isfinite(raw).all() or not np.isfinite(box).all() or not np.isfinite(conf):
                continue
            if box[2] <= box[0] or box[3] <= box[1] or conf < .2:
                continue
            detections.append((box, float(conf), raw/np.array([w, h, 1])))
            if len(detections) >= 50:
                break
        tids = list(self.active)
        costs = np.full((len(detections), len(tids)+len(detections)), .95)
        for i, (box, _, k) in enumerate(detections):
            for j, tid in enumerate(tids):
                costs[i, j] = self.association(self.active[tid], box, k, t, w/h)
        assigned = dict(zip(*linear_sum_assignment(costs))) if detections else {}
        people = []
        for i, (box, conf, k) in enumerate(detections):
            j = assigned[i]
            matched = j < len(tids) and costs[i, j] < .95
            ambiguous = False
            if matched:
                tid = tids[j]
                alternatives = np.delete(costs[i, :len(tids)], j)
                competitors = np.delete(costs[:, j], i)
                ambiguous = bool((alternatives.size and alternatives.min()-costs[i, j] < .06) or
                                 (competitors.size and competitors.min()-costs[i, j] < .06))
            else:
                if conf < .35:
                    continue
                tid = self.next_id
                self.next_id += 1
                self.active[tid] = {'history': deque(maxlen=64), 'decoder': ActionDecoder(),
                                    'velocity': np.zeros(2), 'observations': 0}
            tr = self.active[tid]
            center = (box[:2]+box[2:])/2
            gap = t-tr['last'] if matched else None
            if matched:
                velocity = (center-tr['center'])/max(.001, gap)
                tr['velocity'] = .5*tr['velocity']+.5*np.clip(velocity, -1, 1)
            if ambiguous or (gap is not None and gap > .75):
                tr['history'].clear()
                tr['decoder'].reset()
            raw_keypoints = k.copy()
            k = smooth_keypoints(k, tr.get('filtered_keypoints') if not ambiguous else None,
                                 gap, box[3]-box[1], w/h)
            metric = k.copy()
            metric[:, 0] *= w/h
            scores, quality = action_observation(metric, box, tr['history'], t)
            decoded = tr['decoder'].update(scores, t, valid=quality['valid'] and not ambiguous)
            action = decoded['action']
            raw_action = max(scores, key=scores.get) if max(scores.values(), default=0) >= .6 else 'unknown'
            if min(k[11, 2], k[12, 2]) >= .35 and not ambiguous:
                tr['history'].append((t, float((metric[11, 0]+metric[12, 0])/2)))
            tr.update(last=t, box=box, center=center, keypoints=raw_keypoints,
                      filtered_keypoints=k if not ambiguous else None, observations=tr['observations']+1)
            reliable = quality['valid'] and not ambiguous and tr['observations'] >= 2
            people.append(dict(trackId=tid, confidence=round(conf, 4), bbox=np.round(box, 5).tolist(),
                keypoints=np.round(k, 5).tolist(), rawKeypoints=np.round(raw_keypoints, 5).tolist(),
                action=action, rawAction=raw_action, label=LABELS[action],
                evidence=f"{quality['reason']}；{decoded['reason']}；不等同于工序完成",
                visibleKeypoints=int(sum(k[:, 2] >= .35)), actionScores=decoded['scores'],
                signals=quality['signals'] if reliable else {}, temporal=decoded,
                tracking={'reliable': reliable, 'ambiguous': ambiguous, 'gapSeconds': round(gap or 0, 4)}))
        observed = {person['trackId'] for person in people}
        for tid, track in self.active.items():
            if tid not in observed:
                self.break_evidence(track)
        if len(self.active) > 128:
            self.active = dict(sorted(self.active.items(), key=lambda item:item[1]['last'], reverse=True)[:128])
        return people
