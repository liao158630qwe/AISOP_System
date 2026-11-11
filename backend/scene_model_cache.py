"""One fixed worker and CUDA stream for each exclusively leased scene model."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path

from algorithms.gpu import cuda_index, require_cuda


def model_key(frozen, owner):
    """Keep owner, asset identity, task and device boundaries in the cache key."""
    model = frozen['model']
    return (owner, model['id'], model['sha256'],
            os.path.normcase(str(Path(frozen['model_path']).resolve())),
            model['task'], cuda_index(frozen['config']['device']))


@dataclass(eq=False)
class ModelEntry:
    key: tuple
    model: object = None
    busy: bool = True
    loading: bool = True
    cuda_stream: object = None
    executor: ThreadPoolExecutor = field(default_factory=lambda: ThreadPoolExecutor(
        max_workers=1, thread_name_prefix='scene-model'))

    def close(self):
        first_error = None
        cleared = False
        def attempt(function):
            nonlocal first_error
            try:
                function()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
                logging.getLogger(__name__).exception('Scene model worker cleanup failed')
        def clear():
            nonlocal cleared
            try:
                if self.cuda_stream is not None:
                    self.cuda_stream.synchronize()
            finally:
                self.model = None
                self.cuda_stream = None
                cleared = True
        attempt(lambda: self.executor.submit(clear).result())
        attempt(lambda: self.executor.shutdown(wait=True))
        if not cleared:
            attempt(clear)
        if first_error is not None:
            raise first_error


def run_model_lane(entry, function):
    """Load and infer on the same thread; never queue a second model lease."""
    def execute():
        import torch
        device = require_cuda(entry.key[-1])
        with torch.cuda.device(device):
            if entry.cuda_stream is None:
                entry.cuda_stream = torch.cuda.Stream(device=device)
                entry.cuda_stream.wait_stream(torch.cuda.default_stream(device))
            with torch.cuda.stream(entry.cuda_stream):
                try:
                    result = function()
                except BaseException:
                    try:
                        entry.cuda_stream.synchronize()
                    except BaseException:
                        logging.getLogger(__name__).exception('Scene lane cleanup failed after inference error')
                    raise
                else:
                    # Finish this lane before its lease/model can be released.
                    # This does not introduce a device-wide synchronization.
                    entry.cuda_stream.synchronize()
                    return result
    return entry.executor.submit(execute).result()
