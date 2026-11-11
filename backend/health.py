"""Shared worker presence uses database time, including across multiple hosts."""
from sqlalchemy import delete, func, select, update

from .db import now, worker_heartbeats


def pulse_worker(engine, identity, ttl=30):
    with engine.begin() as c:
        t = now(c)
        values = dict(updated_at=t, expires_at=t + ttl)
        changed = c.execute(update(worker_heartbeats).where(worker_heartbeats.c.id == identity).values(**values))
        if not changed.rowcount:
            c.execute(worker_heartbeats.insert().values(id=identity, **values))
        c.execute(delete(worker_heartbeats).where(worker_heartbeats.c.expires_at < t - 3600))


def remove_worker(engine, identity):
    with engine.begin() as c:
        c.execute(delete(worker_heartbeats).where(worker_heartbeats.c.id == identity))


def worker_snapshot(engine):
    with engine.connect() as c:
        t = now(c)
        active = c.execute(select(func.count()).select_from(worker_heartbeats)
            .where(worker_heartbeats.c.expires_at >= t)).scalar_one()
    return {'status': 'ready' if active else 'unavailable', 'active': active}
