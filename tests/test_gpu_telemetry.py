"""Hardware metric provenance, failure handling, caching and access control."""
from types import SimpleNamespace
import subprocess

import pytest
import torch

from backend.gpu_telemetry import GpuTelemetry
from test_station_terminals import register


def test_gpu_sample_matches_cuda_uuid_and_keeps_real_zero(monkeypatch):
    monkeypatch.setenv('INFERENCE_DEVICE', 'cuda:1')
    def properties(index):
        assert index == 1
        return SimpleNamespace(uuid='reordered-device')
    monkeypatch.setattr(torch.cuda, 'get_device_properties', properties)
    calls = []
    def run(command, **options):
        calls.append(command)
        assert '--id=GPU-reordered-device' in command
        assert options['timeout'] == 2 and options['check']
        return SimpleNamespace(stdout='NVIDIA Test, 0, 2048, 16384\n')
    monkeypatch.setattr(subprocess, 'run', run)
    monitor = GpuTelemetry()
    result = monitor.snapshot()
    assert result['device'] == 'cuda:1'
    assert result['utilizationPercent'] == 0
    assert result['memoryUsedMiB'] == 2048
    assert result['memoryTotalMiB'] == 16384
    assert result['scope'] == 'device-all-processes'
    assert result['status'] == 'ready'
    assert result['deviceUuid'] == 'GPU-reordered-device'
    assert result['source'] == 'nvidia-smi' and result['metric'] == 'gpu-kernel-active-time'
    assert result['refreshIntervalMs'] == 1000 and 0 <= result['sampleAgeMs'] < 1000
    assert monitor.snapshot()['sampledAt'] == result['sampledAt'] and len(calls) == 1
    result['status'] = 'modified-client-copy'
    assert monitor.snapshot()['status'] == 'ready'


@pytest.mark.parametrize('value', ['N/A', '[Not Supported]', 'nan', '-1', '101'])
def test_unavailable_utilization_does_not_become_zero(monkeypatch, value):
    monkeypatch.setattr(torch.cuda, 'get_device_properties', lambda index: SimpleNamespace(uuid='GPU-test'))
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs:
                        SimpleNamespace(stdout=f'NVIDIA Test, {value}, 2048, 16384\n'))
    result = GpuTelemetry().snapshot()
    assert result['status'] == 'partial'
    assert result['utilizationPercent'] is None
    assert result['memoryUsedMiB'] == 2048


def test_failed_refresh_discards_stale_metrics_and_is_cached(monkeypatch):
    clock = [10]
    monkeypatch.setattr('backend.gpu_telemetry.time.monotonic', lambda: clock[0])
    monitor = GpuTelemetry()
    calls = []
    def sample():
        calls.append(True)
        if len(calls) > 1:
            raise subprocess.TimeoutExpired('nvidia-smi', 2)
        return dict(status='ready', utilizationPercent=70, memoryUsedMiB=8192)
    monkeypatch.setattr(monitor, '_sample', sample)
    assert monitor.snapshot()['utilizationPercent'] == 70
    clock[0] += 3
    result = monitor.snapshot()
    assert result['status'] == 'unavailable'
    assert result['utilizationPercent'] is None and result['memoryUsedMiB'] is None
    assert monitor.snapshot() == result and len(calls) == 2


def test_cache_age_and_one_second_refresh_ignore_wall_clock_skew(monkeypatch):
    clock = [10.]
    monkeypatch.setattr('backend.gpu_telemetry.time.monotonic', lambda: clock[0])
    monitor = GpuTelemetry()
    calls = []
    def sample():
        calls.append(1)
        # Deliberately old wall time; only monotonic cache age controls freshness.
        return dict(status='ready', sampledAt=1, utilizationPercent=len(calls))
    monkeypatch.setattr(monitor, '_sample', sample)
    assert monitor.snapshot()['sampleAgeMs'] == 0
    clock[0] += .6
    cached = monitor.snapshot()
    assert cached['sampleAgeMs'] == 600 and cached['utilizationPercent'] == 1
    clock[0] += .5
    fresh = monitor.snapshot()
    assert fresh['sampleAgeMs'] == 0 and fresh['utilizationPercent'] == 2


def test_sample_cost_does_not_halve_one_second_refresh_rate(monkeypatch):
    clock = [10.]
    monkeypatch.setattr('backend.gpu_telemetry.time.monotonic', lambda: clock[0])
    monitor = GpuTelemetry()
    calls = []
    def sample():
        calls.append(1)
        clock[0] += .08
        return dict(status='ready', sampledAt=1, utilizationPercent=len(calls))
    monkeypatch.setattr(monitor, '_sample', sample)
    readings = []
    for second in range(5):
        clock[0] = 10. + second
        readings.append(monitor.snapshot()['utilizationPercent'])
    assert readings == [1, 2, 3, 4, 5]


def test_gpu_metrics_require_login(context, monkeypatch):
    _, app, client = context
    monkeypatch.setattr(app.state.gpu_telemetry, 'snapshot', lambda: pytest.fail('unauthorized GPU query'))
    assert client.get('/api/v1/system/gpu').status_code == 401


def test_gpu_metrics_available_to_admin_and_registered_terminal(authenticated, monkeypatch):
    _, app, client = authenticated
    snapshot = dict(status='ready', utilizationPercent=42, memoryUsedMiB=8192, memoryTotalMiB=16384)
    monkeypatch.setattr(app.state.gpu_telemetry, 'snapshot', lambda: snapshot)
    response = client.get('/api/v1/system/gpu')
    assert response.status_code == 200 and response.json() == snapshot
    assert response.headers['Cache-Control'] == 'no-store'
    binding = register(client)
    client.cookies.clear()
    response = client.get('/api/v1/system/gpu', headers={'X-Terminal-Key': binding['credential']})
    assert response.status_code == 200 and response.json() == snapshot
    assert client.get('/api/v1/system/gpu', headers={'X-Terminal-Key': 'invalid'}).status_code == 401


def test_dashboard_serves_versioned_gpu_metrics_script(context):
    _, _, client = context
    page = client.get('/').text
    assert 'id="metric-gpu"' in page
    assert 'id="metric-gpu-memory"' not in page
    assert 'src="gpu-metrics.js?v=' in page
    assert client.get('/gpu-metrics.js').status_code == 200
