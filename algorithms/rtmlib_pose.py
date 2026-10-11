"""RTMPose / RTMO pose backends through rtmlib (ONNX Runtime).

Optional alternatives to the Ultralytics YOLO11-pose detector used by
algorithms.pipeline. predict() returns a single result object exposing the
subset of the Ultralytics Results API that the tracker consumes
(boxes.xyxy / boxes.conf / keypoints.data / speed), so tracking, pose rules,
reports and evidence rendering stay unchanged.

Models are the official MMPose ONNX exports (COCO-17 keypoints):

- rtmpose: YOLOX person detector + RTMPose top-down keypoints
- rtmo:    RTMO one-stage multi-person pose

Weights are downloaded once into ~/.cache/rtmlib/hub/checkpoints by rtmlib.
onnxruntime-gpu must match the local CUDA runtime (1.22.x for CUDA 12);
the CUDA/cuDNN DLLs shipped with PyTorch are added to the DLL search path.
"""
import os
import platform
import re
import time
from pathlib import Path

import numpy as np
import torch

from .video import file_hash

_NAMES = {'yolox': 'YOLOX', 'rtmpose': 'RTMPose', 'rtmo': 'RTMO'}


def _enable_ort_cuda_dlls():
    """Expose PyTorch's CUDA/cuDNN DLLs to onnxruntime-gpu before it loads."""
    lib = Path(torch.__file__).parent / "lib"
    if lib.is_dir() and hasattr(os, "add_dll_directory"):
        os.add_dll_directory(str(lib))
        os.environ["PATH"] = str(lib) + os.pathsep + os.environ.get("PATH", "")


def _label(path):
    match = re.match(r"(yolox|rtmpose|rtmo)[-_]([a-z0-9]+)", Path(path).name)
    return f"{_NAMES[match.group(1)]}-{match.group(2)}" if match else Path(path).stem


def _scored_yolox():
    """YOLOX subclass that keeps detection scores (upstream drops them)."""
    from rtmlib import YOLOX
    from rtmlib.tools.object_detection.post_processings import multiclass_nms

    class ScoredYOLOX(YOLOX):
        def __call__(self, image):
            processed, ratio = self.preprocess(image)
            outputs = self.inference(processed)[0]
            if outputs.shape[-1] == 4 or outputs.shape[-1] > 5:
                grids, stride_map = [], []
                for stride in (8, 16, 32):
                    hsize = self.model_input_size[0] // stride
                    wsize = self.model_input_size[1] // stride
                    xv, yv = np.meshgrid(np.arange(wsize), np.arange(hsize))
                    grid = np.stack((xv, yv), 2).reshape(1, -1, 2)
                    grids.append(grid)
                    stride_map.append(np.full((*grid.shape[:2], 1), stride))
                grids = np.concatenate(grids, 1)
                stride_map = np.concatenate(stride_map, 1)
                outputs[..., :2] = (outputs[..., :2] + grids) * stride_map
                outputs[..., 2:4] = np.exp(outputs[..., 2:4]) * stride_map
                predictions = outputs[0]
                boxes = predictions[:, :4]
                scores = predictions[:, 4:5] * predictions[:, 5:]
                xyxy = np.ones_like(boxes)
                xyxy[:, 0] = boxes[:, 0] - boxes[:, 2] / 2.
                xyxy[:, 1] = boxes[:, 1] - boxes[:, 3] / 2.
                xyxy[:, 2] = boxes[:, 0] + boxes[:, 2] / 2.
                xyxy[:, 3] = boxes[:, 1] + boxes[:, 3] / 2.
                xyxy /= ratio
                dets, _ = multiclass_nms(xyxy, scores, nms_thr=self.nms_thr, score_thr=self.score_thr)
                if dets is None:
                    return np.zeros((0, 4), np.float32), np.zeros((0,), np.float32)
                return dets[:, :4].astype(np.float32), dets[:, 4].astype(np.float32)
            # ONNX with embedded NMS: (1, N, 5) rows of xyxy + score.
            dets = outputs[0]
            keep = dets[:, 4] > self.score_thr
            return (dets[keep, :4] / ratio).astype(np.float32), dets[keep, 4].astype(np.float32)

    return ScoredYOLOX


