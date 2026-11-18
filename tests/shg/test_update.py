import math
import numpy as np
import pytest
from .test_integration import backend, namespace
from shg_addon.actions import execute


def test_native_user_actions_capture_with_new_channel_api(backend):
    execute(namespace(backend), 'calibrate_all')
    actions = {a['name'] for a in backend.list_user_actions()}
    assert {'shg_sample','shg_reference','shg_acquire_point'} <= actions
    frame = backend.read_channel('SHGframe')
    result = backend.write_channel('shg_sample')
    assert result['status'] == 'finished'
    assert backend.read_channel('SHGframe') == frame + 1
    assert np.isfinite(backend.read_channel('ZWOPhase'))
    result = backend.write_channel('shg_reference')
    assert result['status'] == 'finished'
    assert backend.read_channel('SHGframe') == frame + 2


def test_shg_ni_driver_keeps_output_only_behavior(backend):
    driver = backend.instruments.get('stage').driver
    assert not any(name.startswith('ctr') for name in driver.tasks)
    before = dict(driver.tasks)
    assert math.isnan(backend.read_channel('SampRef'))
    assert driver.tasks == before  # Reading commanded state doesn't reserve/write a DO line.
    backend.write_channel('SampRef', 1)
    assert backend.read_channel('SampRef') == 1
    assert driver.ao_min == 0 and driver.ao_max == 10
    with pytest.raises(Exception, match='limited'):
        driver.write('ao0', -1)


def test_rescan_preserves_shg_driver(backend):
    backend.rescan_drivers()
    from shg_addon.ni6211_shg import SHGNI6211
    assert backend.drivers.get('ni6211') is SHGNI6211
