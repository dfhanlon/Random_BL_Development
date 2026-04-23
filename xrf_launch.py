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
    """Return detector list from pvs.yaml[detectors], or fall back to defaults.

    In mock mode any real prefix is prefixed with 'MOCK:' so DetectorPanel
    skips the onConnectEpics call (which would segfault against DummyPV).
    """
    pv_file = PROJECT_DIR / "pvs.yaml"
    if pv_file.exists():
        try:
            import yaml
            data = yaml.safe_load(pv_file.read_text(encoding="utf-8")) or {}
            dets = data.get("detectors")
            if isinstance(dets, list) and dets:
                if use_mock:
                    # Ensure prefixes start with MOCK: so EPICS connection is skipped
                    dets = [
                        {**d, "prefix": d["prefix"] if d.get("prefix", "").upper().startswith("MOCK:")
                                        else f"MOCK:{d['prefix']}"}
                        for d in dets
                    ]
                return dets
        except Exception as exc:
            print(f"[xrf_launch] Could not parse pvs.yaml detectors: {exc}",
                  file=sys.stderr)
    return _MOCK_DEFAULTS if use_mock else _REAL_DEFAULTS


def _load_controls_cfg() -> dict:
    """Return the controls section from pvs.yaml, or empty dict on failure."""
    pv_file = PROJECT_DIR / "pvs.yaml"
    if pv_file.exists():
        try:
            import yaml
            data = yaml.safe_load(pv_file.read_text(encoding="utf-8")) or {}
            return data.get("controls", {})
        except Exception as exc:
            print(f"[xrf_launch] Could not parse pvs.yaml controls: {exc}",
                  file=sys.stderr)
    return {}


# ── build DetectorConfig objects ──────────────────────────────────────────────

use_mock = args.dummy or args.sim
det_dicts = _load_det_configs(use_mock=use_mock)
_controls_cfg = _load_controls_cfg()

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
        stage_read_pv=d.get("stage_read_pv", ""),
        stage_write_pv=d.get("stage_write_pv", ""),
    )
    for d in det_dicts
]

