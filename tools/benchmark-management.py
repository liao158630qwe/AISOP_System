"""Isolated management read benchmark; synthetic rows, no GPU/training workloads."""
import argparse
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import event, select
from algorithms.video import file_hash
from backend.config import Settings
from backend.db import initialize, make_engine, model_registry, scene_tasks, training_runs
from backend.scenes import SceneDefinition, SceneRegistry
from backend.training_service import list_runs, public_run


def measure(engine, fn, repeats):
    durations, queries = [], []
    fn()  # Warm SQL compilation and asset checksum caches equally.
    for _ in range(repeats):
        statements = []
        def record(*args):
            statements.append(1)
        event.listen(engine, 'before_cursor_execute', record)
        started = time.perf_counter()
        try:
            result = fn()
            encoded = json.dumps(result, ensure_ascii=False).encode('utf-8')
        finally:
            event.remove(engine, 'before_cursor_execute', record)
        durations.append((time.perf_counter() - started) * 1000)
        queries.append(len(statements))
    return {'median_ms': round(statistics.median(durations), 3),
            'max_ms': round(max(durations), 3), 'sql_queries': max(queries), 'json_bytes': len(encoded)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / 'test-results/management-benchmark.json')
    parser.add_argument('--repeats', type=int, default=10)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='aisop-management-') as temporary:
        folder = Path(temporary)
        engine = make_engine('sqlite:///' + (folder / 'fixture.db').as_posix())
        try:
            initialize(engine)
            settings = Settings('unused', folder, 'bench', 'fixture-password', folder / 'unused', 'test', False)
            registry = SceneRegistry(engine, settings)
            identity = str(uuid.uuid4())
            asset = registry.directory / (identity + '.pt')
            asset.write_bytes(b'synthetic asset; never loaded as a model')
            model = dict(id=identity, owner='bench', name='fixture', version='1', task='detect',
                classes={str(i): f'class-{i}' for i in range(80)}, path='registered-models/' + asset.name,
                sha256=file_hash(asset), size=asset.stat().st_size, enabled=True, notes='', created_at=1)
            config = SceneDefinition(name='fixture', model_id=identity, status='published',
                rules=[dict(id='required', name='fixture', class_id=0)]).model_dump(mode='json', exclude={'revision'})
            with engine.begin() as c:
                c.execute(model_registry.insert().values(**model))
                c.execute(scene_tasks.insert(), [dict(id=str(uuid.uuid4()), owner='bench', name='fixture',
                    model_id=identity, status='published', revision=1, config=config, created_at=1, updated_at=1)
                    for _ in range(200)])
                c.execute(training_runs.insert(), [dict(id=str(uuid.uuid4()), owner='bench', name=f'fixture-{i}',
                    request_key=str(uuid.uuid4()), request_hash='fixture', station='station1', stage='CANDIDATE',
                    state='CANDIDATE', recipe={'task': 'detect'}, policy={}, progress=1, message='fixture',
                    audit={'fixture': 'x' * 50000}, evaluation={'fixture': 'x' * 50000}, created_at=i, updated_at=i)
                    for i in range(125)])

            def previous_training():
                with engine.connect() as c:
                    rows = c.execute(select(training_runs).where(training_runs.c.owner == 'bench')
                        .order_by(training_runs.c.created_at.desc()).limit(100)).mappings().all()
                return {'items': [public_run(row) for row in rows]}

            def previous_catalog():
                models = registry.models('bench')
                with engine.connect() as c:
                    ids = c.execute(select(scene_tasks.c.id).where(scene_tasks.c.owner == 'bench')
                        .order_by(scene_tasks.c.updated_at.desc())).scalars().all()
                scenes = []
                for sid in ids:
                    row = registry._scene_row(sid, 'bench')
                    related = registry._model(row['model_id'], 'bench')
                    scenes.append(registry._public_scene(row, related, registry._readiness(related)))
                return {'models': models, 'scenes': scenes}

            report = {'provenance': 'Measured local SQLite synthetic fixture; no network/auth/GPU/inference timing. Previous read paths reconstructed from pre-change queries; recovery write costs excluded.',
                'fixture': {'training_rows': 125, 'report_bytes_per_row': 100000, 'scenes': 200, 'models': 1},
                'repeats': args.repeats,
                'training_before': measure(engine, previous_training, args.repeats),
                'training_after': measure(engine, lambda: list_runs(engine, 'bench'), args.repeats),
                'catalog_before': measure(engine, previous_catalog, args.repeats),
                'catalog_after': measure(engine, lambda: registry.catalog('bench'), args.repeats)}
        finally:
            engine.dispose()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
