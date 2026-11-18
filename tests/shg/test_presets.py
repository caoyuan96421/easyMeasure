import copy
import os
from pathlib import Path
import sys
os.environ['QT_QPA_PLATFORM']='offscreen'
sys.dont_write_bytecode=True
import shg_addon
STAGE=Path(__file__).resolve().parent

import numpy as np
import pytest
from shg_addon.sweep_tools import preset,PRESETS


def test_halfwave_definition_unchanged():
    import json
    installed=Path(shg_addon.__file__).parent/'presets/shg_halfwave.json'
    assert preset()==json.loads(installed.read_text())


def test_mems_nested_shape_and_identical_records():
    hw=preset();nested=preset('shg_halfwave_mems')
    assert nested['dimensions'][0]==hw['dimensions'][0]
    outer=nested['dimensions'][1]
    assert outer['points']==21
    assert outer['sweep']==[dict(channel='MEMS.theta',start=-10,stop=10)]
    assert outer['record']==[]
    assert nested['pre']['constants']==hw['pre']['constants']
    assert not any(c['channel']=='MEMS.Vd' for c in nested['pre']['constants'])
    assert nested['post']==hw['post']


def test_zero_uninitialized_drive_blocks_before_motion():
    code=preset('shg_halfwave_mems')['pre']['hook']
    # Test the added prerequisite with no hardware. The existing prepare_scan
    # invocation is separately covered by the original SHG regression checks.
    code=code[code.index('mems_drive ='):]
    for voltage in (0.,float('nan'),float('inf'),-1.):
        with pytest.raises(ValueError,match='Set MEMS.Vd'):
            exec(code,dict(np=np,get=lambda _:voltage))
    calls=[]
    exec(code,dict(np=np,get=lambda name:(calls.append(name) or 20.)))
    assert calls==['MEMS.Vd']


def test_ui_dropdown_loads_both_without_backend_requests(tmp_path):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from easymeasure.frontend.connection import BackendConnection
    from easymeasure.backend.sweep import normalize_definition
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat,QSettings.UserScope,str(tmp_path/'settings'))
    original_start=BackendConnection.start
    BackendConnection.start=lambda self:None
    from shg_addon.gui import SHGWindow
    app=QApplication.instance() or QApplication([])
    w=SHGWindow();w.timer.stop();w.preview_timer.stop()
    try:
        requests=[];w.conn.request=lambda *args,**kwargs:requests.append((args,kwargs))
        assert [w.shg_preset_choice.itemText(i) for i in range(len(PRESETS))]==[x[0] for x in PRESETS]
        w.open_shg_sweep()
        assert normalize_definition(w.editor.definition())['dimensions']==normalize_definition(preset())['dimensions']
        before=w.editor.definition()
        w.shg_preset_choice.setCurrentIndex(1)
        assert w.editor.definition()==before  # Selection alone does not overwrite a draft.
        from PySide6.QtWidgets import QPushButton
        button=next(b for b in w.editor.findChildren(QPushButton) if b.text()=='Load selected preset')
        button.click()
        result=w.editor.definition()
        assert normalize_definition(result)['dimensions']==normalize_definition(preset('shg_halfwave_mems'))['dimensions']
        assert not requests
        assert 'SHG spectra + CWT' in [w.data_tabs.tabText(i) for i in range(w.data_tabs.count())]
        # Direct SHG-page button still opens the original one-dimensional preset.
        w.open_shg_sweep(False)
        assert len(w.editor.definition()['dimensions'])==1
    finally:
        w.conn.stop();w.close();BackendConnection.start=original_start


def test_nested_simulated_sweep_records_fresh_frames(tmp_path,monkeypatch):
    from shg_addon.backend import SHGBackend
    from shg_addon.configuration import setup
    from shg_addon.actions import execute
    from shg_addon import spot_tracking
    from easymeasure.backend.instrument import Instrument,ChannelSpec
    from easymeasure.backend.storage import DataSet
    monkeypatch.setattr(spot_tracking,'HOME',tmp_path/'tracking')
    class MemoryMEMS(Instrument):
        INTERFACES=('none',)
        CHANNELS=[ChannelSpec('theta','rw'),ChannelSpec('Vd','rw')]
        def connect(self):self.values=dict(theta=0.,Vd=20.);self.written=[]
        def read(self,name):return self.values[name]
        def write(self,name,value):self.values[name]=value;self.written.append((name,value))
    b=SHGBackend(home=tmp_path/'backend',log_to_console=False)
    try:
        cfg=setup(True)
        cfg['instruments'][0]['options'].update(save_acquisitions=False,calibration_path=str(tmp_path/'calibration.json'))
        assert b.load_setup(setup=cfg)==[]
        b.drivers.register('memory_mems',MemoryMEMS)
        b.add_instrument('MEMS','memory_mems','NONE')
        ns=b.scripts.namespace('offline');ns['sleep']=lambda _:None
        execute(ns,'calibrate_all')
        d=preset('shg_halfwave_mems')
        d['dimensions'][0].update(points=3,pre_delay=0)
        d['dimensions'][0]['sweep'][0].update(stop=34)
        d['dimensions'][1].update(points=2,pre_delay=0)
        result=b._execute_sweep_item(d,{'id':None})
        assert result['status']=='finished',result
        data=DataSet.load(result['path'])
        assert data.data['ZWOSpectrum'].shape==(3,2,5496)
        assert len(np.unique(data.data['SHGframe']))==6
        assert b.instruments.get('MEMS').driver.written==[('theta',-10.),('theta',10.)]
        assert b.read_channel('MEMS.Vd')==20.
    finally:b.close()
