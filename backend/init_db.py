"""Explicit upgrade or read-only schema/data preflight."""
import argparse
import json
import os

from sqlalchemy.engine import make_url

from .db import initialize, make_engine
from .migrations import MigrationValidationError, SchemaError, check_database


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='read-only preflight; no DDL or data changes')
    args = parser.parse_args()
    url = os.getenv('DATABASE_URL')
    if not url:
        parser.error('DATABASE_URL is required')
    # sqlite normally creates a missing file even on a read. Check mode must
    # not accidentally bootstrap a database at a mistyped path.
    if args.check:
        from pathlib import Path
        parsed = make_url(url)
        if parsed.get_backend_name() == 'sqlite' and parsed.database not in (None, '', ':memory:'):
            if not Path(parsed.database).is_file():
                parser.error('SQLite database does not exist; --check never creates it')
    engine = make_engine(url)
    try:
        if args.check:
            report = check_database(engine)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report['ready'] else (2 if report['can_upgrade'] else 1)
        version = initialize(engine)
        print(f'Database schema v{version} ready')
        return 0
    except MigrationValidationError as exc:
        print(json.dumps({'error': str(exc), 'issues': exc.issues}, ensure_ascii=False, indent=2))
        return 1
    except SchemaError as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        return 1
    finally:
        engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
