"""Server device contract: model execution requires CUDA, with no CPU fallback."""
import os

from .video import AnalysisError


def cuda_index(requested=None):
    """Normalize a single visible CUDA device; reject CPU/MPS/automatic fallback."""
    value = os.getenv('INFERENCE_DEVICE', 'cuda:0') if requested in (None, 'auto', 'cuda') else requested
    value = str(value).strip().lower()
    if value.startswith('cuda:'):
        value = value[5:]
    if not value.isdecimal():
        raise ValueError('模型推理与训练仅允许 CUDA GPU，禁止 CPU 回退')
    return int(value)


def require_cuda(requested=None):
    try:
        index = cuda_index(requested)
    except ValueError as exc:
        raise AnalysisError('GPU_REQUIRED', str(exc)) from exc
    import torch
    if not torch.cuda.is_available() or index >= torch.cuda.device_count():
        raise AnalysisError('GPU_UNAVAILABLE', f'CUDA GPU {index} 不可用；请检查驱动、CUDA 与容器 GPU 挂载，禁止回退 CPU')
    return index


def check_cuda():
    """Fail service startup if the configured device cannot execute a kernel."""
    import torch
    index = require_cuda()
    try:
        with torch.cuda.device(index):
            probe = torch.ones(1, device=f'cuda:{index}') + 1
            torch.cuda.synchronize(index)
            del probe
    except Exception as exc:
        raise AnalysisError('GPU_UNAVAILABLE', f'CUDA GPU {index} 初始化失败；禁止回退 CPU: {exc}') from exc
    return dict(policy='cuda-only', device=f'cuda:{index}', name=torch.cuda.get_device_name(index), cpuFallback=False,
                stationVision={'backend':'cuda-disk-v1','tasks':['multi-view-screw','sticker-action','yolo-person'],
                    'stages':['jpeg-decode','resize','feature-extraction','feature-matching','homography',
                              'roi-warp','template-correlation','action-features','model-inference'],
                    'controlPlane':'cpu','legacyOptionalImagePaths':'opencv'})


def training_device(requested=None):
    return require_cuda(requested if requested is not None else os.getenv('TRAINING_DEVICE', 'cuda:0'))
