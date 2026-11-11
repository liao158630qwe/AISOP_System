"""Read existing station reports and summarize GPU samples inside their measurement window.

This tool starts no service and executes no inference or GPU telemetry commands.
Device utilization covers all processes; it cannot attribute GPU busy time to AISOP.
"""
import argparse
import csv
import json
import math
from pathlib import Path
import statistics


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def percentile(values, fraction=.95):
    values = sorted(values)
    return round(values[max(0, math.ceil(len(values)*fraction)-1)], 3) if values else None


def gpu_summary(path, started, finished):
    result = dict(scope='device-all-processes', meanMethod='arithmetic mean of valid in-window samples',
        percentileMethod='nearest-rank', measurementStartedAt=started, measurementFinishedAt=finished,
        devices=[], ignoredOutsideWindow=0, malformedSamples=0, failedSamples=0)
    if started is None or finished is None or finished <= started:
        result.update(status='unavailable', reason='No valid completed measurement window')
        return result
    if not path.is_file():
        result.update(status='unavailable', reason='gpu.jsonl not found')
        return result
    devices = {}
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            result['malformedSamples'] += 1
            continue
        at = number(record.get('at')) if isinstance(record, dict) else None
        if at is None:
            result['malformedSamples'] += 1
            continue
        if not started <= at < finished:
            result['ignoredOutsideWindow'] += 1
            continue
        if record.get('returnCode') != 0:
            result['failedSamples'] += 1
            continue
        raw = record.get('csv')
        if not isinstance(raw, str) or not raw.strip():
            result['malformedSamples'] += 1
            continue
        for row in csv.reader(raw.splitlines(), skipinitialspace=True):
            if len(row) != 4 or not row[0].strip().isdigit():
                result['malformedSamples'] += 1
                continue
            index = int(row[0])
            utilization, used, total = map(number, row[1:])
            utilization = utilization if utilization is not None and 0 <= utilization <= 100 else None
            used = used if used is not None and used >= 0 else None
            total = total if total is not None and total > 0 else None
            # One observation per device/query timestamp, including partial readings.
            devices.setdefault(index, {})[at] = (utilization, used, total)
    for index, samples in sorted(devices.items()):
        timestamps = sorted(samples)
        utilization = [sample[0] for sample in samples.values() if sample[0] is not None]
        used = [sample[1] for sample in samples.values() if sample[1] is not None]
        total = [sample[2] for sample in samples.values() if sample[2] is not None]
        endpoints = [started, *timestamps, finished]
        result['devices'].append(dict(index=index, samples=len(samples), validUtilizationSamples=len(utilization),
            meanUtilizationPercent=round(statistics.fmean(utilization), 3) if utilization else None,
            p95UtilizationPercent=percentile(utilization),
            maxUtilizationPercent=max(utilization) if utilization else None,
            peakMemoryUsedMiB=max(used) if used else None,
            memoryTotalMiB=max(total) if total else None,
            firstSampleAt=timestamps[0], lastSampleAt=timestamps[-1],
            maxUnsampledGapSeconds=round(max(b-a for a, b in zip(endpoints, endpoints[1:])), 3)))
    result['status'] = 'ready' if any(row['validUtilizationSamples'] for row in result['devices']) else 'unavailable'
    if result['status'] == 'unavailable':
        result['reason'] = 'No valid GPU utilization samples in the measurement window'
    return result


