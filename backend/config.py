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
    realtime_backend: str = "yolo"
    realtime_warmup: bool = True
    pose_backend: str = "yolo"
    max_upload_bytes: int = 500 * 1024 * 1024
    max_video_seconds: int = 600
    lease_seconds: int = 60
    session_seconds: int = 8 * 3600
    realtime_pool_size: int = 2
    station_queue_wait_ms: int = 500
    station_inference_concurrency: int = 1
    specialist_pool_size: int = 1
    scene_model_cache_size: int = 1
    cleanup_interval_seconds: int = 3600
    run_retention_seconds: int = 30 * 86400
    upload_retention_seconds: int = 7 * 86400
    artifact_accel_redirect: bool = False
    opencv_threads: int = 4
    evidence_retention_seconds: int = 30 * 86400
    torch_threads: int = 4
    worker_analysis_enabled: bool = True
    worker_training_enabled: bool = True
    pilot_registration_enabled: bool = False
    realtime_max_duration_seconds: int = 600
    realtime_no_target_seconds: int = 60

    def __post_init__(self):
        for name in ('realtime_max_duration_seconds', 'realtime_no_target_seconds'):
            if type(getattr(self, name)) is not int or not 1 <= getattr(self, name) <= 86400:
                raise ValueError(f'{name} must be an integer between 1 and 86400')
        if type(self.specialist_pool_size) is not int or not 1 <= self.specialist_pool_size <= 6:
            raise ValueError('SPECIALIST_POOL_SIZE must be an integer between 1 and 6')
        if type(self.torch_threads) is not int or not 1 <= self.torch_threads <= 16:
            raise ValueError('TORCH_THREADS must be an integer between 1 and 16')
        if self.evidence_retention_seconds < 30 * 86400:
            raise ValueError('告警取证录像保留期不得少于30天')

    @classmethod
    def from_env(cls):
        environment = os.getenv("APP_ENV", "production")
        database_url = os.getenv("DATABASE_URL", "")
        password = os.getenv("APP_PASSWORD", "")
        if not database_url:
            raise RuntimeError("Set DATABASE_URL (PostgreSQL required in production)")
        if environment == "production" and not database_url.startswith("postgresql+psycopg://"):
            raise RuntimeError("Production requires postgresql+psycopg:// DATABASE_URL")
        if not password:
            raise RuntimeError("Set APP_PASSWORD to a non-empty password")
        if environment != "development" and len(password) < 12:
            raise RuntimeError("Set APP_PASSWORD to at least 12 characters")
        secure = os.getenv("COOKIE_SECURE", "true").lower() == "true"
        if environment == "production" and not secure:
            raise RuntimeError("Production requires HTTPS and COOKIE_SECURE=true")
        backend = os.getenv("REALTIME_BACKEND", "yolo").strip().lower() or "yolo"
        if backend not in ("rtmo", "rtmpose", "yolo"):
            raise RuntimeError("REALTIME_BACKEND 仅支持 rtmo / rtmpose / yolo")
        offline = os.getenv("POSE_BACKEND", "yolo").strip().lower() or "yolo"
        if offline not in ("rtmo", "rtmpose", "yolo"):
            raise RuntimeError("POSE_BACKEND 仅支持 rtmo / rtmpose / yolo")
        warmup = os.getenv("REALTIME_WARMUP", "true").strip().lower() not in ("0", "false", "no")
        try:
            pool_size = int(os.getenv("REALTIME_POOL_SIZE", "2"))
            cleanup_interval = int(os.getenv("CLEANUP_INTERVAL_SECONDS", "3600"))
            run_retention = int(os.getenv("RUN_RETENTION_SECONDS", str(30 * 86400)))
            upload_retention = int(os.getenv("UPLOAD_RETENTION_SECONDS", str(7 * 86400)))
            max_upload = int(os.getenv("MAX_UPLOAD_BYTES", str(500 * 1024 * 1024)))
            evidence_retention = int(os.getenv('EVIDENCE_RETENTION_SECONDS', str(30 * 86400)))
            station_queue_wait = int(os.getenv('STATION_QUEUE_WAIT_MS', '500'))
            station_concurrency = int(os.getenv('STATION_INFERENCE_CONCURRENCY', '1'))
            specialist_pool = int(os.getenv('SPECIALIST_POOL_SIZE', '1'))
            scene_cache_size = int(os.getenv('SCENE_MODEL_CACHE_SIZE', '1'))
            realtime_max_duration = int(os.getenv('REALTIME_MAX_DURATION_SECONDS', '600'))
            realtime_no_target = int(os.getenv('REALTIME_NO_TARGET_SECONDS', '60'))
        except ValueError as exc:
            raise RuntimeError("清理、实时池和上传大小参数必须是整数") from exc
        if not 1 <= pool_size <= 6:
            raise RuntimeError("REALTIME_POOL_SIZE 必须在 1–6 之间")
        if not 50 <= station_queue_wait <= 5000:
            raise RuntimeError("STATION_QUEUE_WAIT_MS 必须在50–5000之间")
        if not 1 <= station_concurrency <= 6:
            raise RuntimeError("STATION_INFERENCE_CONCURRENCY 必须在1–6之间")
        if not 1 <= specialist_pool <= 6:
            raise RuntimeError("SPECIALIST_POOL_SIZE 必须在1–6之间")
        if not 1 <= scene_cache_size <= 6:
            raise RuntimeError("SCENE_MODEL_CACHE_SIZE 必须在1–6之间")
        try:
            opencv_threads = int(os.getenv("OPENCV_THREADS", "4"))
        except ValueError as exc:
            raise RuntimeError("OPENCV_THREADS 必须是 1–16 之间的整数") from exc
        if not 1 <= opencv_threads <= 16:
            raise RuntimeError("OPENCV_THREADS 必须是 1–16 之间的整数")
        try:
            torch_threads = int(os.getenv('TORCH_THREADS', '4'))
        except ValueError as exc:
            raise RuntimeError('TORCH_THREADS 必须是 1–16 之间的整数') from exc
        if not 1 <= torch_threads <= 16:
            raise RuntimeError('TORCH_THREADS 必须是 1–16 之间的整数')
        if cleanup_interval < 60 or run_retention < 30 * 86400 or upload_retention < 3600:
            raise RuntimeError("清理周期和保留期过短")
        if evidence_retention < 30 * 86400:
            raise RuntimeError('EVIDENCE_RETENTION_SECONDS 不得少于2592000（30天）')
        if max_upload < 1:
            raise RuntimeError("MAX_UPLOAD_BYTES 必须为正整数")
        accel = os.getenv("ARTIFACT_ACCEL_REDIRECT", "false").strip().lower() in ("1", "true", "yes")
        def enabled(name, default='true'):
            value = os.getenv(name, default).strip().lower()
            if value not in ('1', 'true', 'yes', '0', 'false', 'no'):
                raise RuntimeError(f'{name} 必须为 true 或 false')
            return value in ('1', 'true', 'yes')
        return cls(database_url, Path(os.getenv("DATA_DIR", str(ROOT / "runtime-data"))).resolve(),
                   os.getenv("APP_USERNAME", "admin"), password,
                   Path(os.getenv("MODEL_PATH", str(ROOT / "models/yolo11s-pose.pt"))).resolve(),
                   environment, secure, backend, warmup, offline,
                   max_upload_bytes=max_upload,
                   realtime_pool_size=pool_size, cleanup_interval_seconds=cleanup_interval,
                   run_retention_seconds=run_retention, upload_retention_seconds=upload_retention,
                   artifact_accel_redirect=accel, opencv_threads=opencv_threads, torch_threads=torch_threads,
                   station_queue_wait_ms=station_queue_wait,
                   station_inference_concurrency=station_concurrency,
                   specialist_pool_size=specialist_pool,
                   scene_model_cache_size=scene_cache_size,
                   realtime_max_duration_seconds=realtime_max_duration,
                   realtime_no_target_seconds=realtime_no_target,
                   worker_analysis_enabled=enabled('WORKER_ANALYSIS_ENABLED'),
                   worker_training_enabled=enabled('WORKER_TRAINING_ENABLED'),
                   pilot_registration_enabled=enabled('PILOT_REGISTRATION_ENABLED', 'false'),
                   evidence_retention_seconds=evidence_retention)
