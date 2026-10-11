import numpy as np

from scene_diagnostics import event_diagnostics, summarize_events


def test_repeated_event_alignment_prefers_closest_onset_and_counts_missing():
    truth = np.repeat([1, 2, 3, 4], 10)
    prediction = np.array([2]*2+[1]*8+[2]*10+[0]*10+[4]*10)
    result = event_diagnostics(truth, prediction, [1, 2, 3, 4], 5)
    assert result['matched_events'] == 3
    assert [e['stage'] for e in result['missing_events']] == [3]
    assert [e['frame'] for e in result['extra_events']] == [0]
    assert result['onset_offsets_seconds'] == [.4, 0., 0.]
    summary = summarize_events([result])
    assert summary['missing_events'] == 1 and summary['extra_events'] == 1


def test_early_unknown_and_swapped_stages_are_not_hidden():
    truth = [0]*5+[1]*5+[2]*5+[3]*5
    prediction = [1]*10+[3]*5+[2]*5
    result = event_diagnostics(truth, prediction, [1, 2, 3], 10)
    assert result['matched_events'] == 2
    assert len(result['missing_events']) == len(result['extra_events']) == 1
    assert result['onset_offsets_seconds'][0] == -.5
    assert summarize_events([result])['early_events'] >= 1
    empty = event_diagnostics(truth, [-1]*20, [1, 2, 3], 10)
    assert empty['matched_events'] == 0 and len(empty['missing_events']) == 3
    assert summarize_events([empty])['absolute_onset_offset_median_seconds'] is None


def test_unknown_gap_does_not_invent_repeated_sop_event():
    result = event_diagnostics([1]*6+[2]*4, [1, 1, -1, 0, 1, 1, 2, 2, 2, 2], [1, 2], 10)
    assert result['predicted_events'] == result['matched_events'] == 2
    assert result['onset_offsets_seconds'] == [0., 0.]
