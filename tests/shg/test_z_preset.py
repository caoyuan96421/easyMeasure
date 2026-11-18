import os,sys,json,copy
from pathlib import Path
os.environ['QT_QPA_PLATFORM']='offscreen'
sys.dont_write_bytecode=True
import shg_addon
HERE=Path(__file__).resolve().parent

import pytest
import numpy as np
from shg_addon.sweep_tools import preset,PRESETS
from shg_addon import z_control as z


def setup():
    return dict(instruments=[dict(name='box',driver='cygnus16430')],
                aliases=[dict(name='Vlock',target='box.voltage1'),dict(name='Vz',target='box.voltage5')])


def fake_namespace(measured=70.,lock_output='ON',z_output='ON'):
    events=[]
    values={'Vlock':0.,'Vz':0.,'box.Vmeas1':measured,'box.output1':lock_output,'box.output5':z_output}
    def get(name):events.append(('get',name));return values[name]
    def set_value(name,value):events.append(('set',name,value));values[name]=value
    ns=dict(get=get,set=set_value,state={},check=lambda:None,sleep=lambda _:None,
            wait_until=lambda condition,**kwargs:condition(),log=lambda *args:None)
    return ns,events,values


def test_existing_two_presets_unchanged():
    root=Path(shg_addon.__file__).parent/'presets'
    for key in ('shg_halfwave','shg_halfwave_mems'):
        assert preset(key)==json.loads((root/(key+'.json')).read_text())


def test_three_dimensions_identical_records_and_z_order():
    d=preset('shg_halfwave_mems_z')
    old=preset('shg_halfwave_mems')
    assert [x['points'] for x in d['dimensions']]==[61,21,9]
    assert d['dimensions'][0]['record']==old['dimensions'][0]['record']
    assert d['dimensions'][1]==old['dimensions'][1]
    assert d['dimensions'][2]['sweep']==[dict(channel='Vz',start=0.,stop=40.)]
    assert 'prepare(globals())' in d['pre']['hook']
    assert d['dimensions'][2]['hooks']['pre_sweep']['enabled']
    assert d['dimensions'][0]['hooks']['pre_record']['code'].index('guard(globals())') < d['dimensions'][0]['hooks']['pre_record']['code'].index("'acquire_point'")
    assert d['post']==old['post']


def test_personalized_range_step_and_descending():
    a=preset('shg_halfwave_mems_z',z_start=10,z_stop=30,z_step=2.5)
    assert a['dimensions'][2]['points']==9
    assert a['dimensions'][2]['sweep'][0]['start']==10
    b=preset('shg_halfwave_mems_z',z_start=40,z_stop=0,z_step=10)
    assert b['dimensions'][2]['points']==5
    for args in (dict(z_start=-1),dict(z_stop=41),dict(z_step=0),dict(z_step=3)):
        with pytest.raises(ValueError):preset('shg_halfwave_mems_z',**args)


def test_prepare_sets_lock_first_and_never_writes_z():
    ns,events,values=fake_namespace()
    z.prepare(ns,setup())
    writes=[e for e in events if e[0]=='set']
    assert writes==[('set','Vlock',70.)]
    assert values['Vz']==0
    assert ('get','box.Vmeas1') in events


def test_off_lock_output_blocks_without_writes():
    ns,events,_=fake_namespace(lock_output='OFF')
    with pytest.raises(ValueError,match='Vlock output is OFF'):z.prepare(ns,setup())
    assert not [e for e in events if e[0]=='set']


def test_failed_measured_lock_blocks_any_z_write():
    for measured in (0.,60.,float('nan')):
        ns,events,_=fake_namespace(measured=measured)
        with pytest.raises(ValueError,match='did not reach'):z.prepare(ns,setup())
        assert [e for e in events if e[0]=='set']==[('set','Vlock',70.)]


def test_lost_lock_detected_before_next_z_or_acquisition():
    ns,events,values=fake_namespace()
    z.prepare(ns,setup())
    values['box.Vmeas1']=30.
    with pytest.raises(ValueError,match='Vz blocked'):z.guard(ns)
    values['box.Vmeas1']=70.;values['Vlock']=60.
    with pytest.raises(ValueError,match='Vz blocked'):z.guard(ns)


def test_z_output_off_is_not_enabled_automatically():
    ns,events,_=fake_namespace(z_output='OFF')
    with pytest.raises(ValueError,match='Vz output is OFF'):z.prepare(ns,setup())
    assert [e for e in events if e[0]=='set']==[('set','Vlock',70.)]


def test_alias_resolution_handles_current_instrument_names_and_rejects_same_output():
    d=setup();d['instruments'][0]['name']='DanielBox'
    for a in d['aliases']:a['target']=a['target'].replace('box.','DanielBox.')
    assert z.bindings(d)['Vlock']['measured']=='DanielBox.Vmeas1'
    d['aliases'][1]['target']='DanielBox.voltage1'
    with pytest.raises(ValueError,match='different supply'):z.bindings(d)


