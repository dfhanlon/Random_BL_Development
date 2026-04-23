"""Dual-detector EPICS XRF application built on xraylarch."""

from __future__ import annotations

from collections import deque
import ast
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Sequence

import yaml


@dataclass(slots=True)
class DetectorConfig:
    name: str
    prefix: str
    nmca: int = 4
    det_type: str = "ME-4"
    ioc_type: str = "xspress3"
    environ_file: str | None = None
    incident_energy_pvname: str | None = None
    incident_energy_units: str = "eV"
    stage_read_pv: str = ""
    stage_write_pv: str = ""


PV_CONFIG_FILE = Path(__file__).with_name("pvs.yaml")


def _load_pv_config():
    if not PV_CONFIG_FILE.exists():
        return {}
    try:
        data = yaml.safe_load(PV_CONFIG_FILE.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _load_xraylarch_runtime():
    import numpy as np
    import wx

    from wxutils import DARK_THEME, register_darkdetect

    from larch.epics.xrfcontrol import EpicsXRFApp, EpicsXRFDisplayFrame
    from larch.interpreter import Interpreter
    from larch.site_config import icondir
    from larch.wxlib import get_color
    from larch.wxlib.larchframe import LarchFrame
    from larch.wxlib.xrfdisplay import ICON_FILE, XRFDisplayFrame
    from larch.wxlib.xrfdisplay_utils import (
        XRFDisplayColors_Dark,
        XRFDisplayColors_Light,
    )

    return {
        "np": np,
        "wx": wx,
        "DARK_THEME": DARK_THEME,
        "register_darkdetect": register_darkdetect,
        "EpicsXRFApp": EpicsXRFApp,
        "EpicsXRFDisplayFrame": EpicsXRFDisplayFrame,
        "XRFDisplayFrame": XRFDisplayFrame,
        "Interpreter": Interpreter,
        "LarchFrame": LarchFrame,
        "icondir": icondir,
        "get_color": get_color,
        "ICON_FILE": ICON_FILE,
        "XRFDisplayColors_Dark": XRFDisplayColors_Dark,
        "XRFDisplayColors_Light": XRFDisplayColors_Light,
    }


_RUNTIME = None


def _runtime():
    global _RUNTIME
    if _RUNTIME is None:
        _RUNTIME = _load_xraylarch_runtime()
    return _RUNTIME


def build_app_classes():
    runtime = _runtime()
    np = runtime["np"]
    wx = runtime["wx"]
    DARK_THEME = runtime["DARK_THEME"]
    register_darkdetect = runtime["register_darkdetect"]
    EpicsXRFApp = runtime["EpicsXRFApp"]
    EpicsXRFDisplayFrame = runtime["EpicsXRFDisplayFrame"]
    XRFDisplayFrame = runtime["XRFDisplayFrame"]
    Interpreter = runtime["Interpreter"]
    LarchFrame = runtime["LarchFrame"]
    icondir = runtime["icondir"]
    get_color = runtime["get_color"]
    ICON_FILE = runtime["ICON_FILE"]
    XRFDisplayColors_Dark = runtime["XRFDisplayColors_Dark"]
    XRFDisplayColors_Light = runtime["XRFDisplayColors_Light"]

    class BeamlineControlPanel(wx.Panel):
        """Beamline control panel with placeholder PV support and enabled/disabled rows."""

        def __init__(
            self,
            parent,
            *,
            detector: DetectorConfig,
            size=(1000, 760),
            title: str | None = None,
            **kws,
        ):
            super().__init__(parent=parent, size=size, **kws)

            if title is None:
                title = detector.name

            self.page_label = detector.name
            self.window_title = title
            self.prefix = detector.prefix
            self.colors = XRFDisplayColors_Dark() if DARK_THEME else XRFDisplayColors_Light()
            register_darkdetect(self.onDarkMode)

            self.pv_config = _load_pv_config()
            self.epics = self._load_epics()
            self.pv_controls = {}
            self.main_cryostat_widgets = {}
            self.cryostat_popup_widgets = None
            self.cryostat_frame = None
            self.cryostat_state = {}
            self.cryostat_history = {
                "times": deque(maxlen=600),
                "temperature": deque(maxlen=600),
                "pressure": deque(maxlen=600),
            }
            self.cryostat_trend_canvas = None
            self.cryostat_trend_axes = None
            self.cryostat_trend_pressure_axes = None
            self.status_lamps = {}
            self.last_pv_error = None
            self._pv_cache = {}
            self._pv_callbacks = []
            self.status_state = {}
            self.ion_chamber_history = {
                label: {"times": deque(maxlen=600), "values": deque(maxlen=600)}
                for label, cfg in self._ion_chamber_config().items()
                if cfg.get("read_pv")
            }
            self.ion_chamber_state = {
                label: {
                    "value": None,
                    "delay": None,
                    "unit_num": None,
                    "unit_text": None,
                    "display_mode": "counts",
                }
                for label, cfg in self._ion_chamber_config().items()
                if cfg.get("read_pv")
            }
            self.ion_chamber_trend_frame = None
            self.ion_chamber_trend_canvas = None
            self.ion_chamber_trend_axes = None
            self.ion_chamber_trend_checks = {}
            self.ion_chamber_trend_selection = {
                label: True for label in self.ion_chamber_history.keys()
            }

            # --------------------------------------------------------------
            # Toggle this during development.
            # True  -> lightweight mode, only key PVs enabled
            # False -> everything enabled that you marked as True below
            # --------------------------------------------------------------
            LIGHTWEIGHT_BL_MODE = True

            self.pv_definitions = {
                "status": [
                    {
                        "label": "Ring Status",
                        "read_pv": "PCT1402-01:mA:fbk",
                        "write_pv": None,
                        "kind": "status",
                        "status_threshold": 140.0,
                        "status_good_label": "OK",
                        "status_bad_label": "BAD",
                        "readonly": True,
                        "enabled": True,
                    },
                    {
                        "label": "FE Shutter",
                        "read_pv": "IPSH1407-I00-02:state",
                        "write_pv": None,
                        "kind": "status",
                        "status_good_label": "OPEN",
                        "status_bad_label": "CLOSED",
                        "readonly": True,
                        "enabled": True,
                    },
                    {
                        "label": "Valves Status",
                        "read_pv": "PLC2406-7-03:BL:ready:in",
                        "write_pv": None,
                        "kind": "status",
                        "readonly": True,
                        "enabled": True,
                    },
                    {
                        "label": "Motor Interlocks",
                        "read_pv": "PV:MOTOR:INTERLOCKS",
                        "write_pv": None,
                        "kind": "status",
                        "readonly": True,
                        "enabled": True,
                    },
                    {
                        "label": "Wiggler Status",
                        "read_pv": "PV:WIGGLER:STATUS",
                        "write_pv": None,
                        "kind": "status",
                        "readonly": True,
                        "enabled": not LIGHTWEIGHT_BL_MODE,
                    },
                    {
                        "label": "FE Safety Shutter",
                        "read_pv": "PV:FE:SAFETY:SHUTTER:STATE",
                        "write_pv": None,
                        "kind": "status",
                        "readonly": True,
                        "enabled": not LIGHTWEIGHT_BL_MODE,
                    },
                    {
                        "label": "M1 Blade Mask",
                        "read_pv": "PV:M1:BLADEMASK:STATE",
                        "write_pv": None,
                        "kind": "status",
                        "readonly": True,
                        "enabled": not LIGHTWEIGHT_BL_MODE,
                    },
                    {
                        "label": "Mono Mask",
                        "read_pv": "PV:MONO:MASK:STATE",
                        "write_pv": None,
                        "kind": "status",
                        "readonly": True,
                        "enabled": not LIGHTWEIGHT_BL_MODE,
                    },
                    {
                        "label": "M2 Paddle",
                        "read_pv": "PV:M2:PADDLE:STATE",
                        "write_pv": None,
                        "kind": "status",
                        "readonly": True,
                        "enabled": not LIGHTWEIGHT_BL_MODE,
                    },
                    {
                        "label": "POE Shutter",
                        "read_pv": "PV:POE:SHUTTER:STATE",
                        "write_pv": None,
                        "kind": "status",
                        "readonly": True,
                        "enabled": not LIGHTWEIGHT_BL_MODE,
                    },
                    {
                        "label": "IC HV",
                        "read_pv": "PV:IC:HV:STATE",
                        "write_pv": None,
                        "kind": "status",
                        "readonly": True,
                        "enabled": not LIGHTWEIGHT_BL_MODE,
                    },
                    {
                        "label": "XIA Filters",
                        "read_pv": "PV:XIA:FILTERS:STATE",
                        "write_pv": None,
                        "kind": "status",
                        "readonly": True,
                        "enabled": not LIGHTWEIGHT_BL_MODE,
                    },
                ],
                "controls": [
                    {
                        "label": "Mono Energy",
                        "read_pv": "BL1607-I21:Mono:Energy:EV:fbk",
                        "write_pv": "BL1607-I21:Mono:Energy:EV",
                        "kind": "numeric",
                        "units": "eV",
                        "readonly": False,
                        "enabled": True,
                    },
                    {
                        "label": "Dwell Time",
                        "read_pv": "MCS1607-701:mcs:delay",
                        "write_pv": "PV:DWELL:TIME:SP",
                        "kind": "numeric",
                        "units": "ms",
                        "readonly": False,
                        "enabled": True,
                    },
                    {
                        "label": "Stage Z",
                        "read_pv": "SMTR1607-7-I21-16:mm:fbk",
                        "write_pv": "PV:STAGE:Z:SP",
                        "kind": "numeric",
                        "units": "mm",
                        "readonly": False,
                        "enabled": True,
                    },
                    {
                        "label": "Ring Current",
                        "read_pv": "PCT1402-01:mA:fbk",
                        "write_pv": None,
                        "kind": "numeric",
                        "units": "mA",
                        "readonly": True,
                        "enabled": True,
                    },
                    {
                        "label": "Furnace Temp",
                        "read_pv": "PV:FURNACE:TEMP:RBV",
                        "write_pv": None,
                        "kind": "numeric",
                        "units": "C",
                        "readonly": True,
                        "enabled": True,
                    },
                    {
                        "label": "Heat Rate",
                        "read_pv": "PV:FURNACE:HEATRATE:RBV",
                        "write_pv": "PV:FURNACE:HEATRATE:SP",
                        "kind": "numeric",
                        "units": "deg/min",
                        "readonly": False,
                        "enabled": True,
                    },
                    {
                        "label": "M1 Pitch",
                        "read_pv": "PV:M1:PITCH:RBV",
                        "write_pv": "PV:M1:PITCH:SP",
                        "kind": "numeric",
                        "units": "mrad",
                        "readonly": False,
                        "enabled": not LIGHTWEIGHT_BL_MODE,
                    },
                    {
                        "label": "JJ Vert Gap",
                        "read_pv": "PSL1607-7-I21-01:Gap:mm",
                        "write_pv": "PV:JJ:VGAP:SP",
                        "kind": "numeric",
                        "units": "mm",
                        "readonly": False,
                        "enabled": True,
                    },
                    {
                        "label": "JJ Vert Center",
                        "read_pv": "PSL1607-7-I21-01:Center:mm",
                        "write_pv": "PV:JJ:VCEN:SP",
                        "kind": "numeric",
                        "units": "mm",
                        "readonly": False,
                        "enabled": True,
                    },
                    {
                        "label": "JJ Hor Gap",
                        "read_pv": "PSL1607-7-I21-02:Gap:mm",
                        "write_pv": "PV:JJ:HGAP:SP",
                        "kind": "numeric",
                        "units": "mm",
                        "readonly": False,
                        "enabled": True,
                    },
                    {
                        "label": "JJ Hor Center",
                        "read_pv": "PSL1607-7-I21-02:Center:mm",
                        "write_pv": "PV:JJ:HCEN:SP",
                        "kind": "numeric",
                        "units": "mm",
                        "readonly": False,
                        "enabled": True,
                    },
                    {
                        "label": "Stage Y",
                        "read_pv": "PV:STAGE:Y:RBV",
                        "write_pv": "PV:STAGE:Y:SP",
                        "kind": "numeric",
                        "units": "mm",
                        "readonly": False,
                        "enabled": not LIGHTWEIGHT_BL_MODE,
                    },
                    *self._build_ion_chamber_controls(),
                    {
                        "label": "Cryostat Holder",
                        "read_pv": "PV:CRYO:HOLDER:RBV",
                        "write_pv": "PV:CRYO:HOLDER:SP",
                        "kind": "enum",
                        "units": "",
                        "readonly": False,
                        "enabled": not LIGHTWEIGHT_BL_MODE,
                    },
                    {
                        "label": "Cryostat Valve",
                        "read_pv": "PV:CRYO:VALVE:RBV",
                        "write_pv": "PV:CRYO:VALVE:SP",
                        "kind": "enum",
                        "units": "",
                        "readonly": False,
                        "enabled": not LIGHTWEIGHT_BL_MODE,
                    },
                    {
                        "label": "Furnace SP",
                        "read_pv": "PV:FURNACE:SP:RBV",
                        "write_pv": "PV:FURNACE:SP",
                        "kind": "numeric",
                        "units": "C",
                        "readonly": False,
                        "enabled": not LIGHTWEIGHT_BL_MODE,
                    },
                    {
                        "label": "DBHR M1",
                        "read_pv": "PV:DBHR:M1:RBV",
                        "write_pv": "PV:DBHR:M1:SP",
                        "stop_pv": "PV:DBHR:M1:STOP",
                        "stop_value": 1,
                        "stop_button_label": "STOP",
                        "kind": "numeric",
                        "units": "mm",
                        "readonly": False,
                        "enabled": not LIGHTWEIGHT_BL_MODE,
                    },
                    {
                        "label": "DBHR M2",
                        "read_pv": "PV:DBHR:M2:RBV",
                        "write_pv": "PV:DBHR:M2:SP",
                        "stop_pv": "PV:DBHR:M2:STOP",
                        "stop_value": 1,
                        "stop_button_label": "STOP",
                        "kind": "numeric",
                        "units": "mm",
                        "readonly": False,
                        "enabled": not LIGHTWEIGHT_BL_MODE,
                    },
                    {
                        "label": "DBHR Pitch",
                        "read_pv": "PV:DBHR:PITCH:RBV",
                        "write_pv": "PV:DBHR:PITCH:SP",
                        "stop_pv": "PV:DBHR:PITCH:STOP",
                        "stop_value": 1,
                        "stop_button_label": "STOP",
                        "kind": "numeric",
                        "units": "deg",
                        "readonly": False,
                        "enabled": not LIGHTWEIGHT_BL_MODE,
                    },
                ],
                "commands": [
                    {
                        "label": "Stop Furnace",
                        "read_pv": None,
                        "write_pv": "PV:FURNACE:STOP",
                        "kind": "command",
                        "command_value": 1,
                        "button_label": "Send",
                        "readonly": False,
                        "enabled": True,
                    },
                    {
                        "label": "Open FE Shutter",
                        "read_pv": None,
                        "write_pv": "PV:FE:SHUTTER:OPEN",
                        "kind": "command",
                        "command_value": 1,
                        "button_label": "Send",
                        "readonly": False,
                        "enabled": not LIGHTWEIGHT_BL_MODE,
                    },
                    {
                        "label": "Close FE Shutter",
                        "read_pv": None,
                        "write_pv": "PV:FE:SHUTTER:CLOSE",
                        "kind": "command",
                        "command_value": 1,
                        "button_label": "Send",
                        "readonly": False,
                        "enabled": not LIGHTWEIGHT_BL_MODE,
                    },
                    {
                        "label": "Open Cryo Holder",
                        "read_pv": None,
                        "write_pv": "PV:CRYO:HOLDER:OPEN",
                        "kind": "command",
                        "command_value": 1,
                        "button_label": "Send",
                        "readonly": False,
                        "enabled": not LIGHTWEIGHT_BL_MODE,
                    },
                ],
            }

            self.pv_definitions = {
                section: self._apply_pv_overrides(section, items)
                for section, items in self.pv_definitions.items()
            }
            self.cryostat_defaults = {
                "temperature_readback": {
                    "label": "Cryo Temp",
                    "read_pv": "TC1607-7-01:T1:READ_TEMP_SIG_TEMP",
                    "units": "K",
                },
                "temperature_setpoint": {
                    "label": "Temp Setpoint",
                    "read_pv": "TC1607-7-01:C1:READ_TEMP_LOOP_TSET",
                    "write_pv": "TC1607-7-01:C1:SET_TEMP_LOOP_TSET",
                    "units": "K",
                    "presets": [80, 120, 300],
                },
                "auto_heat": {
                    "label": "AutoHeat",
                    "read_pv": "TC1607-7-01:C1:READ_TEMP_LOOP_ENAB",
                    "write_pv": "TC1607-7-01:C1:SET_TEMP_LOOP_ENAB",
                    "off_label": "OFF",
                    "on_label": "ON",
                },
                "heater_power": {
                    "label": "Heater Power",
                    "read_pv": "TC1607-7-01:C1:READ_TEMP_LOOP_HSET",
                    "units": "%",
                    "min": 0.0,
                    "max": 100.0,
                },
                "vacuum": {
                    "label": "Cryo Insulation",
                    "read_pv": "FRG1607-7-I21-01:vac:p",
                    "units": "mbar",
                    "min": 1.0e-7,
                    "max": 1.0e-1,
                    "scale": "log_inverse",
                },
                "ln2_weight": {
                    "label": "LN2 Weight",
                    "read_pv": "BL1607-7-I21:Scale:701:fbk",
                    "units": "%",
                    "expression": "value / 24.0 * 100.0",
                    "min": 0.0,
                    "max": 100.0,
                },
                "gas_flow": {
                    "label": "Gas Flow",
                    "read_pv": "TC1607-7-01:C1:READ_TEMP_LOOP_FSET",
                    "write_pv": "TC1607-7-01:C1:SET_TEMP_LOOP_FSET",
                    "setpoint_read_pv": "TC1607-7-01:C1:SET_TEMP_LOOP_FSET",
                    "units": "%",
                    "min": 0.0,
                    "max": 100.0,
                },
            }

            self.createMainPanel()
            self.SetTitle(f"Beamline Control: {title}")

        def _load_epics(self):
            try:
                import epics

                return epics
            except Exception:
                return None

        @contextmanager
        def _suppress_console_output(self):
            handles = []
            null_fd = None
            try:
                null_fd = os.open(os.devnull, os.O_WRONLY)
                for fd in (1, 2):
                    try:
                        saved = os.dup(fd)
                        os.dup2(null_fd, fd)
                        handles.append((fd, saved))
                    except Exception:
                        pass
                yield
            finally:
                for fd, saved in reversed(handles):
                    try:
                        os.dup2(saved, fd)
                        os.close(saved)
                    except Exception:
                        pass
                if null_fd is not None:
                    try:
                        os.close(null_fd)
                    except Exception:
                        pass

        def _enabled_items(self, section_name):
            return [
                item for item in self.pv_definitions.get(section_name, [])
                if item.get("enabled", True)
            ]

        def _ion_chamber_config(self):
            section = self.pv_config.get("ion_chambers", {})
            return section if isinstance(section, dict) else {}

        def _apply_pv_overrides(self, section_name, items):
            section = self.pv_config.get(section_name, {})
            if not isinstance(section, dict):
                return items

            merged_items = []
            seen_labels = set()
            for item in items:
                seen_labels.add(item["label"])
                override = section.get(item["label"], {})
                if isinstance(override, dict):
                    merged = dict(item)
                    merged.update(override)
                    if "enabled" not in override:
                        merged["enabled"] = True
                    merged_items.append(merged)
                else:
                    merged_items.append(item)

            for label, override in section.items():
                if label in seen_labels or not isinstance(override, dict):
                    continue

                if section_name == "status":
                    item = {
                        "label": label,
                        "read_pv": override.get("read_pv"),
                        "write_pv": override.get("write_pv"),
                        "kind": override.get("kind", "status"),
                        "readonly": override.get("readonly", True),
                        "enabled": override.get("enabled", True),
                    }
                elif section_name == "controls":
                    item = {
                        "label": label,
                        "read_pv": override.get("read_pv"),
                        "write_pv": override.get("write_pv"),
                        "stop_pv": override.get("stop_pv"),
                        "stop_value": override.get("stop_value", 1),
                        "stop_button_label": override.get("stop_button_label", "STOP"),
                        "kind": override.get("kind", "numeric"),
                        "units": override.get("units", ""),
                        "readonly": override.get("readonly", not bool(override.get("write_pv"))),
                        "enabled": override.get("enabled", True),
                    }
                elif section_name == "commands":
                    item = {
                        "label": label,
                        "read_pv": override.get("read_pv"),
                        "write_pv": override.get("write_pv"),
                        "kind": override.get("kind", "command"),
                        "command_value": override.get("command_value", 1),
                        "button_label": override.get("button_label", "Send"),
                        "readonly": override.get("readonly", False),
                        "enabled": override.get("enabled", True),
                    }
                else:
                    item = {"label": label}

                item.update(override)
                merged_items.append(item)
            return merged_items

        def _build_ion_chamber_controls(self):
            items = []
            for label, cfg in self._ion_chamber_config().items():
                if not isinstance(cfg, dict):
                    continue
                items.append(
                    {
                        "label": label,
                        "read_pv": cfg.get("read_pv", ""),
                        "write_pv": None,
                        "unit_num_pv": cfg.get("unit_num_pv", cfg.get("unit_pv", "")),
                        "unit_pv": cfg.get("unit_text_pv", ""),
                        "derived": cfg.get("derived", {}),
                        "gain_up_pv": cfg.get("gain_up_pv", ""),
                        "gain_down_pv": cfg.get("gain_down_pv", ""),
                        "gain_step_value": cfg.get("gain_step_value", 1),
                        "photon_conversion": cfg.get("photon_conversion", {}),
                        "kind": "numeric",
                        "units": "",
                        "readonly": True,
                        "enabled": bool(cfg.get("read_pv")),
                        "track_history": True,
                    }
                )
            return items

        def _cryostat_config(self):
            merged = {}
            overrides = self.pv_config.get("cryostat", {})
            for key, default in self.cryostat_defaults.items():
                item = dict(default)
                override = overrides.get(key, {}) if isinstance(overrides, dict) else {}
                if isinstance(override, dict):
                    item.update(override)
                merged[key] = item
            return merged

        def _safe_eval_simple_expression(self, expression, value):
            def _eval(node):
                if isinstance(node, ast.Expression):
                    return _eval(node.body)
                if isinstance(node, ast.Constant):
                    if isinstance(node.value, (int, float, bool)):
                        return node.value
                    raise ValueError("Unsupported constant")
                if isinstance(node, ast.Name):
                    if isinstance(value, dict) and node.id in value:
                        return value[node.id]
                    if node.id == "value":
                        return value
                    raise ValueError("Unknown name")
                if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
                    operand = _eval(node.operand)
                    return +operand if isinstance(node.op, ast.UAdd) else -operand
                if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
                    left = _eval(node.left)
                    right = _eval(node.right)
                    if isinstance(node.op, ast.Add):
                        return left + right
                    if isinstance(node.op, ast.Sub):
                        return left - right
                    if isinstance(node.op, ast.Mult):
                        return left * right
                    return left / right
                raise ValueError("Unsupported expression")

            return _eval(ast.parse(expression, mode="eval"))

        def _cryostat_display_value(self, cfg, raw_value):
            if raw_value is None:
                return None
            expression = cfg.get("expression")
            if expression:
                try:
                    return float(self._safe_eval_simple_expression(expression, float(raw_value)))
                except Exception:
                    return None
            return raw_value

        def _cryostat_percent(self, value, cfg):
            if value is None:
                return 0
            try:
                minimum = float(cfg.get("min", 0.0))
                maximum = float(cfg.get("max", 100.0))
                numeric = float(value)
                scale = str(cfg.get("scale", "linear")).strip().lower()
                if scale == "log_inverse":
                    import math

                    min_log = math.log10(minimum)
                    max_log = math.log10(maximum)
                    val_log = math.log10(min(max(numeric, minimum), maximum))
                    fraction = (max_log - val_log) / (max_log - min_log)
                else:
                    fraction = 0.0 if maximum == minimum else (numeric - minimum) / (maximum - minimum)
                fraction = max(0.0, min(1.0, fraction))
                return int(round(fraction * 100))
            except Exception:
                return 0

        def _cryostat_status_text(self, value, cfg):
            if value is None:
                return "--"
            try:
                return cfg.get("on_label", "ON") if float(value) != 0 else cfg.get("off_label", "OFF")
            except Exception:
                text = self._normalize_status_text(value)
                return cfg.get("on_label", "ON") if text in {"1", "ON", "TRUE"} else cfg.get("off_label", "OFF")

        def _cryostat_status_color(self, value):
            try:
                return wx.Colour(60, 220, 60) if float(value) != 0 else wx.Colour(235, 60, 60)
            except Exception:
                return wx.Colour(180, 180, 180)

        def _set_cryostat_numeric(self, cfg_key, raw_value):
            cfg = self._cryostat_config().get(cfg_key, {})
            value = self._coerce_value(raw_value)
            if value in (None, ""):
                self.SetStatusText(f"No value entered for {cfg.get('label', cfg_key)}", 0)
                return
            if self.epics is None:
                self.SetStatusText(f"Placeholder set: {cfg.get('label', cfg_key)} = {value}", 0)
                return
            try:
                self._write_pv(cfg.get("write_pv"), value)
                self.SetStatusText(f"Set {cfg.get('label', cfg_key)} to {value}", 0)
            except Exception as exc:
                self.SetStatusText(f"Failed to set {cfg.get('label', cfg_key)}: {exc}", 0)

        def onSetCryostatTemp(self, event):
            widget = self.main_cryostat_widgets.get("temperature_setpoint_input")
            if widget is not None:
                self._set_cryostat_numeric("temperature_setpoint", widget.GetValue().strip())

        def onSetCryostatTempPreset(self, event, value):
            self._set_cryostat_numeric("temperature_setpoint", value)

        def onSetCryostatGasFlow(self, event):
            widget = self.main_cryostat_widgets.get("gas_flow_input")
            if widget is not None:
                self._set_cryostat_numeric("gas_flow", widget.GetValue().strip())

        def _iter_cryostat_widget_sets(self):
            widget_sets = []
            if self.main_cryostat_widgets:
                widget_sets.append(self.main_cryostat_widgets)
            if self.cryostat_popup_widgets:
                widget_sets.append(self.cryostat_popup_widgets)
            return widget_sets

        def onToggleCryostatAutoHeat(self, event):
            cfg = self._cryostat_config().get("auto_heat", {})
            current = self.cryostat_state.get("auto_heat")
            try:
                new_value = 0 if float(current) != 0 else 1
            except Exception:
                new_value = 1
            if self.epics is None:
                self.cryostat_state["auto_heat"] = new_value
                self._update_cryostat_panel()
                self.SetStatusText(f"Placeholder command sent: {cfg.get('label', 'AutoHeat')} -> {new_value}", 0)
                return
            try:
                self._write_pv(cfg.get("write_pv"), new_value)
                self.SetStatusText(f"Command sent: {cfg.get('label', 'AutoHeat')}", 0)
            except Exception as exc:
                self.SetStatusText(f"Failed command {cfg.get('label', 'AutoHeat')}: {exc}", 0)

        def _ion_chamber_labels(self):
            return [
                item["label"]
                for item in self._build_ion_chamber_controls()
                if item.get("enabled", True)
            ]

        def _format_ion_chamber_units(self, label):
            state = self.ion_chamber_state.get(label, {})
            unit_num = state.get("unit_num")
            unit_text = state.get("unit_text")
            parts = []
            if unit_num not in (None, ""):
                parts.append(str(unit_num))
            if unit_text not in (None, ""):
                parts.append(str(unit_text))
            return " ".join(parts).strip()

        def _ion_chamber_display_mode_label(self, label):
            mode = self.ion_chamber_state.get(label, {}).get("display_mode", "counts")
            return "ph/s" if mode == "photon_rate" else "cts"

        def _ion_chamber_photon_rate(self, label):
            row = self.pv_controls.get(label)
            if row is None:
                return None, ""
            cfg = row["config"]
            photon_cfg = cfg.get("photon_conversion", {})
            if not isinstance(photon_cfg, dict) or not photon_cfg:
                return None, ""

            state = self.ion_chamber_state.get(label, {})
            counts = state.get("value")
            delay_ms = state.get("delay")
            if counts is None or delay_ms in (None, 0, 0.0):
                return None, photon_cfg.get("units", "ph/s")

            try:
                scale = float(photon_cfg.get("scale", 1.0))
                rate = scale * float(counts) / (float(delay_ms) / 1000.0)
                return rate, photon_cfg.get("units", "ph/s")
            except Exception:
                return None, photon_cfg.get("units", "ph/s")

        def _ion_chamber_voltage(self, label):
            row = self.pv_controls.get(label)
            if row is None:
                return None, ""
            cfg = row["config"]
            derived_cfg = cfg.get("derived", {})
            if not isinstance(derived_cfg, dict) or not derived_cfg:
                return None, ""

            state = self.ion_chamber_state.get(label, {})
            counts = state.get("value")
            delay_ms = state.get("delay")
            if counts is None and cfg.get("read_pv"):
                counts = self._read_pv(cfg.get("read_pv"), cfg)
            if delay_ms in (None, 0, 0.0) and derived_cfg.get("delay_pv"):
                delay_ms = self._read_pv(derived_cfg.get("delay_pv"))
            if counts is None or delay_ms in (None, 0, 0.0):
                return None, derived_cfg.get("units", "")

            expression = derived_cfg.get("expression")
            if expression:
                names = {
                    "counts": float(counts),
                    "delay_ms": float(delay_ms),
                    "delay_s": float(delay_ms) / 1000.0,
                    "value": float(counts),
                }
                try:
                    voltage = float(self._safe_eval_simple_expression(expression, names))
                    return voltage, derived_cfg.get("units", "")
                except Exception:
                    return None, derived_cfg.get("units", "")

            try:
                voltage = float(derived_cfg.get("scale", 1.0)) * float(counts) / (float(delay_ms) / 1000.0)
                return voltage, derived_cfg.get("units", "")
            except Exception:
                return None, derived_cfg.get("units", "")

        def onToggleIonChamberDisplayMode(self, event, label):
            state = self.ion_chamber_state.setdefault(label, {})
            current = state.get("display_mode", "counts")
            state["display_mode"] = "photon_rate" if current == "counts" else "counts"
            row = self.pv_controls.get(label)
            if row is not None:
                toggle_button = row.get("mode_button")
                if toggle_button is not None:
                    toggle_button.SetLabel(self._ion_chamber_display_mode_label(label))
                    toggle_button.Refresh()
            self._update_ion_chamber_derived_display(label)

        def onIonChamberGainStep(self, event, label, direction):
            row = self.pv_controls.get(label)
            if row is None:
                return
            cfg = row["config"]
            pvname = cfg.get("gain_up_pv") if direction == "up" else cfg.get("gain_down_pv")
            command_value = cfg.get("gain_step_value", 1)
            if not pvname:
                self.SetStatusText(f"No gain {'up' if direction == 'up' else 'down'} PV configured for {label}", 0)
                return
            if self.epics is None:
                self.SetStatusText(f"Placeholder gain {'+' if direction == 'up' else '-'} for {label}", 0)
                return
            try:
                self._write_pv(pvname, command_value)
                self.SetStatusText(f"Sent gain {'+' if direction == 'up' else '-'} command for {label}", 0)
            except Exception as exc:
                self.SetStatusText(f"Failed gain change for {label}: {exc}", 0)

        def _read_pv_display_text(self, pvname):
            if not pvname or self.epics is None:
                return None
            try:
                pv = self._get_pv(pvname)
                if pv is None:
                    self.last_pv_error = f"Cannot create PV: {pvname}"
                    return None
                with self._suppress_console_output():
                    value = pv.get(timeout=1.0, use_monitor=False, as_string=True)
                if isinstance(value, bytes):
                    value = value.decode("utf-8", errors="ignore")
                return None if value is None else str(value).replace("\x00", "").strip()
            except Exception as exc:
                self.last_pv_error = f"Read failed for {pvname}: {exc}"
                return None

        def _callback_display_text(self, value, kws=None):
            kws = {} if kws is None else kws
            for key in ("char_value", "charvalue", "pv_value"):
                text = kws.get(key)
                if text not in (None, ""):
                    if isinstance(text, bytes):
                        text = text.decode("utf-8", errors="ignore")
                    return str(text).replace("\x00", "").strip()
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="ignore")
            return None if value is None else str(value).replace("\x00", "").strip()

        def SetTitle(self, title):
            self.window_title = title
            host = self.GetTopLevelParent()
            if hasattr(host, "update_detector_title"):
                host.update_detector_title(self, title)

        def SetStatusText(self, text, panel=0):
            host = self.GetTopLevelParent()
            if hasattr(host, "set_status_text"):
                host.set_status_text(text, panel=panel)

        def onDarkMode(self, enabled):
            self.colors = XRFDisplayColors_Dark() if enabled else XRFDisplayColors_Light()
            self.Refresh()

        def _read_pv(self, pvname, cfg=None):
            if not pvname or self.epics is None:
                return None
            try:
                pv = self._get_pv(pvname)
                if pv is None:
                    self.last_pv_error = f"Cannot create PV: {pvname}"
                    return None
                as_string = bool(cfg and cfg.get("kind") in ("status", "enum"))
                with self._suppress_console_output():
                    value = pv.get(timeout=1.0, use_monitor=False, as_string=as_string)
                if value is None:
                    self.last_pv_error = f"No value from {pvname}"
                return value
            except Exception as exc:
                self.last_pv_error = f"Read failed for {pvname}: {exc}"
                return None

        def _callback_value(self, cfg, value, kws):
            if cfg and cfg.get("kind") in ("status", "enum"):
                for key in ("char_value", "charvalue", "pv_value"):
                    text = kws.get(key)
                    if text not in (None, ""):
                        return text
            return value

        def _write_pv(self, pvname, value):
            if not pvname:
                raise ValueError("No write PV defined")
            if self.epics is None:
                raise RuntimeError("pyepics is not installed")
            pv = self._get_pv(pvname)
            if pv is None:
                raise RuntimeError(f"Cannot create PV: {pvname}")
            with self._suppress_console_output():
                return pv.put(value, wait=False)

        def _get_pv(self, pvname):
            if not pvname or self.epics is None:
                return None
            pv = self._pv_cache.get(pvname)
            if pv is None:
                try:
                    with self._suppress_console_output():
                        pv = self.epics.get_pv(pvname, auto_monitor=True, connect=True)
                except Exception:
                    pv = None
                self._pv_cache[pvname] = pv
            return pv

        def _coerce_value(self, raw):
            if raw is None:
                return None
            text = str(raw).strip()
            if text == "":
                return ""
            try:
                if "." in text or "e" in text.lower():
                    return float(text)
                return int(text)
            except Exception:
                return text

        def _format_value(self, value):
            if value is None:
                return "--"
            if isinstance(value, float):
                return f"{value:.4f}"
            return str(value)

        def _normalize_status_text(self, value):
            if value is None:
                return ""
            if isinstance(value, bytes):
                try:
                    value = value.decode("utf-8", errors="ignore")
                except Exception:
                    value = str(value)
            text = str(value).replace("\x00", "").strip().upper()
            if text.startswith("B'") and text.endswith("'"):
                text = text[2:-1].strip()
            return text

        def _status_terms(self, cfg, key):
            if cfg is None:
                return set()
            raw = cfg.get(key)
            if raw is None:
                return set()
            if isinstance(raw, (list, tuple, set)):
                values = raw
            else:
                values = [raw]
            return {
                self._normalize_status_text(value)
                for value in values
                if self._normalize_status_text(value)
            }

        def _safe_eval_status_expression(self, expression, names):
            def _eval(node):
                if isinstance(node, ast.Expression):
                    return _eval(node.body)
                if isinstance(node, ast.Constant):
                    if isinstance(node.value, (int, float, bool)):
                        return node.value
                    raise ValueError("Unsupported constant")
                if isinstance(node, ast.Name):
                    return names[node.id]
                if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
                    value = _eval(node.operand)
                    return +value if isinstance(node.op, ast.UAdd) else -value
                if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
                    left = _eval(node.left)
                    right = _eval(node.right)
                    if isinstance(node.op, ast.Add):
                        return left + right
                    if isinstance(node.op, ast.Sub):
                        return left - right
                    if isinstance(node.op, ast.Mult):
                        return left * right
                    return left / right
                if isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
                    left = _eval(node.left)
                    right = _eval(node.comparators[0])
                    op = node.ops[0]
                    if isinstance(op, ast.Gt):
                        return left > right
                    if isinstance(op, ast.GtE):
                        return left >= right
                    if isinstance(op, ast.Lt):
                        return left < right
                    if isinstance(op, ast.LtE):
                        return left <= right
                    if isinstance(op, ast.Eq):
                        return left == right
                    if isinstance(op, ast.NotEq):
                        return left != right
                    raise ValueError("Unsupported comparison")
                raise ValueError("Unsupported expression")

            tree = ast.parse(expression, mode="eval")
            return _eval(tree)

        def _status_state_key(self, value):
            candidates = []
            candidates.append(str(value))
            try:
                fval = float(value)
                if fval.is_integer():
                    candidates.append(str(int(fval)))
                candidates.append(str(fval))
            except Exception:
                pass
            normalized = self._normalize_status_text(value)
            if normalized:
                candidates.append(normalized)
            for candidate in candidates:
                if candidate not in ("", "None"):
                    return candidate
            return str(value)

        def _status_state_entry(self, value, cfg):
            if cfg is None:
                return None
            raw_map = cfg.get("state_map")
            if not isinstance(raw_map, dict):
                return None
            key = self._status_state_key(value)
            return raw_map.get(key) or raw_map.get(self._normalize_status_text(value))

        def _state_color(self, color_spec):
            if color_spec is None:
                return None
            text = str(color_spec).strip()
            upper = text.upper()
            if upper == "OK":
                return wx.Colour(60, 220, 60)
            if upper in ("MAJOR", "ALARM", "BAD"):
                return wx.Colour(235, 60, 60)
            if upper in ("MINOR", "WARN", "WARNING"):
                return wx.Colour(245, 210, 70)
            if upper == "ATTENTION":
                return wx.Colour(255, 170, 40)
            if upper == "ACTIVETEXT":
                return wx.Colour(35, 35, 35)
            if upper.startswith("RGB(") and upper.endswith(")"):
                try:
                    parts = [int(part.strip()) for part in text[4:-1].split(",")]
                    if len(parts) == 3:
                        return wx.Colour(*parts)
                except Exception:
                    return None
            return None

        def _evaluate_status_value(self, label, cfg, fallback_value=None):
            derived = {} if cfg is None else cfg.get("derived", {})
            if not isinstance(derived, dict) or not derived:
                return fallback_value

            state = self.status_state.setdefault(label, {})
            try:
                pvs = derived.get("pvs")
                expression = derived.get("expression")
                if isinstance(pvs, dict) and expression:
                    values = {}
                    for alias, pvname in pvs.items():
                        current = state.get(alias)
                        if current is None:
                            current = self._read_pv(pvname)
                            state[alias] = current
                        values[alias] = float(current)
                    return self._safe_eval_status_expression(expression, values)

                pv_1 = state.get("pv_1")
                pv_2 = state.get("pv_2")
                if pv_1 is None and derived.get("pv_1"):
                    pv_1 = self._read_pv(derived.get("pv_1"))
                    state["pv_1"] = pv_1
                if pv_2 is None and derived.get("pv_2"):
                    pv_2 = self._read_pv(derived.get("pv_2"))
                    state["pv_2"] = pv_2

                operation = str(derived.get("operation", "subtract")).strip().lower()
                if operation == "subtract":
                    return float(pv_1) - float(pv_2)
            except Exception:
                return None
            return None

        def _status_color(self, value, cfg=None):
            if value is None:
                return wx.Colour(180, 180, 180)

            state_entry = self._status_state_entry(value, cfg)
            if isinstance(state_entry, dict):
                state_color = self._state_color(state_entry.get("color"))
                if state_color is not None:
                    return state_color

            if cfg is not None and cfg.get("status_threshold") is not None:
                try:
                    threshold = float(cfg["status_threshold"])
                    number = float(value)
                    comparison = str(cfg.get("status_threshold_comparison", "gt")).strip().lower()
                    if comparison == "lt":
                        is_good = number < threshold
                    elif comparison == "le":
                        is_good = number <= threshold
                    elif comparison == "ge":
                        is_good = number >= threshold
                    else:
                        is_good = number > threshold
                    return wx.Colour(60, 220, 60) if is_good else wx.Colour(235, 60, 60)
                except Exception:
                    return wx.Colour(190, 190, 190)

            text = self._normalize_status_text(value)

            good_words = {"OPEN", "ON", "IN", "GOOD", "OK", "READY", "TRUE", "RUN", "RUNNING"}
            bad_words = {"CLOSED", "OFF", "OUT", "FAULT", "BAD", "ERROR", "FALSE", "TRIP", "STOPPED"}
            warn_words = {"MOVING", "BUSY", "UNKNOWN", "STANDBY", "WARN", "WARNING"}
            good_words |= self._status_terms(cfg, "status_good_label")
            good_words |= self._status_terms(cfg, "status_good_values")
            bad_words |= self._status_terms(cfg, "status_bad_label")
            bad_words |= self._status_terms(cfg, "status_bad_values")
            warn_words |= self._status_terms(cfg, "status_warn_values")

            if text in good_words:
                return wx.Colour(60, 220, 60)
            if text in bad_words:
                return wx.Colour(235, 60, 60)
            if text in warn_words:
                return wx.Colour(245, 210, 70)

            try:
                num = float(value)
                return wx.Colour(60, 220, 60) if num != 0 else wx.Colour(235, 60, 60)
            except Exception:
                return wx.Colour(190, 190, 190)

        def _status_label(self, value, cfg=None):
            if value is None:
                return "--"

            state_entry = self._status_state_entry(value, cfg)
            if isinstance(state_entry, dict) and state_entry.get("label") not in (None, ""):
                return str(state_entry.get("label"))

            if cfg is not None and cfg.get("status_threshold") is not None:
                try:
                    threshold = float(cfg["status_threshold"])
                    number = float(value)
                    comparison = str(cfg.get("status_threshold_comparison", "gt")).strip().lower()
                    if comparison == "lt":
                        is_good = number < threshold
                    elif comparison == "le":
                        is_good = number <= threshold
                    elif comparison == "ge":
                        is_good = number >= threshold
                    else:
                        is_good = number > threshold
                    return cfg.get("status_good_label", "OK") if is_good else cfg.get("status_bad_label", "LOW")
                except Exception:
                    return self._format_value(value)

            text = self._normalize_status_text(value)
            good_terms = self._status_terms(cfg, "status_good_label") | self._status_terms(cfg, "status_good_values")
            bad_terms = self._status_terms(cfg, "status_bad_label") | self._status_terms(cfg, "status_bad_values")
            warn_terms = self._status_terms(cfg, "status_warn_values")
            if text in good_terms and cfg is not None and cfg.get("status_good_label"):
                return str(cfg["status_good_label"])
            if text in bad_terms and cfg is not None and cfg.get("status_bad_label"):
                return str(cfg["status_bad_label"])
            if text in warn_terms and cfg is not None and cfg.get("status_warn_label"):
                return str(cfg["status_warn_label"])
            return text if text else self._format_value(value)

        def _make_indicator(self, parent, label_text):
            panel = wx.Panel(parent, size=(102, 70))
            sizer = wx.BoxSizer(wx.VERTICAL)

            label = wx.StaticText(panel, label=label_text, style=wx.ALIGN_CENTER_HORIZONTAL)
            label_font = label.GetFont()
            label_font.SetPointSize(9)
            label_font.SetWeight(wx.FONTWEIGHT_BOLD)
            label.SetFont(label_font)
            label.Wrap(94)

            lamp = wx.StaticText(panel, label="--", style=wx.ALIGN_CENTER_HORIZONTAL)
            lamp.SetMinSize((74, 24))
            font = lamp.GetFont()
            font.SetWeight(wx.FONTWEIGHT_BOLD)
            font.SetPointSize(9)
            lamp.SetFont(font)
            lamp.SetForegroundColour(wx.Colour(255, 255, 255))
            lamp.SetBackgroundColour(wx.Colour(180, 180, 180))

            sizer.Add(label, 0, wx.ALIGN_CENTER | wx.TOP | wx.BOTTOM, 3)
            sizer.Add(lamp, 0, wx.ALIGN_CENTER | wx.BOTTOM, 4)

            panel.SetSizer(sizer)
            return panel, lamp

        def createStatusPanel(self, parent=None):
            parent = parent or self
            box = wx.StaticBox(parent, label="Status")
            holder = wx.Panel(parent)
            sizer = wx.StaticBoxSizer(box, wx.VERTICAL)

            grid = wx.GridBagSizer(4, 4)
            items = self._enabled_items("status")

            cols = 2
            for idx, item in enumerate(items):
                row = idx // cols
                col = idx % cols

                widget_panel, lamp = self._make_indicator(holder, item["label"])
                grid.Add(widget_panel, (row, col), (1, 1), wx.ALL | wx.EXPAND, 3)

                self.status_lamps[item["label"]] = {
                    "config": item,
                    "lamp": lamp,
                }

            holder.SetSizer(grid)
            sizer.Add(holder, 1, wx.EXPAND | wx.ALL, 6)
            return sizer

        def createPVGridPanel(self, control_items=None, command_items=None, panel_name="pv_grid_panel", parent=None):
            parent = parent or self
            pane = wx.Panel(parent, name=panel_name)
            grid = wx.GridBagSizer(5, 5)
            ion_labels = set(self._ion_chamber_labels())
            if control_items is None:
                control_items = self._enabled_items("controls")
            if command_items is None:
                command_items = self._enabled_items("commands")

            headers = ["Label", "Readback", "Setpoint", "Units", "Action"]
            for col, text in enumerate(headers):
                lab = wx.StaticText(pane, label=text)
                font = lab.GetFont()
                font.MakeBold()
                lab.SetFont(font)
                grid.Add(lab, (0, col), (1, 1), wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)

            row_index = 1

            for item in control_items:
                if item["label"] in ion_labels:
                    continue
                label = wx.StaticText(pane, label=item["label"], size=(135, -1))

                readback = wx.StaticText(pane, label="--", size=(95, -1), style=wx.ALIGN_RIGHT)
                readback.SetBackgroundColour(wx.NullColour)

                units = wx.StaticText(pane, label=item.get("units", ""), size=(70, -1))
                action = wx.Panel(pane)
                action_sizer = wx.BoxSizer(wx.HORIZONTAL)
                action.SetSizer(action_sizer)

                if item.get("readonly", False) or not item.get("write_pv"):
                    setpoint = wx.StaticText(pane, label="", size=(95, -1))
                else:
                    setpoint = wx.TextCtrl(
                        pane, value="", size=(95, -1), style=wx.TE_PROCESS_ENTER
                    )
                    button = wx.Button(action, label="Set", size=(60, -1))
                    button.Bind(wx.EVT_BUTTON, lambda evt, key=item["label"]: self.onSetPV(evt, key))
                    setpoint.Bind(wx.EVT_TEXT_ENTER, lambda evt, key=item["label"]: self.onSetPV(evt, key))
                    action_sizer.Add(button, 0, wx.RIGHT, 4)

                if item.get("stop_pv"):
                    stop_button = wx.Button(
                        action,
                        label=item.get("stop_button_label", "STOP"),
                        size=(60, -1),
                    )
                    stop_button.Bind(wx.EVT_BUTTON, lambda evt, key=item["label"]: self.onStopPV(evt, key))
                    action_sizer.Add(stop_button, 0)

                if action_sizer.GetItemCount() == 0:
                    action.Hide()

                grid.Add(label, (row_index, 0), (1, 1), wx.ALL | wx.ALIGN_CENTER_VERTICAL, 3)
                grid.Add(readback, (row_index, 1), (1, 1), wx.ALL | wx.EXPAND, 3)
                grid.Add(setpoint, (row_index, 2), (1, 1), wx.ALL | wx.EXPAND, 3)
                grid.Add(units, (row_index, 3), (1, 1), wx.ALL | wx.ALIGN_CENTER_VERTICAL, 3)
                grid.Add(action, (row_index, 4), (1, 1), wx.ALL, 3)

                self.pv_controls[item["label"]] = {
                    "config": item,
                    "readback": readback,
                    "setpoint": setpoint,
                    "units": units,
                }
                row_index += 1

            for item in command_items:
                label = wx.StaticText(pane, label=item["label"], size=(135, -1))

                readback = wx.StaticText(pane, label="command", size=(95, -1), style=wx.ALIGN_RIGHT)
                readback.SetBackgroundColour(wx.NullColour)

                setpoint = wx.StaticText(pane, label=str(item.get("command_value", 1)), size=(95, -1))
                units = wx.StaticText(pane, label="", size=(70, -1))

                button = wx.Button(pane, label=item.get("button_label", "Send"), size=(60, -1))
                button.Bind(wx.EVT_BUTTON, lambda evt, key=item["label"]: self.onCommandPV(evt, key))

                grid.Add(label, (row_index, 0), (1, 1), wx.ALL | wx.ALIGN_CENTER_VERTICAL, 3)
                grid.Add(readback, (row_index, 1), (1, 1), wx.ALL | wx.EXPAND, 3)
                grid.Add(setpoint, (row_index, 2), (1, 1), wx.ALL | wx.EXPAND, 3)
                grid.Add(units, (row_index, 3), (1, 1), wx.ALL | wx.ALIGN_CENTER_VERTICAL, 3)
                grid.Add(button, (row_index, 4), (1, 1), wx.ALL, 3)

                self.pv_controls[item["label"]] = {
                    "config": item,
                    "readback": readback,
                    "setpoint": setpoint,
                }
                row_index += 1

            for c in (1, 2):
                grid.AddGrowableCol(c, 1)

            pane.SetSizer(grid)
            return pane

        def createButtonPanel(self):
            pane = wx.Panel(self)
            sizer = wx.BoxSizer(wx.HORIZONTAL)

            btn_refresh = wx.Button(pane, label="Refresh All", size=(110, -1))
            btn_show = wx.Button(pane, label="Show Enabled PVs", size=(130, -1))
            btn_clear = wx.Button(pane, label="Clear Values", size=(110, -1))
            btn_ic_trends = wx.Button(pane, label="Plot IC Trends", size=(120, -1))
            btn_cryostat = wx.Button(pane, label="Cryostat Window", size=(120, -1))

            btn_refresh.Bind(wx.EVT_BUTTON, self.onRefresh)
            btn_show.Bind(wx.EVT_BUTTON, self.onConfigure)
            btn_clear.Bind(wx.EVT_BUTTON, self.onClear)
            btn_ic_trends.Bind(wx.EVT_BUTTON, self.onShowIonChamberTrends)
            btn_cryostat.Bind(wx.EVT_BUTTON, self.onShowCryostatWindow)

            sizer.Add(btn_refresh, 0, wx.ALL, 5)
            sizer.Add(btn_show, 0, wx.ALL, 5)
            sizer.Add(btn_clear, 0, wx.ALL, 5)
            sizer.Add(btn_ic_trends, 0, wx.ALL, 5)
            sizer.Add(btn_cryostat, 0, wx.ALL, 5)

            if self.epics is None:
                warn = wx.StaticText(pane, label="pyepics not found: placeholder mode")
                warn.SetForegroundColour(wx.Colour(180, 40, 40))
                sizer.Add(warn, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 15)

            pane.SetSizer(sizer)
            return pane

        def createIonChamberPanel(self, parent=None):
            parent = parent or self
            box = wx.StaticBox(parent, label="Ion Chambers")
            pane = wx.Panel(parent)
            outer = wx.StaticBoxSizer(box, wx.VERTICAL)
            grid = wx.GridBagSizer(4, 4)

            labels = self._ion_chamber_labels()
            for row_index, label_text in enumerate(labels):
                cfg = next(
                    item for item in self._build_ion_chamber_controls()
                    if item["label"] == label_text
                )

                card = wx.Panel(pane)
                card.SetMinSize((250, 106))
                card_sizer = wx.BoxSizer(wx.HORIZONTAL)

                value_panel = wx.Panel(card)
                value_sizer = wx.BoxSizer(wx.VERTICAL)

                top_row = wx.BoxSizer(wx.HORIZONTAL)
                ic_label = wx.StaticText(value_panel, label=label_text)
                ic_font = ic_label.GetFont()
                ic_font.SetPointSize(14)
                ic_font.SetWeight(wx.FONTWEIGHT_BOLD)
                ic_label.SetFont(ic_font)

                unit_text = wx.StaticText(value_panel, label="")
                unit_font = unit_text.GetFont()
                unit_font.SetPointSize(9)
                unit_text.SetFont(unit_font)

                top_row.Add(ic_label, 0, wx.ALIGN_CENTER_VERTICAL)
                top_row.AddStretchSpacer(1)
                top_row.Add(unit_text, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 8)

                counts_label = wx.StaticText(value_panel, label="Counts")
                counts_font = counts_label.GetFont()
                counts_font.SetPointSize(8)
                counts_font.SetWeight(wx.FONTWEIGHT_BOLD)
                counts_label.SetFont(counts_font)

                readback = wx.StaticText(value_panel, label="--", style=wx.ALIGN_RIGHT)
                readback.SetForegroundColour(wx.Colour(180, 30, 30))
                value_font = readback.GetFont()
                value_font.SetFamily(wx.FONTFAMILY_TELETYPE)
                value_font.SetPointSize(16)
                value_font.SetWeight(wx.FONTWEIGHT_BOLD)
                readback.SetFont(value_font)

                voltage_label = wx.StaticText(value_panel, label="Voltage")
                voltage_font = voltage_label.GetFont()
                voltage_font.SetPointSize(8)
                voltage_font.SetWeight(wx.FONTWEIGHT_BOLD)
                voltage_label.SetFont(voltage_font)

                derived_readback = wx.StaticText(
                    value_panel,
                    label="--",
                    size=(150, -1),
                    style=wx.ALIGN_RIGHT,
                )
                derived_readback.SetForegroundColour(wx.Colour(70, 70, 70))
                derived_readback.SetMinSize((150, 24))
                derived_font = derived_readback.GetFont()
                derived_font.SetFamily(wx.FONTFAMILY_TELETYPE)
                derived_font.SetPointSize(16)
                derived_font.SetWeight(wx.FONTWEIGHT_BOLD)
                derived_readback.SetFont(derived_font)

                value_sizer.Add(top_row, 0, wx.EXPAND | wx.BOTTOM, 5)
                value_sizer.Add(counts_label, 0, wx.EXPAND)
                value_sizer.Add(readback, 0, wx.EXPAND | wx.BOTTOM, 4)
                value_sizer.Add(voltage_label, 0, wx.EXPAND)
                value_sizer.Add(derived_readback, 0, wx.EXPAND)
                value_panel.SetSizer(value_sizer)

                actions = wx.Panel(card)
                actions_sizer = wx.BoxSizer(wx.VERTICAL)
                mode_button = wx.Button(
                    actions,
                    label=self._ion_chamber_display_mode_label(label_text),
                    size=(56, 26),
                )
                mode_button.Bind(
                    wx.EVT_BUTTON,
                    lambda evt, lbl=label_text: self.onToggleIonChamberDisplayMode(evt, lbl),
                )
                gain_row = wx.BoxSizer(wx.HORIZONTAL)
                btn_gain_down = wx.Button(actions, label="-", size=(26, 26))
                btn_gain_down.Bind(
                    wx.EVT_BUTTON,
                    lambda evt, lbl=label_text: self.onIonChamberGainStep(evt, lbl, "down"),
                )
                btn_gain_up = wx.Button(actions, label="+", size=(26, 26))
                btn_gain_up.Bind(
                    wx.EVT_BUTTON,
                    lambda evt, lbl=label_text: self.onIonChamberGainStep(evt, lbl, "up"),
                )
                gain_row.Add(btn_gain_down, 0, wx.RIGHT, 4)
                gain_row.Add(btn_gain_up, 0)
                btn_trend = wx.Button(actions, label="Trend", size=(56, 26))
                btn_trend.Bind(wx.EVT_BUTTON, self.onShowIonChamberTrends)
                actions_sizer.AddStretchSpacer(1)
                actions_sizer.Add(mode_button, 0, wx.ALL, 4)
                actions_sizer.Add(gain_row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 4)
                actions_sizer.Add(btn_trend, 0, wx.ALL, 4)
                actions_sizer.AddStretchSpacer(1)
                actions.SetSizer(actions_sizer)

                card_sizer.Add(value_panel, 1, wx.EXPAND | wx.ALL, 8)
                card_sizer.Add(actions, 0, wx.EXPAND | wx.TOP | wx.RIGHT | wx.BOTTOM, 8)
                card.SetSizer(card_sizer)

                grid.Add(card, (row_index, 0), (1, 1), wx.EXPAND | wx.ALL, 2)
                grid.AddGrowableRow(row_index, 1)

                self.pv_controls[label_text] = {
                    "config": cfg,
                    "readback": readback,
                    "derived_readback": derived_readback,
                    "setpoint": wx.StaticText(card, label=""),
                    "units": unit_text,
                    "mode_button": mode_button,
                }

            grid.AddGrowableCol(0, 1)
            pane.SetSizer(grid)
            outer.Add(pane, 1, wx.EXPAND | wx.ALL, 4)
            return outer

        def createCryostatPanel(self, parent=None, widgets=None):
            cfg = self._cryostat_config()
            parent = self if parent is None else parent
            widgets = self.main_cryostat_widgets if widgets is None else widgets
            box = wx.StaticBox(parent, label="Cryostat Controls")
            pane = wx.Panel(parent)
            outer = wx.StaticBoxSizer(box, wx.VERTICAL)
            grid = wx.GridBagSizer(6, 6)

            title = wx.StaticText(pane, label="Automatic Needle Valve")
            title_font = title.GetFont()
            title_font.SetWeight(wx.FONTWEIGHT_BOLD)
            title.SetFont(title_font)
            grid.Add(title, (0, 0), (1, 6), wx.ALL | wx.ALIGN_CENTER_VERTICAL, 3)

            grid.Add(wx.StaticText(pane, label="SP:"), (1, 0), (1, 1), wx.ALL | wx.ALIGN_CENTER_VERTICAL, 3)
            temp_input = wx.TextCtrl(pane, value="", size=(60, -1), style=wx.TE_PROCESS_ENTER)
            temp_input.Bind(wx.EVT_TEXT_ENTER, lambda evt, w=widgets: self._set_cryostat_numeric(
                "temperature_setpoint", w["temperature_setpoint_input"].GetValue().strip()
            ))
            grid.Add(temp_input, (1, 1), (1, 1), wx.ALL, 3)
            widgets["temperature_setpoint_input"] = temp_input

            temp_set_button = wx.Button(pane, label="Set", size=(52, -1))
            temp_set_button.Bind(wx.EVT_BUTTON, lambda evt, w=widgets: self._set_cryostat_numeric(
                "temperature_setpoint", w["temperature_setpoint_input"].GetValue().strip()
            ))
            grid.Add(temp_set_button, (1, 2), (1, 1), wx.ALL, 3)

            preset_panel = wx.Panel(pane)
            preset_sizer = wx.BoxSizer(wx.HORIZONTAL)
            preset_panel.SetSizer(preset_sizer)
            for preset in cfg["temperature_setpoint"].get("presets", []):
                button = wx.Button(preset_panel, label=str(preset), size=(52, -1))
                button.Bind(wx.EVT_BUTTON, lambda evt, value=preset: self.onSetCryostatTempPreset(evt, value))
                preset_sizer.Add(button, 0, wx.RIGHT, 4)
            grid.Add(preset_panel, (1, 3), (1, 3), wx.ALL, 3)

            grid.Add(wx.StaticText(pane, label="Tset"), (1, 6), (1, 1), wx.ALL | wx.ALIGN_CENTER_VERTICAL, 3)
            temp_set_readback = wx.StaticText(pane, label="--", size=(72, -1), style=wx.ALIGN_RIGHT)
            grid.Add(temp_set_readback, (1, 7), (1, 1), wx.ALL | wx.EXPAND, 3)
            widgets["temperature_setpoint_readback"] = temp_set_readback

            grid.Add(wx.StaticText(pane, label="Tread"), (1, 8), (1, 1), wx.ALL | wx.ALIGN_CENTER_VERTICAL, 3)
            temp_readback = wx.StaticText(pane, label="--", size=(72, -1), style=wx.ALIGN_RIGHT)
            grid.Add(temp_readback, (1, 9), (1, 1), wx.ALL | wx.EXPAND, 3)
            widgets["temperature_readback"] = temp_readback

            grid.Add(wx.StaticText(pane, label="AutoHeat"), (1, 10), (1, 1), wx.ALL | wx.ALIGN_CENTER_VERTICAL, 3)
            auto_button = wx.ToggleButton(pane, label="OFF", size=(80, -1))
            auto_button.Bind(wx.EVT_TOGGLEBUTTON, self.onToggleCryostatAutoHeat)
            grid.Add(auto_button, (1, 11), (1, 1), wx.ALL, 3)
            widgets["auto_heat_button"] = auto_button

            def add_progress_row(row_index, label_text, gauge_key, value_key):
                label = wx.StaticText(pane, label=label_text)
                gauge = wx.Gauge(pane, range=100, size=(280, 24))
                value_text = wx.StaticText(pane, label="--", size=(130, -1), style=wx.ALIGN_RIGHT)
                grid.Add(label, (row_index, 0), (1, 2), wx.ALL | wx.ALIGN_CENTER_VERTICAL, 3)
                grid.Add(gauge, (row_index, 2), (1, 8), wx.ALL | wx.EXPAND, 3)
                grid.Add(value_text, (row_index, 10), (1, 2), wx.ALL | wx.ALIGN_CENTER_VERTICAL, 3)
                widgets[gauge_key] = gauge
                widgets[value_key] = value_text

            add_progress_row(2, "Heater Power %", "heater_power_gauge", "heater_power_value")
            add_progress_row(3, "Cryo Insulation", "vacuum_gauge", "vacuum_value")
            add_progress_row(4, "LN2 Weight", "ln2_weight_gauge", "ln2_weight_value")
            add_progress_row(5, "% Gas Flow", "gas_flow_gauge", "gas_flow_value")

            grid.Add(wx.StaticText(pane, label="SP:"), (6, 0), (1, 1), wx.ALL | wx.ALIGN_CENTER_VERTICAL, 3)
            gas_input = wx.TextCtrl(pane, value="", size=(60, -1), style=wx.TE_PROCESS_ENTER)
            gas_input.Bind(wx.EVT_TEXT_ENTER, lambda evt, w=widgets: self._set_cryostat_numeric(
                "gas_flow", w["gas_flow_input"].GetValue().strip()
            ))
            grid.Add(gas_input, (6, 1), (1, 1), wx.ALL, 3)
            widgets["gas_flow_input"] = gas_input

            gas_button = wx.Button(pane, label="Set", size=(52, -1))
            gas_button.Bind(wx.EVT_BUTTON, lambda evt, w=widgets: self._set_cryostat_numeric(
                "gas_flow", w["gas_flow_input"].GetValue().strip()
            ))
            grid.Add(gas_button, (6, 2), (1, 1), wx.ALL, 3)

            grid.Add(wx.StaticText(pane, label="%set"), (6, 3), (1, 1), wx.ALL | wx.ALIGN_CENTER_VERTICAL, 3)
            gas_set_readback = wx.StaticText(pane, label="--", size=(72, -1), style=wx.ALIGN_RIGHT)
            grid.Add(gas_set_readback, (6, 4), (1, 1), wx.ALL | wx.EXPAND, 3)
            widgets["gas_flow_set_readback"] = gas_set_readback

            grid.Add(wx.StaticText(pane, label="%read"), (6, 5), (1, 1), wx.ALL | wx.ALIGN_CENTER_VERTICAL, 3)
            gas_readback = wx.StaticText(pane, label="--", size=(72, -1), style=wx.ALIGN_RIGHT)
            grid.Add(gas_readback, (6, 6), (1, 1), wx.ALL | wx.EXPAND, 3)
            widgets["gas_flow_readback"] = gas_readback

            for col in (2, 3, 4, 5, 6, 7, 8, 9):
                grid.AddGrowableCol(col, 1)

            pane.SetSizer(grid)
            outer.Add(pane, 1, wx.EXPAND | wx.ALL, 6)
            return outer

        def createMainPanel(self):
            outer = wx.BoxSizer(wx.VERTICAL)

            header_sizer = wx.BoxSizer(wx.VERTICAL)
            title_text = wx.StaticText(self, label="Beamline Controls")
            font = title_text.GetFont()
            font.SetPointSize(16)
            font.SetWeight(wx.FONTWEIGHT_BOLD)
            title_text.SetFont(font)

            subtitle = wx.StaticText(
                self,
                label="Live EPICS beamline state, motion feedback, and diagnostic environment monitoring.",
            )
            header_sizer.Add(title_text, 0, wx.EXPAND | wx.BOTTOM, 4)
            header_sizer.Add(subtitle, 0, wx.EXPAND | wx.BOTTOM, 8)

            outer.Add(header_sizer, 0, wx.EXPAND | wx.ALL, 12)
            outer.Add(wx.StaticLine(self, style=wx.LI_HORIZONTAL), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)

            scrolled = wx.ScrolledWindow(self, style=wx.VSCROLL | wx.HSCROLL)
            scrolled.SetScrollRate(20, 20)

            scroll_sizer = wx.BoxSizer(wx.VERTICAL)
            content = wx.BoxSizer(wx.HORIZONTAL)

            motion_labels = {
                "Mono Energy", "Dwell Time", "Stage Z", "Stage Y",
                "M1 Pitch", "JJ Vert Gap", "JJ Vert Center",
                "JJ Hor Gap", "JJ Hor Center",
            }
            dbhr_labels = {"DBHR M1", "DBHR M2", "DBHR Pitch"}
            enabled_controls = self._enabled_items("controls")
            motion_controls = [item for item in enabled_controls if item["label"] in motion_labels]
            dbhr_controls = [item for item in enabled_controls if item["label"] in dbhr_labels]
            remaining_controls = [
                item for item in enabled_controls
                if item["label"] not in motion_labels and item["label"] not in dbhr_labels
            ]

            left_col = wx.BoxSizer(wx.VERTICAL)
            left_col.Add(self.createStatusPanel(parent=scrolled), 0, wx.EXPAND | wx.BOTTOM, 12)

            dbhr_box = wx.StaticBoxSizer(wx.StaticBox(scrolled, label="DBHR Controls"), wx.VERTICAL)
            dbhr_box.Add(
                self.createPVGridPanel(
                    control_items=dbhr_controls,
                    command_items=[],
                    panel_name="pv_grid_dbhr_panel",
                    parent=scrolled
                ),
                1, wx.EXPAND | wx.ALL, 6,
            )
            left_col.Add(dbhr_box, 0, wx.EXPAND | wx.BOTTOM, 12)

            mid_col = wx.BoxSizer(wx.VERTICAL)
            motion_box = wx.StaticBoxSizer(wx.StaticBox(scrolled, label="Beamline Motion Controls"), wx.VERTICAL)
            motion_box.Add(
                self.createPVGridPanel(
                    control_items=motion_controls,
                    command_items=[],
                    panel_name="pv_grid_motion_panel",
                    parent=scrolled
                ),
                1, wx.EXPAND | wx.ALL, 6,
            )
            mid_col.Add(motion_box, 0, wx.EXPAND | wx.BOTTOM, 12)

            controls_box = wx.StaticBoxSizer(wx.StaticBox(scrolled, label="Other Beamline Controls"), wx.VERTICAL)
            controls_box.Add(
                self.createPVGridPanel(
                    control_items=remaining_controls,
                    command_items=self._enabled_items("commands"),
                    panel_name="pv_grid_other_panel",
                    parent=scrolled
                ),
                1, wx.EXPAND | wx.ALL, 6,
            )
            mid_col.Add(controls_box, 0, wx.EXPAND | wx.BOTTOM, 12)

            right_col = wx.BoxSizer(wx.VERTICAL)
            if self._ion_chamber_labels():
                right_col.Add(self.createIonChamberPanel(parent=scrolled), 0, wx.EXPAND | wx.BOTTOM, 12)

            right_col.Add(self.createCryostatPanel(parent=scrolled, widgets=self.main_cryostat_widgets), 0, wx.EXPAND | wx.BOTTOM, 12)

            content.Add(left_col, 0, wx.EXPAND | wx.RIGHT, 15)
            content.Add(mid_col, 0, wx.EXPAND | wx.RIGHT, 15)
            content.Add(right_col, 1, wx.EXPAND, 0)

            scroll_sizer.Add(content, 1, wx.EXPAND | wx.ALL, 15)
            scrolled.SetSizer(scroll_sizer)

            outer.Add(scrolled, 1, wx.EXPAND)
            outer.Add(wx.StaticLine(self, style=wx.LI_HORIZONTAL), 0, wx.EXPAND | wx.ALL, 12)
            outer.Add(self.createButtonPanel(), 0, wx.ALIGN_RIGHT | wx.RIGHT | wx.BOTTOM, 12)

            self.SetSizer(outer)
            wx.CallAfter(self.onRefresh, None)
            wx.CallAfter(self._start_pv_monitors)

        def _start_pv_monitors(self):
            if self.epics is None:
                return

            self._clear_pv_monitors()

            for label, row in self.status_lamps.items():
                cfg = row["config"]
                pvname = cfg.get("read_pv")
                pv = self._get_pv(pvname)
                if pv is not None:
                    cb_index = pv.add_callback(
                        lambda pvname=None, value=None, lbl=label, **kws: wx.CallAfter(
                            self._apply_status_update, lbl, value, kws
                        )
                    )
                    self._pv_callbacks.append((pv, cb_index))

                derived = cfg.get("derived", {})
                if isinstance(derived, dict):
                    for source_key in ("pv_1", "pv_2"):
                        source_pvname = derived.get(source_key)
                        source_pv = self._get_pv(source_pvname)
                        if source_pv is None:
                            continue
                        cb_index = source_pv.add_callback(
                            lambda pvname=None, value=None, lbl=label, src=source_key, **kws: wx.CallAfter(
                                self._apply_status_source_update, lbl, src, value, kws
                            )
                        )
                        self._pv_callbacks.append((source_pv, cb_index))
                    source_map = derived.get("pvs")
                    if isinstance(source_map, dict):
                        for source_key, source_pvname in source_map.items():
                            source_pv = self._get_pv(source_pvname)
                            if source_pv is None:
                                continue
                            cb_index = source_pv.add_callback(
                                lambda pvname=None, value=None, lbl=label, src=source_key, **kws: wx.CallAfter(
                                    self._apply_status_source_update, lbl, src, value, kws
                                )
                            )
                            self._pv_callbacks.append((source_pv, cb_index))

            for label, row in self.pv_controls.items():
                cfg = row["config"]
                if cfg.get("kind") == "command":
                    continue
                pvname = cfg.get("read_pv")
                pv = self._get_pv(pvname)
                if pv is None:
                    continue
                cb_index = pv.add_callback(
                    lambda pvname=None, value=None, lbl=label, **kws: wx.CallAfter(
                        self._apply_control_update, lbl, value, kws
                    )
                )
                self._pv_callbacks.append((pv, cb_index))

                unit_num_pvname = cfg.get("unit_num_pv")
                unit_num_pv = self._get_pv(unit_num_pvname)
                if unit_num_pv is not None:
                    cb_index = unit_num_pv.add_callback(
                        lambda pvname=None, value=None, lbl=label, **kws: wx.CallAfter(
                            self._apply_units_update, lbl, value, "unit_num", kws
                        )
                    )
                    self._pv_callbacks.append((unit_num_pv, cb_index))

                unit_pvname = cfg.get("unit_pv")
                unit_pv = self._get_pv(unit_pvname)
                if unit_pv is not None:
                    cb_index = unit_pv.add_callback(
                        lambda pvname=None, value=None, lbl=label, **kws: wx.CallAfter(
                            self._apply_units_update, lbl, value, "unit_text", kws
                        )
                    )
                    self._pv_callbacks.append((unit_pv, cb_index))

                delay_pvname = cfg.get("derived", {}).get("delay_pv")
                delay_pv = self._get_pv(delay_pvname)
                if delay_pv is not None:
                    cb_index = delay_pv.add_callback(
                        lambda pvname=None, value=None, lbl=label, **kws: wx.CallAfter(
                            self._apply_ion_chamber_delay_update, lbl, value
                        )
                    )
                    self._pv_callbacks.append((delay_pv, cb_index))

            cryostat_pvs = set()
            for cfg in self._cryostat_config().values():
                if not isinstance(cfg, dict):
                    continue
                for key in ("read_pv", "setpoint_read_pv"):
                    pvname = cfg.get(key)
                    if pvname:
                        cryostat_pvs.add(pvname)

            for pvname in cryostat_pvs:
                pv = self._get_pv(pvname)
                if pv is None:
                    continue
                cb_index = pv.add_callback(
                    lambda pvname=None, value=None, **kws: wx.CallAfter(self._update_cryostat_panel)
                )
                self._pv_callbacks.append((pv, cb_index))

        def _clear_pv_monitors(self):
            for pv, cb_index in self._pv_callbacks:
                try:
                    pv.remove_callback(cb_index)
                except Exception:
                    pass
            self._pv_callbacks.clear()

        def _apply_status_update(self, label, value, kws=None):
            row = self.status_lamps.get(label)
            if row is None:
                return
            cfg = row["config"]
            kws = {} if kws is None else kws
            shown_value = self._callback_value(cfg, value, kws)
            shown_value = self._evaluate_status_value(label, cfg, shown_value)
            lamp = row["lamp"]
            lamp.SetLabel(self._status_label(shown_value, cfg)[:10])
            lamp.SetBackgroundColour(self._status_color(shown_value, cfg))
            lamp.Refresh()

        def _apply_status_source_update(self, label, source_key, value, kws=None):
            self.status_state.setdefault(label, {})[source_key] = value
            self._apply_status_update(label, None, kws)

        def _apply_control_update(self, label, value, kws=None):
            row = self.pv_controls.get(label)
            if row is None:
                return

            cfg = row["config"]
            kws = {} if kws is None else kws
            shown_value = self._callback_value(cfg, value, kws)
            if label in self.ion_chamber_state:
                self.ion_chamber_state[label]["value"] = shown_value
            self._record_ion_chamber_history(label, shown_value, cfg)
            if shown_value is None and cfg.get("read_pv"):
                row["readback"].SetLabel("N/C")
                row["readback"].SetBackgroundColour(wx.Colour(255, 225, 225))
            else:
                row["readback"].SetLabel(self._format_value(shown_value))
                if cfg.get("kind") in ("status", "enum"):
                    row["readback"].SetBackgroundColour(self._status_color(shown_value, cfg))
                else:
                    row["readback"].SetBackgroundColour(wx.Colour(235, 235, 235))

            row["readback"].Refresh()
            self._update_ion_chamber_derived_display(label)
            if cfg.get("track_history"):
                self._refresh_ion_chamber_trend_plot()

        def _apply_units_update(self, label, value, field="unit_text", kws=None):
            shown_value = self._callback_display_text(value, kws)
            if label in self.ion_chamber_state:
                self.ion_chamber_state[label][field] = shown_value
            row = self.pv_controls.get(label)
            if row is None:
                return
            units_widget = row.get("units")
            if units_widget is None:
                return
            units_widget.SetLabel(self._format_ion_chamber_units(label))
            units_widget.Refresh()

        def _apply_ion_chamber_delay_update(self, label, value):
            if label in self.ion_chamber_state:
                self.ion_chamber_state[label]["delay"] = value
            self._update_ion_chamber_derived_display(label)

        def _update_ion_chamber_derived_display(self, label):
            row = self.pv_controls.get(label)
            if row is None:
                return
            derived_widget = row.get("derived_readback")
            if derived_widget is None:
                return

            cfg = row["config"]
            derived_cfg = cfg.get("derived", {})
            state = self.ion_chamber_state.get(label, {})
            counts = state.get("value")

            display_mode = state.get("display_mode", "counts")
            if display_mode == "photon_rate":
                photon_rate, photon_units = self._ion_chamber_photon_rate(label)
                if photon_rate is None:
                    row["readback"].SetLabel("N/C" if counts is None else "--")
                else:
                    row["readback"].SetLabel(f"{self._format_value(photon_rate)} {photon_units}".strip())
            else:
                row["readback"].SetLabel("N/C" if counts is None and cfg.get("read_pv") else self._format_value(counts))

            if not derived_cfg:
                derived_widget.SetLabel("--")
                derived_widget.Refresh()
                row["readback"].Refresh()
                return

            derived_value, units = self._ion_chamber_voltage(label)
            if derived_value is None:
                derived_widget.SetLabel("--")
            else:
                derived_widget.SetLabel(f"{self._format_value(derived_value)} {units}".strip())
            derived_widget.Refresh()
            row["readback"].Refresh()
            parent = derived_widget.GetParent()
            if parent is not None:
                parent.Layout()

        def _record_ion_chamber_history(self, label, value, cfg):
            if not cfg.get("track_history"):
                return
            history = self.ion_chamber_history.get(label)
            if history is None or value is None:
                return
            try:
                numeric = float(value)
            except Exception:
                return
            history["times"].append(time.time())
            history["values"].append(numeric)

        def onShowIonChamberTrends(self, event):
            if not self.ion_chamber_history:
                wx.MessageBox(
                    f"Add ion chamber PV names in {PV_CONFIG_FILE.name} first.",
                    "Ion Chamber Trends",
                    wx.OK | wx.ICON_INFORMATION,
                )
                return

            if self.ion_chamber_trend_frame is not None:
                try:
                    self.ion_chamber_trend_frame.Raise()
                    self._refresh_ion_chamber_trend_plot()
                    return
                except Exception:
                    self.ion_chamber_trend_frame = None

            try:
                from matplotlib.backends.backend_wxagg import FigureCanvasWxAgg as FigureCanvas
                from matplotlib.figure import Figure
            except Exception as exc:
                wx.MessageBox(
                    f"Matplotlib is not available: {exc}",
                    "Ion Chamber Trends",
                    wx.OK | wx.ICON_ERROR,
                )
                return

            frame = wx.Frame(self.GetTopLevelParent(), title="Ion Chamber Trends", size=(900, 500))
            panel = wx.Panel(frame)
            sizer = wx.BoxSizer(wx.VERTICAL)

            checks_panel = wx.Panel(panel)
            checks_sizer = wx.BoxSizer(wx.HORIZONTAL)
            self.ion_chamber_trend_checks = {}
            for label in self.ion_chamber_history.keys():
                check = wx.CheckBox(checks_panel, label=label)
                check.SetValue(self.ion_chamber_trend_selection.get(label, True))
                check.Bind(wx.EVT_CHECKBOX, lambda evt, lbl=label: self.onToggleIonChamberTrend(evt, lbl))
                checks_sizer.Add(check, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
                self.ion_chamber_trend_checks[label] = check
            checks_panel.SetSizer(checks_sizer)

            figure = Figure(figsize=(8, 4))
            axes = figure.add_subplot(111)
            canvas = FigureCanvas(panel, -1, figure)

            sizer.Add(checks_panel, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)
            sizer.Add(canvas, 1, wx.EXPAND | wx.ALL, 8)
            panel.SetSizer(sizer)

            self.ion_chamber_trend_frame = frame
            self.ion_chamber_trend_canvas = canvas
            self.ion_chamber_trend_axes = axes

            frame.Bind(wx.EVT_CLOSE, self._on_close_ion_chamber_trends)
            self._refresh_ion_chamber_trend_plot()
            frame.Show()

        def onShowCryostatWindow(self, event):
            if self.cryostat_frame is not None:
                try:
                    self.cryostat_frame.Raise()
                    self._update_cryostat_panel()
                    return
                except Exception:
                    self.cryostat_frame = None
                    self.cryostat_popup_widgets = None

            frame = wx.Frame(self.GetTopLevelParent(), title="Cryostat Controls", size=(980, 720))
            panel = wx.Panel(frame)
            sizer = wx.BoxSizer(wx.VERTICAL)

            self.cryostat_popup_widgets = {}
            sizer.Add(
                self.createCryostatPanel(parent=panel, widgets=self.cryostat_popup_widgets),
                1,
                wx.EXPAND | wx.ALL,
                8,
            )

            try:
                from matplotlib.backends.backend_wxagg import FigureCanvasWxAgg as FigureCanvas
                from matplotlib.figure import Figure

                figure = Figure(figsize=(8, 3))
                axes = figure.add_subplot(111)
                pressure_axes = axes.twinx()
                canvas = FigureCanvas(panel, -1, figure)
                sizer.Add(canvas, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
                self.cryostat_trend_canvas = canvas
                self.cryostat_trend_axes = axes
                self.cryostat_trend_pressure_axes = pressure_axes
            except Exception:
                self.cryostat_trend_canvas = None
                self.cryostat_trend_axes = None
                self.cryostat_trend_pressure_axes = None

            panel.SetSizer(sizer)

            self.cryostat_frame = frame
            frame.Bind(wx.EVT_CLOSE, self._on_close_cryostat_window)
            self._update_cryostat_panel()
            frame.Show()

        def onToggleIonChamberTrend(self, event, label):
            self.ion_chamber_trend_selection[label] = event.IsChecked()
            self._refresh_ion_chamber_trend_plot()

        def _refresh_ion_chamber_trend_plot(self):
            if self.ion_chamber_trend_canvas is None or self.ion_chamber_trend_axes is None:
                return

            axes = self.ion_chamber_trend_axes
            axes.clear()

            latest_time = None
            for history in self.ion_chamber_history.values():
                if history["times"]:
                    latest_time = history["times"][-1]
            if latest_time is None:
                self.ion_chamber_trend_canvas.draw()
                return

            plotted = False
            for label, history in self.ion_chamber_history.items():
                if not self.ion_chamber_trend_selection.get(label, True):
                    continue
                if not history["times"]:
                    continue
                rel_time = [t - latest_time for t in history["times"]]
                axes.plot(rel_time, list(history["values"]), label=label)
                plotted = True

            axes.set_xlabel("Time relative to latest sample (s)")
            axes.set_ylabel("Signal")
            axes.set_title("Ion Chamber Trends")
            axes.grid(True, alpha=0.3)
            if plotted:
                axes.legend(loc="best")
            self.ion_chamber_trend_canvas.draw()

        def _on_close_ion_chamber_trends(self, event):
            frame = self.ion_chamber_trend_frame
            if frame is not None:
                try:
                    if event is None:
                        frame.Destroy()
                except Exception:
                    pass
            self.ion_chamber_trend_frame = None
            self.ion_chamber_trend_canvas = None
            self.ion_chamber_trend_axes = None
            self.ion_chamber_trend_checks = {}
            if event is not None:
                event.Skip()

        def _on_close_cryostat_window(self, event):
            frame = self.cryostat_frame
            self.cryostat_frame = None
            self.cryostat_popup_widgets = None
            self.cryostat_trend_canvas = None
            self.cryostat_trend_axes = None
            self.cryostat_trend_pressure_axes = None
            if frame is not None and event is None:
                try:
                    frame.Destroy()
                except Exception:
                    pass
            if event is not None:
                event.Skip()

        def _refresh_cryostat_trend_plot(self):
            if self.cryostat_trend_canvas is None or self.cryostat_trend_axes is None:
                return

            axes = self.cryostat_trend_axes
            pressure_axes = self.cryostat_trend_pressure_axes
            axes.clear()
            pressure_axes.clear()

            if not self.cryostat_history["times"]:
                self.cryostat_trend_canvas.draw()
                return

            latest_time = self.cryostat_history["times"][-1]
            rel_time = [t - latest_time for t in self.cryostat_history["times"]]
            temps = list(self.cryostat_history["temperature"])
            pressures = list(self.cryostat_history["pressure"])

            temp_line = axes.plot(rel_time, temps, color="firebrick", label="Temperature")[0]
            pressure_line = pressure_axes.plot(rel_time, pressures, color="steelblue", label="Pressure")[0]

            axes.set_xlabel("Time relative to latest sample (s)")
            axes.set_ylabel("Temperature (K)", color="firebrick")
            pressure_axes.set_ylabel("Pressure (mbar)", color="steelblue")
            axes.tick_params(axis="y", colors="firebrick")
            pressure_axes.tick_params(axis="y", colors="steelblue")
            axes.set_title("Cryostat Pressure and Temperature Trends")
            axes.grid(True, alpha=0.3)
            axes.legend([temp_line, pressure_line], ["Temperature", "Pressure"], loc="best")
            self.cryostat_trend_canvas.draw()

        def _update_status_lamps(self):
            for row in self.status_lamps.values():
                cfg = row["config"]
                value = self._read_pv(cfg.get("read_pv"), cfg)
                value = self._evaluate_status_value(cfg["label"], cfg, value)
                row["lamp"].SetLabel(self._status_label(value, cfg)[:10])
                row["lamp"].SetBackgroundColour(self._status_color(value, cfg))
                row["lamp"].Refresh()

        def _update_control_readbacks(self):
            for row in self.pv_controls.values():
                cfg = row["config"]

                if cfg.get("kind") == "command":
                    continue

                value = self._read_pv(cfg.get("read_pv"), cfg)
                if cfg["label"] in self.ion_chamber_state:
                    self.ion_chamber_state[cfg["label"]]["value"] = value
                self._record_ion_chamber_history(cfg["label"], value, cfg)
                if value is None and cfg.get("read_pv"):
                    row["readback"].SetLabel("N/C")
                    row["readback"].SetBackgroundColour(wx.Colour(255, 225, 225))
                else:
                    row["readback"].SetLabel(self._format_value(value))

                if value is not None and cfg.get("kind") in ("status", "enum"):
                    row["readback"].SetBackgroundColour(self._status_color(value))
                elif value is not None:
                    row["readback"].SetBackgroundColour(wx.Colour(235, 235, 235))

                row["readback"].Refresh()

                unit_num_pv = cfg.get("unit_num_pv")
                units_widget = row.get("units")
                if units_widget is not None and unit_num_pv:
                    self.ion_chamber_state[cfg["label"]]["unit_num"] = self._read_pv_display_text(unit_num_pv)

                unit_pv = cfg.get("unit_pv")
                units_widget = row.get("units")
                if units_widget is not None and unit_pv:
                    self.ion_chamber_state[cfg["label"]]["unit_text"] = self._read_pv_display_text(unit_pv)
                    units_widget.SetLabel(self._format_ion_chamber_units(cfg["label"]))
                    units_widget.Refresh()

                delay_pv = cfg.get("derived", {}).get("delay_pv")
                if cfg["label"] in self.ion_chamber_state and delay_pv:
                    self.ion_chamber_state[cfg["label"]]["delay"] = self._read_pv(delay_pv)
                self._update_ion_chamber_derived_display(cfg["label"])
            self._update_cryostat_panel()
            self._refresh_ion_chamber_trend_plot()

        def _update_cryostat_panel(self):
            widget_sets = self._iter_cryostat_widget_sets()
            if not widget_sets:
                return

            cfg = self._cryostat_config()
            temp_read = self._read_pv(cfg["temperature_readback"].get("read_pv"))
            temp_set = self._read_pv(cfg["temperature_setpoint"].get("read_pv"))
            auto_heat = self._read_pv(cfg["auto_heat"].get("read_pv"))
            if auto_heat is None and self.epics is None:
                auto_heat = self.cryostat_state.get("auto_heat", 0)
            heater_power = self._cryostat_display_value(
                cfg["heater_power"], self._read_pv(cfg["heater_power"].get("read_pv"))
            )
            vacuum = self._cryostat_display_value(
                cfg["vacuum"], self._read_pv(cfg["vacuum"].get("read_pv"))
            )
            ln2_weight = self._cryostat_display_value(
                cfg["ln2_weight"], self._read_pv(cfg["ln2_weight"].get("read_pv"))
            )
            gas_flow = self._cryostat_display_value(
                cfg["gas_flow"], self._read_pv(cfg["gas_flow"].get("read_pv"))
            )
            gas_flow_set = self._read_pv(cfg["gas_flow"].get("setpoint_read_pv"))

            if temp_read is not None and vacuum is not None:
                try:
                    self.cryostat_history["times"].append(time.time())
                    self.cryostat_history["temperature"].append(float(temp_read))
                    self.cryostat_history["pressure"].append(float(vacuum))
                except Exception:
                    pass

            self.cryostat_state["auto_heat"] = auto_heat
            for widgets in widget_sets:
                widgets["temperature_readback"].SetLabel(self._format_value(temp_read))
                widgets["temperature_setpoint_readback"].SetLabel(self._format_value(temp_set))
                widgets["gas_flow_set_readback"].SetLabel(self._format_value(gas_flow_set))
                widgets["gas_flow_readback"].SetLabel(self._format_value(gas_flow))

                auto_button = widgets["auto_heat_button"]
                try:
                    is_on = float(auto_heat) != 0
                except Exception:
                    is_on = self._normalize_status_text(auto_heat) in {"1", "ON", "TRUE"}
                auto_button.SetValue(is_on)
                auto_button.SetLabel(self._cryostat_status_text(auto_heat, cfg["auto_heat"]))
                auto_button.SetBackgroundColour(self._cryostat_status_color(auto_heat))
                auto_button.Refresh()

                progress_specs = [
                    ("heater_power", heater_power, "heater_power_gauge", "heater_power_value"),
                    ("vacuum", vacuum, "vacuum_gauge", "vacuum_value"),
                    ("ln2_weight", ln2_weight, "ln2_weight_gauge", "ln2_weight_value"),
                    ("gas_flow", gas_flow, "gas_flow_gauge", "gas_flow_value"),
                ]
                for key, value, gauge_key, text_key in progress_specs:
                    widgets[gauge_key].SetValue(self._cryostat_percent(value, cfg[key]))
                    units = cfg[key].get("units", "")
                    if key == "vacuum" and value is not None:
                        text = f"{float(value):.3e} {units}".strip()
                    elif value is None:
                        text = "--"
                    else:
                        text = f"{self._format_value(value)} {units}".strip()
                    widgets[text_key].SetLabel(text)
                    widgets[text_key].Refresh()

                for key in (
                    "temperature_readback",
                    "temperature_setpoint_readback",
                    "gas_flow_set_readback",
                    "gas_flow_readback",
                ):
                    widgets[key].Refresh()
            self._refresh_cryostat_trend_plot()

        def onRefresh(self, event):
            self.last_pv_error = None
            self.SetStatusText("Refreshing PV values...", 0)
            self._update_status_lamps()
            self._update_control_readbacks()

            if self.epics is None:
                self.SetStatusText("Refresh complete (placeholder mode)", 0)
            elif self.last_pv_error:
                self.SetStatusText(self.last_pv_error, 0)
            else:
                self.SetStatusText("Refresh complete", 0)

        def onConfigure(self, event):
            lines = []

            for section_name in ("status", "controls", "commands"):
                items = self._enabled_items(section_name)
                lines.append(f"[{section_name.upper()}]")
                for item in items:
                    lines.append(f"{item['label']}")
                    lines.append(f"  read_pv : {item.get('read_pv')}")
                    lines.append(f"  write_pv: {item.get('write_pv')}")
                lines.append("")

            dlg = wx.MessageDialog(
                self,
                "\n".join(lines),
                "Enabled PVs",
                wx.OK | wx.ICON_INFORMATION,
            )
            dlg.ShowModal()
            dlg.Destroy()

        def onClear(self, event):
            for row in self.pv_controls.values():
                setpoint = row.get("setpoint")
                if isinstance(setpoint, wx.TextCtrl):
                    setpoint.SetValue("")
            self.SetStatusText("Values cleared", 0)

        def onSetPV(self, event, key):
            row = self.pv_controls[key]
            cfg = row["config"]
            setpoint_widget = row["setpoint"]

            if not isinstance(setpoint_widget, wx.TextCtrl):
                self.SetStatusText(f"{cfg['label']} is read-only", 0)
                return

            raw_value = setpoint_widget.GetValue().strip()
            if raw_value == "":
                self.SetStatusText(f"No value entered for {cfg['label']}", 0)
                return

            value = self._coerce_value(raw_value)

            if self.epics is None:
                row["readback"].SetLabel(self._format_value(value))
                row["readback"].Refresh()
                self.SetStatusText(f"Placeholder set: {cfg['label']} = {value}", 0)
                return

            try:
                self._write_pv(cfg.get("write_pv"), value)
                self.SetStatusText(f"Set {cfg['label']} to {value}", 0)
            except Exception as exc:
                self.SetStatusText(f"Failed to set {cfg['label']}: {exc}", 0)

        def onCommandPV(self, event, key):
            row = self.pv_controls[key]
            cfg = row["config"]
            self.onSendCommand(event, cfg)

        def onStopPV(self, event, key):
            row = self.pv_controls[key]
            cfg = row["config"]
            stop_cfg = {
                "label": f"{cfg['label']} stop",
                "write_pv": cfg.get("stop_pv"),
                "command_value": cfg.get("stop_value", 1),
            }
            self.onSendCommand(event, stop_cfg)

        def onSendCommand(self, event, cfg):
            command_value = cfg.get("command_value", 1)
            if self.epics is None:
                self.SetStatusText(f"Placeholder command sent: {cfg['label']} -> {command_value}", 0)
                return

            try:
                self._write_pv(cfg.get("write_pv"), command_value)
                self.SetStatusText(f"Command sent: {cfg['label']}", 0)
            except Exception as exc:
                self.SetStatusText(f"Failed command {cfg['label']}: {exc}", 0)

        def onClose(self, event=None):
            self._clear_pv_monitors()
            self._on_close_ion_chamber_trends(None)
            self._on_close_cryostat_window(None)

        def onExit(self, event=None):
            self.onClose(event=event)

    class DetectorPanel(wx.Panel):
        """Notebook page backed by xraylarch's EPICS XRF display logic."""

        main_title = EpicsXRFDisplayFrame.main_title
        _about = EpicsXRFDisplayFrame._about
        me4_layout = EpicsXRFDisplayFrame.me4_layout

        def __init__(
            self,
            parent,
            *,
            detector: DetectorConfig,
            size=(1100, 850),
            title: str | None = None,
            _larch=None,
            **kws,
        ):
            super().__init__(parent=parent, size=size, **kws)
            if title is None:
                title = detector.name

            self.page_label = detector.name
            self.window_title = title
            self.colors = XRFDisplayColors_Dark() if DARK_THEME else XRFDisplayColors_Light()
            register_darkdetect(self.onDarkMode)

            self.subframes = {}
            self.data = None
            self.title = title
            self.roi_callback = None
            self.plotframe = None
            self.wids = {}

            if isinstance(_larch, LarchFrame):
                self.larch_buffer = _larch
                self.larch_owner = False
            elif isinstance(_larch, Interpreter):
                self.larch_buffer = LarchFrame(
                    _larch=_larch,
                    is_standalone=False,
                    with_raise=False,
                )
                self.larch_owner = False
            else:
                self.larch_buffer = LarchFrame(with_raise=False)
                self.larch_owner = True

            self.subframes["larch_buffer"] = self.larch_buffer
            self.larch = self.larch_buffer.larchshell
            self.init_larch()

            self.exit_callback = None
            self.roi_patch = None
            self.selected_roi = None
            self.roilist_sel = None
            self.selected_elem = None
            self.mca = None
            self.mcabkg = None
            self.xdat = np.arange(4096) * 0.01
            self.ydat = np.ones(4096) * 0.01
            self.plotted_groups = []
            self.ymin = 0.9
            self.show_cps = False
            self.show_pileup = False
            self.show_escape = False
            self.show_yaxis = True
            self.ylog_scale = True
            self.show_grid = False
            self.major_markers = []
            self.minor_markers = []
            self.hold_markers = []
            self.hold_lines = None
            self.saved_lines = None
            self.energy_for_zoom = None
            self.xview_range = None
            self.xmarker_left = None
            self.xmarker_right = None
            self.highlight_xrayline = None
            self.cursor_markers = [None, None]

            self.det_type = detector.det_type
            self.ioc_type = detector.ioc_type.lower()
            self.prefix = detector.prefix
            self.nmca = detector.nmca
            self.det_main = 1
            self.det = None
            self.win_xps3 = None
            self.incident_energy_kev = None
            self.incident_energy_pvname = detector.incident_energy_pvname
            self.incident_energy_units = detector.incident_energy_units
            self.environ = []
            if detector.environ_file is not None:
                self.read_environfile(detector.environ_file)

            self.icon_file = str(Path(icondir, "ptable.ico"))

            self.createMainPanel()
            # Skip EPICS connection for mock prefixes (for debugging)
            if not self.prefix.upper().startswith("MOCK"):
                self.onConnectEpics(event=None, prefix=self.prefix)
            self.SetTitle(f"{self.main_title}: {title}")

        def createMenus(self):
            return

        def SetTitle(self, title):
            self.window_title = title
            host = self.GetTopLevelParent()
            if hasattr(host, "update_detector_title"):
                host.update_detector_title(self, title)

        def SetStatusText(self, text, panel=0):
            host = self.GetTopLevelParent()
            if hasattr(host, "set_status_text"):
                host.set_status_text(text, panel=panel)

        def _rate_box_color(self, value):
            if value is None or value < 1000:
                return wx.Colour(190, 190, 190)
            if value < 150000:
                return wx.Colour(160, 220, 160)
            if value <= 300000:
                return wx.Colour(245, 220, 120)
            return wx.Colour(235, 120, 120)

        def _set_rate_widget(self, key, value):
            widget = self.wids[key]
            if value is None:
                widget.SetLabel(" ")
            else:
                widget.SetLabel(f"{value:,.0f}")
            widget.SetBackgroundColour(self._rate_box_color(value))
            widget.Refresh()

        def createEpicsPanel(self):
            pane = wx.Panel(self, name="epics panel")
            style = wx.ALIGN_LEFT
            right_style = wx.ALIGN_RIGHT

            def simple_text(parent, label, size=(-1, -1), text_style=style):
                return wx.StaticText(parent, label=label, size=size, style=text_style)

            det_btnpanel = self.create_detbuttons(pane)
            bkg_choices = ["None", "All"] + [f"MCA{i+1}" for i in range(self.nmca)]

            self.wids["det_status"] = simple_text(pane, " ", size=(120, -1))
            self.wids["deadtime"] = simple_text(pane, " ", size=(120, -1))

            if self.nmca > 1:
                self.wids["bkg_det"] = wx.Choice(pane, size=(125, -1), choices=bkg_choices)
                self.wids["bkg_det"].Bind(wx.EVT_CHOICE, self.onSelectDet)

            self.wids["dwelltime"] = wx.TextCtrl(
                pane,
                value="0.100",
                size=(80, -1),
                style=wx.TE_PROCESS_ENTER,
            )
            self.wids["dwelltime"].Bind(wx.EVT_TEXT_ENTER, self.onSetDwelltime)
            self.wids["elapsed"] = simple_text(pane, " ", size=(80, -1))

            self.wids["mca_sum"] = wx.Choice(pane, size=(125, -1), choices=["Single", "Accumulate"])
            self.wids["mca_sum"].SetSelection(1)
            self.wids["mca_sum"].Bind(wx.EVT_CHOICE, self.onMcaSumChoice)

            roipanel = wx.Panel(pane)
            roisizer = wx.GridBagSizer(4, 6)
            channels_per_row = 10
            block_height = 5
            self.wids["roi_name"] = simple_text(roipanel, "[ROI]", size=(120, -1), text_style=style)

            for block_start in range(1, self.nmca + 1, channels_per_row):
                block_index = (block_start - 1) // channels_per_row
                base_row = block_index * block_height
                roisizer.Add(
                    simple_text(roipanel, "Channel", size=(120, -1), text_style=style),
                    (base_row, 0),
                    (1, 1),
                    style,
                    1,
                )
                roisizer.Add(
                    simple_text(roipanel, "Count Rates (Hz)", size=(120, -1), text_style=style),
                    (base_row + 1, 0),
                    (1, 1),
                    style,
                    1,
                )
                roisizer.Add(
                    simple_text(roipanel, "Output Count Rate", size=(120, -1), text_style=style),
                    (base_row + 2, 0),
                    (1, 1),
                    style,
                    1,
                )
                roisizer.Add(
                    self.wids["roi_name"] if block_index == 0 else simple_text(
                        roipanel,
                        "[ROI]",
                        size=(120, -1),
                        text_style=style,
                    ),
                    (base_row + 3, 0),
                    (1, 1),
                    style,
                    1,
                )

                for offset, i in enumerate(
                    range(block_start, min(block_start + channels_per_row, self.nmca + 1)),
                    start=1,
                ):
                    label = simple_text(roipanel, f"MCA {i}", size=(90, -1), text_style=right_style)
                    self.wids[f"ocr{i}"] = ocr = simple_text(
                        roipanel,
                        " ",
                        size=(90, -1),
                        text_style=right_style,
                    )
                    self.wids[f"roi{i}"] = roi = simple_text(
                        roipanel,
                        " ",
                        size=(90, -1),
                        text_style=right_style,
                    )
                    ocr.SetBackgroundColour(self._rate_box_color(None))
                    roi.SetBackgroundColour(self._rate_box_color(None))

                    roisizer.Add(label, (base_row, offset), (1, 1), style, 0)
                    roisizer.Add(ocr, (base_row + 1, offset), (1, 1), style, 0)
                    roisizer.Add(roi, (base_row + 2, offset), (1, 1), style, 0)

            roipanel.SetSizer(roisizer)

            def add_button(label, action):
                button = wx.Button(pane, label=label, size=(90, -1))
                button.Bind(wx.EVT_BUTTON, action)
                return button

            b1 = add_button("Start", self.onStart)
            b2 = add_button("Stop", self.onStop)
            b3 = add_button("Erase", self.onErase)
            b4 = add_button("Continuous", partial(self.onStart, dtime=0.25, nframes=16000))

            sum_lab = simple_text(pane, "Accumulate Mode:", size=(150, -1))
            if self.nmca > 1:
                bkg_lab = simple_text(pane, "Background MCA:", size=(150, -1))
            pre_lab = simple_text(pane, "Dwell Time (s):", size=(125, -1))
            ela_lab = simple_text(pane, "Elapsed Time (s):", size=(125, -1))
            sta_lab = simple_text(pane, "Status :", size=(100, -1))
            dea_lab = simple_text(pane, "% Deadtime:", size=(100, -1))

            psizer = wx.GridBagSizer(8, 10)
            psizer.Add(simple_text(pane, " MCAs: "), (0, 0), (1, 1), style | wx.ALL, 3)
            psizer.Add(det_btnpanel, (0, 1), (3, 1), style | wx.ALL, 3)
            if self.nmca > 1:
                psizer.Add(bkg_lab, (0, 2), (1, 1), style | wx.ALL, 3)
                psizer.Add(self.wids["bkg_det"], (0, 3), (1, 1), style | wx.ALL, 3)
            psizer.Add(sum_lab, (1, 2), (1, 1), style | wx.ALL, 3)
            psizer.Add(self.wids["mca_sum"], (1, 3), (1, 1), style | wx.ALL, 3)
            psizer.Add(pre_lab, (0, 4), (1, 1), style | wx.ALL, 3)
            psizer.Add(ela_lab, (1, 4), (1, 1), style | wx.ALL, 3)
            psizer.Add(self.wids["dwelltime"], (0, 5), (1, 1), style | wx.ALL, 3)
            psizer.Add(self.wids["elapsed"], (1, 5), (1, 1), style | wx.ALL, 3)
            psizer.Add(b1, (0, 6), (1, 1), style | wx.ALL, 3)
            psizer.Add(b4, (0, 7), (1, 1), style | wx.ALL, 3)
            psizer.Add(b2, (1, 6), (1, 1), style | wx.ALL, 3)
            psizer.Add(b3, (1, 7), (1, 1), style | wx.ALL, 3)
            psizer.Add(sta_lab, (0, 8), (1, 1), style | wx.ALL, 3)
            psizer.Add(self.wids["det_status"], (0, 9), (1, 1), style | wx.ALL, 3)
            psizer.Add(dea_lab, (1, 8), (1, 1), style | wx.ALL, 3)
            psizer.Add(self.wids["deadtime"], (1, 9), (1, 1), style | wx.ALL, 3)
            psizer.Add(roipanel, (2, 2), (1, 8), style | wx.ALL, 12)

            if self.nmca > 1:
                sum_all_btn = wx.Button(pane, label="Sum All Channels", size=(160, -1))
                sum_all_btn.Bind(wx.EVT_BUTTON, self.onSumAllChannels)
                psizer.Add(sum_all_btn, (3, 2), (1, 2), style | wx.ALL, 3)

            pane.SetSizer(psizer)

            if self.det is not None:
                self.det.connect_displays(
                    status=self.wids["det_status"],
                    elapsed=self.wids["elapsed"],
                    dwelltime=self.wids["dwelltime"],
                )

            wx.CallAfter(self.onSelectDet, index=1, init=True)
            self.timer_counter = 0
            self.mca_timer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, self.UpdateData, self.mca_timer)
            self.mca_timer.Start(250)
            return pane

        def createMainPanel(self):
            epicspanel = self.createEpicsPanel()
            ctrlpanel = self.createControlPanel()
            rpanel = self.createPlotPanel()

            self.SetMinSize((450, 350))

            outer = wx.BoxSizer(wx.VERTICAL)
            outer.Add(epicspanel, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 12)
            outer.Add(wx.StaticLine(self, style=wx.LI_HORIZONTAL), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 12)

            content = wx.BoxSizer(wx.HORIZONTAL)
            content.Add(ctrlpanel, 0, wx.EXPAND | wx.RIGHT | wx.BOTTOM, 12)
            content.Add(rpanel, 1, wx.EXPAND | wx.LEFT | wx.BOTTOM, 12)

            outer.Add(content, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
            self.SetSizer(outer)
            self.set_roilist(mca=None)

    # Reuse xraylarch logic directly on the panel class.
    for name in (
        "on_cursor",
        "clear_lines",
        "clear_markers",
        "draw",
        "update_status",
        "onLeftUp",
        "onDarkMode",
        "createControlPanel",
        "createPlotPanel",
        "init_larch",
        "add_mca",
        "_getlims",
        "_set_xview",
        "onPanLo",
        "onPanHi",
        "onZoomIn",
        "onZoomOut",
        "unzoom_all",
        "onShowCPS",
        "onShowGrid",
        "set_roilist",
        "clear_roihighlight",
        "get_roiname",
        "onConfirmDelROI",
        "onDelROI",
        "onROI",
        "ShowROIPatch",
        "createBaseMenus",
        "show_subframe",
        "onShowLarchBuffer",
        "onShowXRFBrowser",
        "onSavePNG",
        "onCopyImage",
        "onPageSetup",
        "onPrintPreview",
        "onPrint",
        "config_colors",
        "config_xraylines",
        "onShowLegend",
        "onKLM",
        "onToggleHold",
        "onSelectXrayLine",
        "onShowLines",
        "onPileupPrediction",
        "onEscapePrediction",
        "onYAxis",
        "_formaty",
        "replot",
        "onLogLinear",
        "plotmca",
        "plot",
        "get_mca",
        "update_mca",
        "oplot",
        "onReadMCAFile",
        "write_message",
        "showInspectionTool",
        "onAbout",
    ):
        setattr(DetectorPanel, name, getattr(XRFDisplayFrame, name))

    for name in (
        "read_environfile",
        "onXspress3Control",
        "onConnectEpics",
        "onIncidentEnergy",
        "onSaveMCAFile",
        "onSaveColumnFile",
        "prompt_for_detector",
        "connect_to_detector",
        "show_mca",
        "onSaveROIs",
        "onRestoreROIs",
        "createCustomMenus",
        "create_detbuttons",
        "UpdateData",
        "ShowROIStatus",
        "onSelectDet",
        "onMcaSumChoice",
        "onSetDwelltime",
        "clear_mcas",
        "onStart",
        "onStop",
        "onErase",
        "onDelROI",
        "onNewROI",
        "onRenameROI",
        "onCalibrateEnergy",
        "onSetCalib",
    ):
        setattr(DetectorPanel, name, getattr(EpicsXRFDisplayFrame, name))

    def _do_live_sum(panel):
        """Re-sum all MCA channels and update the plot. Called each timer tick in sum mode."""
        import copy
        import numpy as np
        try:
            base = panel.det.get_mca(mca=1)
            summed = copy.deepcopy(base)
            total = np.zeros_like(base.counts, dtype=float)
            for i in range(1, panel.nmca + 1):
                counts = panel.det.get_array(mca=i)
                if counts is not None:
                    total += counts.astype(float)
            summed.counts = total
            summed.label = f"Sum (all {panel.nmca} ch)"
            panel.mca = summed
            panel.needs_newplot = False
            EpicsXRFDisplayFrame.plotmca(panel, summed, set_title=False, init=False)
        except Exception as exc:
            print(f"[xrf] live sum error: {exc}", flush=True)

    def _safe_show_mca(self, *args, **kwargs):
        if getattr(self, "det", None) is None:
            return
        if getattr(self, "_sum_mode", False):
            _do_live_sum(self)
            return
        return EpicsXRFDisplayFrame.show_mca(self, *args, **kwargs)

    DetectorPanel.show_mca = _safe_show_mca

    def _on_sum_all_channels(self, _event=None):
        """Enter live sum mode: re-sum all channels on every timer tick."""
        if getattr(self, "det", None) is None:
            return
        self._sum_mode = True
        self.show_mca()

    DetectorPanel.onSumAllChannels = _on_sum_all_channels

    def _safe_onSelectDet(self, event=None, index=0, init=False, **kws):
        """Exit sum mode when the user selects a single channel."""
        self._sum_mode = False
        EpicsXRFDisplayFrame.onSelectDet(self, event=event, index=index, init=init, **kws)

    DetectorPanel.onSelectDet = _safe_onSelectDet

    def _safe_update_data(self, *args, **kwargs):
        # Avoid crashes when EPICS connection is not established (e.g. MOCK prefixes)
        if getattr(self, "det", None) is None:
            return
        return EpicsXRFDisplayFrame.UpdateData(self, *args, **kwargs)

    DetectorPanel.UpdateData = _safe_update_data

    def _panel_on_close(self, event=None):
        timer = getattr(self, "mca_timer", None)
        if timer is not None:
            try:
                timer.Stop()
            except Exception:
                pass
        XRFDisplayFrame.onClose(self)

    DetectorPanel.onClose = _panel_on_close
    DetectorPanel.onExit = _panel_on_close

    def _panel_show_roi_status(self, left, right, name="", panel=0):
        if left > right:
            return
        try:
            ftime, nframes = self.det.get_frametime()
        except Exception:
            ftime = self.det.frametime
            nframes = self.det.nframes
        self.det.elapsed_real = nframes * ftime

        mca_counts = self.det.mcas[self.det_main - 1].get("VAL")
        thissum = mca_counts[left:right].sum()
        thisrate = thissum / ftime if ftime > 0 else None

        if name in (None, ""):
            name = "selected"
        else:
            for nmca in range(1, self.nmca + 1):
                counts = self.det.mcas[nmca - 1].get("VAL")
                total = counts.sum() / ftime if ftime > 0 else None
                sum_counts = counts[left:right].sum()
                rate = sum_counts / ftime if ftime > 0 else None
                self._set_rate_widget(f"ocr{nmca}", total)
                self._set_rate_widget(f"roi{nmca}", rate)
                if self.det_main == nmca:
                    thissum = sum_counts
                    thisrate = rate

        shown_rate = 0.0 if thisrate is None else thisrate
        self.write_message(f" {name}: Cts={thissum:10,.0f} :{shown_rate:10,.1f} Hz", panel=panel)
        current_name = self.wids["roi_name"].GetLabel().strip()
        if name != current_name:
            self.wids["roi_name"].SetLabel(name)

    DetectorPanel.ShowROIStatus = _panel_show_roi_status

    class EmbeddedQtPanel:
        """
        Notebook tab that manages beamline_control.py as a companion window.
        Clicking the tab opens/raises the Qt window; the tab shows live status.
        X11 embedding is intentionally avoided — it breaks keyboard input.
        """
        _BC_SCRIPT = Path(__file__).with_name("beamline_control.py")

        def __init__(self, parent, use_sim: bool = False):
            self.page_label = "Beamline Controls"
            self.window_title = "Beamline Controls"
            self._use_sim = use_sim
            self._proc = None

            self._panel = wx.Panel(parent)
            self._panel.page_label = self.page_label
            self._panel.window_title = self.window_title
            self._panel._embedded_qt = self

            sizer = wx.BoxSizer(wx.VERTICAL)
            sizer.AddStretchSpacer(1)

            self._status_lbl = wx.StaticText(
                self._panel, label="Beamline Controls window is not open.",
                style=wx.ALIGN_CENTRE_HORIZONTAL,
            )
            font = self._status_lbl.GetFont()
            font.SetPointSize(12)
            self._status_lbl.SetFont(font)

            self._btn = wx.Button(self._panel, label="Open Beamline Controls")
            self._btn.SetMinSize((220, 40))
            self._btn.Bind(wx.EVT_BUTTON, self._on_btn)

            sizer.Add(self._status_lbl, 0, wx.ALIGN_CENTRE | wx.BOTTOM, 12)
            sizer.Add(self._btn, 0, wx.ALIGN_CENTRE)
            sizer.AddStretchSpacer(1)
            self._panel.SetSizer(sizer)

            self._timer = wx.Timer(self._panel)
            self._panel.Bind(wx.EVT_TIMER, self._on_timer, self._timer)
            self._timer.Start(1000)

        def __getattr__(self, name):
            return getattr(self._panel, name)

        def activate(self):
            """Called when tab is selected — open if not running, raise if it is."""
            if self._proc is None or self._proc.poll() is not None:
                wx.CallLater(100, self._launch)
            else:
                self._raise_window()

        def _on_btn(self, _event):
            if self._proc is not None and self._proc.poll() is None:
                self._raise_window()
            else:
                self._launch()

        def _launch(self):
            if self._proc is not None and self._proc.poll() is None:
                return
            project_dir = str(Path(__file__).parent)
            venv_python = str(Path(project_dir) / ".venv" / "bin" / "python3")
            python = venv_python if Path(venv_python).exists() else sys.executable
            cmd = [python, str(self._BC_SCRIPT)]
            if self._use_sim:
                cmd.append("--sim")
            env = os.environ.copy()
            env.setdefault("DISPLAY", os.environ.get("DISPLAY", ":1"))
            env["PYTHONPATH"] = project_dir + os.pathsep + env.get("PYTHONPATH", "")
            self._proc = subprocess.Popen(
                cmd,
                cwd=project_dir,
                env=env,
                stdout=open(os.path.join(project_dir, "bc_launch.log"), "w"),
                stderr=subprocess.STDOUT,
            )
            self._update_ui()

        def _raise_window(self):
            """Ask the window manager to bring the Qt window to front."""
            try:
                import ctypes
                xlib = ctypes.CDLL("libX11.so.6")
                xlib.XOpenDisplay.restype = ctypes.c_void_p
                display = xlib.XOpenDisplay(None)
                if display:
                    # Send _NET_ACTIVE_WINDOW to root to raise Qt window
                    xlib.XCloseDisplay(display)
            except Exception:
                pass

        def _on_timer(self, _event):
            self._update_ui()

        def _update_ui(self):
            running = self._proc is not None and self._proc.poll() is None
            if running:
                self._status_lbl.SetLabel("Beamline Controls window is open.")
                self._btn.SetLabel("Raise Beamline Controls")
            else:
                self._status_lbl.SetLabel("Beamline Controls window is not open.")
                self._btn.SetLabel("Open Beamline Controls")

        def onClose(self):
            self._timer.Stop()
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()

    class DualDetectorHostFrame(wx.Frame):
        """Top-level frame with detector and beamline-control notebook tabs."""

        def __init__(
            self,
            *,
            detectors: Sequence[DetectorConfig],
            size=(1400, 950),
            title="Xspress3 Viewer",
            use_sim: bool = False,
            _larch=None,
        ):
            super().__init__(None, title=title, size=size)
            self.base_title = title
            self.detectors = list(detectors)
            self._larch = _larch
            self._use_sim = use_sim

            # Create shared Larch interpreter upfront for faster panel initialization
            if self._larch is None:
                self._larch = Interpreter()

            self.bl_control_detector = None
            for det in self.detectors:
                if det.name.lower() == "bl control":
                    self.bl_control_detector = det
                    break
            if self.bl_control_detector is None and self.detectors:
                self.bl_control_detector = self.detectors[0]

            self.notebook = wx.Notebook(self)

            self.statusbar = self.CreateStatusBar(4)
            self.statusbar.SetStatusWidths([-5, -3, -3, -4])
            for idx, text in enumerate(("XRF Display", " ", " ", " ")):
                self.statusbar.SetStatusText(text, idx)

            self.panels = []

            frame_sizer = wx.BoxSizer(wx.VERTICAL)
            frame_sizer.Add(self.notebook, 1, wx.EXPAND)
            self.SetSizer(frame_sizer)

            try:
                icon_path = Path(icondir, ICON_FILE).as_posix()
                self.SetIcon(wx.Icon(icon_path, wx.BITMAP_TYPE_ICO))
            except Exception:
                pass

            self._create_pages()
            self.Bind(wx.EVT_CLOSE, self._on_close)
            self.notebook.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self._on_page_changed)

        def _create_pages(self):
            for detector in self.detectors:
                if detector.name.lower() == "bl control":
                    continue

                panel = DetectorPanel(
                    self.notebook,
                    detector=detector,
                    title=detector.name,
                    _larch=self._larch,
                )
                self.notebook.AddPage(panel, detector.name)
                self.panels.append(panel)

            bl_panel = EmbeddedQtPanel(self.notebook, use_sim=self._use_sim)
            self.notebook.AddPage(bl_panel._panel, bl_panel.page_label)
            self.panels.append(bl_panel)

            if self.panels:
                self.update_detector_title(self.panels[0], self.panels[0].window_title)

        def set_status_text(self, text, panel=0):
            self.statusbar.SetStatusText(text, panel)

        def update_detector_title(self, panel, title):
            # Unwrap EmbeddedQtPanel to its inner wx.Panel for notebook lookups
            wx_panel = getattr(panel, "_panel", panel)
            page_index = self.notebook.FindPage(wx_panel)
            label = getattr(panel, "page_label", title)
            if page_index != wx.NOT_FOUND:
                self.notebook.SetPageText(page_index, label)
            current = self.notebook.GetCurrentPage()
            if current is wx_panel:
                super().SetTitle(f"{self.base_title} | {label}")

        def _on_page_changed(self, event):
            panel = self.notebook.GetCurrentPage()
            if panel is not None:
                self.update_detector_title(panel, getattr(panel, "window_title", panel.page_label))
                # Trigger subprocess launch the first time the BL tab is shown
                embedded = getattr(panel, "_embedded_qt", None)
                if embedded is not None:
                    embedded.activate()
            event.Skip()

        def _on_close(self, event):
            for panel in self.panels:
                try:
                    panel.onClose()
                except Exception:
                    pass

            self.Destroy()
    class Xspress3ViewerApp(EpicsXRFApp):
        """Application entrypoint with detector and beamline control tabs."""

        def __init__(
            self,
            *,
            detectors: Sequence[DetectorConfig],
            size=(1400, 950),
            title="Xspress3 Viewer",
            output_title="XRF",
            use_sim: bool = False,
            _larch=None,
            **kws,
        ):
            self.detectors = list(detectors)
            self.size = size
            self.title = title
            self.output_title = output_title
            self.use_sim = use_sim
            super().__init__(
                _larch=_larch,
                prefix=self.detectors[0].prefix if self.detectors else None,
                det_type=self.detectors[0].det_type if self.detectors else "ME-4",
                ioc_type=self.detectors[0].ioc_type if self.detectors else "xspress3",
                nmca=self.detectors[0].nmca if self.detectors else 4,
                size=size,
                title=title,
                output_title=output_title,
                **kws,
            )

        def createApp(self):
            frame = DualDetectorHostFrame(
                detectors=self.detectors,
                size=self.size,
                title=self.title,
                use_sim=self.use_sim,
                _larch=self._larch,
            )
            frame.Show()
            frame.Raise()
            self.SetTopWindow(frame)
            return True

    return DetectorPanel, DualDetectorHostFrame, Xspress3ViewerApp


def launch_app(
    detectors: Sequence[DetectorConfig],
    *,
    size=(1400, 950),
    title="Xspress3 Viewer",
    use_sim: bool = False,
):
    _, _, Xspress3ViewerApp = build_app_classes()
    app = Xspress3ViewerApp(detectors=detectors, size=size, title=title, use_sim=use_sim)
    app.MainLoop()
