"""
Launch beamline_control.py with fully simulated PVs.

    python run_sim.py

The sim module patches epics.PV BEFORE the Qt app creates any PVBridge
objects, so every subscribe() call transparently receives a SimPV.

Interactive debugging
─────────────────────
The SimulatedBeamline instance is assigned to the global `sim` so you can
manipulate it from a Python console attached to the process, e.g.:

    sim.set_temperature_setpoint(120.0)   # ramp cryostat to 120 K
    sim.trip_beam(10.0)                   # 10-second beam trip
    sim.close_shutter(5.0)                # close POE shutter briefly
    sim.inject_vacuum_burst(1e-3, 15.0)  # vacuum burst event
    print(sim.status_report())            # print current values
"""
import sys

# ── 1. Patch epics BEFORE importing beamline_control ─────────────────────────
import sim_pvs
sim_pvs.patch_epics()

# ── 2. Create and start the simulated beamline ────────────────────────────────
sim = sim_pvs.SimulatedBeamline()
sim.start(interval=1.0)

# ── 3. Launch the Qt application ─────────────────────────────────────────────
from PyQt5.QtWidgets import QApplication
import beamline_control

app = QApplication(sys.argv)
app.setStyleSheet(beamline_control._qss())

window = beamline_control.BeamlineControlWindow()
window.show()

exit_code = app.exec_()
sim.stop()
sys.exit(exit_code)