def _box_rtmo():
    """RTMO subclass that also returns the person boxes (upstream drops them)."""
    from rtmlib import RTMO
    from rtmlib.tools.object_detection.post_processings import multiclass_nms

    class BoxRTMO(RTMO):
        def __call__(self, image, nms_thr=None, score_thr=None):
            nms_thr = self.nms_thr if nms_thr is None else nms_thr
            score_thr = self.score_thr if score_thr is None else score_thr
            processed, ratio = self.preprocess(image)
            det_outputs, pose_outputs = self.inference(processed)
            boxes = det_outputs[0, :, :4] / ratio
            box_scores = det_outputs[0, :, 4]
            keypoints = pose_outputs[0, :, :, :2] / ratio
            scores = pose_outputs[0, :, :, 2]
            dets, keep = multiclass_nms(boxes, box_scores[:, np.newaxis],
                                        nms_thr=nms_thr, score_thr=score_thr)
            if keep is None:
                return (np.zeros((0, 17, 2), np.float32), np.zeros((0, 17), np.float32),
                        np.zeros((0, 4), np.float32), np.zeros((0,), np.float32))
            return keypoints[keep], scores[keep], dets[:, :4], dets[:, 4]

    return BoxRTMO


class _Boxes:
    def __init__(self, xyxy, conf):
        self.xyxy = torch.as_tensor(xyxy, dtype=torch.float32)
        self.conf = torch.as_tensor(conf, dtype=torch.float32)


class _Keypoints:
    def __init__(self, data):
        self.data = torch.as_tensor(data, dtype=torch.float32)


class _Result:
    """Minimal Ultralytics-Results lookalike consumed by PoseTracker."""

    def __init__(self, boxes_xyxy, boxes_conf, keypoints):
        self.boxes = _Boxes(boxes_xyxy, boxes_conf)
        self.keypoints = _Keypoints(keypoints)
        self.speed = {}

    @classmethod
    def empty(cls):
        return cls(np.zeros((0, 4), np.float32), np.zeros((0,), np.float32),
                   np.zeros((0, 17, 3), np.float32))


def model_files(backend='rtmo', mode='balanced'):
    """按 components 顺序返回权重路径（末项为主模型），不加载模型。"""
    if backend not in ('rtmpose', 'rtmo'):
        raise ValueError(f"未知姿态后端 {backend}")
    from rtmlib import Body
    from rtmlib.tools.file import download_checkpoint
    spec = Body.RTMO_MODE[mode] if backend == 'rtmo' else Body.MODE[mode]
    if backend == 'rtmo':
        return [('pose', download_checkpoint(spec['pose']))]
    return [('det', download_checkpoint(spec['det'])), ('pose', download_checkpoint(spec['pose']))]


