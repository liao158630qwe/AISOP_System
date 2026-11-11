"""Always-on RTSP/USB collector: newest encoded frame only, central GPU inference.

Secrets come from environment variables, never command arguments or application logs.
Native camera drivers may log independently; restrict access to service logs.
Capture is isolated in a child process so a hung driver cannot block shutdown.
This process sends sampled JPEGs; it does not claim to record full-rate video.
"""
import argparse
import http.client
import json
import math
import multiprocessing as mp
import os
from pathlib import Path
import queue
import random
import signal
import ssl
import sys
import threading
import time
import urllib.parse
import uuid


def latest_put(mailbox, message):
    try:
        mailbox.put_nowait(message)
        return
    except queue.Full:
        pass
    try:
        mailbox.get(timeout=.01)
    except queue.Empty:
        pass
    try:
        mailbox.put_nowait(message)
    except queue.Full:
        pass


def sample_size(width, height, max_width, max_height, *, allow_portrait=False):
    if allow_portrait:
        ratio = min(1., max(max_width, max_height)/max(width, height),
                    math.sqrt(max_width*max_height/(width*height)))
    else:
        ratio = min(1., max_width/width, max_height/height)
    return max(1, math.floor(width*ratio)), max(1, math.floor(height*ratio))


def capture_loop(source, mailbox, stop, fps, width, height, allow_portrait=False):
    import cv2
    cv2.setNumThreads(1)
    # URL logging from native backends can contain credentials.
    cv2.setLogLevel(0)
    next_sample = 0.
    device = None
    generation = 0
    while not stop.is_set():
        if device is None:
            if source.isdecimal():
                device = cv2.VideoCapture(int(source))
            else:
                device = cv2.VideoCapture(source, cv2.CAP_FFMPEG, [
                    cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000,
                    cv2.CAP_PROP_READ_TIMEOUT_MSEC, 2000])
            if not device.isOpened():
                device.release(); device = None
                latest_put(mailbox, {'error': '相机连接失败', 'at': time.monotonic()})
                stop.wait(1+random.random())
                continue
            generation += 1
        ok, frame = device.read()
        if not ok:
            device.release(); device = None
            latest_put(mailbox, {'error': '相机视频中断', 'at': time.monotonic()})
            stop.wait(.5)
            continue
        sampled = time.monotonic()
        if sampled < next_sample:
            continue
        next_sample = sampled+1/fps
        size = sample_size(frame.shape[1], frame.shape[0], width, height, allow_portrait=allow_portrait)
        if size != (frame.shape[1], frame.shape[0]):
            frame = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
        payload = None
        for quality in (82, 70, 55):
            ok, encoded = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
            if ok and encoded.size <= 1024*1024:
                payload = encoded.tobytes(); break
        if payload is not None:
            latest_put(mailbox, dict(payload=payload, at=sampled, generation=generation))
        else:
            latest_put(mailbox, {'error': '相机采样编码超出限制', 'at': sampled})
    if device is not None:
        device.release()


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Client:
    # Uvicorn closes idle keep-alive sockets after five seconds by default.
    # Reconnect before sending on an old socket; never replay a sent request.
    IDLE_RECONNECT_SECONDS = 4.

    def __init__(self, server, key):
        parsed = urllib.parse.urlsplit(server)
        if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('server必须是不含凭据或查询参数的HTTP(S)地址')
        if parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
            raise ValueError('远程服务器必须使用HTTPS；本机调试可使用localhost')
        self.server, self.key = server.rstrip('/'), key
        self._scheme, self._host, self._port = parsed.scheme, parsed.hostname, parsed.port
        self._prefix = parsed.path.rstrip('/') + '/api/v1'
        self._tls = ssl.create_default_context() if parsed.scheme == 'https' else None
        self._connection = None
        self._last_response_at = None
        self._lock = threading.Lock()

    def _disconnect(self):
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        self._last_response_at = None

    def close(self):
        with self._lock:
            self._disconnect()

    def request(self, path, method='GET', body=None, headers=None):
        # Only append local API paths to the configured origin/prefix. Redirects
        # are surfaced as HTTP errors, never followed with a device credential.
        if (not isinstance(path, str) or not path.startswith('/') or path.startswith('//') or
                urllib.parse.urlsplit(path).fragment or any(ord(char) < 32 for char in path)):
            raise ValueError('API路径必须是以单个/开头的同源相对路径')
        if any(name.lower() == 'host' for name in (headers or {})):
            raise ValueError('不能覆盖采集服务器Host')
        headers = {name:value for name,value in (headers or {}).items() if name.lower() != 'x-terminal-key'}
        headers['X-Terminal-Key'] = self.key
        if isinstance(body, dict):
            body = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        with self._lock:
            try:
                if (self._connection is not None and self._last_response_at is not None and
                        time.monotonic()-self._last_response_at >= self.IDLE_RECONNECT_SECONDS):
                    self._disconnect()
                if self._connection is None:
                    self._connection = (http.client.HTTPSConnection(self._host, self._port, timeout=15, context=self._tls)
                        if self._scheme == 'https' else http.client.HTTPConnection(self._host, self._port, timeout=15))
                self._connection.request(method, self._prefix+path, body=body, headers=headers)
                response = self._connection.getresponse()
                status = response.status
                # Drain errors as well as successes before reusing HTTP/1.1.
                payload = response.read()
                if response.will_close:
                    self._disconnect()
                else:
                    self._last_response_at = time.monotonic()
            except (http.client.HTTPException, TimeoutError, OSError):
                self._disconnect()
                # An accepted POST can lose its response. Never retry it here;
                # the next caller may reconnect with a new frame/sequence.
                raise ApiError(0, '服务器连接中断') from None
        if not 200 <= status < 300:
            try:
                message = json.loads(payload).get('message', '请求失败')
            except (ValueError, AttributeError):
                message = '请求失败'
            raise ApiError(status, message) from None
        return json.loads(payload)


