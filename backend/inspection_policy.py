"""Sampling gaps must respect the detector's evidence requirements."""


def complete_visual_coverage(result):
    """A completed position inspection does not require uninterrupted action video.

    Only the detector's explicit coverage verdict qualifies. A UI count (which
    can include empty holes), or a temporal action/sequence PASS, does not.
    Gaps remain in the record even when they do not invalidate this verdict.
    """
    sequence = result.get('sequence') or {}
    expected = sequence.get('positionsExpected', 0)
    operations = sequence.get('operationsExpected', 0)
    preassembled = sequence.get('preassembledExpected', 0)
    return (sequence.get('verificationMode') == 'visual-cumulative'
            and sequence.get('configured') is True and sequence.get('status') == 'PASS'
            and expected > 0 and operations + preassembled == expected
            and sequence.get('positionsConfirmed') == expected
            and sequence.get('operationsCompleted') == operations
            and sequence.get('preassembledConfirmed') == preassembled)
