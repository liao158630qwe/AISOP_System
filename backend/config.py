import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    database_url: str
    data_dir: Path
    username: str
    password: str
    model_path: Path
    environment: str = "production"
    secure_cookie: bool = True
    realtime_backend: str = "rtmo"
    realtime_warmup: bool = True
    pose_backend: str = "rtmo"
    max_upload_bytes: int = 500 * 1024 * 1024
    max_video_seconds: int = 600
    lease_seconds: int = 60
    session_seconds: int = 8 * 3600
    realtime_pool_size: int = 2
    cleanup_interval_seconds: int = 3600
    run_retention_seconds: int = 30 * 86400
    upload_retention_seconds: int = 7 * 86400
    artifact_accel_redirect: bool = False

    @classmethod
    def from_env(cls):
        environment = os.getenv("APP_ENV", "production")
        database_url = os.getenv("DATABASE_URL", "")
        password = os.getenv("APP_PASSWORD", "")
        if not database_url:
            raise RuntimeError("Set DATABASE_URL (PostgreSQL required in production)")
        if environment == "production" and not database_url.startswith("postgresql+psycopg://"):
            raise RuntimeError("Production requires postgresql+psycopg:// DATABASE_URL")
        if len(password) < 12:
            raise RuntimeError("Set APP_PASSWORD to at least 12 characters")
        secure = os.getenv("COOKIE_SECURE", "true").lower() == "true"
        if environment == "production" and not secure:
            raise RuntimeError("Production requires HTTPS and COOKIE_SECURE=true")
        backend = os.getenv("REALTIME_BACKEND", "rtmo").strip().lower() or "rtmo"
        if backend not in ("rtmo", "rtmpose", "yolo"):
            raise RuntimeError("REALTIME_BACKEND 仅支持 rtmo / rtmpose / yolo")
        offline = os.getenv("POSE_BACKEND", "rtmo").strip().lower() or "rtmo"
        if offline not in ("rtmo", "rtmpose", "yolo"):
            raise RuntimeError("POSE_BACKEND 仅支持 rtmo / rtmpose / yolo")
        warmup = os.getenv("REALTIME_WARMUP", "true").strip().lower() not in ("0", "false", "no")
        try:
            pool_size = int(os.getenv("REALTIME_POOL_SIZE", "2"))
            cleanup_interval = int(os.getenv("CLEANUP_INTERVAL_SECONDS", "3600"))
            run_retention = int(os.getenv("RUN_RETENTION_SECONDS", str(30 * 86400)))
            upload_retention = int(os.getenv("UPLOAD_RETENTION_SECONDS", str(7 * 86400)))
        except ValueError as exc:
            raise RuntimeError("清理与实时池参数必须是整数") from exc
        if not 1 <= pool_size <= 4:
            raise RuntimeError("REALTIME_POOL_SIZE 必须在 1–4 之间")
        if cleanup_interval < 60 or run_retention < 3600 or upload_retention < 3600:
            raise RuntimeError("清理周期和保留期过短")
        accel = os.getenv("ARTIFACT_ACCEL_REDIRECT", "false").strip().lower() in ("1", "true", "yes")
        return cls(database_url, Path(os.getenv("DATA_DIR", str(ROOT / "runtime-data"))).resolve(),
                   os.getenv("APP_USERNAME", "admin"), password,
                   Path(os.getenv("MODEL_PATH", str(ROOT / "models/yolo11s-pose.pt"))).resolve(),
                   environment, secure, backend, warmup, offline,
                   realtime_pool_size=pool_size, cleanup_interval_seconds=cleanup_interval,
                   run_retention_seconds=run_retention, upload_retention_seconds=upload_retention,
                   artifact_accel_redirect=accel)
