"""Offline GPU report boundaries must not mix warmup or archive samples."""
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('browser_gpu_summary', Path(__file__).resolve().parents[1] / 'tools/summarize-browser-gpu.py')
summary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(summary)


def fixtures(tmp_path, rows, windows):
    gpu, window_file = tmp_path / 'gpu.jsonl', tmp_path / 'windows.json'
    gpu.write_text('\n'.join(json.dumps(row) for row in rows), encoding='utf-8')
    window_file.write_text(json.dumps({'windows': windows}), encoding='utf-8')
    return gpu, window_file


def test_windows_exclude_warmup_archive_and_queries_crossing_start(tmp_path):
    rows = [dict(sampledAt=at, queryStartedAt=began, devices=[dict(index=0, utilizationPercent=util)])
        for at, began, util in [(9.9, 9.8, 99), (10.1, 9.99, 99), (10.2, 10.1, 20),
                               (10.8, 10.7, 40), (11, 10.9, 99), (12, 11.9, 99)]]
    report = summary.summarize(*fixtures(tmp_path, rows, [dict(measurementStartedAtEpochMs=10000,
        measurementStoppedAtEpochMs=11000, targetFps=10)]))
    assert report['samplesInsideWindows'] == 2 and report['samplesOutsideWindows'] == 4
    readings = report['windows'][0]['devices']['0']
    assert readings['utilizationPercent']['mean'] == 30
    assert readings['startGapMs'] == 200 and readings['endGapMs'] == 200
    assert readings['intervalMs']['max'] == 600


def test_legacy_csv_formats_keep_utilization_and_memory_separate(tmp_path):
    rows = [dict(at=10.2, csv='0, 12, 3456, 16303', returnCode=0),
        dict(at=10.3, csv='2026/11/07 10:00:00.000, RTX fixture, 34, 4567, 16303')]
    report = summary.summarize(*fixtures(tmp_path, rows, [dict(measurementStartedAtEpochMs=10000,
        measurementStoppedAtEpochMs=11000)]))
    assert not report['parseFailures']
    devices = report['windows'][0]['devices']
    assert devices['0']['utilizationPercent']['mean'] == 12
    assert devices['0']['memoryUsedMiB']['mean'] == 3456
    assert devices['name:RTX fixture']['utilizationPercent']['mean'] == 34
    assert devices['name:RTX fixture']['memoryUsedMiB']['mean'] == 4567


def test_incomplete_window_is_rejected_and_bad_monitor_schema_is_reported(tmp_path):
    gpu, windows = fixtures(tmp_path, [dict(at=10.2, csv='do not guess these columns')],
        [dict(measurementStartedAtEpochMs=10000, measurementStoppedAtEpochMs=None)])
    with pytest.raises(ValueError, match='unfinished'):
        summary.summarize(gpu, windows)
    windows.write_text(json.dumps({'windows': [dict(measurementStartedAtEpochMs=10000,
        measurementStoppedAtEpochMs=11000)]}), encoding='utf-8')
    report = summary.summarize(gpu, windows)
    assert len(report['parseFailures']) == 1
    assert report['windows'][0]['hasSamples'] is False
