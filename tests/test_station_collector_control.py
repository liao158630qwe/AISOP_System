"""Serial collector boundaries with a durable-control fake, no camera or GPU."""
import copy
import importlib.util
from pathlib import Path
import uuid

import pytest


def load():
    path = Path(__file__).resolve().parents[1] / 'tools' / 'station-collector.py'
    spec = importlib.util.spec_from_file_location('controlled_collector', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Clock:
    def __init__(self): self.value = 10.
    def __call__(self): return self.value
    def advance(self, seconds=.6): self.value += seconds


class Server:
    """Retain commits before selected response losses, like the real API."""
    def __init__(self, module, binding, instance):
        self.module, self.binding, self.instance = module, binding, instance
        self.runtime = str(uuid.uuid4())
        self.command = self.stream = self.record = None
        self.history = {}
        self.calls = []
        self.starts = self.finishes = self.frames = self.deletes = 0
        self.lose = None
        self.closed = False
        self.invalid_finish = False

    def status(self):
        return copy.deepcopy(dict(stationId=self.binding['stationId'], online=True,
            instanceId=self.instance, bindingRevision=self.binding['revision'], runtimeId=self.runtime,
            state='RUNNING' if self.stream and not self.closed else 'IDLE',
            stream=self.stream if not self.closed else None, record=self.record,
            command=self.command, error=''))

    def enqueue(self, action):
        self.command = dict(requestId=str(uuid.uuid4()), action=action,
            streamId=self.stream['streamId'] if action != 'start' else None,
            status='PENDING', reason='操作员结束本件', error='', instanceId=self.instance,
            bindingRevision=self.binding['revision'], runtimeId=self.runtime)
        return self.command['requestId']

    def reply(self, value, kind):
        if self.lose == kind:
            self.lose = None
            raise self.module.ApiError(0, 'response lost after commit')
        return copy.deepcopy(value)

    def request(self, path, method='GET', body=None, headers=None):
        self.calls.append((path, method, copy.deepcopy(body), copy.deepcopy(headers)))
        if path.endswith('/heartbeat'):
            assert body['instanceId'] == self.instance
            if body['streamId'] and self.record and self.record['runtime'] != self.runtime:
                raise self.module.ApiError(409, 'server restarted')
            if body['state'] == 'ERROR':
                if self.record and self.record['status'] == 'RUNNING':
                    self.record['status'] = 'INTERRUPTED'
                    self.closed = True
                if self.command and self.command['status'] == 'PENDING':
                    self.command['status'] = 'FAILED'
                    self.command['error'] = body['error']
            return self.status()
        if path.endswith('/collector'):
            return self.status()
        if path.endswith('/ack'):
            assert body['instanceId'] == self.instance
            assert self.command['requestId'] in path
            self.command['status'] = body['status']
            if self.command['action'] != 'start' and body['status'] == 'SUCCEEDED':
                assert self.closed
            return self.reply(self.status(), 'ack')
        assert headers is None or headers.get('X-Collector-Instance') == self.instance
        if path == '/station-streams':
            assert method == 'POST'
            assert headers['X-Collector-Command'] == self.command['requestId'] == body['requestId']
            if body['requestId'] not in self.history:
                self.starts += 1
                self.stream = dict(self.binding, streamId=str(uuid.uuid4()), streamEpoch=self.starts,
                                   modelRevision='model-v1')
                self.record = dict(streamId=self.stream['streamId'], stationId=self.binding['stationId'],
                    context=self.stream, status='RUNNING', result=None, runtime=self.runtime)
                self.history[body['requestId']] = self.stream
                self.closed = False
            return self.reply(self.stream, 'start')
        if path.endswith('/inspection'):
            return copy.deepcopy(self.record)
        if path.endswith('/frames'):
            assert not self.closed
            assert self.record['status'] == 'RUNNING'
            assert not self.command or self.command['status'] != 'PENDING'
            self.frames += 1
            return dict(self.stream, frameSeq=int(headers['X-Frame-Seq']))
        if path.endswith('/finish'):
            assert headers['X-Collector-Command'] == self.command['requestId']
            self.finishes += 1
            assert self.record['status'] == 'RUNNING', 'Never recompute a saved finish'
            self.record['status'] = 'FINISHED'
            self.record['result'] = dict(self.stream, archived=True, final=True,
                compliance=dict(status='PASS'))
            result = copy.deepcopy(self.record['result'])
            if self.invalid_finish:
                result['streamId'] = str(uuid.uuid4())
            return self.reply(result, 'finish')
        if method == 'DELETE':
            self.deletes += 1
            self.closed = True
            if self.record['status'] == 'RUNNING':
                self.record['status'] = 'INTERRUPTED'
            return self.reply(dict(ok=True), 'delete')
        raise AssertionError((path, method))


@pytest.fixture
def control():
    module, clock = load(), Clock()
    binding = dict(stationId='station-1', workstationId='pc1', cameraId='camera1', revision=1,
                   analysisType='pose', sceneId=None, sampleFps=4)
    instance = str(uuid.uuid4())
    server = Server(module, binding, instance)
    collector = module.ControlledCollector(server, binding, clock=clock, instance_id=instance)
    return collector, server, clock


def sample(control, generation=1, *, age=0):
    collector, _, clock = control
    clock.advance()
    collector.step(dict(payload=b'jpeg', at=clock() - age, generation=generation))


def start(control):
    collector, server, _ = control
    sample(control)
    request = server.enqueue('start')
    sample(control)  # Observe start, drop the pre-command sample.
    sample(control)
    assert collector.stream and collector.command is None
    assert server.frames > 0
    return request


def test_idle_camera_does_not_create_or_submit_a_round(control):
    for _ in range(8): sample(control)
    collector, server, _ = control
    assert server.starts == server.frames == 0
    assert collector.stream is None


def test_start_finish_next_piece_and_stop_have_distinct_boundaries(control):
    collector, server, _ = control
    first_request = start(control)
    first_stream = collector.stream['streamId']
    first_frames = server.frames
    server.enqueue('finish')
    sample(control)
    assert server.frames == first_frames
    assert server.record['status'] == 'FINISHED'
    assert server.finishes == 1 and server.closed
    assert collector.stream is collector.command is None
    for _ in range(3): sample(control)
    assert server.starts == 1 and server.frames == first_frames
    second_request = server.enqueue('start')
    sample(control)
    sample(control)
    assert collector.stream['streamId'] != first_stream
    assert second_request != first_request
    frame_calls = [entry for entry in server.calls if entry[0].endswith('/frames')]
    assert frame_calls[-1][3]['X-Frame-Seq'] == '0'
    server.enqueue('stop')
    sample(control)
    assert server.record['status'] == 'INTERRUPTED'
    assert server.finishes == 1


@pytest.mark.parametrize('loss', ['start', 'ack'])
def test_start_response_loss_never_creates_another_stream(control, loss):
    collector, server, _ = control
    sample(control)
    request = server.enqueue('start')
    server.lose = loss
    sample(control)
    for _ in range(3): sample(control)
    assert server.starts == 1
    assert collector.stream['streamId'] == server.history[request]['streamId']
    assert collector.command is None


@pytest.mark.parametrize('loss', ['finish', 'delete', 'ack'])
def test_finish_response_loss_reconciles_before_retry(control, loss):
    collector, server, _ = control
    start(control)
    before = server.frames
    server.enqueue('finish')
    server.lose = loss
    sample(control)
    for _ in range(3): sample(control)
    assert server.finishes == 1
    assert server.record['status'] == 'FINISHED'
    assert server.command['status'] == 'SUCCEEDED'
    assert server.frames == before
    assert collector.stream is collector.command is None


def test_unconfirmed_finish_is_reconciled_without_destroying_saved_result(control):
    collector, server, _ = control
    start(control)
    server.enqueue('finish')
    server.invalid_finish = True
    sample(control)
    assert server.record['status'] == 'FINISHED' and not server.closed
    sample(control)
    assert server.command['status'] == 'SUCCEEDED'
    assert server.finishes == 1


@pytest.mark.parametrize('failure', ['disconnect', 'generation'])
def test_camera_discontinuity_interrupts_and_requires_explicit_start(control, failure):
    collector, server, clock = control
    start(control)
    previous = server.frames
    if failure == 'disconnect':
        clock.advance()
        collector.step(dict(error='camera disconnected', at=clock()))
    else:
        sample(control, generation=2)
    for _ in range(3): sample(control, generation=2)
    assert server.record['status'] == 'INTERRUPTED'
    assert server.starts == 1 and server.frames == previous
    assert collector.stream is None
    server.enqueue('start')
    sample(control, generation=2)
    sample(control, generation=2)
    assert server.starts == 2 and server.frames > previous


def test_api_restart_does_not_replay_old_start_or_continue_old_round(control):
    collector, server, _ = control
    start(control)
    frames = server.frames
    server.runtime = str(uuid.uuid4())
    server.record['status'] = 'INTERRUPTED'
    server.closed = True
    for _ in range(4): sample(control)
    assert server.starts == 1 and server.frames == frames
    assert collector.stream is None
    server.enqueue('start')
    sample(control)
    sample(control)
    assert server.starts == 2


def test_pre_start_and_expired_samples_are_not_submitted(control):
    collector, server, _ = control
    sample(control)
    server.enqueue('start')
    sample(control)
    assert server.frames == 0
    sample(control, age=.6)
    assert server.frames == 0
    sample(control)
    assert server.frames == 1


def test_shutdown_interrupts_without_auto_finish(control):
    collector, server, _ = control
    start(control)
    collector.shutdown()
    assert server.record['status'] == 'INTERRUPTED'
    assert server.finishes == 0
    assert collector.stream is None


def test_new_process_uses_new_instance_and_does_not_inherit_local_stream(control):
    collector, server, _ = control
    start(control)
    restarted = load().ControlledCollector(server, server.binding)
    assert restarted.instance_id != collector.instance_id
    assert restarted.stream is restarted.command is None
