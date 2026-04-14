"""
xrf_launch.py — Companion subprocess that hosts the wx/xraylarch XRF viewer.

Launched automatically by cls_main.py; can also be run standalone.

Detector configuration is read from pvs.yaml (add a `detectors` list there
to override the defaults).  Example pvs.yaml snippet:

    detectors:
      - name: "Ge Det1"
        prefix: "XSP3A:"
        nmca: 4
        det_type: "ME-4"
        ioc_type: "xspress3"
      - name: "Ge Det2"
        prefix: "XSP3B:"
        nmca: 4
        det_type: "ME-4"
        ioc_type: "xspress3"

Usage:
    python xrf_launch.py              # real EPICS
    python xrf_launch.py --sim        # simulated PVs
    python xrf_launch.py --dummy      # fully offline / mock detectors
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import types
from pathlib import Path

# ── project / env bootstrap ───────────────────────────────────────────────────

PROJECT_DIR = Path(__file__).parent.resolve()

os.environ.setdefault("LARCHDIR",     str(PROJECT_DIR / ".larch"))
os.environ.setdefault("MPLCONFIGDIR", str(PROJECT_DIR / ".mplconfig"))

Path(os.environ["LARCHDIR"]).mkdir(parents=True, exist_ok=True)
Path(os.environ["MPLCONFIGDIR"]).mkdir(parents=True, exist_ok=True)

if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

# ── args ──────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(description="XRF Viewer subprocess launcher")
parser.add_argument("--sim",   action="store_true", help="Use simulated PVs")
parser.add_argument("--dummy", action="store_true", help="Use offline dummy PVs")
args = parser.parse_args()

# ── EPICS setup (must happen before importing app / xraylarch) ────────────────

if args.dummy:
    class DummyPV:
        def __init__(self, pvname, **kw):
            self.pvname = pvname; self._v = 0.0; self._cbs = []
            self._i = 0; self.connected = True
            self.type = "ctrl_double"; self.count = 1

        def connect(self, **k): return True
        def wait_for_connection(self, **k): return True
        def get_ctrlvars(self): return {}

        def get(self, as_string=False, **k):
            return str(self._v) if as_string else self._v

        def put(self, v, **k):
            self._v = v
            for _, cb in self._cbs:
                cb(pvname=self.pvname, value=v,
                   char_value=str(v), charvalue=str(v))
            return 1

        def add_callback(self, callback=None, **k):
            if callback is None:
                return None
            i = self._i; self._i += 1
            self._cbs.append((i, callback))
            threading.Timer(0.05, lambda: callback(
                pvname=self.pvname, value=self._v,
                char_value=str(self._v), charvalue=str(self._v)
            )).start()
            return i

        def remove_callback(self, index=None, **k):
            self._cbs = [(i, c) for i, c in self._cbs if i != index]

    # Import the real epics module first so that larch can still access
    # Device, poll, etc.  Then patch only PV/get_pv/caget/caput so that no
    # real Channel Access connections are attempted.
    try:
        import epics
    except ModuleNotFoundError:
        epics = types.ModuleType("epics")
        sys.modules["epics"] = epics

    epics.PV = DummyPV
    epics.get_pv = DummyPV
    epics.caget = lambda *a, **k: 0
    epics.caput = lambda *a, **k: None

elif args.sim:
    try:
        import sim_pvs
        sim_pvs.patch_epics()
        _sim = sim_pvs.SimulatedBeamline()
        _sim.start()
    except ImportError as exc:
        print(f"[xrf_launch] sim_pvs not available: {exc}", file=sys.stderr)

# ── detector configuration ────────────────────────────────────────────────────

# Real-EPICS defaults for CLS 1607-7-I21.
# Add a `detectors` list to pvs.yaml to override these.
_REAL_DEFAULTS: list[dict] = [
    {"name": "Ge Det1", "prefix": "XSP3A:", "nmca": 4,
     "det_type": "ME-4", "ioc_type": "xspress3"},
    {"name": "Ge Det2", "prefix": "XSP3B:", "nmca": 4,
     "det_type": "ME-4", "ioc_type": "xspress3"},
]

# Offline / sim defaults — MOCK prefix prevents real EPICS connections.
_MOCK_DEFAULTS: list[dict] = [
    {"name": "Ge-32 Det1", "prefix": "MOCK:XSP3A:", "nmca": 32,
     "det_type": "ME-32", "ioc_type": "xspress3"},
    {"name": "Ge-32 Det2", "prefix": "MOCK:XSP3B:", "nmca": 32,
     "det_type": "ME-32", "ioc_type": "xspress3"},
]


def _load_det_configs(use_mock: bool) -> list[dict]:
    """Return detector list from pvs.yaml[detectors], or fall back to defaults."""
    pv_file = PROJECT_DIR / "pvs.yaml"
    if pv_file.exists():
        try:
            import yaml
            data = yaml.safe_load(pv_file.read_text(encoding="utf-8")) or {}
            dets = data.get("detectors")
            if isinstance(dets, list) and dets:
                return dets
        except Exception as exc:
            print(f"[xrf_launch] Could not parse pvs.yaml detectors: {exc}",
                  file=sys.stderr)
    return _MOCK_DEFAULTS if use_mock else _REAL_DEFAULTS


# ── build DetectorConfig objects ──────────────────────────────────────────────

use_mock = args.dummy or args.sim
det_dicts = _load_det_configs(use_mock=use_mock)

# Import after EPICS patch so xraylarch picks up the patched module.
# build_app_classes() also loads wx/larch into the process, after which we
# can import wx and larch helpers directly.
from app import DetectorConfig, build_app_classes  # noqa: E402

DetectorPanel, _, Xspress3ViewerApp = build_app_classes()

import wx                                                     # noqa: E402
from pathlib import Path                                      # noqa: E402
from larch.interpreter import Interpreter                     # noqa: E402
from larch.site_config import icondir                         # noqa: E402
from larch.wxlib.xrfdisplay import ICON_FILE                  # noqa: E402

detectors = [
    DetectorConfig(
        name=d["name"],
        prefix=d["prefix"],
        nmca=d.get("nmca", 4),
        det_type=d.get("det_type", "ME-4"),
        ioc_type=d.get("ioc_type", "xspress3"),
        environ_file=d.get("environ_file"),
        incident_energy_pvname=d.get("incident_energy_pvname"),
        incident_energy_units=d.get("incident_energy_units", "eV"),
    )
    for d in det_dicts
]

mode_tag = "DUMMY" if args.dummy else ("SIM" if args.sim else "LIVE")

# ── Ge-detectors-only frame (no Beamline Controls tab) ───────────────────────

class GeOnlyFrame(wx.Frame):
    """wx.Frame showing only the Ge detector XRF panels — no BL controls tab."""

    def __init__(self, *, detectors, size=(1300, 900), title="XRF Viewer",
                 _larch=None):
        super().__init__(None, title=title, size=size)
        self.base_title = title
        larch = _larch or Interpreter()

        self.notebook = wx.Notebook(self)
        self.statusbar = self.CreateStatusBar(4)
        self.statusbar.SetStatusWidths([-5, -3, -3, -4])
        self.statusbar.SetStatusText("XRF Display", 0)

        self.panels = []
        for det in detectors:
            panel = DetectorPanel(
                self.notebook,
                detector=det,
                title=det.name,
                _larch=larch,
            )
            self.notebook.AddPage(panel, det.name)
            self.panels.append(panel)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.notebook, 1, wx.EXPAND)
        self.SetSizer(sizer)

        try:
            self.SetIcon(wx.Icon(
                Path(icondir, ICON_FILE).as_posix(), wx.BITMAP_TYPE_ICO
            ))
        except Exception:
            pass

        if self.panels:
            self.update_detector_title(self.panels[0], self.panels[0].window_title)

        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.notebook.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self._on_page_changed)

    # Methods called by DetectorPanel via GetTopLevelParent()
    def set_status_text(self, text, panel=0):
        self.statusbar.SetStatusText(text, panel)

    def update_detector_title(self, panel, title):
        idx = self.notebook.FindPage(panel)
        label = getattr(panel, "page_label", title)
        if idx != wx.NOT_FOUND:
            self.notebook.SetPageText(idx, label)
        if self.notebook.GetCurrentPage() is panel:
            super().SetTitle(f"{self.base_title}  |  {label}")

    def _on_page_changed(self, event):
        page = self.notebook.GetCurrentPage()
        if page is not None:
            self.update_detector_title(
                page,
                getattr(page, "window_title", getattr(page, "page_label", "")),
            )
        event.Skip()

    def _on_close(self, _event):
        for panel in self.panels:
            try:
                panel.onClose()
            except Exception:
                pass
        self.Destroy()


class GeOnlyApp(Xspress3ViewerApp):
    """EpicsXRFApp subclass that creates GeOnlyFrame instead of DualDetectorHostFrame."""

    def createApp(self):
        frame = GeOnlyFrame(
            detectors=self.detectors,
            size=self.size,
            title=self.title,
            _larch=self._larch,
        )
        frame.Show()
        frame.Raise()
        self.SetTopWindow(frame)
        return True


# ── launch the wx event loop (blocks until window is closed) ──────────────────

app = GeOnlyApp(
    detectors=detectors,
    size=(1300, 900),
    title=f"XAS XRF Viewer — CLS 1607-7-I21  [{mode_tag}]",
    use_sim=args.sim,
)
app.MainLoop()
