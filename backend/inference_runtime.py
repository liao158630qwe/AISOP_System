"""Process-wide CPU budget, configured before any inference threads start."""
import importlib
import logging
import os
import threading

log = logging.getLogger(__name__)
_lock = threading.Lock()
_configuration = None
_torch_configuration = None


def configure_torch_runtime(threads):
    """Bound CPU tensor work before loading GPU models on many-core hosts."""
    global _torch_configuration
    if type(threads) is not int or not 1 <= threads <= 16:
        raise ValueError('TORCH_THREADS must be an integer between 1 and 16')
    with _lock:
        if _torch_configuration is not None:
            requested, effective, interop = _torch_configuration
            if requested != threads:
                raise RuntimeError('TORCH_THREADS is process-wide; restart the service to change it')
            return dict(torchThreads=effective, torchInteropThreads=interop)
        torch = importlib.import_module('torch')
        effective = min(threads, os.cpu_count() or 1)
        # Inter-op can only be configured before parallel work begins. Do not
        # silently claim a budget that was not applied in the running process.
        if torch.get_num_interop_threads() != 1:
            torch.set_num_interop_threads(1)
        torch.set_num_threads(effective)
        _torch_configuration = (threads, torch.get_num_threads(), torch.get_num_interop_threads())
        log.info('Inference Torch threads: intra-op=%s, inter-op=%s', *_torch_configuration[1:])
        return dict(torchThreads=_torch_configuration[1], torchInteropThreads=_torch_configuration[2])


def configure_inference_runtime(opencv_threads):
    """Initialize dependencies first: Ultralytics disables OpenCV threading.

    OpenCV's setting is process-wide and must not be changed from frame handlers.
    Repeated app lifespans may reuse the same budget, but cannot change it while
    another app in this process could be processing a frame. No weights load here.
    """
    global _configuration
    if not isinstance(opencv_threads, int) or isinstance(opencv_threads, bool) or not 1 <= opencv_threads <= 16:
        raise ValueError("OPENCV_THREADS must be an integer between 1 and 16")
    with _lock:
        if _configuration is not None:
            requested, effective = _configuration
            if requested != opencv_threads:
                raise RuntimeError("OPENCV_THREADS is process-wide; restart the service to change it")
            return effective
        cv2 = importlib.import_module('cv2')
        try:
            importlib.import_module('ultralytics.utils')
        except ImportError as exc:
            # Preserve service startup for installations using an optional backend.
            # Missing YOLO dependencies are still reported by the model loader.
            log.warning("Ultralytics initialization unavailable: %s", exc)
        effective = min(opencv_threads, os.cpu_count() or 1)
        cv2.setNumThreads(effective)
        _configuration = (opencv_threads, cv2.getNumThreads())
        log.info("Inference OpenCV threads: %s (requested %s)", _configuration[1], opencv_threads)
        return _configuration[1]
