import sys
import run_dummy

from app import launch_app, DetectorConfig

print("Imports done")

try:
    det1 = DetectorConfig(name="Det1", prefix="ME4:", nmca=4)
    bl_control = DetectorConfig(name="BL Control", prefix="BL00:")
    print("Launching app...")
    launch_app([det1, bl_control], title="Xspress3 Viewer - Dummy Mode")
    print("App finished")
except Exception as e:
    import traceback
    traceback.print_exc()
