import runpy
from pathlib import Path

summarize = runpy.run_path(str(Path(__file__).parents[1] / 'tools/summarize-temporal.py'))['summarize']


def person(candidate='raised', held=0, action='unknown'):
    return dict(trackId=1, action=action, temporal=dict(
        candidate=candidate, candidateSeconds=held, method='causal-duration-v3'))


def report(samples):
    return summarize(dict(model=dict(sampleFps=10), frames=[
        dict(time=t, people=people) for t, people in samples]))


def test_counts_confirmation_once_and_preserves_paused_wall_time():
    result = report([(0, [person()]), (.1, [person(held=.1)]),
                     (.2, [person(held=.1)]), (.4, [person(held=.3, action='raised')]),
                     (.5, [person(held=.4, action='raised')])])
    assert result['confirmations'] == 1
    assert result['p50Seconds'] == .4
    assert result['groundTruthAvailable'] is False


def test_missing_person_resets_candidate_start():
    result = report([(0, [person()]), (.1, []), (.2, [person()]),
                     (.5, [person(held=.3, action='raised')])])
    assert result['events'][0]['candidateStart'] == .2
    assert result['p95Seconds'] == .3


def test_unconfirmed_candidates_do_not_become_zero_delay_events():
    result = report([(0, [person()]), (.1, [person(candidate=None)])])
    assert result['confirmations'] == 0
    assert result['p50Seconds'] is None
