"""Station HTTP/model benchmark with fixed arrivals or serial request cadence.

Uses real inference and pre-encoded JPEGs. Does not measure camera acquisition,
browser capture/JPEG encoding, video decoding, or UI. Serial mode models only
the frontend's one-in-flight request cadence, not full browser performance.
Credentials: AISOP_TERMINAL_KEY_1 through AISOP_TERMINAL_KEY_6. Stop existing
collectors before running. No production cycle is created or automatically closed.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
import uuid

spec = importlib.util.spec_from_file_location('station_collector', Path(__file__).with_name('station-collector.py'))
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


def percentile(values, fraction):
    values = sorted(values)
    return round(values[max(0, math.ceil(len(values)*fraction)-1)], 2) if values else None


def run(args):
    import cv2
    arrival_mode = getattr(args, 'arrival_mode', 'fixed')
    if arrival_mode not in ('fixed', 'serial'):
        raise ValueError('Arrival mode must be fixed or serial')
    stations = []
    clients = []
    warmup = []
    phase = 'setup'
    def write_failure(exc):
        failure = dict(status='failed', failurePhase=phase, failureType=type(exc).__name__,
            httpStatus=getattr(exc, 'status', None), measurementCompleted=False,
            targetFps=args.fps, seconds=args.duration, arrivalMode=arrival_mode,
            warmup=warmup, stations=[], allTargetsMet=False,
            source='Benchmark did not complete; no throughput or GPU capacity conclusion')
        destination = Path(args.report)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(failure, ensure_ascii=False, indent=2), encoding='utf-8')
    def start(station):
        # A preceding station's cold model load can exceed the server's idle
        # keep-alive timeout. New streams start with a fresh connection.
        if hasattr(station['client'], 'close'):
            station['client'].close()
        station['stream'] = station['client'].request('/station-streams', 'POST', {'requestId':str(uuid.uuid4())})
    def send(station, seq, stamp):
        stream, client = station['stream'], station['client']
        return client.request('/station-streams/'+stream['streamId']+'/frames', 'POST', station['jpeg'], {
            'Content-Type':'image/jpeg', 'X-Frame-Seq':str(seq), 'X-Frame-Time':str(stamp),
            'X-Stream-Epoch':str(stream['streamEpoch']), 'X-Station-Id':stream['stationId'],
            'X-Camera-Id':stream['cameraId']})
    def close(station, wait_seconds=0):
        stream = station.get('stream')
        if stream:
            until = time.perf_counter()+wait_seconds
            while True:
                try:
                    station['client'].request('/station-streams/'+stream['streamId'], 'DELETE')
                    break
                except collector.ApiError as exc:
                    if exc.status != 429 or time.perf_counter() >= until:
                        raise
                    # Retry only a rejected, idempotent release. A timed-out
                    # frame POST is never replayed; its server work must drain.
                    time.sleep(min(.25, max(0, until-time.perf_counter())))
            station['stream'] = None
    try:
        for n, path in enumerate(args.images, 1):
            key = os.getenv('AISOP_TERMINAL_KEY_'+str(n), '')
            if not key:
                raise ValueError(f'Missing AISOP_TERMINAL_KEY_{n}')
            client = collector.Client(args.server, key)
            clients.append(client)
            binding = client.request('/terminal/context')['binding']
            if binding['stationId'] != f'station-{n}' or binding['analysisType'] == 'manual':
                raise ValueError(f'Station {n}: key must match this station and a configured AI task')
            if args.fps > binding['sampleFps']:
                raise ValueError(f'Station {n}: requested FPS exceeds binding sampleFps')
            frame = cv2.imread(str(path))
            if frame is None:
                raise ValueError(f'Station {n}: unreadable input image')
            width, height = (1280,720) if binding['analysisType']=='pose' else (1920,1080)
            size = collector.sample_size(frame.shape[1], frame.shape[0], width, height,
                                         allow_portrait=binding['analysisType'] in ('sticker', 'scene'))
            if size != (frame.shape[1], frame.shape[0]):
                frame = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
            ok, encoded = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY,82])
            if not ok or encoded.size > 1024*1024:
                raise ValueError(f'Station {n}: JPEG exceeds 1 MiB')
            station = dict(client=client, binding=binding, jpeg=encoded.tobytes(), stream=None,
                submitted=0, skippedBusy=0, skippedLate=0, records=[], future=None,
                input=dict(path=str(path.resolve()), sourceSha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    encodedSha256=hashlib.sha256(encoded.tobytes()).hexdigest(),
                    encodedBytes=int(encoded.size), width=int(frame.shape[1]),height=int(frame.shape[0]),jpegQuality=82))
            stations.append(station)
        # Cold load/preparation is measured separately and excluded from the arrival window.
        phase = 'cold-warmup'
        for station in stations:
            start(station)
            began = time.perf_counter()
            station['warmupOrigin'] = began
            send(station, 0, 0.)
            warmup.append(dict(stationId=station['binding']['stationId'], milliseconds=round((time.perf_counter()-began)*1000,2)))
        # Exercise replicas concurrently before the measured window. Sequential
        # preparation alone only warms one slot of each specialist pool.
        def prepare_replica(station, seq):
            try:
                send(station, seq, time.perf_counter()-station['warmupOrigin'])
                return 'OK'
            except collector.ApiError as exc:
                return str(exc.status)
        parallel_warmup = []
        phase = 'parallel-warmup'
        with ThreadPoolExecutor(max_workers=6) as pool:
            for seq in range(1, 4):
                parallel_warmup.append(list(pool.map(lambda s: prepare_replica(s, seq), stations)))
        phase = 'warmup-stream-release'
        for station in stations:
            close(station, wait_seconds=30)
        phase = 'measurement-stream-setup'
        for station in stations:
            start(station)
        measurement_started_at = time.time()
        phase = 'measurement'
        began = time.perf_counter()
        def sample(station, seq, scheduled):
            sent = time.perf_counter()
            try:
                result = send(station, seq, sent-began)
                identity = station['stream']
                if (any(result.get(k) != identity.get(k) for k in ('stationId','cameraId','streamId','streamEpoch')) or
                        result.get('frameSeq') != seq):
                    return dict(status='IDENTITY_MISMATCH', at=time.perf_counter()-began)
                completed_at = time.perf_counter()
                return dict(status='OK', at=completed_at-began,
                    latencyMs=(completed_at-scheduled)*1000, httpMs=(completed_at-sent)*1000,
                    queueMs=result['queueWaitMs'], serverMs=result['serverProcessingMs'],
                    stagesMs={**result.get('timingsMs', {}),
                        **{'service.'+name: value for name, value in result.get('serviceTimingsMs', {}).items()},
                        **{'inspection.'+name: value for name, value in (result.get('inspection') or {}).get('timingsMs', {}).items()}})
            except collector.ApiError as exc:
                return dict(status=str(exc.status), at=time.perf_counter()-began)
        count = math.ceil(args.fps*args.duration) if arrival_mode == 'fixed' else None
        if arrival_mode == 'fixed':
            with ThreadPoolExecutor(max_workers=6) as pool:
                for seq in range(count):
                    scheduled = began+seq/args.fps
                    time.sleep(max(0, scheduled-time.perf_counter()))
                    for station in stations:
                        # Candidate arrivals stay fixed even when an earlier request is slow.
                        if time.perf_counter()-scheduled > 1/args.fps:
                            station['skippedLate'] += 1
                        elif station['future'] and not station['future'].done():
                            station['skippedBusy'] += 1
                        else:
                            if station['future']:
                                station['records'].append(station['future'].result())
                            station['submitted'] += 1
                            station['future'] = pool.submit(sample, station, seq, scheduled)
                for station in stations:
                    if station['future']:
                        station['records'].append(station['future'].result())
        else:
            def serial_station(station):
                # Each station owns one worker. Start the next request only after
                # this response and any remainder of the target interval.
                seq, interval, until = 0, 1/args.fps, began+args.duration
                while True:
                    scheduled = time.perf_counter()
                    if scheduled >= until:
                        return
                    station['submitted'] += 1
                    station['records'].append(sample(station, seq, scheduled))
                    seq += 1
                    now = time.perf_counter()
                    remaining = min(interval-(now-scheduled), until-now)
                    if remaining > 0:
                        time.sleep(remaining)
            with ThreadPoolExecutor(max_workers=len(stations)) as pool:
                list(pool.map(serial_station, stations))
        rows = []
        for station in stations:
            records = station['records']
            ok = [r for r in records if r['status']=='OK']
            completed = [r for r in ok if r['at'] <= args.duration]
            times = [0.] + sorted(r['at'] for r in completed) + [args.duration]
            p95 = percentile([r['latencyMs'] for r in ok], .95)
            fps = len(completed)/args.duration
            rows.append(dict(stationId=station['binding']['stationId'], analysisType=station['binding']['analysisType'],
                candidates=count if arrival_mode == 'fixed' else station['submitted'], submitted=station['submitted'],
                skippedBusy=station['skippedBusy'] if arrival_mode == 'fixed' else None,
                skippedLate=station['skippedLate'] if arrival_mode == 'fixed' else None,
                completed=len(ok), completedWithinWindow=len(completed), effectiveFps=round(fps,3),
                withinDeadline=sum(r['latencyMs']<=args.deadline_ms for r in ok), p95Ms=p95,
                p99Ms=percentile([r['latencyMs'] for r in ok],.99),
                queueP95Ms=percentile([r['queueMs'] for r in ok],.95),
                stagesP95Ms={name: percentile([r['stagesMs'][name] for r in ok if name in r['stagesMs']], .95)
                            for name in sorted({name for r in ok for name in r['stagesMs']})},
                longestNoResultMs=round(max(b-a for a,b in zip(times,times[1:]))*1000,2),
                errors={status:sum(r['status']==status for r in records) for status in sorted({r['status'] for r in records if r['status']!='OK'})},
                targetMet=fps>=args.fps*.95 and p95 is not None and p95<=args.deadline_ms))
        report = dict(source=f'{len(stations)} repeated pre-encoded JPEGs; real HTTP and configured models; excludes camera acquisition, browser capture/JPEG encoding, video decoding and display',
            status='complete', measurementCompleted=True,
            measurementClock=dict(name='perf_counter',resolutionSeconds=time.get_clock_info('perf_counter').resolution),
            arrivalMode=arrival_mode,
            candidateMeaning=('Fixed scheduled frame arrivals, including arrivals skipped while busy or late' if arrival_mode == 'fixed'
                else 'Actual serial request attempts; no hypothetical fixed arrivals or skipped-frame counts (null means not applicable)'),
            cadence=('Fixed target interval from measurement start' if arrival_mode == 'fixed'
                else 'Independent station worker; one request in flight; after completion waits max(0, 1/fps - request elapsed)'),
            targetFps=args.fps, deadlineMs=args.deadline_ms, seconds=args.duration, warmup=warmup, stations=rows,
            inputs=[dict(stationId=station['binding']['stationId'],analysisType=station['binding']['analysisType'],
                modelRevision=station['stream'].get('modelRevision'),bindingRevision=station['binding'].get('revision'),
                **station['input']) for station in stations],
            latencyPopulation='All successful requests submitted during the window, including responses completed during drain',
            measurementStartedAt=measurement_started_at, measurementFinishedAt=measurement_started_at+args.duration,
            totalEffectiveFps=round(sum(row['effectiveFps'] for row in rows),3),
            parallelWarmup=parallel_warmup,
            allTargetsMet=all(r['targetMet'] for r in rows))
        destination = Path(args.report)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report['allTargetsMet'] else 1
    except Exception as exc:
        # Preserve an explicit non-measurement outcome when setup/cold load
        # fails; a missing report must never resemble a zero-throughput run.
        write_failure(exc)
        raise
    finally:
        for station in stations:
            try:
                close(station, wait_seconds=5)
            except collector.ApiError:
                print('A stream could not be released; its lease expires after 30 seconds without frames.',file=sys.stderr)
        for client in clients:
            if hasattr(client, 'close'):
                client.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server',required=True)
    parser.add_argument('--images',nargs='+',type=Path,required=True)
    parser.add_argument('--fps',type=float,default=4)
    parser.add_argument('--arrival-mode', choices=('fixed', 'serial'), default='fixed',
                        help='fixed scheduled arrivals or serial one-request-at-a-time cadence (no browser capture/encoding)')
    parser.add_argument('--duration',type=float,default=60)
    parser.add_argument('--deadline-ms',type=float,default=500)
    parser.add_argument('--report',required=True)
    args = parser.parse_args()
    if not 1<=len(args.images)<=6 or not .5<=args.fps<=30 or not 1<=args.duration<=3600 or not 1<=args.deadline_ms<=30000:
        parser.error('Expected 1–6 images, FPS .5–30, duration 1–3600s, deadline 1–30000ms')
    try:
        raise SystemExit(run(args))
    except (ValueError,collector.ApiError) as error:
        print(str(error),file=sys.stderr)
        raise SystemExit(2)
