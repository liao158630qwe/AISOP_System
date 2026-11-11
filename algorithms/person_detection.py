"""Person pose inference shared by standalone and screw inspection tasks."""
from pathlib import Path

from .video import AnalysisError, file_hash


def snapshot(model_path, backend='yolo'):
    if backend == 'yolo':
        files = [('pose', model_path)]
    else:
        from .rtmlib_pose import model_files
        files = model_files(backend)
    assets = [{'role': role, 'path': str(Path(path).resolve()), 'sha256': file_hash(path)}
              for role, path in files]
    return {'backend': backend, 'assets': assets, 'sha256': assets[-1]['sha256']}


def verify(frozen):
    for asset in frozen['assets']:
        path = Path(asset['path'])
        if not path.is_file() or file_hash(path) != asset['sha256']:
            raise AnalysisError('MODEL_CHANGED', '任务创建后人体姿态模型改变或缺失，请重新创建任务')


def load_detector(model_path, backend='yolo'):
    from .gpu import require_cuda
    require_cuda()
    if backend == 'yolo':
        from ultralytics import YOLO
        return YOLO(str(model_path))
    from .rtmlib_pose import RTMLibDetector
    return RTMLibDetector(variant=backend, mode='balanced')


def predict_people(detector, frame, tracker, timestamp):
    from .gpu import require_cuda
    device = require_cuda(getattr(detector, '_device', None))
    from .cuda_yolo import predict
    if getattr(detector, 'task', None) in ('pose', 'detect'):
        result = predict(detector, frame, classes=[0], imgsz=640, conf=.35, iou=.5,
                         max_det=50, device=device, verbose=False)[0]
    else:
        # Optional RTMLib's API consumes NumPy images; its model stays CUDA.
        from .cuda_vision import CudaFrame
        if isinstance(frame, CudaFrame):
            frame = frame.rgb.flip(0).permute(1,2,0).cpu().numpy()
        result = detector.predict(frame, classes=[0], imgsz=640, conf=.35, iou=.5,
                                  max_det=50, device=device, verbose=False)[0]
    height, width = frame.shape[:2]
    return tracker.update(result, width, height, timestamp)
