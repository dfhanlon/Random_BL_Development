"""
cls_main.py — Primary CLS 1607-7-I21 launcher.

Makes the PyQt5 beamline-controls window the outer host and manages the
wx/xraylarch XRF viewer (xrf_launch.py) as a companion subprocess that
can be opened and closed on demand from a toolbar button.

Usage:
    python cls_main.py            # real EPICS
    python cls_main.py --sim      # simulated PVs (Qt controls + XRF viewer)
    python cls_main.py --dummy    # fully offline, no EPICS at all
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import types
import threading
from pathlib import Path

# ── project path / venv bootstrap ────────────────────────────────────────────

PROJECT_DIR = Path(__file__).parent.resolve()
_venv_py = PROJECT_DIR / ".venv" / "bin" / "python3"
PYTHON = str(_venv_py) if _venv_py.exists() else sys.executable

if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

os.environ.setdefault("LARCHDIR",    str(PROJECT_DIR / ".larch"))
os.environ.setdefault("MPLCONFIGDIR", str(PROJECT_DIR / ".mplconfig"))

# ── early arg parsing (must happen before Qt / beamline_control imports) ──────

_pre = argparse.ArgumentParser(add_help=False)
_pre.add_argument("--sim",   action="store_true")
_pre.add_argument("--dummy", action="store_true")
_pre_args, _ = _pre.parse_known_args()

# ── EPICS patch — must run before importing beamline_control ──────────────────

if _pre_args.dummy:
    # Fully offline: replace epics with in-process DummyPV stubs.
    class _DummyPV:
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

    _ep = types.ModuleType("epics")
    sys.modules["epics"] = _ep
    _ep.PV = _DummyPV; _ep.get_pv = _DummyPV
    _ep.caget = lambda *a, **k: 0; _ep.caput = lambda *a, **k: None

elif _pre_args.sim:
    try:
        import sim_pvs
        sim_pvs.patch_epics()
    except ImportError:
        pass

# ── Qt imports (after EPICS patch) ───────────────────────────────────────────

from PyQt5.QtCore import Qt, QTimer                                  # noqa: E402
from PyQt5.QtWidgets import (                                        # noqa: E402
    QAction, QApplication, QLabel, QToolBar,
)
from beamline_control import BeamlineControlWindow, _qss             # noqa: E402

# ── XRF subprocess manager ────────────────────────────────────────────────────

_XRF_SCRIPT = PROJECT_DIR / "xrf_launch.py"


class XRFProcess:
    """Manages the wx/xraylarch XRF viewer as a companion subprocess."""

    def __init__(self, *, use_sim: bool = False, use_dummy: bool = False):
        self._proc: subprocess.Popen | None = None
        self._use_sim = use_sim
        self._use_dummy = use_dummy
        self._log = None

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def launch(self) -> None:
        if self.is_running():
            return

        cmd = [PYTHON, str(_XRF_SCRIPT)]
        if self._use_dummy:
            cmd.append("--dummy")
        elif self._use_sim:
            cmd.append("--sim")

        env = os.environ.copy()
        env.setdefault("DISPLAY", os.environ.get("DISPLAY", ":0"))
        env.setdefault("LARCHDIR",     str(PROJECT_DIR / ".larch"))
        env.setdefault("MPLCONFIGDIR", str(PROJECT_DIR / ".mplconfig"))
        env["PYTHONPATH"] = (
            str(PROJECT_DIR) + os.pathsep + env.get("PYTHONPATH", "")
        )

        # Rotate log each launch so it doesn't grow unboundedly
        log_path = PROJECT_DIR / "xrf_viewer.log"
        self._log = open(log_path, "w")
        self._proc = subprocess.Popen(
            cmd,
            env=env,
            cwd=str(PROJECT_DIR),
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )

    def raise_window(self) -> bool:
        """Bring the XRF viewer window to the foreground. Returns True on success."""
        if not self.is_running():
            return False
        pid = self._proc.pid
        try:
            # wmctrl: raise any window belonging to this PID
            result = subprocess.run(
                ["wmctrl", "-i", "-a",
                 subprocess.run(
                     ["xdotool", "search", "--pid", str(pid)],
                     capture_output=True, text=True,
                 ).stdout.strip().split("\n")[0]],
                capture_output=True,
            )
            return result.returncode == 0
        except FileNotFoundError:
            pass
        try:
            # Fallback: xdotool alone
            wids = subprocess.run(
                ["xdotool", "search", "--pid", str(pid)],
                capture_output=True, text=True,
            ).stdout.strip().split("\n")
            if wids and wids[0]:
                subprocess.run(["xdotool", "windowactivate", "--sync", wids[0]],
                               capture_output=True)
                return True
        except FileNotFoundError:
            pass
        return False

    def terminate(self) -> None:
        if self.is_running():
            self._proc.terminate()
        self._proc = None
        if self._log is not None:
            try:
                self._log.close()
            except Exception:
                pass
            self._log = None


# ── extended main window ──────────────────────────────────────────────────────

class CLSMainWindow(BeamlineControlWindow):
    """
    Primary application window for CLS 1607-7-I21.

    Inherits the full BeamlineControlWindow UI and adds a toolbar that
    starts / stops the wx/xraylarch XRF viewer subprocess (xrf_launch.py).
    """

    def __init__(self, *, use_sim: bool = False, use_dummy: bool = False):
        super().__init__()
        self._xrf = XRFProcess(use_sim=use_sim, use_dummy=use_dummy)
        self._build_xrf_toolbar()
        self.setWindowTitle("CLS 1607-7-I21 — Beamline Controls")

        # Launch the XRF viewer immediately so it's ready when needed.
        self._xrf.launch()

        # Poll every 1.5 s so the toolbar status stays accurate even if the
        # user closes the XRF window directly (it will be re-launched on click).
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._refresh_xrf_ui)
        self._poll_timer.start(1500)

    # ── toolbar ───────────────────────────────────────────────────────────────

    def _build_xrf_toolbar(self) -> None:
        tb = QToolBar("XRF Viewer", self)
        tb.setMovable(False)
        tb.setStyleSheet("""
            QToolBar {
                background: #2b2b2b;
                border: none;
                border-bottom: 1px solid #444;
                padding: 2px 8px;
                spacing: 8px;
            }
            QToolButton {
                background: #4a4a4a;
                color: #e0e0e0;
                border: 1px solid #555;
                border-radius: 3px;
                padding: 3px 14px;
                font-size: 13px;
            }
            QToolButton:hover   { background: #5a5a5a; }
            QToolButton:pressed { background: #3a3a3a; }
        """)
        self.addToolBar(Qt.TopToolBarArea, tb)

        self._xrf_action = QAction("Show XRF Viewer", self)
        self._xrf_action.triggered.connect(self._on_xrf_clicked)
        tb.addAction(self._xrf_action)

        self._xrf_status = QLabel("  ●  XRF viewer: starting…", self)
        self._xrf_status.setStyleSheet(
            "color: #888888; font-size: 12px; background: transparent; padding-left: 4px;"
        )
        tb.addWidget(self._xrf_status)

    # ── XRF button handler ────────────────────────────────────────────────────

    def _on_xrf_clicked(self) -> None:
        if self._xrf.is_running():
            # Try to raise the existing window; fall back to a no-op if the
            # window-manager tools aren't installed.
            self._xrf.raise_window()
        else:
            # User closed the XRF window manually — relaunch it.
            self._xrf.launch()
            self._refresh_xrf_ui()

    def _refresh_xrf_ui(self) -> None:
        if self._xrf.is_running():
            self._xrf_status.setText("  ●  XRF viewer: running")
            self._xrf_status.setStyleSheet(
                "color: #00c800; font-size: 12px; background: transparent; padding-left: 4px;"
            )
        else:
            self._xrf_status.setText("  ●  XRF viewer: not running")
            self._xrf_status.setStyleSheet(
                "color: #666666; font-size: 12px; background: transparent; padding-left: 4px;"
            )

    # ── cleanup ───────────────────────────────────────────────────────────────

    def closeEvent(self, event):
        self._poll_timer.stop()
        self._xrf.terminate()
        super().closeEvent(event)


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="CLS 1607-7-I21 Primary Launcher",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--sim",   action="store_true", help="Simulated PVs")
    parser.add_argument("--dummy", action="store_true", help="Fully offline / no EPICS")
    args = parser.parse_args()

    # Start the sim update thread (epics was already patched at module level)
    _sim = None
    if args.sim and not args.dummy:
        try:
            import sim_pvs
            _sim = sim_pvs.SimulatedBeamline()
            _sim.start()
        except ImportError:
            pass

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(_qss())

    win = CLSMainWindow(use_sim=args.sim, use_dummy=args.dummy)
    win.show()

    ret = app.exec_()
    if _sim is not None:
        try:
            _sim.stop()
        except Exception:
            pass
    sys.exit(ret)


if __name__ == "__main__":
    main()
