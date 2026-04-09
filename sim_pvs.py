"""
Simulated PV layer for offline testing of beamline_control.py.

Usage (from run_sim.py):
    import sim_pvs
    sim_pvs.patch_epics()          # must happen before any epics.PV calls
    sim = sim_pvs.SimulatedBeamline()
    sim.start()                    # starts background update thread
    # ... launch Qt app ...
    sim.stop()
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
    Drop-in replacement for epics.PV that holds a value in-process.
    Call set_sim_value() from the simulation thread to push updates to
    all subscribers, just like a real PV monitor callback.
    """

    def __init__(self, pvname: str, initial=0.0):
        self.pvname   = pvname
        self._value   = initial
        self._cbs: list[tuple[int, object]] = []
        self._idx     = 0
        self.connected = True
        self.type      = "ctrl_double"
        self.count     = 1

    # ── epics.PV interface ────────────────────────────────────────────────────

    def connect(self, _timeout=None):            return True
    def wait_for_connection(self, _timeout=None): return True
    def get_ctrlvars(self):                       return {}

    def get(self, _timeout=None, as_string=False, **kw):
        return str(self._value) if as_string else self._value

    def put(self, value, _wait=False, **kw):
        """Respond to writes from the UI (e.g. setpoints)."""
        self._value = value
        self._fire(value)
        return 1

    def add_callback(self, callback=None, **kw):
        if callback is None:
            return None
        idx = self._idx
        self._idx += 1
        self._cbs.append((idx, callback))
        # Emit current value immediately so the widget populates on startup
        threading.Timer(0.05, lambda: callback(
            pvname=self.pvname, value=self._value,
            char_value=str(self._value), charvalue=str(self._value),
        )).start()
        return idx

    def remove_callback(self, index=None, **kw):
        self._cbs = [(i, cb) for i, cb in self._cbs if i != index]

    # ── Simulation interface ──────────────────────────────────────────────────

    def set_sim_value(self, value):
        """Push a new value and notify all subscribers."""
        self._value = value
        self._fire(value)

    def _fire(self, value):
        for _, cb in list(self._cbs):
            try:
                cb(pvname=self.pvname, value=value,
                   char_value=str(value), charvalue=str(value))
            except Exception:
                pass


# ── Registry ──────────────────────────────────────────────────────────────────

_REGISTRY: dict[str, SimPV] = {}


def _reg(pvname: str, initial=0.0) -> SimPV:
    p = SimPV(pvname, initial)
    _REGISTRY[pvname] = p
    return p


def get_sim_pv(pvname: str, **kwargs) -> SimPV:
    """Return the SimPV for pvname, or a new stub if unknown. Accepts and ignores epics.PV kwargs."""
    if pvname not in _REGISTRY:
        _REGISTRY[pvname] = SimPV(pvname, 0.0)
    return _REGISTRY[pvname]


def patch_epics():
    """
    Replace epics.PV (and related callables) with SimPV factory functions
    so that PVBridge.subscribe() transparently gets simulated PVs.
    Must be called before beamline_control is imported or instantiated.
    """
    import sys

    try:
        import epics as _epics
    except ModuleNotFoundError:
        _epics = types.ModuleType("epics")
        sys.modules["epics"] = _epics

    _epics.PV      = get_sim_pv
    _epics.get_pv  = get_sim_pv
    _epics.caget   = lambda pvname, **kw: get_sim_pv(pvname).get()
    _epics.caput   = lambda pvname, value, **kw: get_sim_pv(pvname).put(value)

    return _epics


# ── Simulated beamline ────────────────────────────────────────────────────────

