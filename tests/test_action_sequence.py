import pytest
from fastapi import HTTPException
from algorithms.action_sop import ActionSequence, freeze
from algorithms.temporal import ActionDecoder
from backend.streams import StreamRegistry


def sequence(actions=('raised', 'reaching')):
    return ActionSequence(freeze(dict(id='test', name='test', version='1', steps=[
        dict(id=str(i), name=a, actions=[a], min_hold=.5, max_wait=1)
        for i, a in enumerate(actions)])))


def person(tid, action, reliable=True):
    signals = dict.fromkeys(('standing','walking','raised','reaching','bending'), 0.)
    signals[action] = 1.
    return dict(trackId=tid, signals=signals, tracking=dict(reliable=reliable))


@pytest.mark.parametrize('fps', [5, 10, 15, 30])
def test_decoder_confirmation_is_measured_in_seconds(fps):
    decoder = ActionDecoder()
    confirmed = None
    for frame in range(fps+1):
        t = frame/fps
        result = decoder.update({'raised': 1.}, t)
        if result['action'] == 'raised':
            confirmed = t
            break
    assert .3-1e-8 <= confirmed <= .3+1/fps


def test_stale_filtered_score_cannot_revive_missing_action():
    decoder = ActionDecoder()
    for t in (0, .15, .3): decoder.update({'raised': 1.}, t)
    result = decoder.update({'raised': 0.}, .31)
    assert result['scores']['raised'] > .6
    assert result['action'] == 'unknown'
    assert result['candidateSeconds'] == 0
    assert decoder.update({'raised': 1.}, .32)['action'] == 'unknown'


def test_brief_soft_dip_pauses_candidate_without_counting_weak_time():
    decoder = ActionDecoder()
    decoder.update({'raised': 1.}, 0)
    decoder.update({'raised': 1.}, .1)
    assert decoder.update({'raised': .55}, .15)['candidateSeconds'] == .1
    assert decoder.update({'raised': 1.}, .2)['candidateSeconds'] == .1
    assert decoder.update({'raised': 1.}, .3)['action'] == 'unknown'
    assert decoder.update({'raised': 1.}, .4)['action'] == 'raised'


def test_candidate_grace_expiry_and_sustained_weak_action():
    decoder = ActionDecoder()
    decoder.update({'raised': 1.}, 0)
    decoder.update({'raised': 1.}, .1)
    decoder.update({'raised': .55}, .15)
    assert decoder.update({'raised': 1.}, .3)['candidateSeconds'] == 0
    decoder.update({'raised': 1.}, .45)
    assert decoder.update({'raised': 1.}, .6)['action'] == 'raised'
    assert decoder.update({'raised': .4}, .7)['action'] == 'raised'
    assert decoder.update({'raised': .4}, .9)['action'] == 'unknown'


def test_gap_requires_new_hold_and_bad_scores_do_not_mutate_state():
    decoder = ActionDecoder()
    for t in (0, .15, .3): decoder.update({'raised': 1.}, t)
    assert decoder.update({'raised': 1.}, 1.2)['action'] == 'unknown'
    for value in (float('nan'), float('inf'), -1, 2):
        with pytest.raises(ValueError): decoder.update({'raised': value}, 1.3)
    assert decoder.last == 1.2
    decoder.update({'raised': 1.}, 1.3)
    assert decoder.update({'raised': 1.}, 1.5)['action'] == 'raised'


def test_repeated_step_needs_observed_release_and_reports_timeout():
    seq = sequence(('raised', 'raised'))
    for t in (0, .25, .5): seq.update([person(1, 'raised')], t)
    missing = dict(trackId=1, signals={}, tracking=dict(reliable=True))
    for t in (.75, 1., 1.25): seq.update([missing], t)
    for t in (1.5, 1.75, 2.): result = seq.update([person(1, 'raised')], t)
    assert len(result['subjects'][0]['completed']) == 1
    assert sum(e['code'] == 'STEP_TIMEOUT' for e in result['events']) == 1
    for t, action in ((2.25,'standing'),(2.5,'standing'),(2.75,'raised'),(3.,'raised'),(3.25,'raised')):
        result = seq.update([person(1, action)], t)
    assert len(result['subjects'][0]['completed']) == 2


def test_people_cannot_complete_each_others_steps():
    seq=sequence()
    for t in (0, .25, .5):
        result=seq.update([person(1,'raised'), person(2,'reaching')], t)
    assert [len(s['completed']) for s in result['subjects']]==[1,0]
    assert any(e['code']=='OUT_OF_ORDER' and e['trackId']==2 for e in result['events'])
    for t in (.75,1,1.25):
        result=seq.update([person(1,'reaching')],t)
    assert result['subjects'][0]['status']=='OBSERVED_SEQUENCE'


def test_missing_frame_breaks_hold_and_long_gap_resets_cycle():
    seq=sequence()
    seq.update([person(1,'raised')],0)
    seq.update([], .25)
    seq.update([person(1,'raised')], .5)
    assert not seq.update([person(1,'raised')], .75)['subjects'][0]['completed']
    seq.update([person(1,'raised')],1)
    result=seq.update([person(1,'reaching')],2)
    assert result['subjects'][0]['cycle']==2
    assert not result['subjects'][0]['completed']


def test_repeated_step_requires_release():
    seq=sequence(('raised','raised'))
    for t in (0,.25,.5,.75,1): result=seq.update([person(1,'raised')],t)
    assert len(result['subjects'][0]['completed'])==1
    for t,a in ((1.25,'standing'),(1.5,'standing'),(1.75,'raised'),(2,'raised'),(2.25,'raised')):
        result=seq.update([person(1,a)],t)
    assert len(result['subjects'][0]['completed'])==2


def test_decoder_duration_visibility_and_timestamp():
    decoder=ActionDecoder()
    for t in (0,.05,.1): assert decoder.update({'raised':1},t)['action']=='unknown'
    assert decoder.update({'raised':1},.3)['action']=='raised'
    assert decoder.update({'raised':1},.4,valid=False)['action']=='unknown'
    with pytest.raises(ValueError): decoder.update({'raised':1},.4)


def test_stream_identity_owner_isolation_expiry_and_bounds():
    registry=StreamRegistry(ttl=2, maximum=2, per_owner=1)
    a=registry.get(('alice','a'),0,(),clock=0)
    assert registry.get(('alice','a'),.2,(),clock=.2) is a
    assert registry.get(('bob','a'),0,(),clock=.3) is not a
    with pytest.raises(HTTPException): registry.get(('alice','a'),.2,(),clock=.4)
    registry.get(('alice','b'),0,(),clock=.5)
    assert ('alice','a') not in registry.items
    registry.get(('carol','a'),0,(),clock=3)
    assert len(registry.items)==1
    with pytest.raises(HTTPException): registry.get(('x','x'),float('nan'),(),clock=3)


def test_stream_api_passes_identity_and_workflow(authenticated):
    _,app,client=authenticated
    captured={}
    def predict(payload, **kwargs):
        captured.update(kwargs)
        return {'people':[]}
    app.state.realtime.predict=predict
    headers={'Content-Type':'image/jpeg','X-Stream-Id':'00000000-0000-4000-8000-000000000001','X-Frame-Time':'0.25'}
    assert client.post('/api/v1/realtime/frame?workflow_id=generic-actions',content=b'x',headers=headers).status_code==200
    assert captured['frame_time']==.25
    assert captured['workflow']['config']['id']=='generic-actions'
    assert client.post('/api/v1/realtime/frame?workflow_id=generic-actions',content=b'x',headers={'Content-Type':'image/jpeg'}).status_code==422
