"""Independent CUDA streams for bounded realtime inference replicas."""
from functools import wraps
from concurrent.futures import ThreadPoolExecutor
import threading
import torch
from fastapi import HTTPException
from .gpu import require_cuda

_creation_lock=threading.Lock()


def run_in_lane(owner,function,ready=None):
    # CUDA/cuDNN caches are thread-local. A model pool on an unrestricted HTTP
    # thread pool repeatedly pays first-use costs, even after model warmup.
    with _creation_lock:
        if not hasattr(owner,'_cuda_executor'):
            owner._cuda_executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='station-cuda')
            owner._cuda_busy=threading.Lock()
    if not owner._cuda_busy.acquire(blocking=False):
        raise HTTPException(429,'GPU 推理槽位繁忙，请发送最新采样',headers={'Retry-After':'1'})
    def execute():
        if not hasattr(owner,'_cuda_stream'):
            device=require_cuda()
            owner._cuda_stream=torch.cuda.Stream(device=device)
            owner._cuda_stream.wait_stream(torch.cuda.default_stream(device))
        with torch.cuda.stream(owner._cuda_stream):
            if ready is not None:owner._cuda_stream.wait_event(ready)
            return function()
    try:return owner._cuda_executor.submit(execute).result()
    finally:owner._cuda_busy.release()


def cuda_request(function):
    @wraps(function)
    def run(self,*args,**kwargs):
        return run_in_lane(self,lambda:function(self,*args,**kwargs))
    return run
