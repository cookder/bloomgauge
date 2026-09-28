"""Exact serving-set identities and conservative two-model trial screening."""

import itertools, json, math, pathlib, re

PREFIX = '@combo:'
# Darkbloom's `[provider] memory_reserve_gb` default (ProviderSettings.memoryReserveGB).
DEFAULT_RESERVE_GB = 4
# Launch-agent variables that change Darkbloom's load admission in a way Bloomkeeper doesn't
# model: the memory cap fraction and a raised activation reserve (UnifiedMemoryCap
# resolvedCapFraction / resolvedActivationReserveBytes). The installer's other pass-throughs
# (drain, prefix cache, MLX cache and memory guard, KV backend, MTP, prefill) and MLX_/METAL_
# variables don't enter the load gate.
ADMISSION_ENV = ('DARKBLOOM_MEM_CAP_FRACTION', 'DARKBLOOM_ACTIVATION_RESERVE_GB')


def members(selection):
    if not isinstance(selection, str) or not selection:
        return []
    if not selection.startswith(PREFIX):
        return [selection]
    try:
        values = json.loads(selection[len(PREFIX) :])
        if (
            isinstance(values, list)
            and len(values) == 2
            and all(isinstance(v, str) and v and not v.startswith(PREFIX) for v in values)
            and len(set(values)) == 2
        ):
            return sorted(values)
    except (ValueError, TypeError):
        pass
    return []


def selection_key(models):
    if (
        not isinstance(models, list)
        or not 1 <= len(models) <= 2
        or any(not isinstance(v, str) or not v or v.startswith(PREFIX) for v in models)
        or len(set(models)) != len(models)
    ):
        return None
    return (
        models[0]
        if len(models) == 1
        else PREFIX + json.dumps(sorted(models), separators=(',', ':'))
    )


def same_selection(raw, selection):
    return bool(selection and selection_key(raw.get('advertised_models')) == selection)


def selection_label(selection):
    return ' + '.join(members(selection)) or 'Unknown selection'


def pair_budget(
    hardware, provider, models, weights, gpu_cache=0, config_reserve=DEFAULT_RESERVE_GB
):
    total = hardware.get('memoryTotalGB')
    available = hardware.get('memoryAvailableGB')
    resident = provider.get('memoryGB')
    values = [total, available, resident, gpu_cache, *weights]
    if (
        len(models) != 2
        or len(weights) != 2
        or any(
            not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v < 0
            for v in values
        )
    ):
        return None
    # v0.9 reserves activation space for the largest serving-model floor.
    # Add one GB KV per member, exceeding upstream's shared 1GB load minimum.
    # UnifiedMemoryCap.loadReserveBytes: memory_reserve_gb, but at least what the 90% cap
    # (with its 2 GiB floor) leaves the OS.
    reserve = max(config_reserve, total * 0.1, 2)
    activation = max(3.5 if m == 'gpt-oss-20b' else 5.5 for m in models)
    return {
        'afterUnloadGB': min(total, available + resident + gpu_cache),
        'requiredGB': sum(weights) + reserve + activation + len(models),
        'reserveGB': reserve,
        'weightsGB': sum(weights),
    }


def pair_candidates(
    rows,
    hardware,
    provider,
    gpu_cache=0,
    config_error=None,
    solo=(),
    config_reserve=DEFAULT_RESERVE_GB,
):
    """`solo` models are only served alone (gemma advertised with another model earned ~0.46x)."""
    output = []
    for a, b in itertools.combinations(sorted(rows, key=lambda r: r['id']), 2):
        models = [a['id'], b['id']]
        budget = pair_budget(
            hardware,
            provider,
            models,
            [a.get('memoryGB'), b.get('memoryGB')],
            gpu_cache,
            config_reserve,
        )
        reason = config_error or next(
            (
                r.get('reason') or 'A model is unavailable.'
                for r in (a, b)
                if not r.get('available')
            ),
            None,
        )
        if not reason and set(models) & set(solo):
            reason = 'Served alone only: advertised with another model it earns about half as much.'
        if not reason and not budget:
            reason = 'Waiting for memory readings.'
        if not reason and budget['requiredGB'] > hardware.get('memoryTotalGB', 0):
            reason = 'Combined model requirements exceed physical memory.'
        output.append(
            {
                'id': selection_key(models),
                'models': models,
                'name': ' + '.join(r['name'] for r in (a, b)),
                'available': reason is None,
                'reason': reason,
                'loadBudget': budget,
                'fitsNow': bool(not reason and budget['afterUnloadGB'] >= budget['requiredGB']),
            }
        )
    return sorted(
        output,
        key=lambda r: (
            not r['available'],
            (r['loadBudget'] or {}).get('requiredGB', math.inf),
            r['id'],
        ),
    )


