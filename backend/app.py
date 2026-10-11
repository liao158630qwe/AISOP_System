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
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select, update, or_, and_
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from .config import ROOT, Settings
from .db import digest, make_engine, media, now, request_hash, run_metrics, runs, sessions


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


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    media_id: uuid.UUID
    options: Options = Field(default_factory=Options)


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
        yield
        engine.dispose()
    app = FastAPI(title="AI-SOP Server", version="0.1.0", lifespan=lifespan,
                  docs_url=None if settings.environment == "production" else "/docs")
    app.state.engine, app.state.settings = engine, settings
    from .realtime import RealtimeDetector
    app.state.realtime = RealtimeDetector(settings.model_path, settings.realtime_backend, settings.realtime_pool_size)
    if settings.environment != "test" and settings.realtime_warmup:
        # 后台预热：避免摄像头首帧等待数秒的 ONNX CUDA 图编译。
        threading.Thread(target=app.state.realtime.warmup, name="realtime-warmup", daemon=True).start()
    for directory in ["uploads", "runs"]:
        (settings.data_dir / directory).mkdir(parents=True, exist_ok=True)
    # Derive once; compare derived keys to avoid storing passwords in sessions.
    salt = secrets.token_bytes(32)
    password_key = hashlib.pbkdf2_hmac("sha256", settings.password.encode(), salt, 200000)
    failures = {}

    def auth(request):
        token = request.cookies.get("aisop_session", "")
        with engine.connect() as c:
            session = c.execute(select(sessions).where(sessions.c.token_hash == digest(token), sessions.c.expires_at > now(c))).mappings().first()
        if not session:
            raise HTTPException(401, "请先登录")
        if request.method not in ["GET", "HEAD", "OPTIONS"] and not hmac.compare_digest(request.headers.get("x-csrf-token", ""), session["csrf"]):
            raise HTTPException(403, "会话校验失败，请重新登录")
        return session

    def owned_run(run_id, owner):
        with engine.connect() as c:
            row = c.execute(select(runs).where(runs.c.id == run_id, runs.c.owner == owner)).mappings().first()
        if not row:
            raise HTTPException(404, "任务不存在")
        return row

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return JSONResponse({"message": str(exc.detail)}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Do not echo request bodies (which may contain passwords) in errors.
        fields = [".".join(str(part) for part in error["loc"][1:]) or "请求内容" for error in exc.errors()]
        return JSONResponse({"message": "请求参数无效，请检查：" + "、".join(dict.fromkeys(fields))}, status_code=422)

    @app.middleware("http")
    async def no_cache_private(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.get("/api/v1/health")
    def health():
        try:
            with engine.connect() as c:
                c.execute(select(sessions.c.owner).limit(1)).first()
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ready", "analysis": "offline-pose", "sop": "not-implemented",
                "limits": {"max_upload_bytes": settings.max_upload_bytes, "max_video_seconds": settings.max_video_seconds}}

    @app.post("/api/v1/login")
    async def login(body: Login, request: Request):
        peer = request.client.host if request.client else "unknown"
        recent = [t for t in failures.get(peer, []) if t > time.monotonic() - 300]
        if len(recent) >= 10:
            raise HTTPException(429, "尝试次数过多，请稍后重试")
        candidate = await run_in_threadpool(hashlib.pbkdf2_hmac, "sha256", body.password.encode(), salt, 200000)
        if not hmac.compare_digest(candidate, password_key) or not hmac.compare_digest(body.username.encode(), settings.username.encode()):
            failures[peer] = recent + [time.monotonic()]
            raise HTTPException(401, "用户名或密码错误")
        failures.pop(peer, None)
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with engine.begin() as c:
            c.execute(delete(sessions).where(sessions.c.expires_at <= now(c)))
            c.execute(sessions.insert().values(token_hash=digest(token), owner=settings.username, csrf=csrf, expires_at=now(c) + settings.session_seconds))
        response = JSONResponse({"username": settings.username, "csrf": csrf})
        response.set_cookie("aisop_session", token, httponly=True, secure=settings.secure_cookie, samesite="strict", max_age=settings.session_seconds, path="/")
        return response

    @app.get("/api/v1/session")
    def session(request: Request):
        user = auth(request)
        return {"username": user["owner"], "csrf": user["csrf"]}

    @app.post("/api/v1/logout")
    def logout(request: Request):
        user = auth(request)
        with engine.begin() as c:
            c.execute(delete(sessions).where(sessions.c.token_hash == user["token_hash"]))
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
            tmp.replace(final)
            with engine.begin() as c:
                c.execute(media.insert().values(id=mid, owner=user["owner"], name=name, suffix=suffix,
                    size=length, sha256=sha.hexdigest(), metadata=info, created_at=now(c)))
            return {"id": mid, "name": name, "metadata": info, "status": "READY"}
        except BaseException:
            tmp.unlink(missing_ok=True)
            final.unlink(missing_ok=True)
            raise

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
        try:
            validate_options(source["metadata"], **{k: options[k] for k in ["start", "end", "sample_fps", "crop", "imgsz"]})
        except AnalysisError as exc:
            raise HTTPException(422, str(exc)) from exc
        options["pose_backend"] = options.get("pose_backend") or settings.pose_backend
        if options["pose_backend"] == "yolo":
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
            with engine.begin() as c:
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
                             workflow_id: str | None = Query(None, max_length=40), sequential: bool = True):
        user = await run_in_threadpool(auth, request)
        stream_id, timestamp = request.headers.get('x-stream-id'), request.headers.get('x-frame-time')
        extra = {}
        if stream_id is not None or timestamp is not None:
            import math
            try:
                stream_id = str(uuid.UUID(stream_id))
                timestamp = float(timestamp)
                if not math.isfinite(timestamp) or not 0 <= timestamp <= 86400:
                    raise ValueError()
            except (TypeError, ValueError, AttributeError) as exc:
                raise HTTPException(422, '实时流需要有效UUID和0–86400秒采样时间戳') from exc
            extra = {'stream_key': (user['token_hash'], stream_id), 'frame_time': timestamp, 'sequential': sequential}
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
        payload = bytearray()
        async for chunk in request.stream():
            if len(payload) + len(chunk) > 1024 * 1024:
                raise HTTPException(413, "实时画面不能超过1 MiB")
            payload.extend(chunk)
        if test_algorithms:
            return await run_in_threadpool(app.state.realtime.predict,bytes(payload),None,names,**extra)
        if station_id:
            from algorithms.stations import snapshot
            try:
                frozen = await run_in_threadpool(snapshot, station_id)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            return await run_in_threadpool(app.state.realtime.predict, bytes(payload), frozen, **extra)
        return await run_in_threadpool(app.state.realtime.predict, bytes(payload), **extra)

    @app.delete('/api/v1/realtime/streams/{stream_id}')
    def release_stream(stream_id: uuid.UUID, request: Request):
        user = auth(request)
        app.state.realtime.release((user['token_hash'], str(stream_id)))
        return {'ok': True}

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

    @app.get("/api/v1/analysis-runs")
    def list_runs(request: Request, limit: int = Query(100, ge=1, le=100), offset: int = Query(0, ge=0),
                  status: str | None = Query(None, pattern="^(QUEUED|RUNNING|CANCELLING|CANCELLED|FAILED|SUCCEEDED)$"),
                  q: str = Query("", max_length=200), cursor: str | None = Query(None, max_length=200)):
        user = auth(request)
        conditions = [runs.c.owner == user["owner"]]
        if status:
            conditions.append(runs.c.status == status)
        if q.strip():
            conditions.append(media.c.name.icontains(q.strip(), autoescape=True) | runs.c.id.icontains(q.strip(), autoescape=True))
        if cursor:
            created_at, run_id = _decode_cursor(cursor)
            conditions.append(or_(runs.c.created_at < created_at,
                                  and_(runs.c.created_at == created_at, runs.c.id < run_id)))
        source = runs.outerjoin(media, (media.c.id == runs.c.media_id) & (media.c.owner == runs.c.owner))
        with engine.connect() as c:
            total = None if cursor else c.execute(select(func.count()).select_from(source).where(*conditions)).scalar_one()
            query = select(runs, media.c.name.label("media_name")).select_from(source).where(*conditions)
            rows = list(c.execute(query.order_by(runs.c.created_at.desc(), runs.c.id.desc())
                                  .limit(limit + 1).offset(0 if cursor else offset)).mappings())
            has_more = len(rows) > limit
            rows = rows[:limit]
            next_cursor = _encode_cursor(rows[-1]["created_at"], rows[-1]["id"]) if has_more and rows else None
            return {"items": [{**public_run(row), "media_name": row["media_name"]} for row in rows],
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
                "realtime": app.state.realtime.metrics_snapshot()}

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
    def static(path: str):
        path = path or "index.html"
        if path not in ["index.html", "styles.css", "app.js", "server-ui.js"] and not path.startswith("assets/"):
            raise HTTPException(404, "Not found")
        target = (ROOT / path).resolve()
        boundary = (ROOT / "assets").resolve() if path.startswith("assets/") else ROOT
        if not target.is_relative_to(boundary) or not target.is_file():
            raise HTTPException(404, "Not found")
        if path == "index.html":
            html = target.read_text(encoding="utf-8").replace('src="assets/reference.mp4"', '')
            return HTMLResponse(html.replace('<script src="app.js">', '<script>window.AISOP_SERVER=true;</script><script src="app.js">'))
        return FileResponse(target)

    return app