mode_tag = "DUMMY" if args.dummy else ("SIM" if args.sim else "")

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
            try:
                panel = DetectorPanel(
                    self.notebook,
                    detector=det,
                    title=det.name,
                    _larch=larch,
                )
                self.notebook.AddPage(panel, det.name)
                self.panels.append(panel)
            except Exception as exc:
                print(f"[xrf_launch] Could not load detector '{det.name}': {exc}",
                      file=sys.stderr)
                # Add a placeholder tab so the viewer still opens
                placeholder = wx.Panel(self.notebook)
                placeholder.SetBackgroundColour(wx.Colour(43, 43, 43))
                msg = wx.StaticText(
                    placeholder,
                    label=f"Detector '{det.name}' unavailable\n\n{exc}",
                )
                msg.SetForegroundColour(wx.Colour(200, 100, 100))
                sz = wx.BoxSizer(wx.VERTICAL)
                sz.AddStretchSpacer()
                sz.Add(msg, 0, wx.ALIGN_CENTER_HORIZONTAL)
                sz.AddStretchSpacer()
                placeholder.SetSizer(sz)
                self.notebook.AddPage(placeholder, f"{det.name}  (unavailable)")

        # ── Stage position + Mono Energy sidebar (right of notebook) ──────────
        self._stage_rows: list[dict] = []
        stage_panel = wx.Panel(self)
        stage_panel.SetBackgroundColour(wx.Colour(43, 43, 43))
        stage_panel.SetMinSize((170, -1))
        stage_col = wx.BoxSizer(wx.VERTICAL)

        hdr = wx.StaticText(stage_panel, label="Stage Position")
        hdr.SetForegroundColour(wx.Colour(200, 200, 200))
        _font = hdr.GetFont()
        _font.SetWeight(wx.FONTWEIGHT_BOLD)
        hdr.SetFont(_font)
        stage_col.Add(hdr, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.TOP | wx.BOTTOM, 8)
        stage_col.Add(wx.StaticLine(stage_panel, style=wx.LI_HORIZONTAL),
                      0, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)

        for det in detectors:
            rpv = det.stage_read_pv or ""
            wpv = det.stage_write_pv or ""

            card = wx.Panel(stage_panel)
            card.SetBackgroundColour(wx.Colour(43, 43, 43))
            card_sizer = wx.BoxSizer(wx.VERTICAL)

            name_lbl = wx.StaticText(card, label=det.name.strip())
            name_lbl.SetForegroundColour(wx.Colour(160, 200, 255))
            _f2 = name_lbl.GetFont()
            _f2.SetWeight(wx.FONTWEIGHT_BOLD)
            name_lbl.SetFont(_f2)
            card_sizer.Add(name_lbl, 0, wx.LEFT | wx.TOP, 8)

            rbk_row = wx.BoxSizer(wx.HORIZONTAL)
            rbk = wx.TextCtrl(card, value="--", size=(90, -1),
                              style=wx.TE_READONLY | wx.TE_RIGHT | wx.BORDER_SIMPLE)
            rbk.SetBackgroundColour(wx.Colour(30, 30, 30))
            rbk.SetForegroundColour(wx.Colour(255, 255, 255))
            rbk_row.Add(rbk, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 8)
            mm_lbl = wx.StaticText(card, label="mm")
            mm_lbl.SetForegroundColour(wx.Colour(136, 136, 136))
            rbk_row.Add(mm_lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 4)
            card_sizer.Add(rbk_row, 0, wx.TOP, 4)

            sp_row = wx.BoxSizer(wx.HORIZONTAL)
            sp = wx.TextCtrl(card, size=(90, -1),
                             style=wx.TE_PROCESS_ENTER | wx.BORDER_SIMPLE)
            sp.SetBackgroundColour(wx.Colour(64, 64, 64))
            sp.SetForegroundColour(wx.Colour(255, 255, 255))
            sp.SetHint("setpoint")
            sp_row.Add(sp, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 8)
            set_btn = wx.Button(card, label="Set", size=(44, -1))
            sp_row.Add(set_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 4)
            card_sizer.Add(sp_row, 0, wx.TOP | wx.BOTTOM, 4)

            card.SetSizer(card_sizer)
            stage_col.Add(card, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 4)
            stage_col.Add(wx.StaticLine(stage_panel, style=wx.LI_HORIZONTAL),
                          0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 6)

            row_info = dict(rbk=rbk, sp=sp, read_pv=rpv, write_pv=wpv)
            self._stage_rows.append(row_info)

            def _make_set(info):
                def _on_set(_evt):
                    if not info["write_pv"]:
                        return
                    try:
                        val = float(info["sp"].GetValue())
                        import epics as _epics
                        _epics.caput(info["write_pv"], val)
                    except (ValueError, TypeError, Exception):
                        pass
                return _on_set
            handler = _make_set(row_info)
            set_btn.Bind(wx.EVT_BUTTON, handler)
            sp.Bind(wx.EVT_TEXT_ENTER, handler)

            if rpv:
                def _make_cb(info):
                    def _cb(value=None, char_value=None, **_kw):
                        try:
                            txt = f"{float(value):.4g}"
                        except (TypeError, ValueError):
                            txt = str(char_value or value or "--")
                        wx.CallAfter(info["rbk"].SetValue, txt)
                    return _cb
                try:
                    import epics as _epics
                    pv = _epics.PV(rpv, auto_monitor=True)
                    pv.add_callback(_make_cb(row_info))
                except Exception:
                    pass

        # ── Mono Energy card ──────────────────────────────────────────────────
        mono_cfg = _controls_cfg.get("Mono Energy", {})
        mono_read_pv  = mono_cfg.get("read_pv",  "BL1607-I21:Mono:Energy:EV:fbk")
        mono_write_pv = mono_cfg.get("write_pv", "BL1607-I21:Mono:Energy:EV")

        stage_col.Add(wx.StaticLine(stage_panel, style=wx.LI_HORIZONTAL),
                      0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 6)

        mono_card = wx.Panel(stage_panel)
        mono_card.SetBackgroundColour(wx.Colour(43, 43, 43))
        mono_sizer = wx.BoxSizer(wx.VERTICAL)

        mono_title = wx.StaticText(mono_card, label="Mono Energy")
        mono_title.SetForegroundColour(wx.Colour(160, 200, 255))
        _fm = mono_title.GetFont()
        _fm.SetWeight(wx.FONTWEIGHT_BOLD)
        mono_title.SetFont(_fm)
        mono_sizer.Add(mono_title, 0, wx.LEFT | wx.TOP, 8)

        mono_rbk_row = wx.BoxSizer(wx.HORIZONTAL)
        self._mono_rbk = wx.TextCtrl(mono_card, value="--", size=(100, -1),
                                     style=wx.TE_READONLY | wx.TE_RIGHT | wx.BORDER_SIMPLE)
        self._mono_rbk.SetBackgroundColour(wx.Colour(30, 30, 30))
        self._mono_rbk.SetForegroundColour(wx.Colour(255, 255, 255))
        mono_rbk_row.Add(self._mono_rbk, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 8)
        ev_lbl = wx.StaticText(mono_card, label="eV")
        ev_lbl.SetForegroundColour(wx.Colour(136, 136, 136))
        mono_rbk_row.Add(ev_lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 4)
        mono_sizer.Add(mono_rbk_row, 0, wx.TOP, 4)

        mono_sp_row = wx.BoxSizer(wx.HORIZONTAL)
        self._mono_sp = wx.TextCtrl(mono_card, size=(100, -1),
                                    style=wx.TE_PROCESS_ENTER | wx.BORDER_SIMPLE)
        self._mono_sp.SetBackgroundColour(wx.Colour(64, 64, 64))
        self._mono_sp.SetForegroundColour(wx.Colour(255, 255, 255))
        self._mono_sp.SetHint("setpoint eV")
        mono_sp_row.Add(self._mono_sp, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 8)
        mono_set_btn = wx.Button(mono_card, label="Set", size=(44, -1))
        mono_sp_row.Add(mono_set_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 4)
        mono_sizer.Add(mono_sp_row, 0, wx.TOP | wx.BOTTOM, 4)

        mono_card.SetSizer(mono_sizer)
        stage_col.Add(mono_card, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 4)

        _mono_write = mono_write_pv

        def _on_mono_set(_evt):
            if not _mono_write:
                return
            try:
                val = float(self._mono_sp.GetValue())
                import epics as _epics
                _epics.caput(_mono_write, val)
            except (ValueError, TypeError, Exception):
                pass

        mono_set_btn.Bind(wx.EVT_BUTTON, _on_mono_set)
        self._mono_sp.Bind(wx.EVT_TEXT_ENTER, _on_mono_set)

        if mono_read_pv:
            try:
                import epics as _epics
                _mono_pv = _epics.PV(mono_read_pv, auto_monitor=True)

                def _mono_cb(value=None, char_value=None, **_kw):
                    try:
                        txt = f"{float(value):.2f}"
                    except (TypeError, ValueError):
                        txt = str(char_value or value or "--")
                    wx.CallAfter(self._mono_rbk.SetValue, txt)

                _mono_pv.add_callback(_mono_cb)
            except Exception:
                pass

        stage_col.AddStretchSpacer(1)
        stage_panel.SetSizer(stage_col)
        stage_panel.Layout()

        # ── Main layout (horizontal split) ────────────────────────────────────
        sizer = wx.BoxSizer(wx.HORIZONTAL)
        sizer.Add(self.notebook, 1, wx.EXPAND)
        sizer.Add(wx.StaticLine(self, style=wx.LI_VERTICAL), 0, wx.EXPAND)
        sizer.Add(stage_panel, 0, wx.EXPAND)
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
        try:
            frame = GeOnlyFrame(
                detectors=self.detectors,
                size=self.size,
                title=self.title,
                _larch=self._larch,
            )
        except Exception as exc:
            print(f"[xrf_launch] GeOnlyFrame failed: {exc}", flush=True)
            import traceback; traceback.print_exc()
            return False
        frame.Show()
        frame.Raise()
        self.SetTopWindow(frame)
        return True


# ── launch the wx event loop (blocks until window is closed) ──────────────────

app = GeOnlyApp(
    detectors=detectors,
    size=(1300, 900),
    title=f"XAS XRF Viewer — CLS 1607-7-I21" + (f"  [{mode_tag}]" if mode_tag else ""),
)
app.MainLoop()