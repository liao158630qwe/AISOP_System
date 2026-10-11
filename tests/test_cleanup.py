import os
import time

from sqlalchemy import select

from backend.cleanup import cleanup_data
from backend.db import media, run_metrics, runs


def _media(mid, created):
    return dict(id=mid, owner='admin', name=mid + '.mp4', suffix='.mp4', size=3,
                sha256='hash', metadata={}, created_at=created)


def _run(rid, mid, status, created, result_dir=None):
    return dict(id=rid, owner='admin', media_id=mid, status=status, request_key=rid,
                request_hash='hash', options={}, model_hash='hash', progress=1 if status == 'SUCCEEDED' else 0,
                attempts=1, token=1, lease_until=None, error_code=None, error_message=None,
                result_dir=result_dir, created_at=created, updated_at=created)


def test_cleanup_dry_run_and_terminal_retention(authenticated, tmp_path):
    settings, app, _ = authenticated
    root = settings.data_dir
    (root / 'uploads').mkdir(parents=True, exist_ok=True)
    (root / 'runs').mkdir(parents=True, exist_ok=True)
    old = 100.0
    with app.state.engine.begin() as c:
        c.execute(media.insert(), [_media('old-media', old), _media('active-media', old), _media('orphan-media', old)])
        c.execute(runs.insert(), [
            _run('old-run', 'old-media', 'SUCCEEDED', old, 'old-run-1'),
            _run('active-run', 'active-media', 'RUNNING', old),
        ])
        c.execute(run_metrics.insert().values(run_id='old-run', queued_at=old))
    old_upload = root / 'uploads' / 'old-media.mp4'; old_upload.write_bytes(b'old')
    active_upload = root / 'uploads' / 'active-media.mp4'; active_upload.write_bytes(b'active')
    old_folder = root / 'runs' / 'old-run-1'; old_folder.mkdir(); (old_folder / 'report.html').write_text('x')
    stale_part = root / 'uploads' / 'dangling.part'; stale_part.write_bytes(b'x')
    for path in (old_upload, active_upload, old_folder, stale_part):
        os.utime(path, (old, old))

    preview = cleanup_data(app.state.engine, root, run_retention_seconds=100,
                           upload_retention_seconds=100, orphan_retention_seconds=100,
                           now_ts=1000, dry_run=True)
    assert preview['runs'] == 1 and old_folder.exists() and old_upload.exists()

    result = cleanup_data(app.state.engine, root, run_retention_seconds=100,
                          upload_retention_seconds=100, orphan_retention_seconds=100,
                          now_ts=1000)
    assert result['runs'] == 1 and result['parts'] == 1
    assert not old_folder.exists() and not old_upload.exists() and active_upload.exists()
    with app.state.engine.connect() as c:
        assert c.execute(select(runs.c.id)).all() == [('active-run',)]
        assert c.execute(select(run_metrics.c.run_id)).all() == []
        assert c.execute(select(media.c.id)).all() == [('active-media',)]
