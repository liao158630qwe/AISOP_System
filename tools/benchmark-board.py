"""Reproducible board timing against disposable SQLite fixture data; no inference."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import statistics
import sys
import tempfile
import time
import tracemalloc
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import event
from backend.db import initialize, make_engine, pilot_cycles, pilot_events, station_inspections, station_terminals
from backend import board
from backend.terminals import Terminals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--label', default='current')
    parser.add_argument('--parts', type=int, default=2000)
    parser.add_argument('--days', type=int, default=5)
    args = parser.parse_args()
    if args.parts < 1 or args.days < 1 or not re.fullmatch(r'[A-Za-z0-9_-]+', args.label):
        parser.error('parts/days must be positive; label must use letters, digits, underscores or hyphens')
    folder = ROOT / 'test-results' / 'board-performance'
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='board-', dir=folder) as temporary:
        engine = make_engine('sqlite:///' + (Path(temporary) / 'fixture.db').as_posix())
        initialize(engine)
        today = datetime.now(timezone(timedelta(hours=8))).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        # A fixed synthetic 20:00 snapshot keeps fixture contents repeatable at any run time.
        clock = today + 20 * 3600
        board.now = lambda _: clock
        sequence = 0
        with engine.begin() as c:
            for day in range(args.days):
                rows, alarms, inspections = [], [], []
                for part in range(args.parts):
                    for station in range(1, 7):
                        sequence += 1
                        identity = f'cycle-{sequence}'
                        at = today - day * 86400 + (part + 1) / (args.parts + 1) * max(1, clock - today - 60)
                        failed = sequence % 25 == 0
                        rows.append(dict(id=identity, owner='benchmark', station=f'station-{station}',
                            active_station=None, product='灯具', part_number=f'P-{day}-{part}', source='camera',
                            parent_id=None, stage='CLOSED', result='NG' if failed else 'MANUAL_OK', review_status=None,
                            revision=1, config={}, checks={}, reason='漏螺丝' if failed else '',
                            started_at=at-1, updated_at=at, finished_at=at))
                        alarms.append(dict(id=f'event-{sequence}', cycle_id=identity, owner='benchmark',
                            request_key=f'request-{sequence}', request_hash='fixture', kind='CLOSE' if failed else 'FINISH',
                            revision=1, at=at, payload={'outcome':'NG' if failed else 'MANUAL_OK', 'reason':'漏螺丝'}, response={}))
                        if station == 1:
                            inspections.append(dict(stream_id=f'stream-{sequence}', station_id='station-1', owner='benchmark',
                                cycle_id=identity, context={}, status='FINISHED', reason='', started_at=at-1, finished_at=at,
                                result={'compliance':{'status':'FAIL' if failed else 'PASS','reason':'缺卡扣'},'fixturePayload':'x' * 8192}))
                    if len(rows) >= 1200:
                        c.execute(pilot_cycles.insert(), rows); c.execute(pilot_events.insert(), alarms)
                        c.execute(station_inspections.insert(), inspections)
                        rows, alarms, inspections = [], [], []
                if rows:
                    c.execute(pilot_cycles.insert(), rows); c.execute(pilot_events.insert(), alarms)
                    c.execute(station_inspections.insert(), inspections)
            c.execute(station_terminals.insert(), [dict(station_id=f'station-{i}', owner='benchmark',
                workstation_id=f'pc-{i}', camera_id=f'cam-{i}', credential_hash=f'fixture-{i}', revision=1,
                enabled=True, config={'analysisType':'pose'}, updated_at=clock) for i in range(1, 7)])
        app = SimpleNamespace(state=SimpleNamespace(engine=engine,
            settings=SimpleNamespace(station_queue_wait_ms=500, station_inference_concurrency=1)))
        app.state.terminals = Terminals(app)
        calls, durations = [], []
        def count(*_): calls.append(time.perf_counter())
        def timing(*_): durations.append(round((time.perf_counter()-calls[-1])*1000,2))
        event.listen(engine, 'before_cursor_execute', count)
        event.listen(engine, 'after_cursor_execute', timing)
        times = []
        for _ in range(6):
            started = time.perf_counter(); payload = board.snapshot(app, 'benchmark')
            if isinstance(payload, tuple): payload = payload[0]
            times.append((time.perf_counter() - started) * 1000)
        tracemalloc.start(); board.snapshot(app, 'benchmark'); _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
        report = dict(label=args.label, database='disposable SQLite', cycles=sequence, events=sequence,
            inspections=args.parts*args.days, todayParts=args.parts, rawSnapshotMedianMs=round(statistics.median(times),2),
            rawSnapshotMaxMs=round(max(times),2), rawSnapshotQueries=len(calls)//7,
            queryTimingsMs=durations[:len(calls)//7],
            scope='SQL aggregation and six registered offline stations; excludes HTTP/authentication/inference',
            peakPythonMiB=round(peak/1024**2,2), responseBytes=len(json.dumps(payload).encode()), summary=payload['summary'])
        if hasattr(board, 'BoardService'):
            service = board.BoardService(app)
            service.snapshot('benchmark')
            calls.clear(); warm = []
            for _ in range(100):
                started=time.perf_counter(); service.snapshot('benchmark'); warm.append((time.perf_counter()-started)*1000)
            report.update(cachedSnapshotMedianMs=round(statistics.median(warm),3), cachedSnapshotQueries=len(calls),
                          cache=service.metrics())
        engine.dispose()
    (folder / (args.label + '.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
