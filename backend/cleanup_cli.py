"""Run retention cleanup manually: python -m backend.cleanup_cli [--dry-run]."""
import argparse
import json

from .cleanup import cleanup_data
from .config import Settings
from .db import make_engine


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    settings = Settings.from_env()
    result = cleanup_data(make_engine(settings.database_url), settings.data_dir,
                          run_retention_seconds=settings.run_retention_seconds,
                          upload_retention_seconds=settings.upload_retention_seconds,
                          dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
