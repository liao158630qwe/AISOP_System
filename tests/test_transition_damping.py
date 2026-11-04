import pytest
from algorithms.temporal import ActionDecoder, DEFAULT_TRANSITION_DAMPING


def test_transition_damping_blocks_instant_illegal_jump():
    decoder = ActionDecoder()
    # 1. Establish stable 'raised' action
    for t in (0.0, 0.15, 0.3):
        decoder.update({'raised': 1.0}, t)
    assert decoder.action == 'raised'

    # 2. Spurious single-frame jump: 'bending' suddenly scores 0.70 while 'raised' drops to 0.45
    # Effective score for bending: 0.70 - 0.25 (penalty) = 0.45 < 0.60
    # The illegal jump is blocked!
    res = decoder.update({'bending': 0.70, 'raised': 0.45}, 0.4)
    assert res['candidate'] != 'bending', "Illegal jump from raised to bending should be blocked by damping"


def test_natural_transition_through_neutral_state_allowed():
    decoder = ActionDecoder()
    # 1. Establish stable 'raised' action
    for t in (0.0, 0.15, 0.3):
        decoder.update({'raised': 1.0}, t)
    assert decoder.action == 'raised'

    # 2. Transition through natural 'standing' (neutral position has 0 damping)
    for t in (0.4, 0.55, 0.7):
        decoder.update({'standing': 1.0, 'raised': 0.0}, t)
    assert decoder.action == 'standing'

    # 3. Transition from 'standing' to 'bending' (natural progression has 0 damping)
    for t in (0.85, 1.0, 1.15):
        decoder.update({'bending': 1.0, 'standing': 0.0}, t)
    assert decoder.action == 'bending', "Natural transition via neutral standing should succeed"


def test_custom_transition_damping_override():
    # Verify custom damping matrix works
    custom_damping = {('standing', 'walking'): 0.3}
    decoder = ActionDecoder(transition_damping=custom_damping)
    for t in (0.0, 0.15, 0.3):
        decoder.update({'standing': 1.0}, t)
    assert decoder.action == 'standing'

    # Walking score 0.75 - 0.30 = 0.45 < 0.60, blocked by custom damping
    res = decoder.update({'walking': 0.75, 'standing': 0.5}, 0.4)
    assert res['candidate'] != 'walking'
