"""Saved laboratory connections/aliases, never output values or enable states."""
from pathlib import Path
import json
from shg_addon.configuration import ROOT

LAB_SETUP = ROOT / 'state' / 'laboratory_setup.json'
CYGNUS_NAMES = [('V_z', 5), ('V_a', 4), ('V_b', 3), ('V_c', 2), ('V_l', 1)]


def cygnus_aliases(instrument):
    return [dict(name=name, target=f'{instrument}.voltage{channel}',
                 min=0, max=150, ramp_rate=1, unit='V',
                 description=f'Cygnus output {channel}') for name, channel in CYGNUS_NAMES]


def laboratory_setup(path=None, cygnus_port=''):
    path = Path(path) if path is not None else LAB_SETUP
    if not path.exists():
        return None
    config = json.loads(path.read_text(encoding='utf-8'))
    for item in config.get('instruments', []):
        if item['driver'] == 'cygnus16430':
            if cygnus_port:
                item['address'] = cygnus_port
            # Reconnecting must not reset voltage setpoints or output relays.
            item.setdefault('options', {})['reset'] = False
    return config


def restore_on_start(backend, saved_path=None):
    """Restore the most recent autosave, falling back to the named lab profile."""
    autosave = backend.home / 'autosave_setup.json'
    path = autosave if autosave.exists() else (Path(saved_path) if saved_path else LAB_SETUP)
    if not path.exists():
        return []
    config = laboratory_setup(path)
    # Instrument setup JSON holds definitions/options, not last setpoint values.
    return backend.load_setup(setup=config)


def voltage_rows(config):
    """Resolve visible voltage controls from the profile's actual instrument names."""
    instruments = {item['name']: item['driver'] for item in config.get('instruments', [])}
    aliases = {a['name']: a for a in config.get('aliases', [])}
    rows = []
    used = set()
    order = [name for name, _ in CYGNUS_NAMES] + ['Vbg'] + [f'V{k}' for k in range(1, 9)]
    for name in order:
        alias = aliases.get(name)
        if not alias or alias['target'] in used:
            continue
        instrument, channel = alias['target'].split('.', 1)
        driver = instruments.get(instrument)
        if driver == 'cygnus16430' and channel.startswith('voltage'):
            suffix = channel[7:]
            if suffix not in {str(k) for k in range(1, 9)}:
                continue
            rows.append((name, instrument, suffix, alias.get('min') or 0,
                         alias.get('max') if alias.get('max') is not None else 150, .04))
        elif driver == 'keithley2400' and channel == 'voltage':
            rows.append((name, instrument, '', alias.get('min', -75), alias.get('max', 75), .001))
        else:
            continue
        used.add(alias['target'])
    return rows


def voltage_output(channel, config=None):
    config = config if config is not None else laboratory_setup()
    for name, instrument, suffix, *_ in voltage_rows(config or {}):
        if name == channel:
            return instrument + '.output' + suffix
    # Preserve the original demo/setup conventions when no saved alias matches.
    if channel == 'Vbg':
        return 'gate.output'
    if channel in {f'V{k}' for k in range(1, 9)}:
        return 'supply.output' + channel[1:]
    raise ValueError('Unknown voltage alias: ' + channel)
