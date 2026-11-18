"""Vlock-before-Vz sequencing for the SHG three-dimensional sweep preset.

Import/validation never writes outputs. prepare() is called only when a user
runs the preset. guard() precedes every Vz set and every spectrum acquisition.
"""
import math
import re
from shg_addon.configuration import ROOT

LOCK_VOLTAGE = 70.0
LOCK_MEASURED_TOLERANCE = 0.5  # V; allow measurement noise, not a different setpoint.
LOCK_SETTLE_TIMEOUT = 10.0


def bindings(setup):
    aliases = {a['name']:a for a in setup.get('aliases', [])}
    drivers = {i['name']:i['driver'] for i in setup.get('instruments', [])}
    resolved = {}
    for name in ('Vlock', 'Vz'):
        a = aliases.get(name)
        if not a or a.get('read_only'):
            raise ValueError('Define a writable '+name+' alias before running the z preset')
        if float(a.get('scale', 1)) != 1 or float(a.get('offset', 0)) != 0:
            raise ValueError(name+' must use voltage units directly (scale 1, offset 0)')
        match = re.fullmatch(r'(.+)\.voltage([1-8])', a['target'])
        if not match or drivers.get(match[1]) != 'cygnus16430':
            raise ValueError(name+' must target a Cygnus voltage channel')
        resolved[name] = dict(output=match[1]+'.output'+match[2],
                              measured=match[1]+'.Vmeas'+match[2])
    if resolved['Vlock']['output'] == resolved['Vz']['output']:
        raise ValueError('Vlock and Vz must be different supply outputs')
    return resolved


def _current_setup():
    # Get current alias definitions, not a potentially outdated saved profile.
    # This endpoint reads metadata only; it does not query instrument channels.
    from easymeasure.client import BackendClient
    client = BackendClient.connect(ROOT/'state/backend/backend.json')
    try:
        return client.call('get_setup')
    finally:
        client.close()


def _outputs_on(ns, channels, names=('Vlock', 'Vz')):
    for name in names:
        if str(ns['get'](channels[name]['output'])).upper() not in ('ON', '1', 'TRUE'):
            raise ValueError(name+' output is OFF. Enable the intended output explicitly before running the z preset; Vz was not changed.')


def _lock_ready(ns, channels):
    command = float(ns['get']('Vlock'))
    measured = float(ns['get'](channels['Vlock']['measured']))
    return (math.isfinite(command) and abs(command-LOCK_VOLTAGE) <= 1e-6
            and math.isfinite(measured) and abs(measured-LOCK_VOLTAGE) <= LOCK_MEASURED_TOLERANCE)


def guard(ns):
    ns['check']()
    channels = ns['state'].get('shg_z_channels')
    if channels is None:
        channels = bindings(_current_setup())
        ns['state']['shg_z_channels'] = channels
    _outputs_on(ns, channels)
    if not _lock_ready(ns, channels):
        raise ValueError('Vz blocked: Vlock must be set to 70 V and its measured output must be within 0.5 V of 70 V. No new Vz command or acquisition was issued.')


def prepare(ns, setup=None):
    ns['check']()
    channels = bindings(_current_setup() if setup is None else setup)
    ns['state']['shg_z_channels'] = channels
    _outputs_on(ns, channels, names=('Vlock',))
    # The normal alias ramp/limits and abort checks remain in force.
    ns['set']('Vlock', LOCK_VOLTAGE)
    ready = ns['wait_until'](lambda: _lock_ready(ns, channels), timeout=LOCK_SETTLE_TIMEOUT, interval=.25)
    if not ready:
        raise ValueError('Vz blocked: Vlock did not reach 70 V (measured tolerance ±0.5 V) after settling; check output state, load and current limit. Vz was not changed.')
    guard(ns)
    ns['log']('z preset: Vlock setpoint 70 V and measured output verified; Vz sweep may now start. Vlock is retained on finish/abort.')
