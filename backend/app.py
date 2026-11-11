import hashlib
import hmac
import secrets
import os
import threading
import time
import uuid
import asyncio
import base64
import binascii
import json
import math
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select, update, or_, and_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError, DBAPIError, TimeoutError as DatabaseTimeout
from starlette.concurrency import run_in_threadpool

from .config import ROOT, Settings
from .login_limits import LoginLimiter
from .db import digest, make_engine, media, now, option_field, request_hash, run_metrics, runs, sessions, serialized_write
from .scenes import SceneRegistry, SceneDefinition, ModelImport, ModelToggle, import_identity


class Login(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=200)


class Options(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    start: float = Field(default=0, ge=0)
    end: float | None = Field(default=None, gt=0)
    sample_fps: float = Field(default=5, ge=.5, le=10)
    imgsz: int = 640
    crop: list[int] | None = None
    station_id: str | None = Field(default=None, max_length=30)
    workflow_id: str | None = Field(default=None, max_length=40)
    pose_backend: Literal["yolo", "rtmo", "rtmpose"] | None = None
    analysis_type: Literal["pose", "screw", "sticker", "scene"] = "pose"
    scene_id: str | None = Field(default=None, max_length=36)


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    media_id: uuid.UUID
    options: Options = Field(default_factory=Options)


class EvidenceGap(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    start: float = Field(ge=0, le=86400)
    end: float = Field(ge=0, le=86400)
    reason: str = Field(min_length=1, max_length=160)


class FinishRealtimeRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    evidenceGaps: list[EvidenceGap] = Field(default_factory=list, max_length=100)


def public_run(row):
    return {k: row[k] for k in ["id", "media_id", "status", "progress", "attempts", "error_code", "error_message", "created_at", "updated_at"]}


def _encode_cursor(created_at, run_id):
    raw = json.dumps([float(created_at), str(run_id)], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(value):
    try:
        padded = value + "=" * (-len(value) % 4)
        created_at, run_id = json.loads(base64.urlsafe_b64decode(padded).decode())
        created_at, run_id = float(created_at), str(run_id)
        if not 0 <= created_at <= 4102444800 or not 1 <= len(run_id) <= 100:
            raise ValueError()
        return created_at, run_id
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError, binascii.Error):
        raise HTTPException(422, "分页游标无效")


def create_app(settings=None):
    settings = settings or Settings.from_env()
    engine = make_engine(settings.database_url)
    @asynccontextmanager
    async def lifespan(app):
        warmup_thread = None
        warmup_stop = threading.Event()
        try:
            from .inference_runtime import configure_inference_runtime, configure_torch_runtime
            opencv_threads = configure_inference_runtime(settings.opencv_threads)
            torch_runtime = configure_torch_runtime(settings.torch_threads)
            from algorithms.gpu import check_cuda
            gpu = check_cuda()
            from .migrations import require_current
            with engine.connect() as c:
                require_current(c)
            app.state.inference_runtime = {**gpu, 'opencvThreads': opencv_threads, **torch_runtime}
            if settings.environment != "test" and settings.realtime_warmup:
                # Global CPU settings must precede warmup and incoming requests.
                def warmup():
                    for name, service in (('sticker', app.state.sticker_realtime), ('person', app.state.realtime)):
                        if warmup_stop.is_set():
                            break
                        try:
                            if not service.warmup():
                                logging.getLogger(__name__).warning('%s model preparation deferred', name)
                        except Exception:
                            logging.getLogger(__name__).exception('%s model preparation failed', name)
                warmup_thread = threading.Thread(target=warmup, name="realtime-warmup", daemon=True)
                warmup_thread.start()
            if settings.environment != 'test':
                app.state.evidence.start()
            yield
        finally:
            warmup_stop.set()
            from .shutdown import close_in_order
            await close_in_order([
                ('station-scheduler', app.state.terminals.scheduler.close),
                ('realtime-transport', app.state.realtime_transport.close),
                ('model-preparation', lambda: run_in_threadpool(warmup_thread.join) if warmup_thread else asyncio.sleep(0)),
                ('screw-models', lambda: run_in_threadpool(close_specialist, app.state.screw_realtime)),
                ('sticker-models', lambda: run_in_threadpool(close_specialist, app.state.sticker_realtime)),
                ('scene-models', lambda: run_in_threadpool(app.state.scene_realtime.close)),
                ('person-models', lambda: run_in_threadpool(app.state.realtime.close)),
                ('evidence-store', lambda: run_in_threadpool(app.state.evidence.close)),
                ('database', lambda: run_in_threadpool(engine.dispose)),
            ])
    app = FastAPI(title="AI-SOP Server", version="0.1.0", lifespan=lifespan,
                  docs_url=None if settings.environment == "production" else "/docs")
    app.state.engine, app.state.settings = engine, settings
    from .gpu_telemetry import GpuTelemetry
    app.state.gpu_telemetry = GpuTelemetry()
    from .evidence import EvidenceStore, register as register_evidence
    app.state.evidence = EvidenceStore(engine, settings)
    app.state.inference_runtime = {"opencvThreads": None}
    from .realtime import RealtimeDetector
    app.state.realtime = RealtimeDetector(settings.model_path, settings.realtime_backend, settings.realtime_pool_size)
    from .screw_realtime import RealtimeScrews
    from .specialist_pool import create_specialist, specialist_snapshot, close_specialist
    app.state.screw_realtime = create_specialist(lambda: RealtimeScrews(app.state.realtime), settings.specialist_pool_size)
    from .sticker_realtime import RealtimeStickers
    app.state.sticker_realtime = create_specialist(lambda: RealtimeStickers(app.state.realtime), settings.specialist_pool_size)
    app.state.scenes = SceneRegistry(engine, settings)
    from .scene_realtime import RealtimeScenes
    inference_capacity = min(settings.station_inference_concurrency, settings.realtime_pool_size)
    scene_capacity = min(inference_capacity, settings.scene_model_cache_size)
    app.state.scene_realtime = RealtimeScenes(app.state.scenes, settings.scene_model_cache_size,
                                             concurrency=scene_capacity)
    for registry in (app.state.realtime.streams, app.state.screw_realtime.registry,
                     app.state.sticker_realtime.registry, app.state.scene_realtime.streams):
        registry.limit_seconds = (settings.realtime_max_duration_seconds, settings.realtime_no_target_seconds)
    from .transport import RealtimeTransport, RealtimeTransportMiddleware, TextCompressionMiddleware
    app.state.realtime_transport = RealtimeTransport(settings.realtime_pool_size, settings.specialist_pool_size,
        scene_capacity=scene_capacity, total_capacity=inference_capacity)
    app.add_middleware(TextCompressionMiddleware)
    app.add_middleware(RealtimeTransportMiddleware, transport=app.state.realtime_transport)
    from .terminals import Terminals, register as register_terminals
    app.state.terminals = Terminals(app)
    from .collector_control import CollectorControl, register as register_collector_control
    app.state.collector_control = CollectorControl(app.state.terminals)
    asset_revisions = {}
    asset_revision_lock = threading.Lock()

    def asset_revision(path):
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        with asset_revision_lock:
            cached = asset_revisions.get(path)
            if cached is None or cached[0] != signature:
                cached = (signature, hashlib.sha256(path.read_bytes()).hexdigest()[:12])
                asset_revisions[path] = cached
            return cached[1]
    for directory in ["uploads", "runs"]:
        (settings.data_dir / directory).mkdir(parents=True, exist_ok=True)
    # Derive once; compare derived keys to avoid storing passwords in sessions.
    salt = secrets.token_bytes(32)
    password_key = hashlib.pbkdf2_hmac("sha256", settings.password.encode(), salt, 200000)
    login_limiter = LoginLimiter()

    def auth(request):
        terminal = app.state.terminals.authenticate(request)
        if terminal is not None:
            return terminal
        token = request.cookies.get("aisop_session", "")
        with engine.connect() as c:
            session = c.execute(select(sessions).where(sessions.c.token_hash == digest(token), sessions.c.expires_at > now(c))).mappings().first()
        if not session:
            raise HTTPException(401, "请先登录")
        if request.method not in ["GET", "HEAD", "OPTIONS"] and not hmac.compare_digest(request.headers.get("x-csrf-token", ""), session["csrf"]):
            raise HTTPException(403, {'code': 'SESSION_CSRF_INVALID', 'message': '会话已更新，请同步登录状态后重试'})
        return session

    def owned_run(run_id, owner):
        with engine.connect() as c:
            columns = [runs.c[name] for name in ('id', 'media_id', 'status', 'progress', 'attempts',
                'error_code', 'error_message', 'created_at', 'updated_at', 'result_dir')]
            row = c.execute(select(*columns).where(runs.c.id == run_id, runs.c.owner == owner)).mappings().first()
        if not row:
            raise HTTPException(404, "任务不存在")
        return row

    def verify_manual(request, password):
        peer = 'manual:' + (request.client.host if request.client else 'unknown')
        with login_limiter.attempt(peer) as attempt:
            candidate = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 200000)
            attempt.success = hmac.compare_digest(candidate, password_key)
            if not attempt.success:
                raise HTTPException(403, '确认密码不正确，操作未保存')

    from .pilot import register as register_pilot
    register_evidence(app, auth)
    register_pilot(app, auth, verify_manual)
    register_terminals(app, auth)
    register_collector_control(app, auth)
    from .board import register as register_board
    register_board(app, auth)
    from .inspection_history import register as register_inspection_history
    register_inspection_history(app, auth)
    from .board_reset import register as register_board_reset
    register_board_reset(app, auth, verify_manual)
    from .training_service import register as register_training
    register_training(app, auth)
    from .dataset_service import register as register_datasets
    register_datasets(app, auth)
    from .deployment_settings import register as register_deployment_settings
    register_deployment_settings(app, auth)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        return JSONResponse(detail, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Do not echo request bodies (which may contain passwords) in errors.
        fields = [".".join(str(part) for part in error["loc"][1:]) or "请求内容" for error in exc.errors()]
        return JSONResponse({"message": "请求参数无效，请检查：" + "、".join(dict.fromkeys(fields))}, status_code=422)

    @app.exception_handler(DBAPIError)
    @app.exception_handler(DatabaseTimeout)
    async def database_error(request, exc):
        # Do not disclose credentials/SQL. Writes may have committed before a
        # connection was lost; clients must retry with the original request key.
        logging.getLogger(__name__).error('Database unavailable: %s', type(exc).__name__)
        return JSONResponse({'code': 'DATABASE_UNAVAILABLE',
            'message': '数据库暂不可用；如正在保存，请保留原请求编号，稍后重试确认结果'},
            status_code=503, headers={'Retry-After': '2', 'Cache-Control': 'no-store'})

    from algorithms.video import AnalysisError

    @app.exception_handler(AnalysisError)
    async def analysis_error(request, exc):
        return JSONResponse({'code': exc.code, 'message': str(exc)},
            status_code=503 if exc.code.startswith('GPU_') else 422)

    @app.middleware("http")
    async def no_cache_private(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path in ('/', '/index.html', '/app.js', '/styles.css', '/screw-dashboard.js', '/screw-dashboard.css'):
            response.headers['Cache-Control'] = 'no-cache'
        elif request.url.path in ('/admin', *(f'/station-{number}' for number in range(1, 7))) or request.url.path.endswith(('.html', '.js', '.css')):
            response.headers.setdefault('Cache-Control', 'no-cache')
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.get('/api/v1/live')
    def live():
        return {'status': 'alive'}

    @app.get('/api/v1/ready')
    def ready():
        from .health import worker_snapshot
        from .migrations import SchemaError, require_current
        try:
            with engine.connect() as c:
                require_current(c)
                c.execute(select(sessions.c.owner).limit(1)).first()
            workers = worker_snapshot(engine)
        except SchemaError:
            return JSONResponse({'status': 'unavailable', 'database': 'migration_required'},
                status_code=503, headers={'Retry-After': '2'})
        except SQLAlchemyError:
            return JSONResponse({'status': 'unavailable', 'database': 'unavailable'},
                status_code=503, headers={'Retry-After': '2'})
        return JSONResponse({'status': workers['status'], 'database': 'ready', 'workers': workers},
            status_code=200 if workers['active'] else 503,
            headers={} if workers['active'] else {'Retry-After': '2'})

    @app.get('/api/v1/system/gpu')
    def gpu_metrics(request: Request):
        auth(request)
        return app.state.gpu_telemetry.snapshot()

    @app.get("/api/v1/health")
    def health():
        try:
            with engine.connect() as c:
                c.execute(select(sessions.c.owner).limit(1)).first()
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ready", "analysis": "screw-and-sticker-inspection", "sop": "retired",
                "realtime": app.state.realtime.status_snapshot(),
                "capabilities": {"personDetection": True, "poseEstimation": True,
                                 "actionSequence": False, "productionSop": False, "screwInspection": True,
                                 "stickerInspection": True, "selectableInspectionTasks": True,
                                 "sceneTasks": True, "modelManagement": True, "industrialTraining": True, "station1AssistedWorkflow": True,
                                 "assistedStations": list(range(1, 7))},
                "limits": {"max_upload_bytes": settings.max_upload_bytes, "max_video_seconds": settings.max_video_seconds}}

    @app.post("/api/v1/login")
    def login(body: Login, request: Request):
        # FastAPI runs this synchronous route in its worker pool, including the
        # entire transaction. The limiter's reservation lasts until hashing ends.
        peer = request.client.host if request.client else "unknown"
        with login_limiter.attempt(peer) as attempt:
            candidate = hashlib.pbkdf2_hmac("sha256", body.password.encode(), salt, 200000)
            attempt.success = (hmac.compare_digest(candidate, password_key) &
                               hmac.compare_digest(body.username.encode(), settings.username.encode()))
            if not attempt.success:
                raise HTTPException(401, "用户名或密码错误")
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with engine.begin() as c:
            expired = select(sessions.c.token_hash).where(sessions.c.expires_at <= now(c)).order_by(sessions.c.expires_at).limit(100)
            c.execute(delete(sessions).where(sessions.c.token_hash.in_(expired)))
            c.execute(sessions.insert().values(token_hash=digest(token), owner=settings.username, csrf=csrf, expires_at=now(c) + settings.session_seconds))
        response = JSONResponse({"username": settings.username, "csrf": csrf})
        response.set_cookie("aisop_session", token, httponly=True, secure=settings.secure_cookie, samesite="strict", max_age=settings.session_seconds, path="/")
        response.delete_cookie('aisop_terminal', path='/')
        return response

    @app.get("/api/v1/session")
    def session(request: Request):
        user = auth(request)
        return {"username": user["owner"], "csrf": user["csrf"],
                **({'role': 'terminal', 'binding': user['binding']} if user.get('role') == 'terminal' else {})}

    @app.post("/api/v1/logout")
    def logout(request: Request):
        needs_check = False
        try:
            auth(request)
        except HTTPException as exc:
            # Expired cookies can be removed; live sessions still require CSRF.
            if exc.status_code != 401:
                raise
            needs_check = True
        with engine.begin() as c:
            token_hash = digest(request.cookies.get('aisop_session', ''))
            if needs_check:
                # An invalid terminal cookie must not bypass CSRF for a still
                # valid admin cookie supplied alongside it.
                current = c.execute(select(sessions).where(sessions.c.token_hash == token_hash,
                    sessions.c.expires_at > now(c))).mappings().first()
                if current and not hmac.compare_digest(request.headers.get('x-csrf-token', ''), current['csrf']):
                    raise HTTPException(403, {'code': 'SESSION_CSRF_INVALID', 'message': '会话已更新，请同步登录状态后重试'})
            c.execute(delete(sessions).where(sessions.c.token_hash == token_hash))
        response = JSONResponse({"ok": True})
        response.delete_cookie("aisop_session", path="/")
        return response

    @app.post("/api/v1/media", status_code=201)
    async def upload(request: Request, filename: str):
        user = await run_in_threadpool(auth, request)
        name = filename.replace("\\", "/").split("/")[-1][:200]
        suffix = Path(name).suffix.lower()
        if suffix not in [".mp4", ".webm", ".mov", ".avi", ".m4v"]:
            raise HTTPException(415, "支持MP4、WebM、MOV、AVI视频")
        declared_length = request.headers.get("content-length")
        if declared_length and declared_length.isdecimal() and int(declared_length) > settings.max_upload_bytes:
            raise HTTPException(413, "视频超过服务器上传限制")
        mid = str(uuid.uuid4())
        tmp = settings.data_dir / "uploads" / (mid + ".part")
        final = tmp.with_suffix(suffix)
        length, sha = 0, hashlib.sha256()

        def publish_upload():
            # Keep publication, the transaction and lost-ack reconciliation in
            # one worker. Once it owns the files, a disconnected HTTP request
            # must not delete a file while its database commit is in flight.
            try:
                tmp.replace(final)
                with engine.begin() as c:
                    c.execute(media.insert().values(id=mid, owner=user["owner"], name=name, suffix=suffix,
                        size=length, sha256=sha.hexdigest(), metadata=info, created_at=now(c)))
                return {"id": mid, "name": name, "metadata": info, "status": "READY"}
            except SQLAlchemyError:
                # A COMMIT acknowledgement may be lost after persistence.
                # Retain the final upload for reconciliation/retention cleanup.
                tmp.unlink(missing_ok=True)
                try:
                    with engine.connect() as c:
                        saved = c.execute(select(media.c.id).where(media.c.id == mid)).first()
                    if saved:
                        return {"id": mid, "name": name, "metadata": info, "status": "READY"}
                except SQLAlchemyError:
                    pass
                raise
            except BaseException:
                tmp.unlink(missing_ok=True)
                final.unlink(missing_ok=True)
                raise

        publishing = False
        try:
            with tmp.open("xb") as f:
                async for chunk in request.stream():
                    length += len(chunk)
                    if length > settings.max_upload_bytes:
                        raise HTTPException(413, "视频超过服务器上传限制")
                    await run_in_threadpool(f.write, chunk)
                    sha.update(chunk)
                await run_in_threadpool(f.flush)
                await run_in_threadpool(os.fsync, f.fileno())
            if not length:
                raise HTTPException(400, "视频文件为空")
            from algorithms.video import probe, AnalysisError
            try:
                info = await run_in_threadpool(probe, tmp, settings.max_video_seconds)
            except AnalysisError as exc:
                raise HTTPException(422, str(exc)) from exc
            publishing = True
            return await run_in_threadpool(publish_upload)
        except BaseException:
            if not publishing:
                tmp.unlink(missing_ok=True)
                final.unlink(missing_ok=True)
            raise

    @app.get('/api/v1/admin/model-candidates')
    def model_candidates(request: Request):
        owner = auth(request)['owner']
        return {'items': app.state.scenes.candidates(owner),
                'limits': {'max_upload_bytes': settings.max_upload_bytes},
                'locations': ['服务器 models/', '服务器 outputs/', 'DATA_DIR/model-imports/']}

    @app.get('/api/v1/admin/models')
    def list_models(request: Request):
        return {'items': app.state.scenes.models(auth(request)['owner'])}

    @app.get('/api/v1/admin/catalog')
    def management_catalog(request: Request):
        return app.state.scenes.catalog(auth(request)['owner'])

    @app.post('/api/v1/admin/models/import', status_code=201)
    def import_model(body: ModelImport, request: Request):
        return app.state.scenes.import_model(body, auth(request)['owner'], request.headers.get('Idempotency-Key'))

    @app.get('/api/v1/admin/model-imports/{key}')
    def model_import_result(key: uuid.UUID, request: Request):
        owner = auth(request)['owner']
        return app.state.scenes.public_model(app.state.scenes._model(import_identity(owner, str(key)), owner))

    @app.post('/api/v1/admin/models/upload', status_code=201)
    async def upload_model(request: Request, name: str = Query(min_length=1, max_length=100),
                           version: str = Query('1.0', min_length=1, max_length=60),
                           filename: str = Query(min_length=1, max_length=255),
                           notes: str = Query('', max_length=1000)):
        user = await run_in_threadpool(auth, request)
        if 'X-Model-Notes' in request.headers:
            try:
                encoded = request.headers['X-Model-Notes']
                if len(encoded) > 6000:
                    raise ValueError()
                notes = base64.b64decode(encoded, validate=True).decode('utf-8')
                if len(notes) > 1000:
                    raise ValueError()
            except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
                raise HTTPException(422, '模型说明编码无效或超过1000字符') from exc
        if Path(filename.replace('\\', '/')).suffix.lower() != '.pt':
            raise HTTPException(415, '当前支持可信来源的 Ultralytics .pt 模型')
        if not name.strip() or not version.strip():
            raise HTTPException(422, '模型名称和版本不能为空')
        length = request.headers.get('content-length', '')
        if length.isdecimal() and int(length) > settings.max_upload_bytes:
            raise HTTPException(413, '模型文件超过上传限制')
        identity = import_identity(user['owner'], request.headers.get('Idempotency-Key'))
        destination = app.state.scenes.directory / ((str(uuid.uuid4()) if request.headers.get('Idempotency-Key') else identity) + '.pt')
        temporary = destination.with_suffix('.part')
        size = 0
        try:
            with temporary.open('xb') as handle:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > settings.max_upload_bytes:
                        raise HTTPException(413, '模型文件超过上传限制')
                    await run_in_threadpool(handle.write, chunk)
            if not size:
                raise HTTPException(422, '模型文件为空')
            temporary.replace(destination)
            result = await run_in_threadpool(app.state.scenes.register, identity, destination,
                user['owner'], name.strip(), version.strip(), notes.strip())
            if result.get('reused'):
                destination.unlink(missing_ok=True)
            return result
        except SQLAlchemyError:
            temporary.unlink(missing_ok=True)
            raise
        except BaseException:
            temporary.unlink(missing_ok=True)
            destination.unlink(missing_ok=True)
            raise

    @app.patch('/api/v1/admin/models/{model_id}')
    def toggle_model(model_id: uuid.UUID, body: ModelToggle, request: Request):
        return app.state.scenes.toggle(model_id, body.enabled, auth(request)['owner'])

    @app.get('/api/v1/admin/scenes')
    def admin_scenes(request: Request):
        return {'items': app.state.scenes.scenes(auth(request)['owner'])}

    @app.post('/api/v1/admin/scenes', status_code=201)
    def create_scene(body: SceneDefinition, request: Request):
        return app.state.scenes.save(body, auth(request)['owner'], request_key=request.headers.get('Idempotency-Key'))

    @app.put('/api/v1/admin/scenes/{scene_id}')
    def update_scene(scene_id: uuid.UUID, body: SceneDefinition, request: Request):
        if body.revision is None:
            raise HTTPException(422, '修改任务需要当前版本号')
        return app.state.scenes.save(body, auth(request)['owner'], scene_id)

    @app.get('/api/v1/scenes')
    def scene_catalog():
        return {'items': app.state.scenes.scenes()}

    @app.get('/api/v1/scenes/{scene_id}')
    def get_scene(scene_id: uuid.UUID):
        return app.state.scenes.scene(scene_id)

    @app.post("/api/v1/analysis-runs", status_code=202)
    def create_run(body: RunRequest, request: Request):
        user = auth(request)
        key = request.headers.get("idempotency-key", "")
        if not 1 <= len(key) <= 100:
            raise HTTPException(400, "缺少有效Idempotency-Key")
        options = body.options.model_dump()
        mid = str(body.media_id)
        rhash = request_hash(mid, options)
        with engine.connect() as c:
            old = c.execute(select(runs).where(runs.c.owner == user["owner"], runs.c.request_key == key)).mappings().first()
            source = c.execute(select(media).where(media.c.id == mid, media.c.owner == user["owner"])).mappings().first()
        if old:
            if old["request_hash"] != rhash:
                raise HTTPException(409, "同一幂等键对应了不同请求")
            return public_run(old)
        if not source:
            raise HTTPException(404, "媒体不存在")
        from algorithms.video import validate_options, file_hash, AnalysisError
        if options.get('analysis_type') != 'scene':
            try:
                validate_options(source["metadata"], **{k: options[k] for k in ["start", "end", "sample_fps", "crop", "imgsz"]})
            except AnalysisError as exc:
                raise HTTPException(422, str(exc)) from exc
        options["pose_backend"] = options.get("pose_backend") or settings.pose_backend
        if options.get('analysis_type') == 'scene':
            if not options.get('scene_id') or any(options.get(k) for k in ('crop','station_id','workflow_id')):
                raise HTTPException(422, '场景检测需要选择任务，不能叠加旧工位或裁切')
            frozen = app.state.scenes.snapshot(options['scene_id'], user['owner'])
            options['scene_config'] = frozen
            options['sample_fps'] = min(frozen['config']['sample_fps'], source['metadata']['fps'])
            options['imgsz'] = frozen['config']['imgsz']
            try:
                validate_options(source['metadata'], **{k: options[k] for k in ['start','end','sample_fps','crop','imgsz']})
            except AnalysisError as exc:
                raise HTTPException(422, str(exc)) from exc
            model_hash = frozen['model']['sha256']
        elif options.get('scene_id'):
            raise HTTPException(422, 'scene_id 只能用于场景检测')
        elif options.get('analysis_type') in ('screw','sticker'):
            task=options['analysis_type']
            if task=='sticker' and options['sample_fps']<2:
                raise HTTPException(422,'贴条连续视觉确认至少需要2FPS采样')
            if options.get('crop') or options.get('workflow_id') or options.get('station_id'):
                raise HTTPException(422, '专项检测使用独立工件标定，请勿选择旧工位、动作流程或裁切')
            if task=='screw':
                from algorithms.screws import snapshot as task_snapshot
            else:
                from algorithms.stickers import snapshot as task_snapshot
            try:
                options[task+'_config'] = task_snapshot()
            except AnalysisError as exc:
                raise HTTPException(503, str(exc)) from exc
            frozen=options[task+'_config']
            model_hash = frozen['assets'][frozen['config']['model']]
            from algorithms.person_detection import snapshot as person_snapshot
            try:
                options['person_config'] = person_snapshot(settings.model_path, options['pose_backend'])
            except Exception as exc:
                name = '贴条' if task=='sticker' else '螺丝'
                raise HTTPException(503, name+'检测需要人体姿态模型，请检查模型文件与依赖') from exc
        elif options["pose_backend"] == "yolo":
            if not settings.model_path.is_file():
                raise HTTPException(503, "服务器模型文件尚未配置")
            model_hash = file_hash(settings.model_path)
        else:
            try:
                from algorithms.rtmlib_pose import model_files
                model_hash = file_hash(model_files(options["pose_backend"])[-1][1])
            except Exception as exc:
                raise HTTPException(503, "rtmlib 姿态模型不可用，请检查依赖与权重缓存") from exc
        if options.get('workflow_id'):
            if options['sample_fps'] < 2:
                raise HTTPException(422, '动作时序检测至少需要2fps采样')
            from algorithms.action_sop import snapshot as action_snapshot
            try:
                options['action_workflow'] = action_snapshot(options['workflow_id'])
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
        options["title"] = source["name"]
        if options.get('station_id'):
            if options.get('crop') is not None:
                raise HTTPException(422, '工位ROI基于完整画面，不能叠加视频裁切')
            from algorithms.stations import snapshot
            try:
                options['inspection_config'] = snapshot(options['station_id'])
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
        rid = str(uuid.uuid4())
        try:
            with serialized_write(engine) as c:
                # Retention may expire media during model/options validation.
                available = c.execute(select(media.c.id).where(media.c.id == mid,
                    media.c.owner == user['owner']).with_for_update()).first()
                if not available:
                    raise HTTPException(404, '媒体已过期或被清理，请重新上传')
                c.execute(runs.insert().values(id=rid, owner=user["owner"], media_id=mid, status="QUEUED", request_key=key,
                    request_hash=rhash, options=options, model_hash=model_hash, progress=0,
                    attempts=0, token=0, created_at=now(c), updated_at=now(c)))
        except IntegrityError:
            with engine.connect() as c:
                old = c.execute(select(runs).where(runs.c.owner == user["owner"], runs.c.request_key == key)).mappings().one()
            if old["request_hash"] != rhash:
                raise HTTPException(409, "幂等请求冲突")
            return public_run(old)
        return public_run(owned_run(rid, user["owner"]))

    @app.post("/api/v1/realtime/frame")
    async def realtime_frame(request: Request, station_id: str | None = None, test_algorithms: str | None = None,
                             workflow_id: str | None = Query(None, max_length=40), sequential: bool = True,
                             hands: bool = False, analysis_type: Literal['pose','screw','sticker','scene'] = 'pose',
                             scene_id: uuid.UUID | None = None):
        user = await run_in_threadpool(auth, request)
        frame_id = request.headers.get('x-frame-id')
        captured_at = request.headers.get('x-captured-at')
        if frame_id is not None and (not frame_id or len(frame_id) > 128 or any(ord(c) < 32 for c in frame_id)):
            raise HTTPException(422, '采样编号需要为1–128字符')
        if captured_at is not None:
            try:
                captured_at = float(captured_at)
                if not math.isfinite(captured_at) or captured_at < 0:
                    raise ValueError()
            except (ValueError, TypeError) as exc:
                raise HTTPException(422, '采集时间需要为有效毫秒时间戳') from exc

        async def predict(function, *args, **kwargs):
            if stream_id:
                service = {'pose': app.state.realtime, 'screw': app.state.screw_realtime,
                           'sticker': app.state.sticker_realtime, 'scene': app.state.scene_realtime}[analysis_type]
                registry = service.registry if analysis_type in ('screw', 'sticker') else service.streams
                key = (user['token_hash'], stream_id)
                registry.check_limits(key)
            result = await run_in_threadpool(function, *args, **kwargs)
            # Epoch values are diagnostic only: clients measure their own capture-to-display
            # latency with a monotonic clock, without assuming synchronized device clocks.
            result = dict(result)
            if stream_id:
                service = {'pose': app.state.realtime, 'screw': app.state.screw_realtime,
                           'sticker': app.state.sticker_realtime, 'scene': app.state.scene_realtime}[analysis_type]
                registry = service.registry if analysis_type in ('screw', 'sticker') else service.streams
                key = (user['token_hash'], stream_id)
                registry.record_sample(key, timestamp)
                registry.observe_limits(key, analysis_type, result)
                result = registry.protect_result(key, result)
                proof = await run_in_threadpool(save_evidence, user, stream_id, analysis_type,
                    timestamp, bytes(payload), result)
                if proof:
                    result['evidence'] = proof
            result.update(frameId=frame_id, capturedAt=captured_at,
                serverReceivedAt=request.state.realtime_received_at,
                serverRespondedAt=time.time()*1000,
                serverProcessingMs=round((time.perf_counter()-request.state.realtime_started)*1000, 2))
            return result

        stream_id, timestamp = request.headers.get('x-stream-id'), request.headers.get('x-frame-time')
        extra = {}
        if stream_id is not None or timestamp is not None:
            try:
                stream_id = str(uuid.UUID(stream_id))
                timestamp = float(timestamp)
                if not math.isfinite(timestamp) or not 0 <= timestamp <= 86400:
                    raise ValueError()
            except (TypeError, ValueError, AttributeError) as exc:
                raise HTTPException(422, '实时流需要有效UUID和0–86400秒采样时间戳') from exc
            extra = {'stream_key': (user['token_hash'], stream_id), 'frame_time': timestamp, 'sequential': sequential}
        if analysis_type in ('screw','sticker','scene') and (not stream_id or station_id or test_algorithms or workflow_id):
            raise HTTPException(422,'专项实时检测需要独立连续流，不能混用旧工位或动作流程')
        if (analysis_type == 'scene') != (scene_id is not None):
            raise HTTPException(422, '场景检测必须携带对应的 scene_id')
        if workflow_id:
            if not stream_id or test_algorithms:
                raise HTTPException(422, '动作时序需要连续实时流，且不能与算法测试混用')
            from algorithms.action_sop import snapshot as action_snapshot
            try:
                extra['workflow'] = await run_in_threadpool(action_snapshot, workflow_id)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
        if test_algorithms is not None:
            names = test_algorithms.split(',')
            if station_id or not names or len(names)>3 or len(set(names))!=len(names) or any(n not in {'raise','grip','objects'} for n in names):
                raise HTTPException(422, '请选择有效测试算法，且不能与生产工位混用')
        if request.headers.get("content-type", "").split(";")[0] != "image/jpeg":
            raise HTTPException(415, "仅支持JPEG采样画面")
        length = request.headers.get('content-length')
        if length is not None:
            try:
                if int(length) < 0:
                    raise ValueError()
                if int(length) > 1024 * 1024:
                    raise HTTPException(413, '实时画面不能超过1 MiB')
            except ValueError as exc:
                raise HTTPException(422, '画面长度无效') from exc
        payload = bytearray()

        async def read_frame():
            async for chunk in request.stream():
                if len(payload) + len(chunk) > 1024 * 1024:
                    raise HTTPException(413, "实时画面不能超过1 MiB")
                payload.extend(chunk)
        try:
            await asyncio.wait_for(read_frame(), timeout=5)
        except asyncio.TimeoutError as exc:
            raise HTTPException(408, '实时画面传输超时，请发送最新采样') from exc
        if analysis_type=='scene':
            return await predict(app.state.scene_realtime.predict, bytes(payload),
                scene_id=str(scene_id), stream_key=(user['token_hash'],stream_id), frame_time=timestamp, owner=user['owner'])
        if analysis_type=='screw':
            return await predict(app.state.screw_realtime.predict,bytes(payload),
                stream_key=(user['token_hash'],stream_id),frame_time=timestamp)
        if analysis_type=='sticker':
            return await predict(app.state.sticker_realtime.predict,bytes(payload),
                stream_key=(user['token_hash'],stream_id),frame_time=timestamp)
        if test_algorithms:
            return await predict(app.state.realtime.predict,bytes(payload),None,names,**extra)
        if station_id:
            from algorithms.stations import snapshot
            try:
                frozen = await run_in_threadpool(snapshot, station_id)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            return await predict(app.state.realtime.predict, bytes(payload), frozen, hands=hands, **extra)
        return await predict(app.state.realtime.predict, bytes(payload), hands=hands, **extra)

    def save_evidence(user, stream, kind, stamp, image, result, final=False):
        try:
            return app.state.evidence.observe(user['owner'], user['token_hash'], str(stream),
                kind, stamp, image, result, final=final)
        except (OSError, SQLAlchemyError, ValueError) as exc:
            logging.getLogger(__name__).exception('Alert evidence could not be acknowledged')
            app.state.evidence.last_error = type(exc).__name__
            return {'status': 'UNAVAILABLE', 'message': '取证存储暂不可用，请检查告警归档状态'}

    @app.delete('/api/v1/realtime/streams/{stream_id}')
    def release_stream(stream_id: uuid.UUID, request: Request):
        user = auth(request)
        with app.state.realtime_transport.control(user['token_hash'], str(stream_id)):
            app.state.realtime.release((user['token_hash'], str(stream_id)))
            app.state.screw_realtime.release((user['token_hash'], str(stream_id)))
            app.state.sticker_realtime.release((user['token_hash'], str(stream_id)))
            app.state.scene_realtime.release((user['token_hash'], str(stream_id)))
            app.state.evidence.release(user['token_hash'], str(stream_id))
            app.state.realtime_transport.forget(user['token_hash'], str(stream_id))
        return {'ok': True}

    @app.post('/api/v1/realtime/streams/{stream_id}/finish')
    def finish_screw_stream(stream_id: uuid.UUID,request: Request,
                            body: FinishRealtimeRequest | None = None,
                            analysis_type: Literal['screw','sticker','scene']='screw'):
        user=auth(request)
        gaps = [gap.model_dump() for gap in body.evidenceGaps] if body else []
        if any(gap['end'] < gap['start'] for gap in gaps):
            raise HTTPException(422, '证据缺口结束时间不能早于开始时间')
        service=app.state.scene_realtime if analysis_type=='scene' else app.state.sticker_realtime if analysis_type=='sticker' else app.state.screw_realtime
        key = (user['token_hash'], str(stream_id))
        with app.state.realtime_transport.control(*key, kinds=(analysis_type,)):
            registry = service.streams if analysis_type == 'scene' else service.registry
            try:
                registry.check_limits(key)
            except HTTPException as exc:
                if exc.status_code != 410:
                    raise
                # Preserve the last evidence, while the expired round is REVIEW.
            result = service.finish(key)
            result = registry.protect_result(key, result, client_gaps=gaps, final=True)
            proof = save_evidence(user, stream_id, analysis_type, 0, None, result, final=True)
            return dict(result, evidence=proof) if proof else result

    @app.get('/api/v1/stations')
    def station_catalog():
        from algorithms.stations import profiles
        return {'items':[{'id':s.id,'name':s.name,'version':s.version,'approved':s.approved,
                          'checks':[{'id':c.id,'name':c.name,'expected':c.expected,
                                     'configured':bool(c.targets) and s.approved and s.fixed_fixture}
                                    for c in s.checks]} for s in profiles()]}

    @app.get('/api/v1/action-workflows')
    def action_workflows():
        from algorithms.action_sop import catalog
        return {'items': [workflow.model_dump(mode='json') for workflow in catalog()]}

    @app.get('/api/v1/screw-model')
    def screw_model():
        from algorithms.screws import CONFIG, sequence_profiles
        config = json.loads(CONFIG.read_text(encoding='utf-8'))
        sequences = sequence_profiles(config)
        result_path = ROOT/config.get('training_result',
            'outputs/screw-inspection-2026-10-17/refinement/training-result.json'
            if config.get('algorithm')=='local-roi-v2' else 'outputs/screw-inspection-2026-10-17/training-result.json')
        training = json.loads(result_path.read_text(encoding='utf-8')) if result_path.is_file() else None
        pointer = ROOT/'.runtime/screw-reference-run.json'
        reference_run = json.loads(pointer.read_text(encoding='utf-8')).get('id') if pointer.is_file() else None
        from algorithms.skeleton import topology
        person_status=app.state.realtime.status_snapshot()
        return {'name': config['name'], 'version': config['version'], 'targets': config['targets'],
                'scope': config['scope'], 'ready': (ROOT/config['model']).is_file(), 'training': training,
                'referenceRun': reference_run,'modelName':config.get('model_name','YOLO11n 螺丝专项 v1'),
                'detectionClasses':['screw','person'],'skeleton':topology(),
                'personModelName':person_status['model'],
                'sequence': config.get('sequence'), 'sequences': sequences,
                'verificationMode': config.get('verification_mode','visual-cumulative'),
                'preassembled': [target['id'] for target in config['targets'] if target.get('preassembled')],
                'operationCount': sum(not target.get('preassembled') for target in config['targets'])}

    @app.get('/api/v1/sticker-model')
    def sticker_model():
        from algorithms.stickers import CONFIG
        from algorithms.skeleton import topology
        if not CONFIG.is_file():raise HTTPException(503,'贴条模型正在准备，尚未启用')
        config=json.loads(CONFIG.read_text(encoding='utf-8'))
        return {'name':config['name'],'version':config['version'],'targets':config['targets'],
                'scope':config['scope'],'ready':all((ROOT/config[k]).is_file() for k in ('model','reference')+ (('material_model',) if config.get('material_model') else ())),
                'modelName':config['model_name'],'mode':config.get('mode','appearance'),'sequence':config.get('sequence',{}),
                'verificationMode':config.get('verification_mode','temporal-actions' if config.get('mode')=='action' else 'placement-order'),
                'detectionClasses':['sticker','person'],'skeleton':topology(),
                'personModelName':app.state.realtime.status_snapshot()['model']}

    @app.get('/api/v1/pose-model')
    def pose_model():
        from algorithms.skeleton import topology
        status = app.state.realtime.status_snapshot()
        return {'name': '人体检测与骨骼', 'version': 1, 'mode': 'pose',
                'modelName': status['model'], 'ready': settings.model_path.is_file() or status['state'] == 'loaded',
                'offlineBackend': settings.pose_backend, 'realtimeBackend': settings.realtime_backend,
                'scope': '仅检测人体，绘制17点骨骼、头颈轮廓并输出姿态规则',
                'detectionClasses': ['person'], 'skeleton': topology(), 'targets': []}

    @app.get('/api/v1/inspection-tasks')
    def inspection_tasks():
        tasks=[]
        for identity,name,rule in [('screw','螺丝安装检测','逐位置核对螺丝安装，同时检测人体和17点骨骼'),
                                   ('sticker','贴条工序检测','先完成两处三角贴放/按压，再完成方形动作，同时检测人体和17点骨骼')]:
            path=ROOT/f'config/{"screw" if identity=="screw" else "sticker"}-inspection.json'
            config=json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
            tasks.append({'id':identity,'name':name,'rule':rule,'ready':bool(config) and
                          all((ROOT/config[k]).is_file() for k in ('model','reference')+(('material_model',) if config.get('material_model') else ()))})
        tasks.append({'id': 'pose', 'name': '人体检测与骨骼',
                      'rule': '人体框、17点骨骼与姿态规则', 'ready': pose_model()['ready']})
        return {'items':tasks,'default':'screw'}

    @app.get("/api/v1/analysis-runs")
    def list_runs(request: Request, limit: int = Query(100, ge=1, le=100), offset: int = Query(0, ge=0),
                  status: str | None = Query(None, pattern="^(QUEUED|RUNNING|CANCELLING|CANCELLED|FAILED|SUCCEEDED)$"),
                  q: str = Query("", max_length=200), cursor: str | None = Query(None, max_length=200),
                  analysis_type: Literal['pose','screw','sticker','scene'] | None = None,
                  scene_id: uuid.UUID | None = None):
        user = auth(request)
        conditions = [runs.c.owner == user["owner"]]
        if status:
            conditions.append(runs.c.status == status)
        if analysis_type:
            conditions.append(option_field('analysis_type') == analysis_type)
        if scene_id:
            conditions.append(option_field('scene_id') == str(scene_id))
        if q.strip():
            conditions.append(media.c.name.icontains(q.strip(), autoescape=True) | runs.c.id.icontains(q.strip(), autoescape=True))
        if cursor:
            created_at, run_id = _decode_cursor(cursor)
            conditions.append(or_(runs.c.created_at < created_at,
                                  and_(runs.c.created_at == created_at, runs.c.id < run_id)))
        source = runs.outerjoin(media, (media.c.id == runs.c.media_id) & (media.c.owner == runs.c.owner))
        with engine.connect() as c:
            total = None if cursor else c.execute(select(func.count()).select_from(source).where(*conditions)).scalar_one()
            # Project only the short fields shown by history. Frozen model/ROI
            # configurations remain available on the detail/worker path.
            columns = [runs.c[name] for name in ('id', 'media_id', 'status', 'progress', 'attempts',
                'error_code', 'error_message', 'created_at', 'updated_at')]
            options = runs.c.options
            kind = option_field('analysis_type')
            model_name = func.coalesce(options['scene_config']['model']['name'].as_string(),
                options['screw_config']['config']['model_name'].as_string(),
                options['sticker_config']['config']['model_name'].as_string())
            query = select(*columns, media.c.name.label('media_name'), kind.label('analysis_type'),
                options['scene_id'].as_string().label('scene_id'),
                options['scene_config']['config']['name'].as_string().label('scene_name'),
                model_name.label('model_name')).select_from(source).where(*conditions)
            rows = list(c.execute(query.order_by(runs.c.created_at.desc(), runs.c.id.desc())
                                  .limit(limit + 1).offset(0 if cursor else offset)).mappings())
            has_more = len(rows) > limit
            rows = rows[:limit]
            next_cursor = _encode_cursor(rows[-1]["created_at"], rows[-1]["id"]) if has_more and rows else None
            return {"items": [{**public_run(row), "media_name": row["media_name"],
                    'analysis_type':row['analysis_type'] or 'pose',
                    'scene_id':row['scene_id'], 'scene_name':row['scene_name'],
                    'model_name':row['model_name'] or ('YOLO11n 螺丝专项 v1' if row['analysis_type']=='screw' else '历史分析模型')}
                    for row in rows],
                    "total": total, "limit": limit, "offset": offset, "next_cursor": next_cursor}

    @app.get("/api/v1/analysis-runs/{run_id}")
    def get_run(run_id: uuid.UUID, request: Request):
        return public_run(owned_run(str(run_id), auth(request)["owner"]))

    @app.get("/api/v1/analysis-runs/{run_id}/wait")
    async def wait_run(run_id: uuid.UUID, request: Request, since: float = Query(0, ge=0),
                       timeout: int = Query(25, ge=1, le=30)):
        owner = (await run_in_threadpool(auth, request))["owner"]
        deadline = time.monotonic() + timeout
        while True:
            row = await run_in_threadpool(owned_run, str(run_id), owner)
            if row["status"] in ("CANCELLED", "FAILED", "SUCCEEDED") or row["updated_at"] > since:
                return public_run(row)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return public_run(row)
            if await request.is_disconnected():
                return Response(status_code=204)
            await asyncio.sleep(min(.5, remaining))

    @app.get("/api/v1/metrics")
    def metrics(request: Request):
        owner = auth(request)["owner"]
        cutoff = time.time() - 3600
        with engine.connect() as c:
            status_rows = c.execute(select(runs.c.status, func.count()).where(runs.c.owner == owner)
                                    .group_by(runs.c.status)).all()
            metric_rows = c.execute(select(run_metrics).join(runs, runs.c.id == run_metrics.c.run_id)
                                    .where(runs.c.owner == owner)
                                    .order_by(run_metrics.c.finished_at.desc()).limit(512)).mappings().all()
            recent_success = c.execute(select(func.count()).select_from(runs).where(
                runs.c.owner == owner, runs.c.status == "SUCCEEDED", runs.c.updated_at >= cutoff)).scalar_one()
            failures = c.execute(select(runs.c.error_code, func.count()).where(
                runs.c.owner == owner, runs.c.status == "FAILED").group_by(runs.c.error_code)
                .order_by(func.count().desc()).limit(10)).all()
        def p95(values):
            values = sorted(v for v in values if v is not None)
            return round(values[min(len(values) - 1, int((len(values) - 1) * .95))], 2) if values else 0
        return {"runs": dict(status_rows), "throughputLastHour": recent_success,
                "failureReasons": [{"code": code or "UNKNOWN", "count": count} for code, count in failures],
                "queueWaitP95Seconds": p95([r["queue_wait_seconds"] for r in metric_rows]),
                "processingP95Seconds": p95([r["processing_seconds"] for r in metric_rows]),
                "inferenceP95Ms": p95([r["inference_p95_ms"] for r in metric_rows]),
                "peakMemoryMB": p95([r["peak_memory_mb"] for r in metric_rows]),
                "peakGpuMemoryMB": p95([r["peak_gpu_memory_mb"] for r in metric_rows]),
                "inferenceRuntime": app.state.inference_runtime,
                "realtimeLimits": {"maxDurationSeconds": settings.realtime_max_duration_seconds,
                                   "noTargetTimeoutSeconds": settings.realtime_no_target_seconds},
                "board": app.state.board.metrics(),
                "evidenceStorage": app.state.evidence.snapshot(owner),
                "streamStates": {kind: registry.snapshot() for kind, registry in (
                    ('pose', app.state.realtime.streams), ('screw', app.state.screw_realtime.registry),
                    ('sticker', app.state.sticker_realtime.registry), ('scene', app.state.scene_realtime.streams))},
                "realtime": app.state.realtime.metrics_snapshot(),
                "sceneModelCache": app.state.scene_realtime.cache_metrics(),
                "specialistPools": {kind: specialist_snapshot(service) for kind, service in (
                    ('screw', app.state.screw_realtime), ('sticker', app.state.sticker_realtime))},
                "realtimeTransport": app.state.realtime_transport.snapshot()}

    @app.post("/api/v1/analysis-runs/{run_id}/cancel")
    def cancel(run_id: uuid.UUID, request: Request):
        owner = auth(request)["owner"]
        owned_run(str(run_id), owner)
        with engine.begin() as c:
            c.execute(update(runs).where(runs.c.id == str(run_id), runs.c.status == "QUEUED").values(status="CANCELLED", updated_at=now(c)))
            c.execute(update(runs).where(runs.c.id == str(run_id), runs.c.status == "RUNNING").values(status="CANCELLING", updated_at=now(c)))
        return public_run(owned_run(str(run_id), owner))

    @app.api_route("/api/v1/analysis-runs/{run_id}/artifacts/{name}", methods=["GET", "HEAD"])
    def artifact(run_id: uuid.UUID, name: str, request: Request):
        row = owned_run(str(run_id), auth(request)["owner"])
        if row["status"] != "SUCCEEDED" or not row["result_dir"]:
            raise HTTPException(409, "分析尚未成功，不能读取未完成产物")
        import re
        if name not in ["analysis.json", "manifest.json", "report.html", "segments.csv", "video.webm"] and not re.fullmatch(r"evidence-[1-6]\.jpg", name):
            raise HTTPException(404, "产物不存在")
        path = settings.data_dir / "runs" / row["result_dir"] / name
        if not path.is_file():
            raise HTTPException(404, "产物文件缺失")
        if settings.artifact_accel_redirect:
            disposition = f'attachment; filename="{name}"' if name in ["analysis.json", "manifest.json", "segments.csv"] else "inline"
            return Response(status_code=200, headers={
                "X-Accel-Redirect": f"/_protected-runs/{row['result_dir']}/{name}",
                "Content-Disposition": disposition,
            })
        return FileResponse(path, filename=name if name in ["analysis.json", "manifest.json", "segments.csv"] else None)

    @app.api_route("/{path:path}", methods=["GET", "HEAD"])
    def static(path: str, request: Request):
        path = 'index.html' if path in ('', 'index.html') else path
        if path in {f'station-{number}' for number in range(1, 7)}:
            return RedirectResponse('/?task=station' + path[-1], status_code=307)
        if path == 'station-one.html' and request.query_params.get('embed') != '1':
            return RedirectResponse('/?task=station1', status_code=307)
        if path == 'training':
            path = 'training.html'
        if path == 'datasets':
            path = 'datasets.html'
        if path == 'admin':
            path = 'admin.html'
        if path == 'settings':
            path = 'settings.html'
        if path == 'terminal':
            path = 'terminal.html'
        if path == 'terminals':
            path = 'terminals.html'
        if path == 'board':
            path = 'board.html'
        if path == 'station-1':
            path = 'station-one.html'
        static_files = {
            'index.html', 'styles.css', 'app.js', 'screw-dashboard.js', 'screw-dashboard.css',
            'screw-inspection.html', 'screw-inspection.js', 'screw-inspection.css',
            'admin.html', 'admin.js', 'workspace.css', 'workspace-api.js', 'review-outbox.js', 'gpu-metrics.js',
            'training.html', 'training.js', 'training.css',
            'datasets.html', 'datasets.js', 'datasets.css',
            'scene-console.html', 'scene-console.js',
            'station-one.html', 'station-one.js', 'station-one.css',
            'pilot-dashboard.js', 'pilot-dashboard.css', 'scene-dashboard.js', 'station-detection.js', 'station-frame-worker.js', 'station-collector-control.js',
            'inspection-trigger.js', 'inspection-trigger.css',
            'evidence-history.js', 'evidence-history.css',
            'settings.html', 'settings.js', 'equipment-settings.js', 'equipment-settings.css',
            'terminal.html', 'terminal.js', 'terminal.css', 'terminals.html', 'terminals.js',
            'board.html', 'board.js', 'board.css', 'board-history.js', 'board-history.css', 'board-advanced.js', 'board-advanced.css',
        }
        if path not in static_files and not path.startswith("assets/"):
            raise HTTPException(404, "Not found")
        public_root = ROOT if path.startswith("assets/") else ROOT / "frontend"
        target = (public_root / path).resolve()
        boundary = (ROOT / "assets").resolve() if path.startswith("assets/") else public_root.resolve()
        if not target.is_relative_to(boundary) or not target.is_file():
            raise HTTPException(404, "Not found")
        no_store = {'Cache-Control': 'no-store, max-age=0'}
        if path.endswith('.html'):
            text = target.read_text(encoding='utf-8')
            if path == 'index.html':
                text = text.replace('src="assets/reference.mp4"', '')
                text = text.replace('<script src="app.js">', '<script>window.AISOP_SERVER=true;</script><script src="app.js">')
                if request.cookies.get('aisop_terminal'):
                    try:
                        terminal = app.state.terminals.authenticate(request, restrict=False)
                    except HTTPException as exc:
                        if exc.status_code != 401:
                            raise
                        response = RedirectResponse('/terminal', status_code=303)
                        response.delete_cookie('aisop_terminal', path='/')
                        return response
                    fixed = json.dumps(terminal['binding'], ensure_ascii=True).replace('<', '\\u003c')
                    text = text.replace('<script src="app.js">', f'<script>window.AISOP_TERMINAL={fixed};</script><script src="app.js">')
            for asset in ('app.js', 'styles.css', 'screw-dashboard.js', 'screw-dashboard.css',
                          'screw-inspection.js', 'screw-inspection.css', 'admin.js', 'workspace.css',
                          'workspace-api.js', 'review-outbox.js', 'gpu-metrics.js', 'training.js', 'training.css', 'scene-console.js', 'station-one.js', 'station-one.css',
                          'pilot-dashboard.js', 'pilot-dashboard.css', 'scene-dashboard.js', 'station-detection.js', 'station-collector-control.js',
                          'inspection-trigger.js', 'inspection-trigger.css', 'settings.js', 'equipment-settings.js', 'equipment-settings.css',
                          'evidence-history.js', 'evidence-history.css', 'terminal.js', 'terminal.css', 'terminals.js',
                          'board.js', 'board.css', 'board-history.js', 'board-history.css', 'board-advanced.js', 'board-advanced.css'):
                asset_path = ROOT / 'frontend' / asset
                if not asset_path.is_file():
                    continue
                revision = asset_revision(asset_path)
                for attribute in ('src', 'href'):
                    text = text.replace(f'{attribute}="{asset}"', f'{attribute}="{asset}?v={revision}"')
            return HTMLResponse(text, headers=no_store)
        headers = no_store if path.endswith(('.js', '.css')) else None
        return FileResponse(target, headers=headers)

    return app
