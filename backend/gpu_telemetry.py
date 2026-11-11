"""Cached device telemetry, sampled separately from the inference path."""
import csv
import math
import os
import subprocess
import threading
import time


def _number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except ValueError:
        return None


class GpuTelemetry:
    def __init__(self, ttl=1):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._cached = None
        self._expires = 0
        self._sampled_monotonic = None

    def _sample(self):
        import torch
        from algorithms.gpu import cuda_index

        index = cuda_index()
        # CUDA_VISIBLE_DEVICES can reorder devices. Match the configured CUDA
        # device by UUID rather than assuming nvidia-smi uses the same index.
        device_uuid = str(torch.cuda.get_device_properties(index).uuid)
        if not device_uuid.startswith('GPU-'):
            device_uuid = 'GPU-' + device_uuid
        result = subprocess.run([
            'nvidia-smi', '--id=' + device_uuid,
            '--query-gpu=name,utilization.gpu,memory.used,memory.total',
            '--format=csv,noheader,nounits',
        ], capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=2, check=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        rows = list(csv.reader(result.stdout.strip().splitlines(), skipinitialspace=True))
        if len(rows) != 1 or len(rows[0]) != 4:
            raise ValueError('Invalid GPU telemetry')
        name, utilization, used, total = rows[0]
        utilization, used, total = map(_number, (utilization, used, total))
        if utilization is not None and utilization > 100:
            utilization = None
        return dict(status='ready' if all(v is not None for v in (utilization, used, total)) else 'partial',
                    device=f'cuda:{index}', deviceUuid=device_uuid, name=name.strip(), utilizationPercent=utilization,
                    memoryUsedMiB=used, memoryTotalMiB=total, sampledAt=time.time(),
                    scope='device-all-processes')

    def snapshot(self):
        with self._lock:
            if self._cached is None or time.monotonic() >= self._expires:
                started = time.monotonic()
                try:
                    self._cached = self._sample()
                except (OSError, subprocess.SubprocessError, ValueError, RuntimeError, AttributeError, csv.Error):
                    # Never substitute old readings or zero for unavailable data.
                    self._cached = dict(status='unavailable', utilizationPercent=None,
                        memoryUsedMiB=None, memoryTotalMiB=None, sampledAt=None,
                        scope='device-all-processes', reason='暂时无法读取服务器 GPU 指标')
                sampled = time.monotonic()
                self._sampled_monotonic = sampled if self._cached.get('sampledAt') is not None else None
                # Match the caller's start-to-start polling cadence. Starting
                # TTL after a slow nvidia-smi call makes every second poll hit
                # the cache and silently halves the effective sample rate.
                self._expires = started + self.ttl
            # Cache age uses a monotonic clock: browser/server clocks need not
            # agree. This is driver kernel-active time, not a sum of OS engines
            # and not the percentage of VRAM allocated by this application.
            age = None if self._sampled_monotonic is None else max(0, (time.monotonic()-self._sampled_monotonic)*1000)
            return dict(self._cached, source='nvidia-smi', metric='gpu-kernel-active-time',
                        refreshIntervalMs=round(self.ttl*1000), sampleAgeMs=None if age is None else round(age))
