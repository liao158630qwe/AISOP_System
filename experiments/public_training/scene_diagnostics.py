"""Descriptive event errors and onset offsets; never used as inference inputs."""
from functools import lru_cache

import numpy as np

from scene_runtime import check_sop


def event_diagnostics(truth, prediction, steps, hz):
    """Align equal stage IDs in order, maximizing matches then minimizing onset distance.

    Offsets include early predictions and only describe aligned events. Missing events
    have no latency value. This is annotation-relative timing, not wall-clock latency.
    Background/unknown gaps follow check_sop's event collapsing convention.
    """
    reference = check_sop(truth, steps, hz)['events']
    proposed = check_sop(prediction, steps, hz)['events']

    @lru_cache(None)
    def align(i, j):
        if i == len(reference) or j == len(proposed):
            return (), 0
        choices = [align(i+1, j), align(i, j+1)]
        if reference[i]['stage'] == proposed[j]['stage']:
            pairs, cost = align(i+1, j+1)
            choices.append((((i, j),)+pairs, cost+abs(proposed[j]['frame']-reference[i]['frame'])))
        return max(choices, key=lambda item: (len(item[0]), -item[1]))

    pairs, _ = align(0, 0)
    matched_reference = {i for i, _ in pairs}
    matched_proposed = {j for _, j in pairs}
    return {
        'reference_events': len(reference), 'predicted_events': len(proposed),
        'matched_events': len(pairs),
        'missing_events': [e for i, e in enumerate(reference) if i not in matched_reference],
        'extra_events': [e for j, e in enumerate(proposed) if j not in matched_proposed],
        'onset_offsets_seconds': [(proposed[j]['frame']-reference[i]['frame'])/hz for i, j in pairs],
        'matches': [{'stage': reference[i]['stage'], 'reference_frame': reference[i]['frame'],
                     'predicted_frame': proposed[j]['frame']} for i, j in pairs],
    }


def summarize_events(records):
    offsets = [v for record in records for v in record['onset_offsets_seconds']]
    return {
        'reference_events': sum(r['reference_events'] for r in records),
        'predicted_events': sum(r['predicted_events'] for r in records),
        'matched_events': sum(r['matched_events'] for r in records),
        'missing_events': sum(len(r['missing_events']) for r in records),
        'extra_events': sum(len(r['extra_events']) for r in records),
        'early_events': sum(v < 0 for v in offsets),
        'signed_onset_offset_median_seconds': float(np.median(offsets)) if offsets else None,
        'absolute_onset_offset_median_seconds': float(np.median(np.abs(offsets))) if offsets else None,
        'absolute_onset_offset_p90_seconds': float(np.quantile(np.abs(offsets), .9)) if offsets else None,
        'meaning': 'Order-preserving equal-ID alignment, maximum matches then minimum absolute onset distance. '
                   'Negative offsets are early. Missing events excluded from timing and counted separately. '
                   'Extra/missing are sequence alignment errors, not measured anomaly false positives/recall.',
    }