def run(args):
    key, source = os.getenv(args.key_env, ''), os.getenv(args.source_env, '')
    if not key or not source:
        raise ValueError('请通过指定环境变量设置设备凭据和相机源')
    client = Client(args.server, key)
    try:
        return collect(args, source, client)
    finally:
        client.close()


def collect(args, source, client):
    binding = client.request('/terminal/context')['binding']
    if binding['analysisType'] == 'manual':
        raise ValueError('该工位尚未配置AI任务')
    if getattr(args, 'controlled', False):
        return collect_controlled(args, source, client, binding)
    fps = min(args.fps or binding['sampleFps'], binding['sampleFps'])
    width, height = (1280,720) if binding['analysisType']=='pose' else (1920,1080)
    stop = mp.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    mailbox = mp.Queue(maxsize=1)
    process = mp.Process(target=capture_loop, args=(source,mailbox,stop,fps,width,height,
        binding['analysisType'] in ('sticker', 'scene')), daemon=True)
    process.start()
    stream = None
    pending_start = None
    capture_generation = None
    origin = None
    seq = 0
    last_received = time.monotonic()
    last_report = 0
    start_blocked_at = None
    began = time.monotonic()
    counters = dict(submitted=0, completed=0, rejected=0, discarded=0, reconnects=0)
    print(json.dumps(dict(event='connected',station=binding['stationId'],camera=binding['cameraId'],targetFps=fps),ensure_ascii=False),flush=True)
    try:
        while not stop.is_set() and (not args.duration or time.monotonic()-began < args.duration):
            try:
                item = mailbox.get(timeout=.5)
                last_received = time.monotonic()
            except queue.Empty:
                if not process.is_alive() or time.monotonic()-last_received > 15:
                    print('采集进程失去响应，请由服务管理器重启',flush=True)
                    return 2
                continue
            if item.get('error'):
                if time.monotonic()-last_report>3:
                    print(item['error'],flush=True);last_report=time.monotonic()
                continue
            if time.monotonic()-item['at'] > .5:
                counters['discarded'] += 1
                continue
            # A camera reconnect starts a new round; the old round is interrupted.
            if capture_generation is not None and item['generation'] != capture_generation:
                counters['reconnects'] += 1
                if stream:
                    try:
                        client.request('/station-streams/'+stream['streamId'], 'DELETE')
                    except ApiError as exc:
                        if exc.status != 404:
                            counters['discarded'] += 1
                            stop.wait(.5+random.random()*.5)
                            continue
                stream, pending_start = None, None
            capture_generation = item['generation']
            try:
                if stream is None:
                    pending_start = pending_start or dict(requestId=str(uuid.uuid4()))
                    stream = client.request('/station-streams','POST',pending_start)
                    origin = item['at']; seq = 0; pending_start = None
                    start_blocked_at = None
                    if time.monotonic()-item['at'] > .5:
                        counters['discarded'] += 1
                        continue
                counters['submitted'] += 1
                current_seq = seq; seq += 1
                result = client.request('/station-streams/'+stream['streamId']+'/frames', 'POST', item['payload'], {
                    'Content-Type':'image/jpeg', 'X-Frame-Seq':str(current_seq), 'X-Frame-Time':str(item['at']-origin),
                    'X-Stream-Epoch':str(stream['streamEpoch']), 'X-Station-Id':binding['stationId'], 'X-Camera-Id':binding['cameraId']})
                if (any(result.get(k)!=stream.get(k) for k in
                        ('stationId','workstationId','cameraId','revision','streamId','streamEpoch')) or
                        result.get('frameSeq') != current_seq):
                    raise ValueError('服务器结果身份不匹配，采集已停止')
                counters['completed'] += 1
            except ApiError as exc:
                counters['rejected'] += 1
                if exc.status == 404 and stream:
                    stream, pending_start = None, None
                    counters['reconnects'] += 1
                    stop.wait(.5+random.random()*.5)
                    continue
                if exc.status == 409 and stream is None:
                    start_blocked_at = start_blocked_at or time.monotonic()
                    if time.monotonic()-start_blocked_at < 35:
                        # Recover an abandoned 30-second lease, without replacing a live producer.
                        stop.wait(1+random.random())
                        continue
                if exc.status in (401,403,404,409,410,422):
                    print(f'采集停止：HTTP {exc.status} {exc}',flush=True)
                    return 3
                stop.wait(.25+random.random()*.25)
            if time.monotonic()-last_report >= 10:
                print(json.dumps(dict(event='samples',station=binding['stationId'],**counters),ensure_ascii=False),flush=True)
                last_report=time.monotonic()
    finally:
        stop.set()
        process.join(timeout=2)
        if process.is_alive():
            process.terminate();process.join(timeout=2)
        if stream:
            try:client.request('/station-streams/'+stream['streamId'],'DELETE')
            except ApiError:pass
        mailbox.close()
        if args.report:
            destination=Path(args.report)
            destination.parent.mkdir(parents=True,exist_ok=True)
            destination.write_text(json.dumps(dict(station=binding['stationId'],seconds=time.monotonic()-began,
                **counters),ensure_ascii=False,indent=2),encoding='utf-8')
    return 0


