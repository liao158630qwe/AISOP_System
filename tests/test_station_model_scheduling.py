"""CPU-only checks for fair scheduling around shared scene model instances."""
import asyncio

import pytest
from fastapi import HTTPException

from backend.station_scheduler import StationScheduler


async def wait_until(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(0)
    await asyncio.wait_for(poll(),2)


def submit_job(scheduler,station,label,resource,gate,order,*,group='scene',error=None):
    started=asyncio.Event()
    async def work(wait_ms):
        order.append(label)
        started.set()
        await gate.wait()
        if error is not None:
            raise error
        return label
    task=asyncio.create_task(scheduler.submit(station,work,group,resource=resource))
    return task,started


async def finish(scheduler,tasks,gates):
    for gate in gates:
        gate.set()
    await asyncio.wait_for(scheduler.close(),2)
    await asyncio.gather(*tasks,return_exceptions=True)
    snapshot=scheduler.snapshot()
    assert snapshot['activeResources']==0
    assert snapshot['activeGroups']=={}
    assert snapshot['active'] is None
    assert snapshot['queueDepth']==0


def test_busy_model_is_skipped_while_another_model_runs():
    async def scenario():
        scheduler=StationScheduler(5000,concurrency=3,group_limits={'scene':3})
        gates=[asyncio.Event() for _ in range(3)];order=[];tasks=[]
        # Tuple keys represent owner/model/revision, without string coercion.
        model_a=('owner', 'private/model-a.pt', 7)
        model_b=('owner', 'private/model-b.pt', 2)
        try:
            first,started=submit_job(scheduler,'s1','a1',model_a,gates[0],order);tasks.append(first)
            await asyncio.wait_for(started.wait(),2)
            second,blocked=submit_job(scheduler,'s2','a2',model_a,gates[1],order);tasks.append(second)
            await wait_until(lambda:'s2' in scheduler.pending)
            third,parallel=submit_job(scheduler,'s3','b',model_b,gates[2],order);tasks.append(third)
            await asyncio.wait_for(parallel.wait(),2)
            assert not blocked.is_set()
            assert order==['a1','b']
            status=scheduler.snapshot()
            assert status['activeResources']==2
            assert status['activeGroups']=={'scene':2}
            assert 'private/' not in repr(status)
            gates[0].set()
            assert await first=='a1'
            await asyncio.wait_for(blocked.wait(),2)
            assert order==['a1','b','a2']
        finally:
            await finish(scheduler,tasks,gates)
    asyncio.run(scenario())


@pytest.mark.parametrize(('concurrency','scene_limit'),[(1,3),(3,1),(3,2)])
def test_distinct_models_still_obey_global_and_scene_limits(concurrency,scene_limit):
    async def scenario():
        scheduler=StationScheduler(5000,concurrency=concurrency,group_limits={'scene':scene_limit})
        gate=asyncio.Event();order=[];tasks=[]
        try:
            for index in range(3):
                task,_=submit_job(scheduler,f's{index}',str(index),index,gate,order)
                tasks.append(task)
            limit=min(concurrency,scene_limit)
            await wait_until(lambda:len(order)==limit)
            assert scheduler.snapshot()['activeResources']==limit
            assert scheduler.snapshot()['queueDepth']==3-limit
            gate.set()
            assert await asyncio.gather(*tasks)==['0','1','2']
        finally:
            await finish(scheduler,tasks,[gate])
    asyncio.run(scenario())


def test_omitting_resource_preserves_parallel_group_scheduling():
    async def scenario():
        scheduler=StationScheduler(5000,concurrency=2,group_limits={'scene':2})
        gate=asyncio.Event();order=[];tasks=[]
        try:
            for index in range(2):
                task,_=submit_job(scheduler,f's{index}',str(index),None,gate,order)
                tasks.append(task)
            await wait_until(lambda:len(order)==2)
            assert scheduler.snapshot()['activeResources']==0
            assert scheduler.snapshot()['activeGroups']=={'scene':2}
        finally:
            await finish(scheduler,tasks,[gate])
    asyncio.run(scenario())


def test_replacement_retains_turn_behind_a_busy_model():
    async def scenario():
        scheduler=StationScheduler(5000,concurrency=3,group_limits={'scene':3})
        gates=[asyncio.Event() for _ in range(4)];order=[];tasks=[]
        try:
            first,started=submit_job(scheduler,'s1','first','model',gates[0],order);tasks.append(first)
            await asyncio.wait_for(started.wait(),2)
            old,_=submit_job(scheduler,'s2','old','model',gates[1],order);tasks.append(old)
            await wait_until(lambda:'s2' in scheduler.pending)
            last,last_started=submit_job(scheduler,'s3','last','model',gates[2],order);tasks.append(last)
            await wait_until(lambda:'s3' in scheduler.pending)
            replacement,replaced_started=submit_job(scheduler,'s2','replacement','model',gates[3],order)
            tasks.append(replacement)
            with pytest.raises(HTTPException) as rejected:
                await old
            assert rejected.value.status_code==409
            gates[0].set();await first
            await asyncio.wait_for(replaced_started.wait(),2)
            assert not last_started.is_set()
            assert order==['first','replacement']
            gates[3].set();await replacement
            await asyncio.wait_for(last_started.wait(),2)
            assert order==['first','replacement','last']
            assert scheduler.snapshot()['stations']['s2']['replaced']==1
        finally:
            await finish(scheduler,tasks,gates)
    asyncio.run(scenario())


def test_cancelled_pending_frame_does_not_claim_or_release_a_model():
    async def scenario():
        scheduler=StationScheduler(5000,concurrency=2,group_limits={'scene':2})
        gates=[asyncio.Event() for _ in range(3)];order=[];tasks=[]
        try:
            first,started=submit_job(scheduler,'s1','first','model',gates[0],order);tasks.append(first)
            await asyncio.wait_for(started.wait(),2)
            pending,never_started=submit_job(scheduler,'s2','cancelled','model',gates[1],order);tasks.append(pending)
            await wait_until(lambda:'s2' in scheduler.pending)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert not never_started.is_set()
            assert scheduler.snapshot()['activeResources']==1
            assert scheduler.snapshot()['queueDepth']==0
            next_job,next_started=submit_job(scheduler,'s3','next','model',gates[2],order);tasks.append(next_job)
            gates[0].set();await first
            await asyncio.wait_for(next_started.wait(),2)
            assert order==['first','next']
        finally:
            await finish(scheduler,tasks,gates)
    asyncio.run(scenario())


def test_cancelled_http_waiter_keeps_model_reserved_until_work_finishes():
    async def scenario():
        scheduler=StationScheduler(5000,concurrency=3,group_limits={'scene':3})
        gates=[asyncio.Event() for _ in range(3)];order=[];tasks=[]
        try:
            first,started=submit_job(scheduler,'s1','first','model-a',gates[0],order);tasks.append(first)
            await asyncio.wait_for(started.wait(),2)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            second,blocked=submit_job(scheduler,'s2','second','model-a',gates[1],order);tasks.append(second)
            third,parallel=submit_job(scheduler,'s3','parallel','model-b',gates[2],order);tasks.append(third)
            await asyncio.wait_for(parallel.wait(),2)
            assert not blocked.is_set()
            assert scheduler.snapshot()['activeResources']==2
            gates[0].set()
            await asyncio.wait_for(blocked.wait(),2)
            assert order==['first','parallel','second']
        finally:
            await finish(scheduler,tasks,gates)
    asyncio.run(scenario())


@pytest.mark.parametrize('failure',[RuntimeError('model failed'),asyncio.CancelledError()])
def test_failed_or_cancelled_model_work_releases_resource_for_next_station(failure):
    async def scenario():
        scheduler=StationScheduler(5000,concurrency=2,group_limits={'scene':2})
        gates=[asyncio.Event() for _ in range(2)];order=[];tasks=[]
        try:
            first,started=submit_job(scheduler,'s1','failure','model',gates[0],order,error=failure);tasks.append(first)
            await asyncio.wait_for(started.wait(),2)
            second,next_started=submit_job(scheduler,'s2','next','model',gates[1],order);tasks.append(second)
            await wait_until(lambda:'s2' in scheduler.pending)
            gates[0].set()
            with pytest.raises(type(failure)):
                await first
            await asyncio.wait_for(next_started.wait(),2)
            assert order==['failure','next']
            assert scheduler.snapshot()['activeResources']==1
        finally:
            await finish(scheduler,tasks,gates)
    asyncio.run(scenario())


def test_close_rejects_pending_models_and_drains_active_work():
    async def scenario():
        scheduler=StationScheduler(5000,concurrency=1,group_limits={'scene':3})
        gates=[asyncio.Event() for _ in range(2)];order=[];tasks=[]
        closing=None
        try:
            first,started=submit_job(scheduler,'s1','active','model-a',gates[0],order);tasks.append(first)
            await asyncio.wait_for(started.wait(),2)
            pending,never_started=submit_job(scheduler,'s2','pending','model-b',gates[1],order);tasks.append(pending)
            await wait_until(lambda:'s2' in scheduler.pending)
            closing=asyncio.create_task(scheduler.close())
            with pytest.raises(HTTPException) as rejected:
                await pending
            assert rejected.value.status_code==503
            assert not never_started.is_set()
            assert not closing.done()
            assert scheduler.snapshot()['activeResources']==1
            gates[0].set();await first
            await asyncio.wait_for(closing,2)
            assert scheduler.snapshot()['activeResources']==0
            async def unexpected(_):
                pytest.fail('Closed scheduler ran new work')
            with pytest.raises(HTTPException) as closed:
                await scheduler.submit('s3',unexpected,'scene',resource='model-a')
            assert closed.value.status_code==503
        finally:
            await finish(scheduler,tasks,gates)
            if closing is not None:
                await closing
    asyncio.run(scenario())


def test_unhashable_resource_is_rejected_without_queuing():
    async def scenario():
        scheduler=StationScheduler()
        async def unexpected(_):
            pytest.fail('Invalid resource ran work')
        with pytest.raises(TypeError):
            await scheduler.submit('s1',unexpected,resource=[])
        assert scheduler.snapshot()['queueDepth']==0
        assert scheduler.snapshot()['activeResources']==0
        await scheduler.close()
    asyncio.run(scenario())
