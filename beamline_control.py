from __future__ import annotations

import math
import sys
import threading
import time
import types
from collections import deque
from pathlib import Path

import yaml
from PyQt5.QtCore import Qt, QRectF, QTimer, pyqtSignal, QObject
from PyQt5.QtGui import QPainter, QColor, QWindow
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QScrollArea, QFrame, QGroupBox,
    QSizePolicy, QProgressBar, QTabWidget,
)


# ── EPICS / DummyPV ───────────────────────────────────────────────────────────

class DummyPV:
    """In-process PV stub for offline / demo use."""

    def __init__(self, pvname, auto_monitor=False, connect=True, **kwargs):
        self.pvname = pvname
        self._value = 0.0
        self._callbacks: list = []
        self._idx = 0
        self.connected = True
        self.type = "ctrl_double"
        self.count = 1

    def connect(self, timeout=None): return True
    def wait_for_connection(self, timeout=None): return True
    def get_ctrlvars(self): return {}

    def get(self, timeout=None, as_string=False, **kwargs):
        return str(self._value) if as_string else self._value

    def put(self, value, wait=False, **kwargs):
        self._value = value
        for _, cb in self._callbacks:
            cb(pvname=self.pvname, value=self._value,
               char_value=str(self._value), charvalue=str(self._value))
        return 1

    def add_callback(self, callback=None, **kwargs):
        if callback is None:
            return None
        idx = self._idx
        self._idx += 1
        self._callbacks.append((idx, callback))
        threading.Timer(0.05, lambda: callback(
            pvname=self.pvname, value=self._value,
            char_value=str(self._value), charvalue=str(self._value)
        )).start()
        return idx

    def remove_callback(self, index=None, **kwargs):
        self._callbacks = [(i, cb) for i, cb in self._callbacks if i != index]


try:
    import epics
except ModuleNotFoundError:
    epics = types.ModuleType("epics")
    sys.modules["epics"] = epics

if not hasattr(epics, "PV"):
    epics.PV = DummyPV
    epics.get_pv = DummyPV
    epics.caget = lambda *a, **k: 0
    epics.caput = lambda *a, **k: None


# ── Colour palette ────────────────────────────────────────────────────────────

BG      = "#2b2b2b"
PANEL   = "#363636"
BORDER  = "#555555"
TEXT    = "#e0e0e0"
DIM     = "#888888"
OK      = "#00c800"
MAJOR   = "#e00000"
MINOR   = "#e0c000"
ATTN    = "#e08000"
DISCONN = "#808080"
BLUE    = "#1b6ac9"
VALBG   = "#1e1e1e"
INBG    = "#404040"
BTN     = "#4a4a4a"
BTNHOV  = "#5a5a5a"

_LED_PALETTE: dict[str, QColor] = {
    "ok":        QColor(0, 200, 0),
    "major":     QColor(220, 0, 0),
    "minor":     QColor(220, 192, 0),
    "attention": QColor(220, 128, 0),
    "disconn":   QColor(120, 120, 120),
    "blue":      QColor(27, 106, 201),
}


def _qss() -> str:
    return f"""
* {{
    font-family: "Liberation Sans", "DejaVu Sans", Arial, sans-serif;
    font-size: 15px;
    color: {TEXT};
}}
QMainWindow, QWidget#root {{
    background-color: {BG};
}}
QScrollArea, QScrollArea > QWidget > QWidget {{
    background-color: {BG};
}}
QGroupBox {{
    background-color: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 3px;
    margin-top: 16px;
    padding: 8px 6px 6px 6px;
    font-weight: bold;
    font-size: 15px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 8px;
    padding: 0 4px;
    background-color: {PANEL};
    color: {TEXT};
}}
QLabel {{ background-color: transparent; color: {TEXT}; }}
QLineEdit {{
    background-color: {INBG};
    color: #ffffff;
    border: 1px solid {BORDER};
    border-radius: 2px;
    padding: 1px 6px;
    font-family: "Liberation Mono", "DejaVu Sans Mono", monospace;
    font-size: 16px;
    selection-background-color: {BLUE};
}}
QLineEdit:focus {{ border: 1px solid {BLUE}; }}
QPushButton {{
    background-color: {BTN};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 3px;
    padding: 2px 8px;
    min-height: 22px;
}}
QPushButton:hover {{
    background-color: {BTNHOV};
    border: 1px solid #777;
}}
QPushButton:pressed {{
    background-color: {BLUE};
    border: 1px solid #2a80d9;
}}
QProgressBar {{
    background-color: {VALBG};
    border: 1px solid {BORDER};
    border-radius: 2px;
    text-align: right;
    color: #aaa;
    font-size: 14px;
    padding-right: 4px;
}}
QProgressBar::chunk {{
    background-color: {BLUE};
    border-radius: 2px;
}}
QScrollBar:vertical {{
    background: {PANEL};
    width: 10px;
    border: none;
}}
QScrollBar::handle:vertical {{
    background: #666;
    min-height: 20px;
    border-radius: 4px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{
    background: {PANEL};
    height: 10px;
    border: none;
}}
QScrollBar::handle:horizontal {{
    background: #666;
    min-width: 20px;
    border-radius: 4px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
"""


# ── Widget helpers ────────────────────────────────────────────────────────────

def _sep() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.HLine)
    f.setStyleSheet(f"background-color: {BORDER}; min-height: 1px; max-height: 1px; border: none;")
    return f


def _value_label(text: str = "--", width: int = 96, mono: bool = True) -> QLabel:
    lbl = QLabel(text)
    lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    lbl.setFixedWidth(width)
    font_family = '"Liberation Mono", "DejaVu Sans Mono", monospace' if mono else "inherit"
    lbl.setStyleSheet(
        f"background-color: {VALBG}; color: #ffffff; "
        f"font-family: {font_family}; font-size: 16px; "
        f"border: 1px solid {BORDER}; padding: 1px 6px;"
    )
    return lbl


def _btn(text: str, width: int | None = None, color: str = BTN,
         border: str = BORDER, text_color: str = TEXT, bold: bool = False) -> QPushButton:
    b = QPushButton(text)
    b.setFixedHeight(22)
    if width:
        b.setFixedWidth(width)
    weight = "bold" if bold else "normal"
    b.setStyleSheet(
        f"QPushButton {{ background-color: {color}; color: {text_color}; "
        f"border: 1px solid {border}; border-radius: 3px; "
        f"padding: 2px 6px; font-weight: {weight}; }}"
        f"QPushButton:hover {{ background-color: {_lighten(color)}; }}"
        f"QPushButton:pressed {{ background-color: {BLUE}; }}"
    )
    return b


def _lighten(hex_color: str, amount: int = 20) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    r, g, b = min(255, r + amount), min(255, g + amount), min(255, b + amount)
    return f"#{r:02x}{g:02x}{b:02x}"


# ── Dashboard card ────────────────────────────────────────────────────────────