class ControlledCollector:
    """One serial producer; durable server commands define workpiece boundaries.

    The camera stays open between pieces. Only a new start command authorizes
    samples, and every retry retains that command's UUID and stream identity.
    """
    IDENTITY = ('stationId', 'workstationId', 'cameraId', 'revision',
                'streamId', 'streamEpoch', 'analysisType', 'sceneId', 'modelRevision')
    TERMINAL_ERRORS = (401, 403, 404, 409, 410, 422)

    def __init__(self, client, binding, *, clock=None, instance_id=None):
        self.client, self.binding = client, binding
        self.clock = clock or time.monotonic
        self.instance_id = instance_id or str(uuid.uuid4())
        self.path = '/stations/' + binding['stationId'] + '/collector'
        self.runtime_id = None
        self.stream = self.command = None
        self.start_after = self.origin = 0.
        self.seq = 0
        self.capture_generation = None
        self.latest_capture = None
        self.camera_ready = False
        self.error = ''
        self.error_reported = False
        self.poll_at = 0.
        self.retry_at = 0.
        self.archive_done = self.release_done = False
        self.command_stream = None
        self.last_status = None
        self.counters = dict(submitted=0, completed=0, rejected=0, discarded=0, reconnects=0,
                             commands=0, interrupted=0)

    def headers(self, command=False):
        result = {'X-Collector-Instance': self.instance_id}
        if command:
            result['X-Collector-Command'] = self.command['requestId']
        return result

    def _identity(self, value):
        if (not isinstance(value, dict) or not value.get('streamId') or
                not isinstance(value.get('streamEpoch'), int) or value['streamEpoch'] < 1 or
                not isinstance(value.get('modelRevision'), str) or not value['modelRevision'] or
                any(value.get(key) != self.binding.get(key) for key in
                    ('stationId', 'workstationId', 'cameraId', 'revision', 'analysisType', 'sceneId'))):
            raise ValueError('后台采集的流身份不匹配')
        return {key: value.get(key) for key in self.IDENTITY}

    def _status(self, status):
        if status.get('stationId') != self.binding['stationId']:
            raise ValueError('后台采集的工位身份不匹配')
        if status.get('bindingRevision') != self.binding['revision']:
            raise ApiError(409, '工位绑定已变更，请重新启动采集服务')
        if status.get('instanceId') not in (None, self.instance_id):
            raise ApiError(409, '该工位已有其他采集进程')
        runtime = status.get('runtimeId')
        if not runtime:
            raise ValueError('后台采集状态缺少服务代次')
        if self.runtime_id and runtime != self.runtime_id:
            # An API restart loses the temporal model. Even a surviving camera
            # must not silently re-create the previous command as a new piece.
            self.stream = self.command = self.command_stream = None
            self.archive_done = self.release_done = False
            self.error = '推理服务已重启，原轮次中断；请核对后开始新一件'
            self.error_reported = False
            self.counters['interrupted'] += 1
        self.runtime_id = runtime
        self.last_status = status
        remote = status.get('command')
        if remote and remote.get('status') == 'PENDING':
            if (remote.get('instanceId') != self.instance_id or
                    remote.get('runtimeId') != self.runtime_id or
                    remote.get('bindingRevision') != self.binding['revision']):
                raise ValueError('后台采集命令已失效')
            if not self.command or self.command['requestId'] != remote['requestId']:
                self.command = dict(remote)
                self.archive_done = self.release_done = False
                self.command_stream = remote.get('streamId')
                # Do not use a frame already waiting before this start.
                if remote['action'] == 'start':
                    self.start_after = self.clock()
                    self.origin = self.start_after
                    self.seq = 0
                    self.capture_generation = None
            if remote['action'] == 'start' and not self.stream and status.get('stream'):
                self.stream = self._identity(status['stream'])
                self.command_stream = self.stream['streamId']
        elif self.command and remote and remote.get('requestId') == self.command['requestId']:
            if remote.get('status') == 'FAILED':
                # A failed command may still refer to a durable finished record;
                # it can never authorize another frame or implicit start.
                if self.command['action'] == 'start':
                    self.stream = None
                self.error = remote.get('error') or '本轮控制失败，请核对服务器记录'
                self.error_reported = True
            self.command = None
        record = status.get('record')
        if (self.stream and record and record.get('streamId') == self.stream['streamId'] and
                record.get('status') == 'INTERRUPTED'):
            self.stream = None
        return status

    def poll(self, *, force=False):
        if not force and self.clock() < self.poll_at:
            return True
        self.poll_at = self.clock() + .5
        body = dict(instanceId=self.instance_id, streamId=self.stream['streamId'] if self.stream else None,
                    state='ERROR' if self.error and not self.error_reported else 'RUNNING' if self.stream else 'IDLE',
                    error=self.error if self.error and not self.error_reported else '')
        try:
            result = self.client.request(self.path + '/heartbeat', 'POST', body)
        except ApiError as exc:
            if exc.status == 409 and self.stream:
                # A new API runtime rejects the obsolete stream in heartbeat.
                # Read first; never infer a fresh start from a missing response.
                status = self.client.request(self.path)
                if (status.get('runtimeId') != self.runtime_id or
                        not status.get('online') and status.get('instanceId') == self.instance_id):
                    self._status(status)
                    self.stream = self.command = self.command_stream = None
                    self.error = status.get('error') or '后台采集租约已失效，原轮次中断；请核对后开始新一件'
                    self.error_reported = False
                    return self.poll(force=True)
            raise
        self._status(result)
        if body['state'] == 'ERROR':
            self.error_reported = True
            self.stream = None
        return True

    def fault(self, reason):
        if not self.error:
            self.counters['interrupted'] += int(self.stream is not None)
        self.error, self.error_reported = reason, False
        self.camera_ready = False
        self.poll_at = 0.
        if self.stream:
            try:
                self.client.request('/station-streams/' + self.stream['streamId'], 'DELETE',
                                    {'reason': reason}, self.headers())
            except ApiError:
                # ERROR heartbeat atomically interrupts it if DELETE was lost.
                # If the server is unreachable, its lease will expire instead.
                pass

    def _inspection(self, stream_id):
        result = self.client.request('/station-streams/' + stream_id + '/inspection')
        record = result.get('record', result)
        if record.get('streamId') != stream_id or record.get('stationId') != self.binding['stationId']:
            raise ValueError('后台采集归档记录身份不匹配')
        return record

    def _ack(self, status='SUCCEEDED', error=''):
        result = self.client.request(self.path + '/commands/' + self.command['requestId'] + '/ack', 'POST',
            dict(instanceId=self.instance_id, streamId=self.command_stream, status=status, error=error))
        self.counters['commands'] += 1
        self._status(result)

    def execute(self):
        if not self.command or self.error and not self.error_reported:
            return
        command = self.command
        if command['action'] == 'start':
            if (not self.camera_ready or self.latest_capture is None or
                    self.clock() - self.latest_capture > .5):
                return
            if self.stream is None:
                # Reconcile before every retry: a successful POST can lose its
                # response, but must not create a second detection round.
                remote = self._status(self.client.request(self.path))
                if not self.command or self.command['requestId'] != command['requestId']:
                    return
                if remote.get('stream'):
                    self.stream = self._identity(remote['stream'])
                else:
                    self.stream = self._identity(self.client.request('/station-streams', 'POST',
                        dict(requestId=command['requestId'], inputSource='device'), self.headers(True)))
                self.command_stream = self.stream['streamId']
            self.error = ''
            self.error_reported = False
            self._ack()
            return
        stream_id = command.get('streamId')
        if not stream_id or self.stream and stream_id != self.stream['streamId']:
            raise ValueError('控制命令与当前检测轮次不一致')
        self.command_stream = stream_id
        record = self._inspection(stream_id)
        if command['action'] == 'finish' and not self.archive_done:
            if record['status'] == 'FINISHED':
                self.archive_done = True
            elif record['status'] == 'INTERRUPTED':
                self._ack('FAILED', '本轮已中断，不能补记完成')
                self.stream = None
                return
            else:
                final = self.client.request('/station-streams/' + stream_id + '/finish', 'POST', {}, self.headers(True))
                expected = self.stream or record.get('context', {})
                if (not final.get('archived') or not final.get('final') or
                        any(final.get(key) != expected.get(key) for key in self.IDENTITY)):
                    raise ApiError(503, '结束响应尚未确认保存，保留原命令等待核对')
                self.archive_done = True
        if not self.release_done:
            self.client.request('/station-streams/' + stream_id, 'DELETE',
                                {'reason': command.get('reason') or '操作员结束本轮后台采集'}, self.headers(True))
            self.release_done = True
        self.stream = None
        self._ack()

    def step(self, item=None):
        """Consume at most one latest sample; all HTTP work remains serial."""
        if item:
            if item.get('error'):
                self.fault(item['error'])
            else:
                self.latest_capture = item['at']
                self.camera_ready = self.clock() - item['at'] <= .5
                if (self.stream and self.capture_generation is not None and
                        item['generation'] != self.capture_generation):
                    self.counters['reconnects'] += 1
                    self.fault('相机重新连接，原轮次中断；请核对后开始新一件')
        try:
            self.poll()
            if self.clock() < self.retry_at:
                return
            self.execute()
            if not item or item.get('error') or not self.stream or self.command or self.error:
                return
            if item['at'] <= self.start_after or self.clock() - item['at'] > .5:
                self.counters['discarded'] += 1
                return
            if self.capture_generation is None:
                self.capture_generation = item['generation']
            current_seq = self.seq
            self.seq += 1
            self.counters['submitted'] += 1
            result = self.client.request('/station-streams/' + self.stream['streamId'] + '/frames',
                'POST', item['payload'], dict(self.headers(), **{
                    'Content-Type': 'image/jpeg', 'X-Frame-Seq': str(current_seq),
                    'X-Frame-Time': str(item['at'] - self.origin),
                    'X-Stream-Epoch': str(self.stream['streamEpoch']),
                    'X-Station-Id': self.binding['stationId'], 'X-Camera-Id': self.binding['cameraId']}))
            if (any(result.get(key) != self.stream.get(key) for key in self.IDENTITY) or
                    result.get('frameSeq') != current_seq):
                raise ValueError('服务器采样结果身份不匹配，本轮已中断')
            self.counters['completed'] += 1
        except ApiError as exc:
            self.counters['rejected'] += 1
            self.retry_at = self.clock() + .5
            self.poll_at = 0.
            if exc.status in self.TERMINAL_ERRORS:
                if exc.status in (401, 403):
                    self.fault('设备认证已失效，本轮中断，请重新配置采集服务')
                    raise
                self.fault('检测流或控制状态已失效，本轮中断，请核对后重新开始')
        except ValueError as exc:
            self.fault(str(exc))

    def shutdown(self):
        self.fault('采集服务退出，本轮未正常归档，请人工复核')
        try:
            self.poll(force=True)
        except (ApiError, ValueError):
            pass


