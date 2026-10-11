"""Summarize logged candidate-to-confirmation time; never action ground truth."""
import argparse
import hashlib
import json
from pathlib import Path


def summarize(data):
    active, events = {}, []
    methods = set()
    for frame in data['frames']:
        now = frame['time']
        present = {p['trackId'] for p in frame['people']}
        active = {key: value for key, value in active.items() if key in present}
        for person in frame['people']:
            temporal = person.get('temporal', {})
            if temporal.get('method'):
                methods.add(temporal['method'])
            candidate = temporal.get('candidate')
            key = person['trackId']
            old = active.get(key)
            held = temporal.get('candidateSeconds', 0)
            if not candidate:
                active.pop(key, None)
                continue
            if (old is None or old['candidate'] != candidate or
                    now-old['last'] > .75 or held < old['held']):
                old = dict(candidate=candidate, start=now, confirmed=False)
            if person['action'] == candidate and not old['confirmed']:
                events.append(dict(trackId=key, action=candidate,
                                   candidateStart=old['start'], confirmedAt=now,
                                   delaySeconds=round(now-old['start'], 4)))
                old['confirmed'] = True
            old.update(last=now, held=held)
            active[key] = old
    values = sorted(e['delaySeconds'] for e in events)
    def quantile(q):
        if not values:
            return None
        pos = (len(values)-1)*q
        lo = int(pos)
        return round(values[lo]+(values[min(lo+1,len(values)-1)]-values[lo])*(pos-lo), 4)
    return dict(metric='logged candidate onset to first confirmation',
                groundTruthAvailable=False, sampleFps=data['model']['sampleFps'],
                declaredMethod=data['model'].get('temporalModel'),
                observedMethods=sorted(methods), confirmations=len(events),
                p50Seconds=quantile(.5), p95Seconds=quantile(.95), events=events,
                limitations=['Candidate onset is not physical action onset.',
                             'Only confirmed candidates are summarized; no recall or accuracy estimate.',
                             'Sampling, evidence pauses and tracking resets affect this metric.'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('analysis', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    raw = args.analysis.read_bytes()
    result = summarize(json.loads(raw))
    result['analysisSha256'] = hashlib.sha256(raw).hexdigest()
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k != 'events'}, ensure_ascii=True))
