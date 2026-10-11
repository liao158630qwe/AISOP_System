"""Validate station configuration and referenced local assets before restarting services."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from algorithms.stations import profiles,snapshot

if __name__ == '__main__':
    for station in profiles():
        frozen=snapshot(station.id)
        count=sum(len(c.targets) for c in station.checks)
        print(f'{station.id}: {station.name}, {len(station.checks)} checks, {count} configured targets, approved={station.approved}, hash={frozen["sha256"][:12]}')
