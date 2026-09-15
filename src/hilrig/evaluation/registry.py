"""Dispatcher registry for compiled assertion operations."""

from hilrig.evaluation import analogue, digital, pwm
from hilrig.evaluation.common import AssertionHandler

EVALUATOR_REGISTRY: dict[tuple[str, str], AssertionHandler] = {
    ("digital_input", "state_at_tick"): digital.evaluate_state_at_tick,
    ("digital_input", "remain_high"): digital.evaluate_remain_high,
    ("digital_input", "remain_low"): digital.evaluate_remain_low,
    ("digital_input", "transition"): digital.evaluate_transition,
    ("pwm_input", "period_near"): pwm.evaluate_period_near,
    ("pwm_input", "frequency_near"): pwm.evaluate_frequency_near,
    ("pwm_input", "duty_cycle_near"): pwm.evaluate_duty_cycle_near,
    ("pwm_input", "waveform_near"): pwm.evaluate_waveform_near,
    ("pwm_input", "frequency_remain_within"): pwm.evaluate_frequency_remain_within,
    ("pwm_input", "duty_cycle_remain_within"): pwm.evaluate_duty_cycle_remain_within,
    ("analogue_input", "near"): analogue.evaluate_near,
    ("analogue_input", "within"): analogue.evaluate_within,
    ("analogue_input", "remain_within"): analogue.evaluate_remain_within,
    ("analogue_input", "remain_above"): analogue.evaluate_remain_above,
    ("analogue_input", "remain_below"): analogue.evaluate_remain_below,
}