class RTMLibDetector:
    """Drop-in detector with the same predict() contract as the YOLO model."""

    def __init__(self, variant='rtmpose', mode='balanced', device='cuda'):
        if variant not in ('rtmpose', 'rtmo'):
            raise ValueError(f"未知姿态后端 {variant}")
        _enable_ort_cuda_dlls()
        from rtmlib import Body, RTMPose
        from rtmlib.tools.file import download_checkpoint

        self.variant, self.mode = variant, mode
        self._device = 'cpu'
        self._files = []
        if variant == 'rtmo':
            spec = Body.RTMO_MODE[mode]
            pose_path = download_checkpoint(spec['pose'])
            self._files = [('pose', pose_path)]
            self._det = None
            self._pose = self._build(_box_rtmo(), pose_path, spec['pose_input_size'], device)
        else:
            spec = Body.MODE[mode]
            det_path = download_checkpoint(spec['det'])
            pose_path = download_checkpoint(spec['pose'])
            self._files = [('det', det_path), ('pose', pose_path)]
            self._det = self._build(_scored_yolox(), det_path, spec['det_input_size'], device)
            self._pose = self._build(RTMPose, pose_path, spec['pose_input_size'], device)
        self._device = self._resolve_device()
        self._input = self._input_label(spec, variant)

    def _build(self, cls, path, size, device):
        try:
            return cls(path, model_input_size=size, backend='onnxruntime', device=device)
        except Exception:
            if device == 'cpu':
                raise
            return cls(path, model_input_size=size, backend='onnxruntime', device='cpu')

    def _resolve_device(self):
        models = [model for model in (self._det, self._pose) if model is not None]
        try:
            providers = [p for model in models for p in model.session.get_providers()]
        except AttributeError:
            return 'cuda'
        return 'cuda' if 'CUDAExecutionProvider' in providers else 'cpu'

    @staticmethod
    def _input_label(spec, variant):
        def wh(size):
            return f"{size[1]}x{size[0]}"
        if variant == 'rtmo':
            return f"单阶段 {wh(spec['pose_input_size'])}"
        return f"检测 {wh(spec['det_input_size'])} / 姿态 {wh(spec['pose_input_size'])}"

    @property
    def label(self):
        """短名称，例如 RTMO-m 或 YOLOX-m + RTMPose-m。"""
        labels = {role: _label(path) for role, path in self._files}
        return labels['pose'] if self.variant == 'rtmo' else f"{labels['pose']} + {labels['det']}"

    def predict(self, image, imgsz=None, conf=.35, iou=None, max_det=50, device=None, verbose=False):
        image = np.ascontiguousarray(image)
        started = time.perf_counter()
        if self.variant == 'rtmo':
            keypoints, kp_scores, boxes, box_scores = self._pose(image)
            keep = box_scores >= conf
            boxes, box_scores = boxes[keep], box_scores[keep]
            keypoints, kp_scores = keypoints[keep], kp_scores[keep]
            if len(boxes) > max_det:
                order = np.argsort(-box_scores)[:max_det]
                boxes, box_scores = boxes[order], box_scores[order]
                keypoints, kp_scores = keypoints[order], kp_scores[order]
        else:
            boxes, box_scores = self._det(image)
            if len(boxes):
                keep = box_scores >= conf
                boxes, box_scores = boxes[keep], box_scores[keep]
                if len(boxes) > max_det:
                    order = np.argsort(-box_scores)[:max_det]
                    boxes, box_scores = boxes[order], box_scores[order]
                keypoints, kp_scores = self._pose(image, bboxes=boxes.tolist())
            else:
                keypoints = np.zeros((0, 17, 2), np.float32)
                kp_scores = np.zeros((0, 17), np.float32)
        elapsed_ms = (time.perf_counter() - started) * 1000
        if len(boxes) == 0:
            result = _Result.empty()
        else:
            stacked = np.concatenate([keypoints.astype(np.float32),
                                      kp_scores[..., None].astype(np.float32)], axis=2)
            result = _Result(boxes, box_scores, stacked)
        result.speed = {'inference': elapsed_ms}
        return [result]

    def model_meta(self):
        files = [dict(role=role, name=Path(path).name, sha256=file_hash(path),
                      bytes=Path(path).stat().st_size) for role, path in self._files]
        if self.variant == 'rtmo':
            name = f"{self.label}（rtmlib 单阶段多人姿态）"
        else:
            name = f"{self.label}（rtmlib 两阶段）"
        device = (f"ONNX Runtime CUDA / {torch.cuda.get_device_name(0)}" if self._device == 'cuda'
                  else platform.processor() + " / CPU (ONNX Runtime)")
        return dict(name=name, sha256=files[-1]['sha256'], device=device, backend='rtmlib',
                    inputSize=self._input, components=files)
