import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import numpy as np
import pytest
from shg_addon.actions import action_code, execute
from shg_addon.sweep_tools import preset
from .test_integration import backend, namespace


def test_editor_preset_acquires_a_new_frame_at_every_point(backend):
    execute(namespace(backend), 'calibrate_all')
    definition = preset()
    definition['dimensions'][0].update(points=3, pre_delay=0)
    definition['dimensions'][0]['sweep'][0].update(start=28, stop=34)
    first_frame = backend.read_channel('SHGframe')
    result = backend._execute_sweep_item(definition, {'id': None})
    assert result['status'] == 'finished', result
    from easymeasure.backend.storage import DataSet
    data = DataSet.load(result['path'])
    assert data.data['ZWOSpectrum'].shape == (3, 5496)
    np.testing.assert_array_equal(data.data['SHGframe'], first_frame + np.arange(1, 4))
    assert np.isfinite(data.data['ZWOPhase']).all()
    assert np.isfinite(data.data['ZWOAmplitude']).all()
    assert backend.read_channel('camera.exposure') == 1


def test_uncalibrated_preset_stops_before_sweeping(backend):
    writes = []
    original = backend.channels.set
    def record(name, value, **kwargs):
        writes.append(name)
        return original(name, value, **kwargs)
    backend.channels.set = record
    result = backend._execute_sweep_item(preset(), {'id': None})
    assert result['status'] == 'error'
    assert 'Calibrate SHG' in result['error']
    assert not writes


def test_standard_nested_sweep_records_inner_frames(backend):
    execute(namespace(backend), 'calibrate_all')
    definition = preset()
    definition['dimensions'][0].update(points=2, pre_delay=0)
    definition['dimensions'][0]['sweep'][0].update(start=28, stop=31)
    definition['dimensions'].append(dict(points=2,
        sweep=[dict(channel='V1', start=0, stop=.01)], record=[]))
    definition['pre']['hook'] = action_code('prepare_scan', voltage_channels=['V1'])
    backend.write_channel('supply.output1', 'ON')  # simulated supply only
    result = backend._execute_sweep_item(definition, {'id': None})
    assert result['status'] == 'finished', result
    from easymeasure.backend.storage import DataSet
    data = DataSet.load(result['path'])
    assert data.data['ZWOSpectrum'].shape == (2, 2, 5496)
    assert len(np.unique(data.data['SHGframe'])) == 4
    assert np.isfinite(data.data['ZWOPhase']).all()


def test_voltage_output_check_does_not_enable_outputs(backend):
    backend.write_channel('supply.output1', 'OFF')  # Emulator state persists by resource name.
    execute(namespace(backend), 'calibrate_all')
    with pytest.raises(ValueError, match='output is OFF'):
        execute(namespace(backend), 'prepare_scan', voltage_channels=['V1'])
    assert backend.read_channel('supply.output1') == 'OFF'


def test_reference_action_forces_switch_and_settles_before_capture():
    values = {'SampRef': 1, 'halfwave': 28}
    calls = []
    ns = dict(check=lambda: None, get=values.__getitem__,
              set=lambda n, v: calls.append(('set', n, v)),
              sleep=lambda s: calls.append(('sleep', s)),
              action=lambda ref: calls.append(('action', ref)))
    execute(ns, 'reference')
    assert calls == [('set', 'SampRef', 1), ('sleep', 1.5),
                     ('set', 'camera.halfwave_angle', 28),
                     ('action', 'camera.capture_reference')]


def test_editor_roundtrip_and_preserved_views(tmp_path):
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen',
               EASYMEASURE_HOME=str(tmp_path / 'isolated'))
    script = Path(__import__('shg_addon').__file__).with_name('standard_sweep_smoke.py')
    result = subprocess.run([sys.executable, str(script), str(tmp_path)],
                            env=env, capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr
