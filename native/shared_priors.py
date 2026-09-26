"""Shared starting curves for the shadow estimator.

shared-priors.json ships with the app. It is built by native/build_priors.py
(development tool) from Andrew's own history and, later, from pay summaries
that other Bloomkeeper users chose to share. Each model's curve is
level * (pressure + 0.01) ** exponent in $/warm-hour, like demand_curves.
Anything malformed is ignored, so a bad file only means no starting curves.

Curves record the hardware ('chip family|memory band') they were measured on.
A Mac gets its own class's curve when one exists; otherwise the all-hardware
curve, flagged sameHardware=False so the estimator widens its range. Pay isn't
scaled across chips: there's no data yet on how it differs.
"""

import json
import math
from pathlib import Path

PATH = Path(__file__).with_name('shared-priors.json')
MAX_MODELS = 64


def valid(curve):
    ok = lambda v, lo, hi: type(v) in (int, float) and math.isfinite(v) and lo <= v <= hi
    rng = curve.get('range') if isinstance(curve, dict) else None
    return (
        isinstance(curve, dict)
        and ok(curve.get('level'), 0, 5)
        and ok(curve.get('exponent'), 0, 2)
        and (
            rng is None
            or (
                isinstance(rng, list)
                and len(rng) == 2
                and ok(rng[0], 0, 10)
                and ok(rng[1], rng[0], 10)
            )
        )
    )


def _curves(models, same):
    if not isinstance(models, dict):
        return {}
    return {
        m: {
            'level': c['level'],
            'exponent': c['exponent'],
            'range': tuple(c['range']) if c.get('range') else None,
            'sameHardware': same(c),
        }
        for m, c in list(models.items())[:MAX_MODELS]
        if isinstance(m, str) and 0 < len(m) <= 200 and valid(c)
    }


def parse(data, hardware=None):
    if (
        not isinstance(data, dict)
        or data.get('schema') != 1
        or not isinstance(data.get('models'), dict)
    ):
        return {}
    listed = lambda c: isinstance(c.get('hardware'), list) and hardware in c['hardware']
    result = _curves(data['models'], lambda c: hardware is None or listed(c))
    classes = data.get('byHardware')
    if hardware and isinstance(classes, dict):
        result.update(_curves(classes.get(hardware), lambda c: True))
    return result


_cache = {}


def load(path=PATH, hardware=None):
    key = (str(path), hardware)
    if key not in _cache:
        try:
            _cache[key] = parse(json.loads(Path(path).read_text()), hardware)
        except (OSError, ValueError):
            _cache[key] = {}
        while len(_cache) > 8:
            del _cache[next(iter(_cache))]
    return _cache[key]
