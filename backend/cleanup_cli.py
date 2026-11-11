"""Run retention cleanup manually: python -m backend.cleanup_cli [--dry-run]."""
import argparse
import json

from .cleanup import cleanup_data
from .config import Settings
from .db import make_engine
from .evidence import expire_evidence


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    settings = Settings.from_env()
    engine = make_engine(settings.database_url)
    evidence = expire_evidence(engine, settings.data_dir, retention=settings.evidence_retention_seconds, dry_run=args.dry_run)
    result = cleanup_data(engine, settings.data_dir,
                          run_retention_seconds=settings.run_retention_seconds,
                          upload_retention_seconds=settings.upload_retention_seconds,
                          dry_run=args.dry_run)
    result['evidence'] = evidence
    engine.dispose()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