def collect_controlled(args, source, client, binding):
    fps = min(args.fps or binding['sampleFps'], binding['sampleFps'])
    width, height = (1280, 720) if binding['analysisType'] == 'pose' else (1920, 1080)
    stop = mp.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    mailbox = mp.Queue(maxsize=1)
    process = mp.Process(target=capture_loop, args=(source, mailbox, stop, fps, width, height,
        binding['analysisType'] in ('sticker', 'scene')), daemon=True)
    control = ControlledCollector(client, binding)
    began = last_received = last_report = time.monotonic()
    process.start()
    print(json.dumps(dict(event='controlled-ready', station=binding['stationId'],
        camera=binding['cameraId'], targetFps=fps), ensure_ascii=False), flush=True)
    try:
        while not stop.is_set() and (not args.duration or time.monotonic() - began < args.duration):
            item = None
            try:
                item = mailbox.get(timeout=.1)
                if not item.get('error'):
                    last_received = time.monotonic()
            except queue.Empty:
                pass
            if not process.is_alive() or time.monotonic() - last_received > 15:
                control.fault('相机采集进程失去响应，本轮中断，请检查采集服务')
                return 2
            control.step(item)
            if time.monotonic() - last_report >= 10:
                print(json.dumps(dict(event='controlled-samples', station=binding['stationId'],
                    state='ERROR' if control.error else 'RUNNING' if control.stream else 'IDLE',
                    **control.counters), ensure_ascii=False), flush=True)
                last_report = time.monotonic()
    finally:
        stop.set()
        control.shutdown()
        process.join(timeout=2)
        if process.is_alive():
            process.terminate(); process.join(timeout=2)
        mailbox.close()
        if args.report:
            destination = Path(args.report)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(dict(station=binding['stationId'], controlled=True,
                seconds=time.monotonic() - began, **control.counters), ensure_ascii=False, indent=2), encoding='utf-8')
    return 0


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server',required=True)
    parser.add_argument('--key-env',default='AISOP_TERMINAL_KEY')
    parser.add_argument('--source-env',default='AISOP_CAMERA_SOURCE')
    parser.add_argument('--fps',type=float)
    parser.add_argument('--duration',type=float,default=0,help='0 means continuous')
    parser.add_argument('--report')
    parser.add_argument('--controlled', action='store_true',
                        help='Production mode: wait for durable per-piece commands from the station page')
    args=parser.parse_args()
    if args.fps is not None and not .5 <= args.fps <= 30:
        parser.error('fps must be between .5 and 30')
    if not math.isfinite(args.duration) or args.duration < 0:
        parser.error('duration must be nonnegative')
    try:
        sys.exit(run(args))
    except (ValueError,ApiError) as error:
        print(str(error),file=sys.stderr)
        sys.exit(2)
