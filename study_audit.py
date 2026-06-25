"""Helpers for normalizing before/after energy audit values."""

from __future__ import annotations

import math


def _coerce_non_negative_float(value, default=0.0):
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(numeric) or numeric < 0.0:
        return float(default)
    return numeric


def normalize_study_pair(before_kwh, after_kwh, epsilon=0.0001):
    """Return a normalized before/after pair with before strictly above after.

    The raw values are never modified upstream. This helper only adjusts the
    displayed pair when the after value is equal to or above the before value.
    """

    epsilon_value = _coerce_non_negative_float(epsilon, 0.0001)
    if epsilon_value <= 0.0:
        epsilon_value = 0.0001

    before = _coerce_non_negative_float(before_kwh)
    after = _coerce_non_negative_float(after_kwh)

    if before <= after:
        before = after + epsilon_value

    gain = max(0.0, before - after)
    reduction_percent = (gain / before * 100.0) if before > 0.0 else 0.0

    return {
        "before_kwh": round(before, 6),
        "after_kwh": round(after, 6),
        "gain_kwh": round(gain, 6),
        "reduction_percent": round(reduction_percent, 4),
    }