class DashboardCard(QWidget):
    """Large single-value readback for a key parameter."""

    def __init__(self, label: str, read_pv: str, bridge: PVBridge,
                 fmt: str = ".2f", units: str = "", parent=None):
        super().__init__(parent)
        self._pvname = read_pv
        self._fmt    = fmt
        self._units  = units

        self.setStyleSheet(
            f"background: {PANEL}; border: 1px solid {BORDER}; border-radius: 4px;"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(2)

        lbl = QLabel(label)
        lbl.setStyleSheet(f"color: {DIM}; font-size: 14px; background: transparent; border: none;")
        lay.addWidget(lbl)

        self.val_lbl = QLabel("--")
        self.val_lbl.setStyleSheet(
            "color: #ffffff; font-family: 'Liberation Mono', 'DejaVu Sans Mono', monospace; "
            "font-size: 22px; font-weight: bold; background: transparent; border: none;"
        )
        lay.addWidget(self.val_lbl)

        if read_pv:
            bridge.updated.connect(self._on_update)
            bridge.subscribe(read_pv)

    def _on_update(self, pvname: str, value):
        if pvname != self._pvname or value is None:
            return
        try:
            txt = format(float(value), self._fmt)
            self.val_lbl.setText(f"{txt} {self._units}".strip())
        except (TypeError, ValueError):
            self.val_lbl.setText(str(value))


# ── LED indicator ─────────────────────────────────────────────────────────────

class LEDWidget(QWidget):
    """Circular Phoebus-style LED."""

    def __init__(self, diameter: int = 14, parent=None):
        super().__init__(parent)
        self.d = diameter
        self._color = _LED_PALETTE["disconn"]
        self.setFixedSize(diameter, diameter)
        self.setAttribute(Qt.WA_TranslucentBackground)

    def set_state(self, state: str):
        col = _LED_PALETTE.get(state.lower(), _LED_PALETTE["disconn"])
        if col != self._color:
            self._color = col
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        d, c = self.d, self._color
        cx, cy = d / 2.0, d / 2.0
        r = d * 0.42
        p.setPen(Qt.NoPen)
        # Outer ring
        p.setBrush(c.darker(180))
        p.drawEllipse(QRectF(cx - r, cy - r, 2*r, 2*r))
        # Body
        ir = r * 0.76
        p.setBrush(c)
        p.drawEllipse(QRectF(cx - ir, cy - ir, 2*ir, 2*ir))
        # Highlight
        hr = ir * 0.42
        p.setBrush(c.lighter(200))
        p.drawEllipse(QRectF(cx - ir*0.55, cy - ir*0.65, hr, hr))
        p.end()


# ── PV bridge (thread-safe Qt signal dispatch) ────────────────────────────────

class PVBridge(QObject):
    """Owns all epics.PV objects and re-emits their callbacks as Qt signals."""

    updated = pyqtSignal(str, object)   # (pvname, value)

    def __init__(self):
        super().__init__()
        self._pvs: dict[str, object] = {}
        self._handles: list[tuple] = []   # (pv, cb_idx)

    def subscribe(self, pvname: str):
        if not pvname or pvname in self._pvs:
            return
        pv = epics.PV(pvname, auto_monitor=True)
        self._pvs[pvname] = pv
        idx = pv.add_callback(self._dispatch)
        self._handles.append((pv, idx))

    def put(self, pvname: str, value):
        if not pvname:
            return
        pv = self._pvs.get(pvname)
        if pv is None:
            # Write-only PVs are never subscribed — create/fetch on first use
            pv = epics.PV(pvname)
            self._pvs[pvname] = pv
        pv.put(value, wait=False)

    def _dispatch(self, pvname="", value=None, **_):
        self.updated.emit(pvname, value)

    def cleanup(self):
        for pv, idx in self._handles:
            try:
                pv.remove_callback(idx)
            except Exception:
                pass


# ── Config loading ────────────────────────────────────────────────────────────

PV_CONFIG_FILE = Path(__file__).with_name("pvs.yaml")

_DEFAULT_UNITS: dict[str, str] = {
    "Mono Energy": "eV",      "Dwell Time": "ms",       "Stage Z": "mm",
    "Stage Y": "mm",          "Ring Current": "mA",     "Furnace Temp": "C",
    "Heat Rate": "deg/min",   "Furnace SP": "C",        "JJ Vert Gap": "mm",
    "JJ Vert Center": "mm",   "JJ Hor Gap": "mm",       "JJ Hor Center": "mm",
    "M1 Pitch": "mrad",       "DBHR M1": "mm",          "DBHR M2": "mm",
    "DBHR Pitch": "deg",
}


def load_pv_config() -> dict:
    if not PV_CONFIG_FILE.exists():
        return {}
    try:
        data = yaml.safe_load(PV_CONFIG_FILE.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ── Status chip ───────────────────────────────────────────────────────────────

_GOOD_WORDS = {"OPEN", "ON", "OK", "READY", "ENABLED", "IN", "NITROGEN", "HIGH"}
_BAD_WORDS  = {"CLOSED", "OFF", "BAD", "ERROR", "FAULT", "ALARM", "LOW"}
_WARN_WORDS = {"MOVING", "BUSY", "WARN", "WARNING", "STANDBY", "ARGON"}

_STATE_COLOR = {"ok": OK, "major": MAJOR, "minor": MINOR, "attention": ATTN, "disconn": DIM}


def _evaluate_status(value, cfg: dict) -> tuple[str, str]:
    if value is None:
        return "disconn", "--"
    threshold = cfg.get("status_threshold")
    if threshold is not None:
        try:
            fv = float(value)
            cmp = cfg.get("status_threshold_comparison", "gt")
            good = (fv > threshold if cmp == "gt" else
                    fv < threshold if cmp == "lt" else
                    fv >= threshold)
            label = cfg.get("status_good_label", "OK") if good else cfg.get("status_bad_label", "BAD")
            return ("ok" if good else "major"), label
        except (TypeError, ValueError):
            pass
    text = str(value).upper().strip()
    if text in _GOOD_WORDS:
        return "ok", text
    if text in _BAD_WORDS:
        return "major", text
    if text in _WARN_WORDS:
        return "minor", text
    return "disconn", text or "--"


class StatusChip(QWidget):
    """LED + label status indicator — mirrors a Phoebus LED widget."""

    def __init__(self, label: str, cfg: dict, bridge: PVBridge, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._pvname = cfg.get("read_pv") or ""

        self.setFixedWidth(110)
        self.setStyleSheet(
            f"StatusChip {{ background: {PANEL}; border: 1px solid {BORDER}; "
            f"border-radius: 3px; }}"
        )

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(2)

        # LED + name row
        top = QHBoxLayout()
        top.setSpacing(4)
        self.led = LEDWidget(12, self)
        top.addWidget(self.led, 0, Qt.AlignVCenter)
        name = QLabel(label)
        name.setStyleSheet("font-weight: bold; font-size: 14px; background: transparent;")
        top.addWidget(name, 1)
        lay.addLayout(top)

        # Value text
        self.val_lbl = QLabel("--")
        self.val_lbl.setAlignment(Qt.AlignCenter)
        self.val_lbl.setStyleSheet(
            f"background: {VALBG}; border: 1px solid {BORDER}; "
            f"font-size: 15px; font-weight: bold; color: {DIM}; padding: 1px 0;"
        )
        lay.addWidget(self.val_lbl)

        if self._pvname:
            bridge.updated.connect(self._on_update)
            bridge.subscribe(self._pvname)

    def _on_update(self, pvname: str, value):
        if pvname != self._pvname:
            return
        state, text = _evaluate_status(value, self.cfg)
        self.led.set_state(state)
        color = _STATE_COLOR.get(state, DIM)
        self.val_lbl.setText(text)
        self.val_lbl.setStyleSheet(
            f"background: {VALBG}; border: 1px solid {BORDER}; "
            f"font-size: 15px; font-weight: bold; color: {color}; padding: 1px 0;"
        )


# ── FE Shutter widget ─────────────────────────────────────────────────────────

class FEShutterWidget(QWidget):
    """Large state indicator + Open / Close buttons for the FE shutter."""

    _STYLE_OPEN = (
        "background: #1a6a1a; color: #90ff90; font-size: 18px; font-weight: bold; "
        "border-radius: 4px; border: 1px solid #2aaa2a;"
    )
    _STYLE_CLOSED = (
        "background: #6a1a1a; color: #ff9090; font-size: 18px; font-weight: bold; "
        "border-radius: 4px; border: 1px solid #aa2a2a;"
    )
    _STYLE_UNKNOWN = (
        "background: #555555; color: #aaaaaa; font-size: 18px; font-weight: bold; "
        "border-radius: 4px; border: 1px solid #777777;"
    )

    def __init__(self, read_pv: str, open_pv: str, close_pv: str,
                 bridge: PVBridge, parent=None):
        super().__init__(parent)
        self._read_pv  = read_pv
        self._open_pv  = open_pv
        self._close_pv = close_pv
        self.bridge    = bridge

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        # Label
        lbl = QLabel("FE Shutter")
        lbl.setStyleSheet("font-size: 15px; font-weight: bold; color: #aaaaaa;")
        lay.addWidget(lbl)

        # State indicator
        self.state_lbl = QLabel("---")
        self.state_lbl.setAlignment(Qt.AlignCenter)
        self.state_lbl.setFixedSize(80, 34)
        self.state_lbl.setStyleSheet(self._STYLE_UNKNOWN)
        lay.addWidget(self.state_lbl)

        # Open button
        open_btn = QPushButton("Open")
        open_btn.setFixedSize(58, 34)
        open_btn.setStyleSheet(
            "QPushButton { background: #1a4a1a; color: #88dd88; "
            "border: 1px solid #2a7a2a; border-radius: 4px; "
            "font-weight: bold; font-size: 16px; }"
            "QPushButton:hover  { background: #2a6a2a; }"
            "QPushButton:pressed{ background: #1a8a1a; }"
        )
        open_btn.clicked.connect(self._open)
        lay.addWidget(open_btn)

        # Close button
        close_btn = QPushButton("Close")
        close_btn.setFixedSize(58, 34)
        close_btn.setStyleSheet(
            "QPushButton { background: #4a1a1a; color: #dd8888; "
            "border: 1px solid #7a2a2a; border-radius: 4px; "
            "font-weight: bold; font-size: 16px; }"
            "QPushButton:hover  { background: #6a2a2a; }"
            "QPushButton:pressed{ background: #8a1a1a; }"
        )
        close_btn.clicked.connect(self._close)
        lay.addWidget(close_btn)

        if read_pv:
            bridge.updated.connect(self._on_update)
            bridge.subscribe(read_pv)

    def _on_update(self, pvname: str, value):
        if pvname != self._read_pv or value is None:
            return
        text = str(value).upper().strip()
        if text == "OPEN":
            self.state_lbl.setText("OPEN")
            self.state_lbl.setStyleSheet(self._STYLE_OPEN)
        elif text in ("CLOSED", "CLOSE"):
            self.state_lbl.setText("CLOSED")
            self.state_lbl.setStyleSheet(self._STYLE_CLOSED)
        else:
            self.state_lbl.setText(text or "---")
            self.state_lbl.setStyleSheet(self._STYLE_UNKNOWN)

    def _open(self):
        if self._open_pv:
            self.bridge.put(self._open_pv, 1)

    def _close(self):
        if self._close_pv:
            self.bridge.put(self._close_pv, 1)


# ── Control row ───────────────────────────────────────────────────────────────

class ControlRow(QWidget):
    """Parameter | Readback | Setpoint | Units | Set | [Stop]"""

    def __init__(self, label: str, cfg: dict, bridge: PVBridge, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.bridge = bridge
        self._read_pv  = cfg.get("read_pv") or ""
        self._write_pv = cfg.get("write_pv") or ""
        self._stop_pv  = cfg.get("stop_pv") or ""
        self._readonly = not bool(self._write_pv)
        self.setStyleSheet(f"background: transparent;")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 1, 2, 1)
        lay.setSpacing(4)

        # Label
        lbl = QLabel(label)
        lbl.setFixedWidth(128)
        lbl.setStyleSheet("font-size: 15px;")
        lay.addWidget(lbl)

        # Readback
        self.readback = _value_label("--", 96)
        lay.addWidget(self.readback)

        # Setpoint / spacer
        if not self._readonly:
            self.sp_input = QLineEdit()
            self.sp_input.setPlaceholderText("setpoint")
            self.sp_input.setFixedWidth(80)
            self.sp_input.setFixedHeight(22)
            self.sp_input.returnPressed.connect(self._on_set)
            lay.addWidget(self.sp_input)
        else:
            self.sp_input = None
            lay.addSpacing(84)

        # Units
        units_lbl = QLabel(cfg.get("units") or _DEFAULT_UNITS.get(label, ""))
        units_lbl.setFixedWidth(52)
        units_lbl.setStyleSheet(f"color: {DIM}; font-size: 14px;")
        lay.addWidget(units_lbl)

        # Set button
        if not self._readonly:
            set_btn = _btn("Set", width=36, color=BLUE, border="#2a80d9",
                           text_color="#ffffff", bold=True)
            set_btn.clicked.connect(self._on_set)
            lay.addWidget(set_btn)
        else:
            lay.addSpacing(40)

        # Stop button
        if self._stop_pv:
            stop_lbl = cfg.get("stop_button_label", "STOP")
            stop_btn = _btn(stop_lbl, width=44, color="#8b0000",
                            border="#cc0000", text_color="#ffffff", bold=True)
            stop_btn.clicked.connect(self._on_stop)
            lay.addWidget(stop_btn)

        lay.addStretch()

        if self._read_pv:
            bridge.updated.connect(self._on_update)
            bridge.subscribe(self._read_pv)

    def _on_update(self, pvname: str, value):
        if pvname != self._read_pv:
            return
        if value is None:
            self.readback.setText("--")
        elif isinstance(value, float):
            self.readback.setText(f"{value:.4g}")
        else:
            self.readback.setText(str(value))

    def _on_set(self):
        if not self.sp_input or not self._write_pv:
            return
        text = self.sp_input.text().strip()
        if not text:
            return
        try:
            val: float | str = float(text)
        except ValueError:
            val = text
        self.bridge.put(self._write_pv, val)

    def _on_stop(self):
        if self._stop_pv:
            self.bridge.put(self._stop_pv, self.cfg.get("stop_value", 1))


# ── Command row ───────────────────────────────────────────────────────────────

class CommandRow(QWidget):
    """Read-only label + value + Send button for fire-and-forget commands."""

    def __init__(self, label: str, cfg: dict, bridge: PVBridge, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.bridge = bridge
        self._write_pv = cfg.get("write_pv") or ""
        self._cmd_val  = cfg.get("command_value", 1)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 1, 2, 1)
        lay.setSpacing(4)

        lbl = QLabel(label)
        lbl.setFixedWidth(128)
        lay.addWidget(lbl)

        lay.addSpacing(100)

        val_lbl = _value_label(str(self._cmd_val), 80)
        lay.addWidget(val_lbl)

        units_space = QLabel("")
        units_space.setFixedWidth(52)
        lay.addWidget(units_space)

        btn_lbl = cfg.get("button_label", "Send")
        btn = _btn(btn_lbl, width=50, color=BTN)
        btn.clicked.connect(self._on_send)
        lay.addWidget(btn)
        lay.addStretch()

    def _on_send(self):
        if self._write_pv:
            self.bridge.put(self._write_pv, self._cmd_val)


# ── Ion chamber card ──────────────────────────────────────────────────────────

class IonChamberCard(QWidget):
    """Card display: IC name, counts value, derived voltage, gain ± buttons."""

    def __init__(self, label: str, cfg: dict, bridge: PVBridge, parent=None):
        super().__init__(parent)
        self.label = label
        self.cfg = cfg
        self.bridge = bridge
        self._read_pv       = cfg.get("read_pv") or ""
        self._gain_up_pv    = cfg.get("gain_up_pv") or ""
        self._gain_dn_pv    = cfg.get("gain_down_pv") or ""
        self._unit_num_pv   = cfg.get("unit_num_pv") or ""
        self._unit_text_pv  = cfg.get("unit_text_pv") or ""
        derived = cfg.get("derived", {})
        self._delay_pv      = derived.get("delay_pv") or ""
        self._scale         = derived.get("scale", 1.0)
        self._derived_units = derived.get("units", "V")
        self._mode          = "counts"   # "counts" | "ph/s"
        self._counts        = None
        self._delay_ms      = None
        self._gain_num      = None
        self._gain_text     = None
        self.history_times: deque = deque(maxlen=600)
        self.history_values: deque = deque(maxlen=600)

        self.setStyleSheet(
            f"IonChamberCard {{ background: {PANEL}; border: 1px solid {BORDER}; "
            f"border-radius: 4px; }}"
        )

        outer = QHBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(8)

        # ── Left: name + readouts
        left = QVBoxLayout()
        left.setSpacing(2)

        name_row = QHBoxLayout()
        name_lbl = QLabel(f"<b>{label}</b>")
        name_lbl.setStyleSheet("font-size: 16px; background: transparent;")
        name_row.addWidget(name_lbl)
        name_row.addStretch()
        self.unit_lbl = QLabel(self._derived_units)
        self.unit_lbl.setStyleSheet(f"color: {DIM}; font-size: 14px; background: transparent;")
        name_row.addWidget(self.unit_lbl)
        left.addLayout(name_row)

        cts_row = QHBoxLayout()
        cts_hdr = QLabel("Counts")
        cts_hdr.setStyleSheet(f"color: {DIM}; font-size: 13px; background: transparent;")
        cts_row.addWidget(cts_hdr)
        cts_row.addStretch()
        self.counts_val = QLabel("--")
        self.counts_val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.counts_val.setStyleSheet(
            f"background: {VALBG}; color: #cc4444; "
            f"font-family: 'Liberation Mono', monospace; font-size: 19px; font-weight: bold; "
            f"border: 1px solid {BORDER}; padding: 1px 6px; min-width: 110px;"
        )
        cts_row.addWidget(self.counts_val)
        left.addLayout(cts_row)

        drv_row = QHBoxLayout()
        self.drv_hdr = QLabel("Voltage")
        self.drv_hdr.setStyleSheet(f"color: {DIM}; font-size: 13px; background: transparent;")
        drv_row.addWidget(self.drv_hdr)
        drv_row.addStretch()
        self.derived_val = QLabel("--")
        self.derived_val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.derived_val.setStyleSheet(
            f"background: {VALBG}; color: #7a7aaa; "
            f"font-family: 'Liberation Mono', monospace; font-size: 16px; "
            f"border: 1px solid {BORDER}; padding: 1px 6px; min-width: 110px;"
        )
        drv_row.addWidget(self.derived_val)
        left.addLayout(drv_row)
        outer.addLayout(left, 1)

        # ── Right: mode toggle + gain buttons
        right = QVBoxLayout()
        right.setSpacing(4)
        right.addStretch()

        self.mode_btn = QPushButton("cts")
        self.mode_btn.setFixedSize(44, 22)
        self.mode_btn.setStyleSheet(
            f"QPushButton {{ background: #2a4a2a; color: #90ee90; "
            f"border: 1px solid #3a6a3a; border-radius: 3px; font-size: 14px; }}"
            f"QPushButton:hover {{ background: #3a6a3a; }}"
        )
        self.mode_btn.clicked.connect(self._toggle_mode)
        right.addWidget(self.mode_btn, 0, Qt.AlignCenter)

        gain_row = QHBoxLayout()
        gain_row.setSpacing(4)

        _btn_ss = (
            f"QPushButton {{ background: {PANEL}; color: {TEXT}; "
            f"border: 1px solid {BORDER}; border-radius: 3px; "
            f"font-size: 18px; font-weight: bold; padding: 0 4px; }}"
            f"QPushButton:hover {{ background: {BTNHOV}; }}"
        )

        minus_btn = QPushButton("−")
        minus_btn.setFixedSize(28, 26)
        minus_btn.setStyleSheet(_btn_ss)
        minus_btn.clicked.connect(lambda: self.bridge.put(self._gain_dn_pv, 1) if self._gain_dn_pv else None)
        gain_row.addWidget(minus_btn)

        self.gain_val = QLabel("--")
        self.gain_val.setAlignment(Qt.AlignCenter)
        self.gain_val.setStyleSheet(
            f"color: {TEXT}; font-size: 13px; font-weight: bold; "
            f"background: {VALBG}; border: 1px solid {BORDER}; "
            f"border-radius: 3px; padding: 1px 5px; min-width: 72px;"
        )
        gain_row.addWidget(self.gain_val)

        plus_btn = QPushButton("+")
        plus_btn.setFixedSize(28, 26)
        plus_btn.setStyleSheet(_btn_ss)
        plus_btn.clicked.connect(lambda: self.bridge.put(self._gain_up_pv, 1) if self._gain_up_pv else None)
        gain_row.addWidget(plus_btn)
        right.addLayout(gain_row)
        right.addStretch()
        outer.addLayout(right)

        # Subscribe
        if self._read_pv:
            bridge.updated.connect(self._on_counts)
            bridge.subscribe(self._read_pv)
        if self._delay_pv:
            bridge.updated.connect(self._on_delay)
            bridge.subscribe(self._delay_pv)
        if self._unit_num_pv or self._unit_text_pv:
            bridge.updated.connect(self._on_gain_pv)
            if self._unit_num_pv:
                bridge.subscribe(self._unit_num_pv)
            if self._unit_text_pv:
                bridge.subscribe(self._unit_text_pv)

    def _on_counts(self, pvname: str, value):
        if pvname != self._read_pv:
            return
        self._counts = value
        if value is not None:
            try:
                self.history_times.append(time.time())
                self.history_values.append(float(value))
            except (TypeError, ValueError):
                pass
        self._refresh()

    def _on_delay(self, pvname: str, value):
        if pvname != self._delay_pv:
            return
        self._delay_ms = value
        self._refresh()

    def _on_gain_pv(self, pvname: str, value):
        if pvname == self._unit_num_pv:
            self._gain_num = value
        elif pvname == self._unit_text_pv:
            self._gain_text = str(value).strip() if value is not None else None
        else:
            return
        self._refresh_gain()

    def _refresh_gain(self):
        parts = []
        if self._gain_num is not None:
            try:
                parts.append(str(int(float(self._gain_num))))
            except (TypeError, ValueError):
                parts.append(str(self._gain_num))
        if self._gain_text:
            parts.append(self._gain_text)
        self.gain_val.setText(" ".join(parts) if parts else "--")

    def _refresh(self):
        c = self._counts
        if c is None:
            self.counts_val.setText("--")
            self.derived_val.setText("--")
            return

        if self._mode == "counts":
            self.counts_val.setText(f"{int(c):,}" if isinstance(c, (int, float)) else str(c))
        else:
            if self._delay_ms and float(self._delay_ms) > 0:
                rate = self._scale * float(c) / (float(self._delay_ms) / 1000.0)
                self.counts_val.setText(f"{rate:.3g}")
            else:
                self.counts_val.setText("--")

        if self._delay_ms and float(self._delay_ms) > 0 and isinstance(c, (int, float)):
            v = self._scale * c / (float(self._delay_ms) / 1000.0)
            self.derived_val.setText(f"{v:.3e}")
        else:
            self.derived_val.setText("--")

    def _toggle_mode(self):
        if self._mode == "counts":
            self._mode = "ph/s"
            self.mode_btn.setText("ph/s")
        else:
            self._mode = "counts"
            self.mode_btn.setText("cts")
        self._refresh()


# ── Cryostat bar ──────────────────────────────────────────────────────────────

class CryoBar(QWidget):
    """Label + progress bar + numeric readout for a single cryostat quantity."""

    def __init__(self, label: str, cfg: dict, bridge: PVBridge,
                 vmin: float = 0.0, vmax: float = 100.0,
                 scale: str | None = None, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.bridge = bridge
        self._pvname = cfg.get("read_pv") or ""
        self._vmin   = vmin
        self._vmax   = vmax
        self._scale  = scale or cfg.get("scale")
        self._expr   = cfg.get("expression")
        self._units  = cfg.get("units", "")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 1, 0, 1)
        lay.setSpacing(6)

        lbl = QLabel(label)
        lbl.setFixedWidth(104)
        lay.addWidget(lbl)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(14)
        lay.addWidget(self.bar, 1)

        self.val_lbl = QLabel(f"-- {self._units}")
        self.val_lbl.setFixedWidth(96)
        self.val_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.val_lbl.setStyleSheet(f"font-family: monospace; font-size: 15px;")
        lay.addWidget(self.val_lbl)

        if self._pvname:
            bridge.updated.connect(self._on_update)
            bridge.subscribe(self._pvname)

    def _on_update(self, pvname: str, value):
        if pvname != self._pvname or value is None:
            return
        raw = float(value)
        display = raw
        if self._expr:
            try:
                display = eval(self._expr, {"__builtins__": {}}, {"value": raw})  # noqa: S307
            except Exception:
                pass

        if self._scale == "log_inverse":
            vmin, vmax = self._vmin, self._vmax
            if raw > 0 and vmin > 0 and vmax > 0:
                log_rng = math.log10(vmax) - math.log10(vmin)
                pct = 100.0 * (1.0 - (math.log10(raw) - math.log10(vmin)) / log_rng)
            else:
                pct = 0.0
        else:
            rng = self._vmax - self._vmin
            pct = 100.0 * (display - self._vmin) / rng if rng else 0.0

        self.bar.setValue(int(max(0.0, min(100.0, pct))))
        txt = f"{display:.3e}" if (abs(display) < 1e-3 or abs(display) >= 1e4) else f"{display:.4g}"
        self.val_lbl.setText(f"{txt} {self._units}")


# ── Cryostat panel ────────────────────────────────────────────────────────────

class CryostatPanel(QGroupBox):
    def __init__(self, cfg: dict, bridge: PVBridge, parent=None):
        super().__init__("Cryostat", parent)
        self.bridge = bridge
        self._tsp_write = cfg.get("temperature_setpoint", {}).get("write_pv") or ""
        self._ah_write  = cfg.get("auto_heat", {}).get("write_pv") or ""
        self._gf_write  = cfg.get("gas_flow", {}).get("write_pv") or ""
        self._ah_on     = False
        self._last_temp     = None
        self._last_pressure = None
        self._hist_times:    deque = deque(maxlen=600)
        self._hist_temp:     deque = deque(maxlen=600)
        self._hist_pressure: deque = deque(maxlen=600)
        self._trend_window  = None
        self._trend_canvas  = None
        self._trend_ax      = None
        self._trend_ax_p    = None

        lay = QVBoxLayout(self)
        lay.setSpacing(4)

        # ── Temperature row ──
        tsp = cfg.get("temperature_setpoint", {})
        trd = cfg.get("temperature_readback", {})

        t_row = QHBoxLayout()
        t_row.setSpacing(4)
        t_row.addWidget(QLabel("Temp SP"))

        self.t_input = QLineEdit()
        self.t_input.setPlaceholderText("K")
        self.t_input.setFixedWidth(58)
        self.t_input.setFixedHeight(22)
        self.t_input.returnPressed.connect(self._set_temp)
        t_row.addWidget(self.t_input)

        t_set = _btn("Set", 36, BLUE, "#2a80d9", "#fff", bold=True)
        t_set.clicked.connect(self._set_temp)
        t_row.addWidget(t_set)

        for val in tsp.get("presets", [80, 120, 300]):
            pb = QPushButton(str(val))
            pb.setFixedSize(38, 22)
            pb.setStyleSheet(
                f"QPushButton {{ background: #3a3a5a; color: #aaaad0; "
                f"border: 1px solid #4a4a7a; border-radius: 3px; font-size: 14px; }}"
                f"QPushButton:hover {{ background: #4a4a7a; }}"
            )
            pb.clicked.connect(lambda _, v=val: self.bridge.put(self._tsp_write, float(v)))
            t_row.addWidget(pb)

        t_row.addStretch()

        self.tset_lbl = QLabel("Tset: --")
        self.tset_lbl.setStyleSheet(f"font-family: monospace; font-size: 15px;")
        t_row.addWidget(self.tset_lbl)

        self.tread_lbl = QLabel("Tread: --")
        self.tread_lbl.setStyleSheet(f"font-family: monospace; font-size: 15px;")
        t_row.addWidget(self.tread_lbl)

        self.ah_btn = QPushButton("AutoHeat  OFF")
        self.ah_btn.setFixedHeight(22)
        self.ah_btn.setStyleSheet(
            "QPushButton { background: #5a1a1a; color: #ee9090; "
            "border: 1px solid #7a2a2a; border-radius: 3px; font-weight: bold; }"
            "QPushButton:hover { background: #7a2a2a; }"
        )
        self.ah_btn.clicked.connect(self._toggle_ah)
        t_row.addWidget(self.ah_btn)

        lay.addLayout(t_row)
        lay.addWidget(_sep())

        # ── Progress bars ──
        bars = [
            ("heater_power", "Heater Power", 0.0, 100.0, None),
            ("vacuum",       "Cryo Insulation", 1e-7, 1e-1, "log_inverse"),
            ("ln2_weight",   "LN2 Weight",   0.0, 100.0, None),
            ("gas_flow",     "Gas Flow",     0.0, 100.0, None),
        ]
        for key, lbl, vmin, vmax, scale in bars:
            bcfg = cfg.get(key, {})
            if bcfg.get("read_pv"):
                lay.addWidget(CryoBar(lbl, bcfg, bridge, vmin, vmax, scale))

        # ── Gas flow setpoint ──
        gf = cfg.get("gas_flow", {})
        if gf.get("write_pv"):
            gf_row = QHBoxLayout()
            gf_row.setSpacing(4)
            gf_row.addWidget(QLabel("Gas Flow SP"))
            self.gf_input = QLineEdit()
            self.gf_input.setPlaceholderText("%")
            self.gf_input.setFixedWidth(58)
            self.gf_input.setFixedHeight(22)
            self.gf_input.returnPressed.connect(self._set_gas)
            gf_row.addWidget(self.gf_input)
            gf_set = _btn("Set", 36, BLUE, "#2a80d9", "#fff", bold=True)
            gf_set.clicked.connect(self._set_gas)
            gf_row.addWidget(gf_set)
            gf_row.addStretch()
            lay.addLayout(gf_row)
        else:
            self.gf_input = None

        # Subscribe temperature + vacuum PVs
        self._tsp_read = tsp.get("read_pv") or ""
        self._trd_read = trd.get("read_pv") or ""
        self._ah_read  = cfg.get("auto_heat", {}).get("read_pv") or ""
        self._vac_read = cfg.get("vacuum", {}).get("read_pv") or ""

        bridge.updated.connect(self._on_update)
        for pv in (self._tsp_read, self._trd_read, self._ah_read, self._vac_read):
            if pv:
                bridge.subscribe(pv)

        # Trend button
        trend_btn = _btn("Show Trend", 100)
        trend_btn.clicked.connect(self._show_trend)
        lay.addWidget(trend_btn)

    def _record_history(self):
        if self._last_temp is not None and self._last_pressure is not None:
            self._hist_times.append(time.time())
            self._hist_temp.append(self._last_temp)
            self._hist_pressure.append(self._last_pressure)
            self._refresh_trend()

    def _on_update(self, pvname: str, value):
        if pvname == self._tsp_read and value is not None:
            self.tset_lbl.setText(
                f"Tset: {float(value):.1f} K" if isinstance(value, (int, float)) else f"Tset: {value}"
            )
        elif pvname == self._trd_read and value is not None:
            self.tread_lbl.setText(
                f"Tread: {float(value):.1f} K" if isinstance(value, (int, float)) else f"Tread: {value}"
            )
            try:
                self._last_temp = float(value)
                self._record_history()
            except (TypeError, ValueError):
                pass
        elif pvname == self._vac_read and value is not None:
            try:
                self._last_pressure = float(value)
                self._record_history()
            except (TypeError, ValueError):
                pass
        elif pvname == self._ah_read:
            on = (bool(float(value)) if isinstance(value, (int, float))
                  else str(value).upper() in ("1", "ON", "ENABLED"))
            self._ah_on = on
            if on:
                self.ah_btn.setText("AutoHeat  ON")
                self.ah_btn.setStyleSheet(
                    "QPushButton { background: #1a5a1a; color: #90ee90; "
                    "border: 1px solid #2a7a2a; border-radius: 3px; font-weight: bold; }"
                    "QPushButton:hover { background: #2a7a2a; }"
                )
            else:
                self.ah_btn.setText("AutoHeat  OFF")
                self.ah_btn.setStyleSheet(
                    "QPushButton { background: #5a1a1a; color: #ee9090; "
                    "border: 1px solid #7a2a2a; border-radius: 3px; font-weight: bold; }"
                    "QPushButton:hover { background: #7a2a2a; }"
                )

    def _show_trend(self):
        if self._trend_window is not None and not self._trend_window.isHidden():
            self._trend_window.raise_()
            self._trend_window.activateWindow()
            return

        try:
            from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
            from matplotlib.figure import Figure
        except Exception as exc:
            from PyQt5.QtWidgets import QMessageBox
            QMessageBox.warning(self, "Cryostat Trend", f"Matplotlib not available: {exc}")
            return

        win = QWidget()
        win.setWindowTitle("Cryostat Trends")
        win.resize(900, 500)
        lay = QVBoxLayout(win)
        lay.setContentsMargins(8, 8, 8, 8)

        figure = Figure(figsize=(8, 4), facecolor="#2b2b2b")
        ax = figure.add_subplot(111, facecolor="#1e1e1e")
        ax_p = ax.twinx()
        canvas = FigureCanvas(figure)

        self._trend_canvas = canvas
        self._trend_ax = ax
        self._trend_ax_p = ax_p

        lay.addWidget(canvas)

        timer = QTimer(win)
        timer.timeout.connect(self._refresh_trend)
        timer.start(2000)

        win.show()
        self._trend_window = win
        self._refresh_trend()

    def _refresh_trend(self):
        if self._trend_canvas is None or self._trend_window is None or self._trend_window.isHidden():
            return
        ax = self._trend_ax
        ax_p = self._trend_ax_p
        ax.clear()
        ax_p.clear()

        if self._hist_times:
            latest = self._hist_times[-1]
            rel = [t - latest for t in self._hist_times]
            tl = ax.plot(rel, list(self._hist_temp), color="#cc4444", label="Temperature")[0]
            pl = ax_p.plot(rel, list(self._hist_pressure), color="#4488cc", label="Pressure")[0]
            ax.legend([tl, pl], ["Temperature (K)", "Pressure (mbar)"], loc="best",
                      facecolor="#363636", labelcolor="#e0e0e0")

        ax.set_xlabel("Time relative to latest sample (s)", color="#888888")
        ax.set_ylabel("Temperature (K)", color="#cc4444")
        ax_p.set_ylabel("Pressure (mbar)", color="#4488cc")
        ax.set_title("Cryostat Trends", color="#e0e0e0")
        ax.tick_params(axis="y", colors="#cc4444")
        ax_p.tick_params(axis="y", colors="#4488cc")
        ax.tick_params(axis="x", colors="#888888")
        ax.grid(True, alpha=0.2, color="#555555")
        self._trend_canvas.draw()

    def _set_temp(self):
        if self._tsp_write and self.t_input.text().strip():
            try:
                self.bridge.put(self._tsp_write, float(self.t_input.text()))
            except ValueError:
                pass

    def _toggle_ah(self):
        if self._ah_write:
            self.bridge.put(self._ah_write, 0 if self._ah_on else 1)

    def _set_gas(self):
        if self._gf_write and self.gf_input and self.gf_input.text().strip():
            try:
                self.bridge.put(self._gf_write, float(self.gf_input.text()))
            except ValueError:
                pass


# ── Furnace panel ─────────────────────────────────────────────────────────────

class FurnacePanel(QWidget):
    def __init__(self, cfg: dict, bridge: PVBridge, parent=None):
        super().__init__(parent)
        self.bridge = bridge

        trd_cfg = cfg.get("temperature_readback", {})
        tsp_cfg = cfg.get("temperature_setpoint", {})
        hr_cfg  = cfg.get("heat_rate", {})

        self._trd_pv   = trd_cfg.get("read_pv", "")
        self._tsp_read = tsp_cfg.get("read_pv", "")
        self._tsp_write= tsp_cfg.get("write_pv", "")
        self._hr_read  = hr_cfg.get("read_pv", "")
        self._hr_write = hr_cfg.get("write_pv", "")
        self._stop_pv  = cfg.get("stop_pv", "")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)

        # ── Temperature setpoint row ──
        tsp_row = QHBoxLayout()
        tsp_row.setSpacing(4)
        tsp_row.addWidget(QLabel("Temp SP"))
        self.t_input = QLineEdit()
        self.t_input.setPlaceholderText("°C")
        self.t_input.setFixedWidth(58)
        self.t_input.setFixedHeight(22)
        self.t_input.returnPressed.connect(self._set_temp)
        tsp_row.addWidget(self.t_input)
        t_set = _btn("Set", 36, BLUE, "#2a80d9", "#fff", bold=True)
        t_set.clicked.connect(self._set_temp)
        tsp_row.addWidget(t_set)
        for val in tsp_cfg.get("presets", [100, 200, 400, 600, 800]):
            pb = QPushButton(str(val))
            pb.setFixedSize(38, 22)
            pb.setStyleSheet(
                "QPushButton { background: #5a3a1a; color: #e0b080; "
                "border: 1px solid #7a5a2a; border-radius: 3px; font-size: 14px; }"
                "QPushButton:hover { background: #7a5a2a; }"
            )
            pb.clicked.connect(lambda _, v=val: self.bridge.put(self._tsp_write, float(v)))
            tsp_row.addWidget(pb)
        tsp_row.addStretch()
        self.tset_lbl = QLabel("SP: --")
        self.tset_lbl.setStyleSheet("font-family: monospace; font-size: 15px;")
        tsp_row.addWidget(self.tset_lbl)
        self.tread_lbl = QLabel("T: --")
        self.tread_lbl.setStyleSheet("font-family: monospace; font-size: 15px;")
        tsp_row.addWidget(self.tread_lbl)
        lay.addLayout(tsp_row)
        lay.addWidget(_sep())

        # ── Heat rate row ──
        hr_row = QHBoxLayout()
        hr_row.setSpacing(4)
        hr_row.addWidget(QLabel("Heat Rate"))
        self.hr_input = QLineEdit()
        self.hr_input.setPlaceholderText("°C/min")
        self.hr_input.setFixedWidth(58)
        self.hr_input.setFixedHeight(22)
        self.hr_input.returnPressed.connect(self._set_hr)
        hr_row.addWidget(self.hr_input)
        hr_set = _btn("Set", 36, BLUE, "#2a80d9", "#fff", bold=True)
        hr_set.clicked.connect(self._set_hr)
        hr_row.addWidget(hr_set)
        hr_row.addStretch()
        self.hr_lbl = QLabel("Rate: --")
        self.hr_lbl.setStyleSheet("font-family: monospace; font-size: 15px;")
        hr_row.addWidget(self.hr_lbl)
        lay.addLayout(hr_row)
        lay.addWidget(_sep())

        # ── Progress bars ──
        lay.addWidget(CryoBar("Temperature", trd_cfg, bridge, 0.0, 1000.0))
        lay.addWidget(CryoBar("Heat Rate",   hr_cfg,  bridge, 0.0, 50.0))

        # ── Stop button ──
        stop_btn = _btn("STOP Furnace", color="#7a1a1a", border="#aa2a2a",
                        text_color="#ffaaaa", bold=True)
        stop_btn.setFixedHeight(26)
        stop_btn.clicked.connect(self._stop)
        lay.addWidget(stop_btn)
        lay.addStretch()

        # Subscribe PVs
        bridge.updated.connect(self._on_update)
        for pv in (self._trd_pv, self._tsp_read, self._hr_read):
            if pv:
                bridge.subscribe(pv)

    def _on_update(self, pvname: str, value):
        if value is None:
            return
        if pvname == self._trd_pv:
            self.tread_lbl.setText(
                f"T: {float(value):.1f} °C" if isinstance(value, (int, float)) else f"T: {value}"
            )
        elif pvname == self._tsp_read:
            self.tset_lbl.setText(
                f"SP: {float(value):.1f} °C" if isinstance(value, (int, float)) else f"SP: {value}"
            )
        elif pvname == self._hr_read:
            self.hr_lbl.setText(
                f"Rate: {float(value):.2f} °C/min" if isinstance(value, (int, float)) else f"Rate: {value}"
            )

    def _set_temp(self):
        if self._tsp_write and self.t_input.text().strip():
            try:
                self.bridge.put(self._tsp_write, float(self.t_input.text()))
            except ValueError:
                pass

    def _set_hr(self):
        if self._hr_write and self.hr_input.text().strip():
            try:
                self.bridge.put(self._hr_write, float(self.hr_input.text()))
            except ValueError:
                pass

    def _stop(self):
        if self._stop_pv:
            self.bridge.put(self._stop_pv, 1)


# ── Main window ───────────────────────────────────────────────────────────────

class BeamlineControlWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Beamline Control — CLS 1607-7-I21")
        self.resize(1380, 900)

        self.bridge = PVBridge()
        config = load_pv_config()
        self._control_rows: list[ControlRow] = []
        self._ic_cards: list[IonChamberCard] = []
        self._ic_trend_window = None

        central = QWidget()
        central.setObjectName("root")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(5)

        # ── Header bar ──
        hdr = QHBoxLayout()
        hdr.setSpacing(10)
        title = QLabel("<b>Beamline 1607-7-I21  Control</b>")
        title.setStyleSheet("font-size: 20px; color: #e0e0e0; background: transparent;")
        hdr.addWidget(title)
        hdr.addStretch()
        mode_lbl = QLabel("●  DUMMY MODE — no EPICS connection")
        mode_lbl.setStyleSheet(f"color: {MINOR}; font-size: 15px; font-weight: bold; background: transparent;")
        hdr.addWidget(mode_lbl)

        # Ring current readback
        _rc_pv = (config.get("status") or {}).get("Ring Status", {}).get("read_pv", "PCT1402-01:mA:fbk")
        rc_lbl = QLabel("Ring: -- mA")
        rc_lbl.setStyleSheet(
            f"font-family: monospace; font-size: 17px; font-weight: bold; "
            f"color: {OK}; background: transparent;"
        )
        def _on_rc(pvname, value, _lbl=rc_lbl):
            if pvname == _rc_pv and value is not None:
                try:
                    _lbl.setText(f"Ring: {float(value):.1f} mA")
                except (TypeError, ValueError):
                    pass
        self.bridge.updated.connect(_on_rc)
        self.bridge.subscribe(_rc_pv)
        hdr.addWidget(rc_lbl)

        _st  = config.get("status")   or {}
        _cmd = config.get("commands") or {}
        _fe_read  = _st.get("FE Shutter",       {}).get("read_pv",  "IPSH1407-I00-02:state")
        _fe_open  = _cmd.get("Open FE Shutter",  {}).get("write_pv", "PV:FE:SHUTTER:OPEN")
        _fe_close = _cmd.get("Close FE Shutter", {}).get("write_pv", "PV:FE:SHUTTER:CLOSE")
        hdr.addWidget(FEShutterWidget(_fe_read, _fe_open, _fe_close, self.bridge))
        root.addLayout(hdr)

        # ── Status chips (inline, no scroll wrapper) ──
        status_cfg = config.get("status") or {}
        chips_row = QHBoxLayout()
        chips_row.setContentsMargins(0, 2, 0, 2)
        chips_row.setSpacing(5)
        for lbl, scfg in status_cfg.items():
            chips_row.addWidget(StatusChip(lbl, scfg, self.bridge))
        chips_row.addStretch()
        root.addLayout(chips_row)
        root.addWidget(_sep())

        # ── Main scrollable content ──
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setStyleSheet(f"background: {BG}; border: none;")

        content = QWidget()
        content.setStyleSheet(f"background: {BG};")
        content_lay = QHBoxLayout(content)
        content_lay.setContentsMargins(0, 0, 0, 0)
        content_lay.setSpacing(8)

        # ── Left column: controls ──
        left = QWidget()
        left.setStyleSheet(f"background: {BG};")
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(6)

        # ── Dashboard cards ──
        _ctrl = config.get("controls") or {}
        dash_row = QHBoxLayout()
        dash_row.setSpacing(6)
        _dash_params = [
            ("Mono Energy", ".1f", "eV"),
            ("M1 Pitch",    ".4f", "mrad"),
            ("Stage Z",     ".3f", "mm"),
            ("Stage Y",     ".3f", "mm"),
        ]
        for param, fmt, units in _dash_params:
            pv = _ctrl.get(param, {}).get("read_pv", "")
            card = DashboardCard(param, pv, self.bridge, fmt=fmt, units=units)
            card.setMinimumWidth(130)
            dash_row.addWidget(card, 1)
        left_lay.addLayout(dash_row)

        ctrl_group = QGroupBox("Beamline Controls")
        cg = QVBoxLayout(ctrl_group)
        cg.setSpacing(0)

        # Column headers
        hdr_row = QHBoxLayout()
        hdr_row.setContentsMargins(2, 0, 2, 4)
        hdr_row.setSpacing(4)
        for txt, w in [("Parameter", 128), ("Readback", 96), ("Setpoint", 80), ("Units", 52), ("", 40)]:
            h = QLabel(txt)
            h.setStyleSheet(f"color: {DIM}; font-size: 14px; font-weight: bold; background: transparent;")
            h.setFixedWidth(w)
            hdr_row.addWidget(h)
        hdr_row.addStretch()
        cg.addLayout(hdr_row)
        cg.addWidget(_sep())

        controls_cfg = config.get("controls") or {}

        def _section(title: str):
            cg.addWidget(_sep())
            sh = QLabel(title)
            sh.setStyleSheet(
                f"color: {DIM}; font-size: 14px; font-weight: bold; "
                f"background: transparent; padding: 3px 4px 1px 4px;"
            )
            cg.addWidget(sh)

        def _add_row(lbl: str):
            ccfg = controls_cfg.get(lbl)
            if ccfg is None:
                return
            row = ControlRow(lbl, ccfg, self.bridge)
            cg.addWidget(row)
            self._control_rows.append(row)

        _section("Beam")
        for lbl in ("Mono Energy", "Dwell Time", "M1 Pitch"):
            _add_row(lbl)

        _section("Stage")
        for lbl in ("Stage Z", "Stage Y"):
            _add_row(lbl)

        _section("JJ Slits")
        for lbl in ("JJ Vert Gap", "JJ Vert Center", "JJ Hor Gap", "JJ Hor Center"):
            _add_row(lbl)

        _section("DBHR")
        for lbl in ("DBHR M1", "DBHR M2", "DBHR Pitch"):
            _add_row(lbl)

        # Any remaining controls not in a named group
        known = {"Mono Energy", "Dwell Time", "M1 Pitch",
                 "Stage Z", "Stage Y",
                 "JJ Vert Gap", "JJ Vert Center", "JJ Hor Gap", "JJ Hor Center",
                 "DBHR M1", "DBHR M2", "DBHR Pitch"}
        extra = [l for l in controls_cfg if l not in known]
        if extra:
            _section("Other")
            for lbl in extra:
                _add_row(lbl)

        left_lay.addWidget(ctrl_group)
        left_lay.addStretch()
        content_lay.addWidget(left, 3)

        # ── Right column: ion chambers + cryostat ──
        right = QWidget()
        right.setStyleSheet(f"background: {BG};")
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.setSpacing(6)

        ic_cfg = config.get("ion_chambers", {})
        if ic_cfg:
            ic_group = QGroupBox("Ion Chambers")
            ic_lay = QVBoxLayout(ic_group)
            ic_lay.setSpacing(5)
            for ic_lbl, iccfg in ic_cfg.items():
                card = IonChamberCard(ic_lbl, iccfg, self.bridge)
                self._ic_cards.append(card)
                ic_lay.addWidget(card)
            trend_btn = _btn("Show Trend", 100)
            trend_btn.clicked.connect(self._show_ic_trends)
            ic_lay.addWidget(trend_btn)
            right_lay.addWidget(ic_group)

        cryo_cfg     = config.get("cryostat", {})
        furnace_cfg  = config.get("furnace", {})
        if cryo_cfg or furnace_cfg:
            tab = QTabWidget()
            tab.setStyleSheet(f"""
                QTabWidget::pane {{
                    border: 1px solid {BORDER};
                    background: {PANEL};
                }}
                QTabBar::tab {{
                    background: {BTN};
                    color: {TEXT};
                    border: 1px solid {BORDER};
                    border-bottom: none;
                    padding: 4px 12px;
                    font-size: 15px;
                }}
                QTabBar::tab:selected {{
                    background: {PANEL};
                    border-bottom: 1px solid {PANEL};
                    font-weight: bold;
                }}
                QTabBar::tab:hover {{ background: {BTNHOV}; }}
            """)
            if cryo_cfg:
                tab.addTab(CryostatPanel(cryo_cfg, self.bridge), "Cryostat")
            if furnace_cfg:
                tab.addTab(FurnacePanel(furnace_cfg, self.bridge), "Furnace")
            right_lay.addWidget(tab)

        right_lay.addStretch()
        content_lay.addWidget(right, 2)

        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        # ── Footer ──
        root.addWidget(_sep())
        footer = QHBoxLayout()
        refresh_btn = _btn("Refresh All", 100)
        refresh_btn.setFixedHeight(26)
        footer.addWidget(refresh_btn)
        clear_btn = _btn("Clear Setpoints", 110)
        clear_btn.setFixedHeight(26)
        clear_btn.clicked.connect(self._clear_setpoints)
        footer.addWidget(clear_btn)
        footer.addStretch()
        info = QLabel(f"pvs.yaml: {PV_CONFIG_FILE.name}  |  pyepics {getattr(epics, '__version__', 'n/a')}")
        info.setStyleSheet(f"color: {DIM}; font-size: 14px; background: transparent;")
        footer.addWidget(info)
        root.addLayout(footer)

    def _clear_setpoints(self):
        for row in self._control_rows:
            if row.sp_input:
                row.sp_input.clear()

    def _show_ic_trends(self):
        if self._ic_trend_window is not None and not self._ic_trend_window.isHidden():
            self._ic_trend_window.raise_()
            self._ic_trend_window.activateWindow()
            return

        if not self._ic_cards:
            return

        try:
            from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
            from matplotlib.figure import Figure
        except Exception as exc:
            from PyQt5.QtWidgets import QMessageBox
            QMessageBox.warning(self, "Ion Chamber Trends", f"Matplotlib not available: {exc}")
            return

        n = len(self._ic_cards)
        win = QWidget()
        win.setWindowTitle("Ion Chamber Trends")
        win.resize(900, 220 * n)
        lay = QVBoxLayout(win)
        lay.setContentsMargins(8, 8, 8, 8)

        figure = Figure(figsize=(8, 2.5 * n), facecolor="#2b2b2b")
        figure.tight_layout()
        axes_list = []
        shared_ax = None
        for i, card in enumerate(self._ic_cards):
            ax = figure.add_subplot(n, 1, i + 1, sharex=shared_ax, facecolor="#1e1e1e")
            if shared_ax is None:
                shared_ax = ax
            ax.set_title(card.label, color="#e0e0e0")
            ax.set_ylabel("Signal", color="#888888")
            ax.tick_params(colors="#888888")
            ax.grid(True, alpha=0.2, color="#555555")
            if i < n - 1:
                ax.tick_params(labelbottom=False)
            axes_list.append(ax)
        if axes_list:
            axes_list[-1].set_xlabel("Time relative to latest sample (s)", color="#888888")
        figure.subplots_adjust(hspace=0.45)

        canvas = FigureCanvas(figure)
        lay.addWidget(canvas)

        def _refresh():
            for ax, card in zip(axes_list, self._ic_cards):
                ax.clear()
                ax.set_title(card.label, color="#e0e0e0")
                ax.set_ylabel("Signal", color="#888888")
                ax.tick_params(colors="#888888")
                ax.grid(True, alpha=0.2, color="#555555")
                if card.history_times:
                    latest = card.history_times[-1]
                    rel = [t - latest for t in card.history_times]
                    ax.plot(rel, list(card.history_values), color="#4488cc")
            if axes_list:
                axes_list[-1].set_xlabel("Time relative to latest sample (s)", color="#888888")
            canvas.draw()

        timer = QTimer(win)
        timer.timeout.connect(_refresh)
        timer.start(2000)

        _refresh()
        win.show()
        self._ic_trend_window = win

    def closeEvent(self, event):
        self.bridge.cleanup()
        super().closeEvent(event)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--embed", type=int, default=None, metavar="PARENT_WINID")
    parser.add_argument("--width", type=int, default=0)
    parser.add_argument("--height", type=int, default=0)
    parser.add_argument("--sim", action="store_true", help="Use simulated PVs")
    args, _ = parser.parse_known_args()

    if args.sim:
        try:
            import sim_pvs
            sim_pvs.patch_epics()
            _sim = sim_pvs.SimulatedBeamline()
            _sim.start()
        except ImportError:
            pass

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(_qss())
    win = BeamlineControlWindow()

    if args.embed is not None:
        # Embed inside the wx panel using Qt's native foreign-window API.
        # QWindow.fromWinId() wraps the wx panel's X11 window; setting it as
        # the Qt-level parent causes the child to paint inside it correctly.
        win.setWindowFlags(Qt.FramelessWindowHint)
        win.setAttribute(Qt.WA_DontCreateNativeAncestors, True)
        win.winId()  # force native X11 window creation before reparenting

        parent_qwin = QWindow.fromWinId(args.embed)
        win.windowHandle().setParent(parent_qwin)

        w = args.width or parent_qwin.width() or 1200
        h = args.height or parent_qwin.height() or 800
        win.resize(w, h)
        win.show()

        print(f"WINID:{int(win.winId())}", flush=True)
    else:
        win.show()

    ret = app.exec_()
    sys.exit(ret)


if __name__ == "__main__":
    main()

