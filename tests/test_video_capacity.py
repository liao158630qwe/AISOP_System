"""A timed-out camera must not manufacture gaps in other benchmark streams."""
from concurrent.futures import Future, ThreadPoolExecutor
import importlib.util
from pathlib import Path
import threading


def test_finishes_healthy_stream_before_another_transport_timeout_resolves():
    path=Path(__file__).resolve().parents[1]/'tools/evaluate-board-video.py'
    spec=importlib.util.spec_from_file_location('video_capacity',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    blocked=Future()
    ready=Future()
    ready.set_result({'status':200,'frame':12})
    healthy_finished=threading.Event()
    calls=[]
    class Client:
        def request(self,path,method):
            assert method=='POST'
            calls.append(path)
            if path.endswith('/healthy/finish'):
                healthy_finished.set()
            return {'path':path}
    stations=[dict(future=future,records=[],client=Client(),stream={'streamId':name})
              for future,name in ((blocked,'blocked'),(ready,'healthy'))]
    with ThreadPoolExecutor(max_workers=1) as pool:
        final=pool.submit(module.finish_station_runs,stations)
        try:
            assert healthy_finished.wait(2), 'Healthy station was held open by the unrelated hung request'
            assert '/station-streams/blocked/finish' not in calls
            assert not final.done()
        finally:
            blocked.set_result({'status':0,'error':'transport timeout'})
        results=final.result(timeout=2)
    assert [item['path'] for item in results]==[
        '/station-streams/blocked/finish','/station-streams/healthy/finish']
    assert stations[0]['records']==[{'status':0,'error':'transport timeout'}]
    assert stations[1]['records']==[{'status':200,'frame':12}]
