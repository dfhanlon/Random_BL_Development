"""
Simulated PV layer for offline testing of cls_main.py / beamline_control.py.

Usage:
    python cls_main.py --sim        # full sim with live updates
    python cls_main.py --dummy      # fully static stubs (no updates)

All PV names must match pvs.yaml exactly so the beamline controls panel
picks them up as if talking to a real IOC.
"""
from __future__ import annotations

import math
import random
import threading
import time
import types


# ── SimPV ─────────────────────────────────────────────────────────────────────

class SimPV:
    """
    Drop-in for epics.PV.  set_sim_value() pushes updates to all subscribers
    exactly like a real PV monitor callback.
    """

    def __init__(self, pvname: str, initial=0.0):
        self.pvname    = pvname
        self._value    = initial
        self._cbs: list[tuple[int, object]] = []
        self._idx      = 0
        self.connected = True
        self.type      = "ctrl_double"
        self.count     = 1

    def connect(self, **k):              return True
    def wait_for_connection(self, **k):  return True
    def get_ctrlvars(self):              return {}

    def get(self, as_string=False, **k):
        return str(self._value) if as_string else self._value

    def put(self, value, **k):
        self._value = value
        self._fire(value)
        return 1

    def add_callback(self, callback=None, **k):
        if callback is None:
            return None
        idx = self._idx
        self._idx += 1
        self._cbs.append((idx, callback))
        threading.Timer(0.05, lambda: self._fire(self._value)).start()
        return idx

    def remove_callback(self, index=None, **k):
        self._cbs = [(i, c) for i, c in self._cbs if i != index]

    def set_sim_value(self, value):
        self._value = value
        self._fire(value)

    def _fire(self, value):
        cv = str(value)
        for _, cb in list(self._cbs):
            try:
                cb(pvname=self.pvname, value=value,
                   char_value=cv, charvalue=cv)
            except Exception:
                pass


# ── Registry ──────────────────────────────────────────────────────────────────

_REGISTRY: dict[str, SimPV] = {}


def _reg(pvname: str, initial=0.0) -> SimPV:
    if pvname not in _REGISTRY:
        _REGISTRY[pvname] = SimPV(pvname, initial)
    return _REGISTRY[pvname]


def get_sim_pv(pvname: str, **kw) -> SimPV:
    if pvname not in _REGISTRY:
        _REGISTRY[pvname] = SimPV(pvname, 0.0)
    return _REGISTRY[pvname]


def patch_epics():
    """Replace epics module with SimPV stubs. Call before importing beamline_control."""
    try:
        import epics as _ep
    except ModuleNotFoundError:
        _ep = types.ModuleType("epics")
        import sys
        sys.modules["epics"] = _ep

    _ep.PV     = get_sim_pv
    _ep.get_pv = get_sim_pv
    _ep.caget  = lambda pv, **k: get_sim_pv(pv).get()
    _ep.caput  = lambda pv, v, **k: get_sim_pv(pv).put(v)
    return _ep


# ── Simulated beamline ────────────────────────────────────────────────────────