def provider_settings(home, options):
    """What the load gate uses from provider.toml, never rewriting it: (settings, error).

    settings: {'reserveGB': `[provider] memory_reserve_gb` (Darkbloom's default 4 when
    absent or unreadable), 'memoryError': a memory setting Bloomkeeper can't model,
    'slotsError': why a pair can't be served}, each None when fine. error: why the file
    can't be read reliably.
    """
    settings = {'reserveGB': DEFAULT_RESERVE_GB, 'memoryError': None, 'slotsError': None}
    paths = [options[i + 1] for i, v in enumerate(options[:-1]) if v in ('--config', '-c')]
    if len(paths) > 1:
        return settings, 'Multiple custom configs need review before pair testing.'
    defaults = [
        pathlib.Path(home) / suffix
        for suffix in (
            '.config/darkbloom/provider.toml',
            'Library/Application Support/darkbloom/provider.toml',
            '.config/eigeninference/provider.toml',
            'Library/Application Support/eigeninference/provider.toml',
        )
    ]
    path = (
        pathlib.Path(paths[0]).expanduser()
        if paths
        else next((p for p in defaults if p.exists()), defaults[0])
    )
    if not path.is_absolute():
        return settings, 'Use an absolute provider config path before pair testing.'
    if not path.exists():
        return settings, 'The custom provider config is unavailable.' if paths else None
    try:
        text = path.read_text()
    except OSError:
        return settings, 'The provider config could not be checked.'
    section = ''
    reserve = None
    for line in text.splitlines():
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        if line.startswith('['):
            section = line[1:-1].strip()
            continue
        if '=' not in line:
            continue
        key, value = (part.strip() for part in line.split('=', 1))
        if value.startswith('{'):
            return settings, 'Inline provider config tables need review before pair testing.'
        if '\\' in key:
            return settings, 'Escaped provider config keys need review before pair testing.'
        parts = [part.strip().strip('"\'') for part in key.split('.')]
        key = parts[-1]
        key_section = '.'.join(([section.strip('"\'')] if section else []) + parts[:-1])
        if 'memory' in key or any(w in key for w in ('activation_reserve', 'kv_reserve')):
            # Darkbloom reads a whole number of GB (UInt64) from [provider] only.
            if (
                key_section == 'provider'
                and key == 'memory_reserve_gb'
                and re.fullmatch(r'[0-9]{1,6}', value)
                and reserve is None
            ):
                reserve = int(value)
                continue
            settings['memoryError'] = settings['memoryError'] or (
                'provider.toml sets %s, which Bloomkeeper can’t account for. It won’t '
                'move models on its own; restores and your own picks still work.' % key
            )
        if key == 'max_model_slots' and (
            key_section not in ('backend', '')
            or not re.fullmatch(r'[0-9]+', value)
            or int(value) < 2
        ):
            settings['slotsError'] = 'The provider must allow at least two resident model slots.'
    if reserve is not None:
        settings['reserveGB'] = reserve
    return settings, None


def configured_reserve_gb(home, options):
    """`memory_reserve_gb` for Bloomkeeper's load budgets (Darkbloom's default when unreadable)."""
    return provider_settings(home, options)[0]['reserveGB']


def combination_config_error(home, options, environment, require_pair=True, voluntary=True):
    """Why Bloomkeeper's load arithmetic can't be trusted here, or None; never rewrites it.

    `memory_reserve_gb` is read into the reserve rather than refused. With `voluntary` (the
    default), knobs that change Darkbloom's load admission and that Bloomkeeper can't model
    also count; they stop automatic moves only, never restores or the user's own picks.
    """
    if voluntary:
        knobs = sorted(k for k in environment if k in ADMISSION_ENV and environment[k] != '')
        if knobs:
            return (
                '%s is set for Darkbloom. Bloomkeeper can’t predict memory with it, so it '
                'won’t move models on its own; restores and your own picks still work.'
                % ' and '.join(knobs)
            )
    settings, error = provider_settings(home, options)
    return (
        error
        or (settings['memoryError'] if voluntary else None)
        or (settings['slotsError'] if require_pair else None)
    )
