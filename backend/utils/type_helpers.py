import math

_SENTINEL = object()


def safe_float(value, default=0.0, nonfinite_to=_SENTINEL):
    if value is None:
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(f):
        return default if nonfinite_to is _SENTINEL else nonfinite_to
    return f


def safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def scrub_nonfinite(obj):
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: scrub_nonfinite(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub_nonfinite(v) for v in obj]
    if isinstance(obj, tuple):
        return [scrub_nonfinite(v) for v in obj]
    return obj
