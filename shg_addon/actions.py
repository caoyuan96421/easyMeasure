"""Reusable SHG actions for the installed easyMeasure script/point-action API.

The original easyMeasure package is deliberately unchanged. These snippets also
work with an already-running SHG backend; no backend restart is needed.
"""
from shg_addon.workflows import run, before_point


ACTIONS = {
    'acquire_point': 'Acquire a fresh sample (with optional reference refresh)',
    'prepare_scan': 'Check calibration and explicitly listed voltage outputs',
    'sample': 'Sample spectrum (switch optical path, then capture)',
    'reference': 'Reference spectrum (switch optical path, then capture)',
    'background': 'Take background with the shutter blocked',
    'refresh_reference': 'Refresh reference and restore sample path/exposure',
    'sample_center': 'Calibrate sample phase center',
    'reference_center': 'Calibrate reference phase center',
    'calibration_phase': 'Calibrate five-pair phase offset',
    'calibrate_all': 'Calibrate both centers and phase offset',
    'single_diagnostic': 'Single spectrum and CWT diagnostic',
    'loop_sample': 'Loop sample acquisition until aborted',
    'loop_reference': 'Loop reference acquisition until aborted',
    'autocal': 'Enable or disable periodic reference acquisition',
    'measure_retardation': 'Measure half-wave retardation',
    'fit_retardation': 'Fit the measured retardation',
    'corrected_move': 'Move corrected X/Y and polarization',
}


def execute(ns, name, **options):
    """Run in the caller's script namespace so pause, abort and aliases apply."""
    if name not in ACTIONS:
        raise ValueError('Unknown SHG action: ' + name)
    if name == 'prepare_scan':
        if ns['get']('camera.analysis_status') != 'calibrated':
            raise ValueError('Calibrate SHG centers and phase before measurement')
        for channel in options.get('voltage_channels', ()):
            from shg_addon.lab_profile import voltage_output
            output = voltage_output(channel)
            if str(ns['get'](output)).upper() not in ('ON', '1', 'TRUE'):
                raise ValueError(channel + ' output is OFF; review current limit and enable it explicitly')
    elif name == 'acquire_point':
        # Only coordinates in this point's dimension request a corrected move.
        corrected = options.get('corrected', bool(
            {'x', 'y', 'polarization'} & set(ns.get('setpoints', {}))))
        before_point(ns, corrected=corrected)
    else:
        run(ns, name, **options)


def action_code(name, **options):
    """A portable snippet for Queue scripts and any standard editor hook."""
    if name not in ACTIONS:
        raise ValueError('Unknown SHG action: ' + name)
    return ('from shg_addon.actions import execute\n'
            'execute(globals(), %r, **%r)' % (name, options))