def test_ui_edits_z_parameters_and_loads_without_any_backend_command(tmp_path):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication,QPushButton
    from easymeasure.frontend.connection import BackendConnection
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat,QSettings.UserScope,str(tmp_path/'settings'))
    old=BackendConnection.start;BackendConnection.start=lambda _:None
    from shg_addon.gui import SHGWindow
    app=QApplication.instance() or QApplication([])
    w=SHGWindow();w.timer.stop();w.preview_timer.stop()
    try:
        calls=[];w.conn.request=lambda *args,**kwargs:calls.append(args)
        assert w.shg_preset_choice.count()==3
        w.shg_preset_choice.setCurrentIndex(2)
        w.shg_z_fields['z_start'].setValue(10)
        w.shg_z_fields['z_stop'].setValue(30)
        w.shg_z_fields['z_step'].setValue(2.5)
        button=next(x for x in w.editor.findChildren(QPushButton) if x.text()=='Load selected preset')
        button.click()
        dim=w.editor.definition()['dimensions'][2]
        assert dim['points']==9 and dim['sweep'][0]['start']==10 and dim['sweep'][0]['stop']==30
        assert not calls
        w.open_shg_sweep()
        assert len(w.editor.definition()['dimensions'])==1
    finally:w.conn.stop();w.close();BackendConnection.start=old


def test_simulated_three_dimensional_scan_orders_lock_then_z(tmp_path,monkeypatch):
    from easymeasure.backend.instrument import Instrument,ChannelSpec
    from shg_addon.backend import SHGBackend
    from shg_addon.configuration import setup as simulated_setup
    from shg_addon.actions import execute
    from shg_addon import spot_tracking
    from easymeasure.backend.storage import DataSet
    monkeypatch.setattr(spot_tracking,'HOME',tmp_path/'tracking')
    class MemorySupply(Instrument):
        INTERFACES=('none',)
        CHANNELS=[ChannelSpec(n,'rw','str' if 'output' in n else 'float') for n in ('lock','z','theta','Vd','lock_measured','lock_output','z_output')]
        def connect(self):self.values=dict(lock=0.,z=0.,theta=0.,Vd=20.,lock_measured=0.,lock_output='ON',z_output='ON');self.writes=[]
        def read(self,n):return self.values[n]
        def write(self,n,v):
            if n=='z':assert self.values['lock']==70 and self.values['lock_measured']==70
            self.values[n]=v;self.writes.append((n,v))
            if n=='lock':self.values['lock_measured']=v
    b=SHGBackend(home=tmp_path/'backend',log_to_console=False)
    try:
        cfg=simulated_setup(True);cfg['instruments'][0]['options'].update(save_acquisitions=False,calibration_path=str(tmp_path/'cal.json'))
        assert b.load_setup(setup=cfg)==[]
        b.drivers.register('memory_supply',MemorySupply);b.add_instrument('testbox','memory_supply','NONE')
        for name,target in (('Vlock','lock'),('Vz','z')):b.define_alias(name=name,target='testbox.'+target,min=0,max=80)
        b.define_alias(name='memstheta',target='testbox.theta',min=-10,max=10)
        b.define_alias(name='memsdrive',target='testbox.Vd',min=0,max=150)
        resolved=dict(Vlock=dict(output='testbox.lock_output',measured='testbox.lock_measured'),Vz=dict(output='testbox.z_output',measured='testbox.z'))
        monkeypatch.setattr(z,'bindings',lambda _:resolved)
        monkeypatch.setattr(z,'_current_setup',lambda:{})
        ns=b.scripts.namespace('offline');ns['sleep']=lambda _:None
        execute(ns,'calibrate_all')
        d=preset('shg_halfwave_mems_z')
        for dim in d['dimensions']:dim['points']=2;dim['pre_delay']=0
        d['dimensions'][0]['sweep'][0]['stop']=31
        d['dimensions'][1]['sweep'][0]['channel']='memstheta'
        d['dimensions'][2]['sweep'][0]['stop']=5
        d['pre']['hook']=d['pre']['hook'].replace('MEMS.Vd','memsdrive')
        result=b._execute_sweep_item(d,{'id':None})
        assert result['status']=='finished',result
        data=DataSet.load(result['path'])
        assert data.data['ZWOSpectrum'].shape==(2,2,2,5496)
        assert len(np.unique(data.data['SHGframe']))==8
        writes=b.instruments.get('testbox').driver.writes
        assert writes[0]==('lock',70.)
        assert [x for x in writes if x[0]=='z']==[('z',0.),('z',5.)]
        assert b.read_channel('Vlock')==70.
    finally:b.close()
