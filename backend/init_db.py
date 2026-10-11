from .config import Settings
from .db import initialize, make_engine

if __name__ == "__main__":
    settings = Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    initialize(make_engine(settings.database_url))
    print("Database schema v1 ready")