def summarize_run(path):
    directory = path if path.is_dir() else path.parent
    report_path = directory / 'report.json' if path.is_dir() else path
    summary = dict(directory=str(directory.resolve()), report=str(report_path.resolve()),
        notes=['GPU utilization is for the entire device, including other processes; it is not AISOP-exclusive utilization.',
               'GPU statistics exclude warmup and drain samples using report timestamps; polling can miss short bursts.',
               'Effective FPS counts responses completed inside the requested window.',
               'Latency, queue and stage P95 use all successful requests, including responses drained after the window, unless the report says otherwise.',
               'Stage percentiles are nested measurements and cannot be added to reconstruct total P95.',
               'Repeated JPEG HTTP/model throughput excludes cameras, network deployment, capture/encoding and display.'])
    if not report_path.is_file():
        summary.update(status='missing-report', measurementCompleted=False, stations=[],
            gpu=gpu_summary(directory/'gpu.jsonl', None, None))
        return summary
    report = json.loads(report_path.read_text(encoding='utf-8-sig'))
    settings_path = directory / 'settings.json'
    settings = json.loads(settings_path.read_text(encoding='utf-8-sig')) if settings_path.is_file() else {}
    safe_settings = ('profile', 'fps', 'duration', 'concurrency', 'posePool', 'specialistPool',
        'sceneConcurrency', 'sceneModelCacheSize', 'opencvThreads', 'torchThreads', 'queueWaitMs',
        'tasks', 'sourceRoot', 'arrivalMode')
    summary['settings'] = {key: settings[key] for key in safe_settings if key in settings}
    if report.get('status') == 'failed' or report.get('measurementCompleted') is False:
        summary.update(status='failed', measurementCompleted=False, stations=[],
            failurePhase=report.get('failurePhase'), failureType=report.get('failureType'),
            httpStatus=report.get('httpStatus'), gpu=gpu_summary(directory/'gpu.jsonl', None, None))
        return summary
    started, finished = number(report.get('measurementStartedAt')), number(report.get('measurementFinishedAt'))
    columns = ('stationId', 'analysisType', 'effectiveFps', 'completedWithinWindow', 'submitted', 'completed',
        'candidates', 'skippedBusy', 'skippedLate', 'p95Ms', 'p99Ms', 'queueP95Ms', 'stagesP95Ms',
        'longestNoResultMs', 'errors', 'targetMet')
    rows = [{key: row.get(key) for key in columns} for row in report.get('stations', [])]
    summary.update(status='complete', measurementCompleted=True,
        targetFps=report.get('targetFps'), seconds=report.get('seconds'), deadlineMs=report.get('deadlineMs'),
        arrivalMode=report.get('arrivalMode', 'fixed'), totalEffectiveFps=report.get('totalEffectiveFps'),
        allTargetsMet=report.get('allTargetsMet'), warmup=report.get('warmup', []), stations=rows,
        gpu=gpu_summary(directory/'gpu.jsonl', started, finished))
    return summary


def markdown(summaries):
    lines = ['Station benchmark summary', '',
        'GPU busy percentages cover all processes on the device. Warmup and drain GPU samples are excluded.', '',
        'Effective FPS uses completed responses inside the measurement window. Response P95 includes drain responses.', '',
        'These runs repeat JPEG inputs and exclude camera acquisition, capture/encoding and display.', '']
    def value(item):
        return 'unavailable' if item is None else str(item)
    for run in summaries:
        lines.extend([f'Run: {run["directory"]}', '', f'Status: {run["status"]}', ''])
        if run.get('measurementCompleted'):
            lines.extend([f'Target: {value(run.get("targetFps"))} FPS/station; total effective: {value(run.get("totalEffectiveFps"))} FPS', '',
                '| Station | Task | Effective FPS | Response P95 ms | Queue P95 ms | Errors | Target met |',
                '| --- | --- | ---: | ---: | ---: | --- | --- |'])
            for row in run['stations']:
                values = [row.get('stationId'), row.get('analysisType'), row.get('effectiveFps'), row.get('p95Ms'),
                    row.get('queueP95Ms'), json.dumps(row.get('errors') or {}, ensure_ascii=False), row.get('targetMet')]
                lines.append('| ' + ' | '.join(value(item).replace('|', '\\|') for item in values) + ' |')
            lines.append('')
            for row in run['stations']:
                stages = row.get('stagesP95Ms') or {}
                lines.append(f'{row.get("stationId")} stage P95 ms: ' + ', '.join(f'{key}={value(v)}' for key, v in stages.items()))
            lines.append('')
        else:
            lines.extend([f'No throughput conclusion. Failure phase: {value(run.get("failurePhase"))}.', ''])
        gpu = run['gpu']
        lines.extend(['| GPU index | Samples | Mean busy % | P95 busy % | Max sample gap s |',
                      '| --- | ---: | ---: | ---: | ---: |'])
        for device in gpu['devices']:
            lines.append('| ' + ' | '.join(value(device[key]) for key in
                ('index', 'validUtilizationSamples', 'meanUtilizationPercent', 'p95UtilizationPercent', 'maxUnsampledGapSeconds')) + ' |')
        if not gpu['devices']:
            lines.append('| unavailable | 0 | unavailable | unavailable | unavailable |')
        lines.append('')
    return '\n'.join(lines) + '\n'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runs', nargs='+', type=Path, help='Benchmark directories or report.json files')
    parser.add_argument('--output', type=Path, help='Write JSON summary')
    parser.add_argument('--markdown', type=Path, help='Write readable Markdown summary')
    args = parser.parse_args()
    summaries = [summarize_run(path) for path in args.runs]
    result = json.dumps(dict(runs=summaries), ensure_ascii=False, indent=2, allow_nan=False)
    for destination, content in ((args.output, result), (args.markdown, markdown(summaries))):
        if destination is not None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding='utf-8')
    print(result)
