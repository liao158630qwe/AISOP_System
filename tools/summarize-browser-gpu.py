"""Summarize existing GPU JSONL only inside completed browser measurement windows.

This is an offline reader; it never queries a GPU, starts a model, or changes data.
Accepted monitor formats: sampledAt/devices (scene profiler), at/csv with four
columns index/utilization/memory-used/memory-total (station benchmark), and the
five-column timestamp/name/utilization/memory-used/memory-total video monitor.
"""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import statistics


FIELDS = ('utilizationPercent', 'memoryUsedMiB', 'memoryTotalMiB', 'powerWatts', 'smClockMHz')


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def stats(values):
    values = sorted(value for value in values if finite(value))
    if not values:
        return dict(count=0, min=None, mean=None, median=None, p95=None, max=None)
    return dict(count=len(values), min=round(values[0], 3), mean=round(statistics.mean(values), 3),
        median=round(statistics.median(values), 3), p95=round(values[math.ceil(.95*len(values))-1], 3),
        max=round(values[-1], 3))


def read_samples(path):
    samples, failures = [], []
    for line_number, text in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
        if not text.strip():
            continue
        try:
            row = json.loads(text)
            at = row.get('atEpochMs')
            if at is None:
                at = number(row.get('sampledAt', row.get('at')))
                at = at*1000 if at is not None else None
            if not finite(at):
                raise ValueError('Missing finite epoch timestamp')
            if row.get('returnCode', row.get('raw', {}).get('returncode', 0)) != 0:
                raise ValueError('Monitor query failed')
            devices = row.get('devices')
            if devices is None:
                devices = []
                for values in csv.reader(row.get('csv', '').splitlines()):
                    values = [value.strip() for value in values]
                    if len(values) == 4 and number(values[0]) is not None:
                        devices.append(dict(index=int(values[0]), utilizationPercent=number(values[1]),
                            memoryUsedMiB=number(values[2]), memoryTotalMiB=number(values[3])))
                    elif len(values) == 5 and '/' in values[0]:
                        devices.append(dict(index=None, name=values[1], utilizationPercent=number(values[2]),
                            memoryUsedMiB=number(values[3]), memoryTotalMiB=number(values[4])))
                    else:
                        raise ValueError('Unknown CSV schema; do not guess utilization columns')
            if not devices:
                raise ValueError('No GPU devices in sample')
            began = number(row.get('queryStartedAt'))
            for device in devices:
                samples.append(dict(atEpochMs=at, queryStartedAtEpochMs=began*1000 if began is not None else None,
                    device=device))
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            failures.append(dict(line=line_number, reason=str(error)))
    return samples, failures


def summarize(gpu, windows):
    document = json.loads(windows.read_text(encoding='utf-8-sig'))
    intervals = document.get('windows', document.get('rounds'))
    if not isinstance(intervals, list) or not intervals:
        raise ValueError('A nonempty windows/rounds array is required')
    samples, failures = read_samples(gpu)
    output = []
    used = set()
    for interval in intervals:
        start, stop = interval.get('measurementStartedAtEpochMs'), interval.get('measurementStoppedAtEpochMs')
        if not finite(start) or not finite(stop) or stop <= start:
            raise ValueError('Every measurement window must have finite start < stop; unfinished runs cannot be summarized')
        selected = [(index, sample) for index, sample in enumerate(samples)
            if start <= sample['atEpochMs'] < stop and
            (sample['queryStartedAtEpochMs'] is None or sample['queryStartedAtEpochMs'] >= start)]
        used.update(index for index, _ in selected)
        devices = {}
        for _, sample in selected:
            device = sample['device']
            identity = str(device['index']) if device.get('index') is not None else 'name:' + str(device.get('name', 'unknown'))
            devices.setdefault(identity, []).append(sample)
        summaries = {}
        for identity, readings in devices.items():
            readings.sort(key=lambda sample: sample['atEpochMs'])
            times = [sample['atEpochMs'] for sample in readings]
            summaries[identity] = dict(index=readings[0]['device'].get('index'), name=readings[0]['device'].get('name'),
                samples=len(readings), firstAtEpochMs=times[0], lastAtEpochMs=times[-1],
                startGapMs=round(times[0]-start, 3), endGapMs=round(stop-times[-1], 3),
                intervalMs=stats([after-before for before, after in zip(times, times[1:])]),
                **{field: stats([sample['device'].get(field) for sample in readings]) for field in FIELDS})
        output.append(dict(targetFps=interval.get('targetFps'), streamId=interval.get('streamId', interval.get('identity', {}).get('streamId')),
            measurementStartedAtEpochMs=start, measurementStoppedAtEpochMs=stop, durationSeconds=(stop-start)/1000,
            hasSamples=bool(selected), devices=summaries))
    return dict(gpuFile=str(gpu.resolve()), windowsFile=str(windows.resolve()),
        gpuSha256=hashlib.sha256(gpu.read_bytes()).hexdigest(), windowsSha256=hashlib.sha256(windows.read_bytes()).hexdigest(),
        scope='device-all-processes',
        method='Use start <= sample timestamp < stop; if queryStartedAt exists, it must also be >= start. Means are arithmetic sample means, not time-weighted. NVIDIA utilization has its own sampling interval.',
        limitations='These are whole-device readings including unrelated processes, not inference-process utilization. Empty or sparse windows do not establish sustained utilization. Browser and monitor clocks must refer to the same host epoch.',
        sourceRoot=document.get('sourceRoot'), samplesParsed=len(samples), samplesInsideWindows=len(used),
        samplesOutsideWindows=len(samples)-len(used), parseFailures=failures, windows=output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gpu', required=True, type=Path, help='Existing monitor JSONL')
    parser.add_argument('--windows', required=True, type=Path, help='throughput-measurement-windows.json or station-throughput-ui.json')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if args.output.resolve() in (args.gpu.resolve(), args.windows.resolve()):
        parser.error('Output must differ from both input files')
    try:
        report = summarize(args.gpu, args.windows)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(dict(output=str(args.output.resolve()), windows=len(report['windows']),
        samplesInsideWindows=report['samplesInsideWindows'], parseFailures=len(report['parseFailures']))))
    return 0 if all(window['hasSamples'] for window in report['windows']) and not report['parseFailures'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
