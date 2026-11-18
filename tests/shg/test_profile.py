import copy
import json
from pathlib import Path
import numpy as np
from shg_addon.backend import SHGBackend
from shg_addon.configuration import setup
from shg_addon.lab_profile import (cygnus_aliases, laboratory_setup, restore_on_start,
                               voltage_rows, voltage_output)


def simulated_profile():
    config = setup(True)
    for item in config['instruments']:
        if item['name'] == 'supply':
            item['name'] = 'cygnus'
    for alias in config['aliases']:
        alias['target'] = alias['target'].replace('supply.', 'cygnus.')
    config['aliases'].extend(cygnus_aliases('cygnus'))
    return config


def test_mapping_and_control_routes():
    config = simulated_profile()
    mapping = {a['name']: a['target'] for a in config['aliases']}
    assert [mapping[name] for name in ['V_z','V_a','V_b','V_c','V_l']] == [
        f'cygnus.voltage{k}' for k in [5,4,3,2,1]]
    rows = voltage_rows(config)
    assert [(r[0], r[1], r[2]) for r in rows[:5]] == [
        (name, 'cygnus', str(k)) for name, k in [('V_z',5),('V_a',4),('V_b',3),('V_c',2),('V_l',1)]]
    assert len({(r[1], r[2]) for r in rows}) == len(rows)
    assert voltage_output('V_z', config) == 'cygnus.output5'


def test_saved_setup_restores_connections_aliases_without_output_values(tmp_path):
    home = tmp_path / 'backend'
    first = SHGBackend(home=home, log_to_console=False)
    try:
        config = simulated_profile()
        config['instruments'][0]['options'].update(calibration_path='', save_acquisitions=False)
        assert first.load_setup(setup=config) == []
        # Only a simulator is written in this persistence test.
        first.write_channel('V_z', 1.25, ramp=False)
        first.write_channel('cygnus.output5', 'ON')
        saved = first.save_setup()
        assert all(set(i) == {'name','driver','address','options'} for i in saved['instruments'])
        assert not any(a.get('value') is not None for a in saved['aliases'])
    finally:
        first.close()
    second = SHGBackend(home=home, log_to_console=False)
    try:
        assert restore_on_start(second) == []
        assert 'cygnus' in second.instruments.names()
        assert second.channels.info('V_z')['target'] == 'cygnus.voltage5'
        # Restore reconnects without resetting a supply's existing state.
        assert second.read_channel('V_z') == 1.25
        assert second.read_channel('cygnus.output5') == 'ON'
    finally:
        second.close()


def test_profile_disables_reset_and_supports_port_override(tmp_path):
    path = tmp_path / 'laboratory.json'
    config = simulated_profile()
    item = next(i for i in config['instruments'] if i['driver'] == 'cygnus16430')
    item['options']['reset'] = True
    path.write_text(json.dumps(config))
    prepared = laboratory_setup(path, cygnus_port='COM6')
    item = next(i for i in prepared['instruments'] if i['driver'] == 'cygnus16430')
    assert item['name'] == 'cygnus' and item['address'] == 'COM6'
    assert item['options']['reset'] is False
    assert json.loads(path.read_text())['instruments'][-1]['options']['reset'] is True
