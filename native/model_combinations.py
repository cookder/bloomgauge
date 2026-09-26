"""Exact serving-set identities and conservative two-model trial screening."""

import itertools, json, math, pathlib, re

PREFIX = '@combo:'


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


def pair_budget(hardware, provider, models, weights, gpu_cache=0):
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
    reserve = max(4, total * 0.1)
    activation = max(3.5 if m == 'gpt-oss-20b' else 5.5 for m in models)
    return {
        'afterUnloadGB': min(total, available + resident + gpu_cache),
        'requiredGB': sum(weights) + reserve + activation + len(models),
        'reserveGB': reserve,
        'weightsGB': sum(weights),
    }


def pair_candidates(rows, hardware, provider, gpu_cache=0, config_error=None):
    output = []
    for a, b in itertools.combinations(sorted(rows, key=lambda r: r['id']), 2):
        models = [a['id'], b['id']]
        budget = pair_budget(
            hardware, provider, models, [a.get('memoryGB'), b.get('memoryGB')], gpu_cache
        )
        reason = config_error or next(
            (
                r.get('reason') or 'A model is unavailable.'
                for r in (a, b)
                if not r.get('available')
            ),
            None,
        )
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


def combination_config_error(home, options, environment, require_pair=True):
    """Only accept known default memory policy and >=2 slots; never rewrite it."""
    if any(str(k).startswith(('DARKBLOOM_', 'MLX_', 'METAL_')) for k in environment):
        return 'Custom Darkbloom, MLX or Metal environment overrides are not supported by automatic switching. Keep your settings and use Help & feedback to review compatibility.'
    paths = [options[i + 1] for i, v in enumerate(options[:-1]) if v in ('--config', '-c')]
    if len(paths) > 1:
        return 'Multiple custom configs need review before pair testing.'
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
        return 'Use an absolute provider config path before pair testing.'
    if not path.exists():
        return 'The custom provider config is unavailable.' if paths else None
    try:
        text = path.read_text()
    except OSError:
        return 'The provider config could not be checked.'
    section = ''
    default_reserve_seen = False
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
            return 'Inline provider config tables need review before pair testing.'
        if '\\' in key:
            return 'Escaped provider config keys need review before pair testing.'
        parts = [part.strip().strip('"\'') for part in key.split('.')]
        key = parts[-1]
        key_section = '.'.join(([section.strip('"\'')] if section else []) + parts[:-1])
        if 'memory' in key or any(w in key for w in ('activation_reserve', 'kv_reserve')):
            # ProviderSettings defaults to UInt64(4), including freshly generated
            # provider.toml. Both Bloomkeeper load budgets already reserve at least 4 GB.
            # Accept only this exact known default; do not erase custom settings.
            if (
                key_section == 'provider'
                and key == 'memory_reserve_gb'
                and value == '4'
                and not default_reserve_seen
            ):
                default_reserve_seen = True
                continue
            return 'Provider memory settings differ from the defaults Bloomkeeper supports. The standard provider memory_reserve_gb = 4 is supported. Keep your configuration and use Help & feedback to review compatibility; do not delete it.'
        if (
            require_pair
            and key == 'max_model_slots'
            and (
                key_section not in ('backend', '')
                or not re.fullmatch(r'[0-9]+', value)
                or int(value) < 2
            )
        ):
            return 'The provider must allow at least two resident model slots.'
    return None
