import hashlib
import math
from pathlib import Path

import cv2


class AnalysisError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Cancelled(Exception):
    pass


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def probe(path, max_seconds=600):
    cap = cv2.VideoCapture(str(path))
    try:
        fps = cap.get(cv2.CAP_PROP_FPS)
        count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        w, h = cap.get(cv2.CAP_PROP_FRAME_WIDTH), cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
        if not cap.isOpened() or not all(math.isfinite(x) and x > 0 for x in [fps, count, w, h]):
            raise AnalysisError("INVALID_VIDEO", "无法解码视频或读取有效时长")
        duration = count / fps
        if fps > 120 or w * h > 3840 * 2160 or duration > max_seconds:
            raise AnalysisError("VIDEO_LIMIT", f"首版支持最长{max_seconds}秒、最高4K/120fps视频")
        ok, _ = cap.read()
        if not ok:
            raise AnalysisError("INVALID_VIDEO", "视频没有可解码帧")
        return dict(fps=fps, frame_count=int(count), width=int(w), height=int(h), duration=duration)
    finally:
        cap.release()


def validate_options(meta, start, end, sample_fps, crop, imgsz):
    end = meta["duration"] if end is None else end
    if not all(math.isfinite(float(x)) for x in [start, end, sample_fps]):
        raise AnalysisError("INVALID_OPTIONS", "时间和采样率必须为有限数值")
    if not 0 <= start < end <= meta["duration"] + .001 or not .5 <= sample_fps <= min(10, meta["fps"]):
        raise AnalysisError("INVALID_OPTIONS", "时间范围或采样率无效")
    if (end - start) * sample_fps > 6000 or imgsz not in [320, 640, 960, 1280]:
        raise AnalysisError("INVALID_OPTIONS", "采样帧数或模型输入尺寸超出限制")
    crop = crop or [0, 0, meta["width"], meta["height"]]
    if len(crop) != 4 or any(not isinstance(v, int) for v in crop):
        raise AnalysisError("INVALID_OPTIONS", "裁切区域需要4个整数")
    x, y, w, h = crop
    if min(x, y) < 0 or min(w, h) < 16 or x + w > meta["width"] or y + h > meta["height"]:
        raise AnalysisError("INVALID_OPTIONS", "裁切区域越界或过小")
    return start, min(end, meta["duration"]), sample_fps, crop, imgsz
