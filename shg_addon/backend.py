import os
import sys
from pathlib import Path
from easymeasure.backend.service import Backend
from easymeasure.backend.server import Server,read_runtime_file,backend_alive
from shg_addon.configuration import ROOT,setup

class SHGBackend(Backend):
    COMMANDS=Backend.COMMANDS+("save_microscope_snapshot",)
    def __init__(self,**kwargs):
        options=dict(driver_paths=[str(ROOT/"driver_modules")],auto_ppt=False,folders_confirmed=True)
        options.update(kwargs.pop("settings",{}))
        super().__init__(settings=options,**kwargs)
        from shg_addon.ni6211_shg import SHGNI6211
        self.drivers.register("ni6211", SHGNI6211)
        self._decorate_runner(self.scripts)

    def _decorate_runner(self, runner):
        original=runner.namespace
        def namespace(name):
            ns=original(name)
            def require_optics():
                from shg_addon.shg_mode import mode
                kind,message=mode(self.list_instruments())
                if kind not in ("real","simulation"):raise ValueError(message)
                if kind=="simulation":ns["log"]("SIMULATED acquisition: synthetic spectrum; no physical mirror movement")
            ns["shg_require_optics"]=require_optics
            ns["cleanup_set"]=lambda name,value:self.channels.set(name,value,ramp=False)
            # Existing SHG scripts/saved sweeps keep working; the actual action
            # runs through upstream's new write-only channel API.
            ns["action"]=lambda ref:ns["set"](ref)
            return ns
        runner.namespace=namespace
        return runner

    def _action_runner(self, control):
        return self._decorate_runner(super()._action_runner(control))

    def _options(self,driver,options):
        defaults=dict(self.drivers.get(driver).DEFAULT_OPTIONS)
        defaults.update(options or {})
        return defaults

    def save_microscope_snapshot(self,path):
        import numpy as np
        from PIL import Image
        destination=Path(path)
        destination.parent.mkdir(parents=True,exist_ok=True)
        if destination.exists(): raise FileExistsError("Choose a new snapshot filename")
        frame=self.read_channel("microscope.image")
        with destination.open("xb") as stream:
            Image.fromarray(np.clip(frame,0,255).astype(np.uint8)).save(stream,format="PNG")
        return dict(path=str(destination),width=int(frame.shape[1]),height=int(frame.shape[0]))

    def add_instrument(self,name,driver,address="NONE",options=None,connect=True):
        return super().add_instrument(name,driver,address,self._options(driver,options),connect)

    def load_setup(self,setup=None,path=None,replace=True,connect=True):
        import copy
        from easymeasure.backend.setup_io import read
        prepared=copy.deepcopy(read(path) if path else setup)
        for item in prepared.get("instruments",[]):
            # Upgrade legacy setup files without retaining a duplicate driver.
            if item["driver"] == "shg_stage":
                item["driver"] = "ni6211"
                item["options"] = dict(item.get("options") or {}, start_counters=False,
                                       ao_min=0., ao_max=10., digital_readback="commanded")
            item["options"]=self._options(item["driver"],item.get("options"))
        from shg_addon.actions import ACTIONS, action_code
        actions=prepared.setdefault("actions",[])
        existing={item["name"] for item in actions}
        for name in ("sample","reference","background","refresh_reference",
                     "sample_center","reference_center","calibration_phase",
                     "calibrate_all","single_diagnostic","loop_sample","loop_reference",
                     "acquire_point","prepare_scan"):
            action_name="shg_"+name
            if action_name not in existing:
                actions.append(dict(name=action_name,code=action_code(name),description=ACTIONS[name],confirm=False))
        from shg_addon.spot_tracking import definitions
        for definition in definitions():
            if definition["name"] not in existing:
                actions.append(definition)
        return super().load_setup(setup=prepared,replace=replace,connect=connect)

def main():
    home=ROOT/"state"/"backend"
    os.environ["EASYMEASURE_HOME"]=str(home)
    info=read_runtime_file()
    if info and backend_alive(info): return
    backend=SHGBackend(home=home)
    from shg_addon.lab_profile import restore_on_start
    restore_on_start(backend)
    server=Server(backend,host="127.0.0.1",port=47821)
    try: server.serve_forever()
    finally: backend.close()

if __name__=="__main__": main()