class SimulatedBeamline:
    """
    Creates SimPV instances for every PV in pvs.yaml and runs a background
    thread that updates them with realistic, slowly-varying values.

    Physics summary
    ───────────────
    Ring current   : sawtooth decay 200 → ~185 mA over 5 min, then refill
    Mono energy    : slow sine wobble ±0.5 eV around 7112 eV (Fe K-edge)
    Cryostat temp  : tracks setpoint with ±0.2 K thermal noise
    Cryostat vacuum: ~1.5e-5 Torr with log-normal fluctuation
    Heater power   : proportional response to temp error + noise
    LN2 weight     : drains ~0.7 % per hour (settable)
    Ion chambers   : correlated lognormal counts tracking beam intensity
                     IC0 > IC1 > IC2 >> PIPS (typical transmission chain)
    """

    def __init__(self):
        # ── Status strip ──────────────────────────────────────────────────────
        self.ring_current  = _reg("PCT1402-01:mA:fbk",             199.5)
        self.fe_shutter    = _reg("IPSH1407-I00-02:state",         "OPEN")
        self.valves        = _reg("PLC2406-7-03:BL:ready:in",      "OK")
        self.motor_il      = _reg("SWES1607-7-01:Em:Off",          "OK")
        self.wiggler_gap   = _reg("WIG1407-01:gap:mm:fbk",         10.5)
        self.fe_safety     = _reg("SSH1407-I00-01:state",          "OPEN")
        self.m1_blade      = _reg("SMTR1607-5-I21-08:mm:fbk",      1.5)
        self.mono_mask_1   = _reg("SMTR1607-5-I21-09:mm:fbk",      2.0)
        self.mono_mask_2   = _reg("SMTR1607-5-I21-10:mm:fbk",      1.5)
        self.m2_paddle     = _reg("SMTR1607-5-I21-28:mm:sp",       20.0)
        self.poe_shutter   = _reg("SSH1607-5-I21-01:state",        "OPEN")
        self.ps_701_a      = _reg("PS1607-701:A:voltage",          1900.0)
        self.ps_701_b      = _reg("PS1607-701:B:voltage",          1900.0)
        self.ps_702_a      = _reg("PS1607-702:A:voltage",          1900.0)
        self.filt_250      = _reg("PFIL1607-7-I21-01:control",     0)
        self.filt_500      = _reg("PFIL1607-7-I21-02:control",     0)
        self.filt_1000     = _reg("PFIL1607-7-I21-03:control",     0)
        self.filt_10000    = _reg("PFIL1607-7-I21-04:control",     0)

        # ── Controls ─────────────────────────────────────────────────────────
        self.mono_energy   = _reg("BL1607-I21:Mono:Energy:EV:fbk", 7112.0)
        self.dwell_time    = _reg("MCS1607-701:mcs:delay",          1000.0)
        self.stage_z       = _reg("BL1607-I21:Cryo:Z:mm:fbk",      0.0)
        self.furnace_temp  = _reg("PV:FURNACE:TEMP:RBV",            25.0)
        self.heat_rate     = _reg("PV:FURNACE:HEATRATE:RBV",        0.0)
        self.m1_pitch      = _reg("BL1607-I21:M1:Pitch:deg:fbk",   2.74)
        self.jj_vgap       = _reg("PSL1607-7-I21-01:Gap:mm",        2.0)
        self.jj_vcen       = _reg("PSL1607-7-I21-01:Center:mm",     0.0)
        self.jj_hgap       = _reg("PSL1607-7-I21-02:Gap:mm",        10.0)
        self.jj_hcen       = _reg("PSL1607-7-I21-02:Center:mm",     0.0)
        self.stage_y       = _reg("BL1607-I21:Cryo:Y:mm:fbk",      0.0)
        self.dbhr_m1       = _reg("SMTR1607-7-I21-08:mm:fbk",      0.0)
        self.dbhr_m2       = _reg("SMTR1607-7-I21-09:mm:fbk",      0.0)
        self.dbhr_pitch    = _reg("SMTR1607-7-I21-07:deg:fbk",     0.0)

        # ── Write → readback mirrors ──────────────────────────────────────────
        # When a setpoint PV is written the corresponding readback PV updates
        # immediately, giving instant feedback in the UI.
        _mirrors: dict[str, str] = {
            "BL1607-I21:Mono:Energy:EV":   "BL1607-I21:Mono:Energy:EV:fbk",
            "PV:DWELL:TIME:SP":            "MCS1607-701:mcs:delay",
            "PV:STAGE:Z:SP":               "BL1607-I21:Cryo:Z:mm:fbk",
            "PV:STAGE:Y:SP":               "BL1607-I21:Cryo:Y:mm:fbk",
            "PV:FURNACE:HEATRATE:SP":      "PV:FURNACE:HEATRATE:RBV",
            "PV:FURNACE:SP":               "PV:FURNACE:SP:RBV",
            "PV:M1:PITCH:SP":              "BL1607-I21:M1:Pitch:deg:fbk",
            "PV:JJ:VGAP:SP":              "PSL1607-7-I21-01:Gap:mm",
            "PV:JJ:VCEN:SP":              "PSL1607-7-I21-01:Center:mm",
            "PV:JJ:HGAP:SP":              "PSL1607-7-I21-02:Gap:mm",
            "PV:JJ:HCEN:SP":              "PSL1607-7-I21-02:Center:mm",
            "PV:DBHR:M1:SP":              "SMTR1607-7-I21-08:mm:fbk",
            "PV:DBHR:M2:SP":              "SMTR1607-7-I21-09:mm:fbk",
            "PV:DBHR:PITCH:SP":           "SMTR1607-7-I21-07:deg:fbk",
        }
        for write_pv, read_pv in _mirrors.items():
            wp = _reg(write_pv, 0)
            rp = _reg(read_pv)           # already registered above; fetches existing
            # capture by value in the closure
            wp.add_callback(lambda pvname, value, _rp=rp, **kw: _rp.set_sim_value(value)
                            if value is not None else None)

        # Remaining write-only stubs with no readback to mirror
        for pvname in (
            "PV:CRYO:HOLDER:RBV", "PV:CRYO:HOLDER:SP",
            "PV:CRYO:VALVE:RBV",  "PV:CRYO:VALVE:SP",
            "PV:DBHR:M1:STOP", "PV:DBHR:M2:STOP", "PV:DBHR:PITCH:STOP",
            "PV:FURNACE:STOP",
            "PV:FE:SHUTTER:OPEN", "PV:FE:SHUTTER:CLOSE",
            "PV:CRYO:HOLDER:OPEN",
            "PV:IC0:GAIN:DOWN", "PV:IC0:GAIN:UP",
            "PV:IC1:GAIN:DOWN", "PV:IC1:GAIN:UP",
            "PV:IC2:GAIN:DOWN", "PV:IC2:GAIN:UP",
            "PV:PIPS:GAIN:DOWN","PV:PIPS:GAIN:UP",
        ):
            _reg(pvname, 0)

        # ── Cryostat ─────────────────────────────────────────────────────────
        # Setpoint PVs are writable from the UI; we read them back in the loop
        self.cryo_temp_rbv  = _reg("TC1607-7-01:T1:READ_TEMP_SIG_TEMP",   85.0)
        self.cryo_tset_rbv  = _reg("TC1607-7-01:C1:READ_TEMP_LOOP_TSET",  85.0)
        self.cryo_tset_sp   = _reg("TC1607-7-01:C1:SET_TEMP_LOOP_TSET",   85.0)
        self.cryo_ah_rbv    = _reg("TC1607-7-01:C1:READ_TEMP_LOOP_ENAB",  1)
        self.cryo_ah_sp     = _reg("TC1607-7-01:C1:SET_TEMP_LOOP_ENAB",   1)
        self.cryo_hpower    = _reg("TC1607-7-01:C1:READ_TEMP_LOOP_HSET",  25.0)
        self.cryo_vacuum    = _reg("FRG1607-7-I21-01:vac:p",              1.5e-5)
        self.cryo_ln2       = _reg("BL1607-7-I21:Scale:701:fbk",          12.0)   # raw kg; /24*100 = 50 %
        self.cryo_gflow_rbv = _reg("TC1607-7-01:C1:READ_TEMP_LOOP_FSET",  30.0)
        self.cryo_gflow_sp  = _reg("TC1607-7-01:C1:SET_TEMP_LOOP_FSET",   30.0)

        # ── Ion chambers ─────────────────────────────────────────────────────
        self.ic0   = _reg("MCS1607-701:mcs16:fbk",  1_000_000.0)
        self.ic1   = _reg("MCS1607-701:mcs17:fbk",    800_000.0)
        self.ic2   = _reg("MCS1607-701:mcs18:fbk",    400_000.0)
        self.pips  = _reg("MCS1607-701:mcs19:fbk",    100_000.0)

        self.ic0_snum   = _reg("A1607-701:sens_num",  "0")
        self.ic0_sunit  = _reg("A1607-701:sens_unit", "nA/V")
        self.ic1_snum   = _reg("A1607-702:sens_num",  "0")
        self.ic1_sunit  = _reg("A1607-702:sens_unit", "nA/V")
        self.ic2_snum   = _reg("A1607-703:sens_num",  "0")
        self.ic2_sunit  = _reg("A1607-703:sens_unit", "nA/V")
        self.pips_snum  = _reg("A1607-704:sens_num",  "0")
        self.pips_sunit = _reg("A1607-704:sens_unit", "nA/V")

        # Internal sim state
        self._t       = 0.0          # seconds elapsed
        self._ln2_kg  = 12.0         # tracks draining LN2
        self._running = False
        self._thread: threading.Thread | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self, interval: float = 1.0):
        """Start the background update thread."""
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

        # ── Ring current: sawtooth decay over 5-min cycles, then instant refill
        phase = t % 300.0                                        # 0-300 s
        rc = 200.0 - phase * 0.05 + random.gauss(0, 0.08)       # ~200 → ~185 mA
        self.ring_current.set_sim_value(max(0.0, rc))

        # ── Mono energy: slow sine ±0.5 eV around 7112 eV
        energy = 7112.0 + 0.5 * math.sin(t * 0.015) + random.gauss(0, 0.03)
        self.mono_energy.set_sim_value(energy)

        # ── Wiggler gap: tiny vibration
        self.wiggler_gap.set_sim_value(10.5 + random.gauss(0, 0.002))

        # ── Cryostat temperature: first-order response toward setpoint + noise
        tset = float(self.cryo_tset_sp._value)
        tcur = float(self.cryo_temp_rbv._value)
        tau  = 30.0                                              # thermal time constant (s)
        tnew = tcur + (tset - tcur) * (self._interval / tau)
        tnew += random.gauss(0, 0.05)
        self.cryo_temp_rbv.set_sim_value(round(tnew, 3))
        self.cryo_tset_rbv.set_sim_value(tset)

        # Mirror auto-heat enable readback from setpoint
        self.cryo_ah_rbv.set_sim_value(self.cryo_ah_sp._value)

        # ── Heater power: proportional to |ΔT| + noise
        delta_t = abs(tnew - tset)
        hp = max(0.0, min(100.0, 25.0 + delta_t * 2.0 + random.gauss(0, 0.4)))
        self.cryo_hpower.set_sim_value(round(hp, 2))

        # ── Cryostat vacuum: log-normal fluctuation around 1.5e-5 Torr
        vac = 1.5e-5 * math.exp(random.gauss(0, 0.05))
        self.cryo_vacuum.set_sim_value(vac)

        # ── LN2 weight: drain ~0.7 % per hour = ~0.00012 kg/s raw
        self._ln2_kg = max(0.0, self._ln2_kg - 0.00012 * self._interval)
        self.cryo_ln2.set_sim_value(self._ln2_kg + random.gauss(0, 0.005))

        # ── Gas flow: track setpoint with noise
        gf_set = float(self.cryo_gflow_sp._value)
        gf = gf_set + random.gauss(0, 0.15)
        self.cryo_gflow_rbv.set_sim_value(round(gf, 2))

        # ── Ion chambers: correlated lognormal counts
        # Beam intensity modulation: slow sine + faster flicker
        beam = (
            1.0
            + 0.04 * math.sin(t * 0.008)     # slow ~130 s modulation
            + 0.01 * math.sin(t * 0.25)       # faster flicker
        )
        ic0  = max(0.0, 1_000_000.0 * beam * math.exp(random.gauss(0, 0.015)))
        ic1  = max(0.0,   800_000.0 * beam * math.exp(random.gauss(0, 0.015)))
        ic2  = max(0.0,   400_000.0 * beam * math.exp(random.gauss(0, 0.018)))
        pips = max(0.0,   100_000.0 * beam * math.exp(random.gauss(0, 0.025)))
        self.ic0.set_sim_value(round(ic0))
        self.ic1.set_sim_value(round(ic1))
        self.ic2.set_sim_value(round(ic2))
        self.pips.set_sim_value(round(pips))

    # ── Convenience helpers for interactive debugging ─────────────────────────

    def set_temperature_setpoint(self, kelvin: float):
        """Change the cryostat temperature setpoint (mirrors UI write)."""
        self.cryo_tset_sp.set_sim_value(kelvin)
        self.cryo_tset_rbv.set_sim_value(kelvin)

    def trip_beam(self, duration: float = 5.0):
        """Simulate a beam trip: ring current → 0 for `duration` seconds."""
        def _trip():
            self.ring_current.set_sim_value(0.0)
            time.sleep(duration)
            self.ring_current.set_sim_value(199.5)
        threading.Thread(target=_trip, daemon=True).start()

    def close_shutter(self, duration: float = 3.0):
        """Close POE shutter for `duration` seconds."""
        def _close():
            self.poe_shutter.set_sim_value("CLOSED")
            time.sleep(duration)
            self.poe_shutter.set_sim_value("OPEN")
        threading.Thread(target=_close, daemon=True).start()

    def inject_vacuum_burst(self, peak: float = 1e-3, duration: float = 10.0):
        """Simulate a vacuum burst in the cryostat."""
        def _burst():
            steps = int(duration)
            for i in range(steps):
                frac = i / steps
                v = 1.5e-5 + (peak - 1.5e-5) * math.exp(-5 * frac)
                self.cryo_vacuum.set_sim_value(v)
                time.sleep(1.0)
        threading.Thread(target=_burst, daemon=True).start()

    def status_report(self) -> str:
        """Print a quick summary of current simulated values."""
        lines = [
            f"t = {self._t:.0f} s",
            f"Ring current : {self.ring_current._value:.2f} mA",
            f"Mono energy  : {self.mono_energy._value:.3f} eV",
            f"Cryo temp    : {self.cryo_temp_rbv._value:.2f} K  (SP {self.cryo_tset_sp._value:.1f} K)",
            f"Vacuum       : {self.cryo_vacuum._value:.2e} Torr",
            f"LN2          : {self._ln2_kg / 24.0 * 100.0:.1f} %",
            f"IC0          : {self.ic0._value:,.0f}  counts",
            f"IC1          : {self.ic1._value:,.0f}  counts",
            f"IC2          : {self.ic2._value:,.0f}  counts",
            f"PIPS         : {self.pips._value:,.0f}  counts",
        ]
        return "\n".join(lines)