class SimulatedBeamline:
    """
    Creates SimPV instances for every PV in pvs.yaml and runs a background
    thread that pushes realistic, slowly-varying values.

    Pandabox IC formula (matches app.py):
        voltage  = VAL × GAIN × 1e-8   (in V)
        flux     = scale × voltage × keithley_A_per_V
                   / (1.6e-19 × (E_eV/35) × η)
    At sim baseline (VAL=3e8, GAIN=1, keithley=2µA/V, 12 keV, η=1.57%, scale=0.287)
        → voltage ≈ 3.0 V  →  flux ≈ 2.0×10¹² ph/s
    """

    def __init__(self):
        # ── Status strip ──────────────────────────────────────────────────────
        self.ring_current = _reg("PCT1402-01:mA:fbk",           199.5)
        self.fe_shutter   = _reg("IPSH1407-I00-02:state",       1.0)   # >0.5 = OPEN
        self.valves       = _reg("PLC2406-7-03:BL:ready:in",    1.0)
        self.motor_il     = _reg("SWES1607-7-01:Em:Off",        1.0)
        self.wiggler_gap  = _reg("WIG1407-01:gap:mm:fbk",       10.5)
        self.fe_safety    = _reg("SSH1407-I00-01:state",        1.0)
        self.m1_blade     = _reg("SMTR1607-5-I21-08:mm:fbk",   1.5)
        self.mono_mask_1  = _reg("SMTR1607-5-I21-09:mm:fbk",   2.0)
        self.mono_mask_2  = _reg("SMTR1607-5-I21-10:mm:fbk",   1.5)
        self.m2_paddle    = _reg("SMTR1607-5-I21-28:mm:sp",    20.0)
        self.poe_shutter  = _reg("SSH1607-5-I21-01:state",     0.0)   # <0.5 = OPEN
        self.ps_701_a     = _reg("PS1607-701:A:voltage",       1900.0)
        self.ps_701_b     = _reg("PS1607-701:B:voltage",       1900.0)
        self.ps_702_a     = _reg("PS1607-702:A:voltage",       1900.0)
        self.filt_250     = _reg("PFIL1607-7-I21-01:control",  0)
        self.filt_500     = _reg("PFIL1607-7-I21-02:control",  0)
        self.filt_1000    = _reg("PFIL1607-7-I21-03:control",  0)
        self.filt_10000   = _reg("PFIL1607-7-I21-04:control",  0)

        # ── Beamline controls ─────────────────────────────────────────────────
        self.mono_energy  = _reg("BL1607-I21:Mono:Energy:EV:fbk",  12000.0)
        self.dwell        = _reg("MCS1607-701:mcs:delay",           1000.0)
        self.stage_z      = _reg("SMTR1607-7-I21-16:mm:fbk",       0.0)
        self.stage_z_sp   = _reg("SMTR1607-7-I21-16:mm:sp",        0.0)
        self.stage_y      = _reg("SMTR1607-7-I21-15:mm:sp",        0.0)
        self.sample_wheel = _reg("SMTR1607-7-I21-19:deg",          0.0)
        self.det_inboard  = _reg("SMTR1607-7-I21-18:mm",           25.0)
        self.det_outboard = _reg("SMTR1607-7-I21-17:mm",           25.0)
        self.dbhr_m1      = _reg("SMTR1607-7-I21-08:mm:fbk",       0.0)
        self.dbhr_m2      = _reg("SMTR1607-7-I21-09:mm:fbk",       6.0)
        self.dbhr_pitch   = _reg("SMTR1607-7-I21-07:deg:fbk",      0.0)
        self.m1_pitch     = _reg("BL1607-I21:M1:Pitch:deg:fbk",    2.74)
        self.jj_vgap      = _reg("PSL1607-7-I21-01:Gap:mm",        2.0)
        self.jj_vcen      = _reg("PSL1607-7-I21-01:Center:mm",     0.0)
        self.jj_hgap      = _reg("PSL1607-7-I21-02:Gap:mm",        10.0)
        self.jj_hcen      = _reg("PSL1607-7-I21-02:Center:mm",     0.0)

        # ── Pandabox ion chambers ─────────────────────────────────────────────
        # VAL × GAIN × 1e-8 = voltage in V.  Baseline: 3e8 × 1.0 × 1e-8 = 3.0 V
        self.ic0_val  = _reg("TRG1607-702:FMC_IN:VAL1",  3.0e8)
        self.ic0_gain = _reg("TRG1607-702:FMC_IN:GAIN1", 1.0)
        self.ic1_val  = _reg("TRG1607-702:FMC_IN:VAL2",  2.8e8)
        self.ic1_gain = _reg("TRG1607-702:FMC_IN:GAIN2", 1.0)
        self.ic2_val  = _reg("TRG1607-702:FMC_IN:VAL3",  2.5e8)
        self.ic2_gain = _reg("TRG1607-702:FMC_IN:GAIN3", 1.0)
        self.pips_val = _reg("TRG1607-702:FMC_IN:VAL4",  0.5e8)
        self.pips_gain= _reg("TRG1607-702:FMC_IN:GAIN4", 1.0)

        # Zebra / MCS scaler PVs (counts per dwell)
        self.mcs_ic0  = _reg("MCS1607-701:mcs16:fbk",  1_000_000.0)
        self.mcs_ic1  = _reg("MCS1607-701:mcs17:fbk",    800_000.0)
        self.mcs_ic2  = _reg("MCS1607-701:mcs18:fbk",    500_000.0)
        self.mcs_pips = _reg("MCS1607-701:mcs19:fbk",    100_000.0)

        # Keithley sensitivity — char_value must be parseable by _ic_keithley_sensitivity_av.
        # num="2" + unit="uA/V" → 2e-6 A/V
        self.ic0_snum  = _reg("A1607-701:sens_num",  "2")
        self.ic0_sunit = _reg("A1607-701:sens_unit", "uA/V")
        self.ic1_snum  = _reg("A1607-702:sens_num",  "2")
        self.ic1_sunit = _reg("A1607-702:sens_unit", "uA/V")
        self.ic2_snum  = _reg("A1607-703:sens_num",  "2")
        self.ic2_sunit = _reg("A1607-703:sens_unit", "uA/V")
        self.pips_snum = _reg("A1607-704:sens_num",  "2")
        self.pips_sunit= _reg("A1607-704:sens_unit", "uA/V")

        # Keithley gain step commands (no-op stubs)
        for pv in ("A1607-701:gainIncrease", "A1607-701:gainDecrease",
                   "A1607-702:gainIncrease", "A1607-702:gainDecrease",
                   "A1607-703:gainIncrease", "A1607-703:gainDecrease",
                   "PV:PIPS:GAIN:UP",        "PV:PIPS:GAIN:DOWN"):
            _reg(pv, 0)

        # ── Cryostat ─────────────────────────────────────────────────────────
        self.cryo_temp_rbv  = _reg("TC1607-7-01:T1:READ_TEMP_SIG_TEMP",  85.0)
        self.cryo_tset_rbv  = _reg("TC1607-7-01:C1:READ_TEMP_LOOP_TSET", 85.0)
        self.cryo_tset_sp   = _reg("TC1607-7-01:C1:SET_TEMP_LOOP_TSET",  85.0)
        self.cryo_ah_rbv    = _reg("TC1607-7-01:C1:READ_TEMP_LOOP_ENAB", 1)
        self.cryo_ah_sp     = _reg("TC1607-7-01:C1:SET_TEMP_LOOP_ENAB",  1)
        self.cryo_hpower    = _reg("TC1607-7-01:C1:READ_TEMP_LOOP_HSET", 25.0)
        self.cryo_vacuum    = _reg("FRG1607-7-I21-01:vac:p",             1.5e-5)
        self.cryo_ln2       = _reg("BL1607-7-I21:Scale:701:fbk",         12.0)
        self.cryo_gflow_rbv = _reg("TC1607-7-01:C1:READ_TEMP_LOOP_FSET", 30.0)
        self.cryo_gflow_sp  = _reg("TC1607-7-01:C1:SET_TEMP_LOOP_FSET",  30.0)

        # ── Furnace ───────────────────────────────────────────────────────────
        self.furnace_temp = _reg("PV:FURNACE:TEMP:RBV",      25.0)
        self.furnace_sp   = _reg("PV:FURNACE:SP:RBV",        25.0)
        self.heat_rate    = _reg("PV:FURNACE:HEATRATE:RBV",  0.0)

        # ── Write → readback mirrors ──────────────────────────────────────────
        # Writing to the write PV immediately updates the readback for instant UI feedback.
        _mirrors: dict[str, str] = {
            # Mono energy
            "BL1607-I21:Mono:Energy:EV":      "BL1607-I21:Mono:Energy:EV:fbk",
            # Stages — write PV → read PV
            "SMTR1607-7-I21-16:mm":           "SMTR1607-7-I21-16:mm:fbk",
            "STR1607-7-I21-16:mm":            "SMTR1607-7-I21-16:mm:fbk",  # pvs.yaml typo
            "SMTR1607-7-I21-15:mm:sp":        "SMTR1607-7-I21-15:mm:sp",
            "SMTR1607-7-I21-19:deg":          "SMTR1607-7-I21-19:deg",
            "SMTR1607-7-I21-18:mm":           "SMTR1607-7-I21-18:mm",
            "MTR1607-7-I21-18:mm":            "SMTR1607-7-I21-18:mm",       # pvs.yaml typo
            "SMTR1607-7-I21-17:mm":           "SMTR1607-7-I21-17:mm",
            "MTR1607-7-I21-17:mm":            "SMTR1607-7-I21-17:mm",       # pvs.yaml typo
            "SMTR1607-7-I21-08:mm":           "SMTR1607-7-I21-08:mm:fbk",
            "SMTR1607-7-I21-09:mm":           "SMTR1607-7-I21-09:mm:fbk",
            "SMTR1607-7-I21-07:deg":          "SMTR1607-7-I21-07:deg:fbk",
            # Furnace
            "PV:FURNACE:SP":                  "PV:FURNACE:SP:RBV",
            "PV:FURNACE:HEATRATE:SP":         "PV:FURNACE:HEATRATE:RBV",
            # Cryostat setpoints
            "TC1607-7-01:C1:SET_TEMP_LOOP_TSET": "TC1607-7-01:C1:READ_TEMP_LOOP_TSET",
            "TC1607-7-01:C1:SET_TEMP_LOOP_ENAB": "TC1607-7-01:C1:READ_TEMP_LOOP_ENAB",
            "TC1607-7-01:C1:SET_TEMP_LOOP_FSET": "TC1607-7-01:C1:READ_TEMP_LOOP_FSET",
            # JJ slits (read=write same PV, self-mirror for instant feedback)
            "PSL1607-7-I21-01:Gap:mm":        "PSL1607-7-I21-01:Gap:mm",
            "PSL1607-7-I21-01:Center:mm":     "PSL1607-7-I21-01:Center:mm",
            "PSL1607-7-I21-02:Gap:mm":        "PSL1607-7-I21-02:Gap:mm",
            "PSL1607-7-I21-02:Center:mm":     "PSL1607-7-I21-02:Center:mm",
            # M1 pitch
            "PV:M1:PITCH:SP":                 "BL1607-I21:M1:Pitch:deg:fbk",
        }
        for write_pv, read_pv in _mirrors.items():
            wp = _reg(write_pv, 0)
            rp = _reg(read_pv)
            wp._cbs.append((-1, lambda pvname, value, _rp=rp, **kw:
                            _rp.set_sim_value(value) if value is not None else None))

        # ── No-op command stubs ───────────────────────────────────────────────
        for pv in (
            "SSH1607-5-I21-01:opr:open", "SSH1607-5-I21-01:opr:close",
            "PV:DBHR:M1:STOP", "PV:DBHR:M2:STOP", "PV:DBHR:PITCH:STOP",
            "PV:FURNACE:STOP",
            "PV:FE:SHUTTER:OPEN", "PV:FE:SHUTTER:CLOSE",
            "PV:CRYO:HOLDER:OPEN",
            "BL1607-I21:M2:Slit:mm",
            "PV:DWELL:TIME:SP",
        ):
            _reg(pv, 0)

        # ── Internal state ────────────────────────────────────────────────────
        self._t        = 0.0
        self._ln2_kg   = 12.0
        self._running  = False
        self._thread: threading.Thread | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self, interval: float = 1.0):
        self._interval = interval
        self._running  = True
        self._thread   = threading.Thread(
            target=self._loop, name="SimBeamline", daemon=True
        )
        self._thread.start()

    def stop(self):
        self._running = False

    # ── Update loop ───────────────────────────────────────────────────────────

    def _loop(self):
        while self._running:
            try:
                self._step()
            except Exception:
                pass
            time.sleep(self._interval)

    def _step(self):
        self._t += self._interval
        t = self._t

        # Ring current: sawtooth 200→185 mA over 5-min cycles then instant refill
        phase = t % 300.0
        rc = 200.0 - phase * 0.05 + random.gauss(0, 0.08)
        self.ring_current.set_sim_value(max(0.0, rc))

        # Mono energy: slow sine ±5 eV around 12000 eV
        energy = 12000.0 + 5.0 * math.sin(t * 0.015) + random.gauss(0, 0.05)
        self.mono_energy.set_sim_value(round(energy, 2))

        # Wiggler gap: tiny vibration
        self.wiggler_gap.set_sim_value(round(10.5 + random.gauss(0, 0.002), 4))

        # Cryostat temperature: first-order toward setpoint + noise
        tset = float(self.cryo_tset_sp._value)
        tcur = float(self.cryo_temp_rbv._value)
        tnew = tcur + (tset - tcur) * (self._interval / 30.0) + random.gauss(0, 0.05)
        self.cryo_temp_rbv.set_sim_value(round(tnew, 3))
        self.cryo_tset_rbv.set_sim_value(tset)
        self.cryo_ah_rbv.set_sim_value(self.cryo_ah_sp._value)

        # Heater power
        hp = max(0.0, min(100.0, 25.0 + abs(tnew - tset) * 2.0 + random.gauss(0, 0.4)))
        self.cryo_hpower.set_sim_value(round(hp, 2))

        # Cryostat vacuum
        vac = 1.5e-5 * math.exp(random.gauss(0, 0.05))
        self.cryo_vacuum.set_sim_value(vac)

        # LN2 weight: drains ~0.7 %/hr
        self._ln2_kg = max(0.0, self._ln2_kg - 0.00012 * self._interval)
        self.cryo_ln2.set_sim_value(self._ln2_kg + random.gauss(0, 0.005))

        # Gas flow
        gf = float(self.cryo_gflow_sp._value) + random.gauss(0, 0.15)
        self.cryo_gflow_rbv.set_sim_value(round(gf, 2))

        # Beam intensity envelope (slow modulation + flicker)
        beam = (
            1.0
            + 0.04 * math.sin(t * 0.008)
            + 0.01 * math.sin(t * 0.25)
        )

        # Pandabox IC VAL PVs — baseline 3e8 each so voltage ≈ 3 V → ~2e12 ph/s
        self.ic0_val.set_sim_value(max(0, round(3.0e8 * beam * math.exp(random.gauss(0, 0.012)))))
        self.ic1_val.set_sim_value(max(0, round(2.8e8 * beam * math.exp(random.gauss(0, 0.013)))))
        self.ic2_val.set_sim_value(max(0, round(2.5e8 * beam * math.exp(random.gauss(0, 0.015)))))
        self.pips_val.set_sim_value(max(0, round(0.5e8 * beam * math.exp(random.gauss(0, 0.02)))))

        # MCS scaler counts (correlated with beam, slightly different noise)
        self.mcs_ic0.set_sim_value(max(0, round(1_000_000 * beam * math.exp(random.gauss(0, 0.010)))))
        self.mcs_ic1.set_sim_value(max(0, round(  800_000 * beam * math.exp(random.gauss(0, 0.011)))))
        self.mcs_ic2.set_sim_value(max(0, round(  500_000 * beam * math.exp(random.gauss(0, 0.013)))))
        self.mcs_pips.set_sim_value(max(0, round( 100_000 * beam * math.exp(random.gauss(0, 0.018)))))

    # ── Interactive helpers ───────────────────────────────────────────────────

    def set_temperature_setpoint(self, kelvin: float):
        self.cryo_tset_sp.set_sim_value(kelvin)
        self.cryo_tset_rbv.set_sim_value(kelvin)

    def set_keithley_gain(self, ic: str, num: str, unit: str):
        """
        Set Keithley sensitivity for an IC.
        ic: "IC0" | "IC1" | "IC2" | "PIPS"
        num: sensitivity number string, e.g. "2", "10", "100"
        unit: "pA/V" | "nA/V" | "uA/V" | "mA/V"

        Example: sim.set_keithley_gain("IC0", "2", "uA/V")
        """
        snum_map  = {"IC0": self.ic0_snum,  "IC1": self.ic1_snum,
                     "IC2": self.ic2_snum,  "PIPS": self.pips_snum}
        sunit_map = {"IC0": self.ic0_sunit, "IC1": self.ic1_sunit,
                     "IC2": self.ic2_sunit, "PIPS": self.pips_sunit}
        if ic in snum_map:
            snum_map[ic].set_sim_value(num)
            sunit_map[ic].set_sim_value(unit)

    def trip_beam(self, duration: float = 5.0):
        """Simulate a beam trip: ring current → 0 for duration seconds."""
        def _trip():
            self.ring_current.set_sim_value(0.0)
            for pv in (self.ic0_val, self.ic1_val, self.ic2_val, self.pips_val):
                pv.set_sim_value(0.0)
            time.sleep(duration)
            self.ring_current.set_sim_value(199.5)
        threading.Thread(target=_trip, daemon=True).start()

    def close_shutter(self, duration: float = 3.0):
        """Close POE shutter (state → 1.0) for duration seconds."""
        def _close():
            self.poe_shutter.set_sim_value(1.0)
            time.sleep(duration)
            self.poe_shutter.set_sim_value(0.0)
        threading.Thread(target=_close, daemon=True).start()

    def inject_vacuum_burst(self, peak: float = 1e-3, duration: float = 10.0):
        """Simulate a cryostat vacuum burst."""
        def _burst():
            for i in range(int(duration)):
                frac = i / max(duration, 1)
                self.cryo_vacuum.set_sim_value(1.5e-5 + (peak - 1.5e-5) * math.exp(-5 * frac))
                time.sleep(1.0)
        threading.Thread(target=_burst, daemon=True).start()

    def status_report(self) -> str:
        v0 = self.ic0_val._value * self.ic0_gain._value * 1e-8
        lines = [
            f"t = {self._t:.0f} s",
            f"Ring current  : {self.ring_current._value:.2f} mA",
            f"Mono energy   : {self.mono_energy._value:.2f} eV",
            f"Cryo temp     : {self.cryo_temp_rbv._value:.2f} K  (SP {self.cryo_tset_sp._value:.1f} K)",
            f"Vacuum        : {self.cryo_vacuum._value:.2e} Torr",
            f"LN2           : {self._ln2_kg / 24.0 * 100.0:.1f} %",
            f"IC0 voltage   : {v0:.3f} V",
            f"IC0 VAL       : {self.ic0_val._value:.3e}",
        ]
        return "\n".join(lines)
