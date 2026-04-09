import os
import sys
import threading
import types
from pathlib import Path


def _prefer_project_venv():
    """Re-run under the local virtualenv so GUI dependencies resolve consistently."""
    venv_python = Path(__file__).with_name(".venv") / "bin" / "python3"
    venv_root = venv_python.parent.parent.resolve()

    if not venv_python.exists():
        return

    if Path(sys.prefix).resolve() == venv_root:
        return

    os.execv(str(venv_python), [str(venv_python), __file__, *sys.argv[1:]])


_prefer_project_venv()
os.environ.setdefault("LARCHDIR", str(Path(__file__).with_name(".larch")))
os.environ.setdefault("MPLCONFIGDIR", str(Path(__file__).with_name(".mplconfig")))
Path(os.environ["LARCHDIR"]).mkdir(parents=True, exist_ok=True)
Path(os.environ["MPLCONFIGDIR"]).mkdir(parents=True, exist_ok=True)

class DummyPV:
    def __init__(self, pvname, auto_monitor=False, connect=True, **kwargs):
        self.pvname = pvname
        self._value = 0.0
        self._callbacks = []
        self._callback_idx = 0
        self.connected = True
        self.type = "ctrl_double"
        self.count = 1

    def connect(self, timeout=None):
        self.connected = True
        return True

    def wait_for_connection(self, timeout=None):
        return True

    def get_ctrlvars(self):
        return {}
    def get(self, timeout=None, use_monitor=False, as_string=False, **kwargs):
        val = self._value
        if as_string:
            return str(val)
        return val

    def put(self, value, wait=False, **kwargs):
        self._value = value
        for idx, cb in self._callbacks:
            cb(pvname=self.pvname, value=self._value, char_value=str(self._value), charvalue=str(self._value))
        return 1

    def add_callback(self, callback=None, index=None, **kwargs):
        if callback is None:
            return None
        idx = self._callback_idx
        self._callback_idx += 1
        self._callbacks.append((idx, callback))
        # Fire initial update
        def init_fire():
            callback(pvname=self.pvname, value=self._value, char_value=str(self._value), charvalue=str(self._value))
        threading.Timer(0.1, init_fire).start()
        return idx

    def remove_callback(self, index=None, **kwargs):
        self._callbacks = [cb for cb in self._callbacks if cb[0] != index]

try:
    import epics
except ModuleNotFoundError:
    epics = types.ModuleType("epics")
    sys.modules["epics"] = epics

def dummy_caget(pvname, **kwargs):
    return 0

def dummy_caput(pvname, value, **kwargs):
    pass

epics.PV = DummyPV
epics.get_pv = DummyPV
epics.caget = dummy_caget
epics.caput = dummy_caput

if __name__ == '__main__':
    import sim_pvs
    sim_pvs.patch_epics()
    sim = sim_pvs.SimulatedBeamline()
    sim.start()

    from app import launch_app, DetectorConfig

    det1 = DetectorConfig(name="Ge-32 Det1", prefix="MOCK:XSP3A:", nmca=32, det_type="ME-32")
    det2 = DetectorConfig(name="Ge-32 Det2", prefix="MOCK:XSP3B:", nmca=32, det_type="ME-32")

    launch_app([det1, det2], title="XAS XRF Viewer — 2×32 Ge Detectors", use_sim=True)
