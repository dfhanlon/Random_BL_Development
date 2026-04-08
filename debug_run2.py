import sys
import run_dummy

from app import launch_app, DetectorConfig
import traceback

def safe_launch():
    try:
        det1 = DetectorConfig(name="Det1", prefix="ME4:", nmca=4)
        bl_control = DetectorConfig(name="BL Control", prefix="BL00:")
        
        from app import build_app_classes
        _, _, AppCls = build_app_classes()
        class DebugApp(AppCls):
            def OnInit(self):
                print("App OnInit called")
                try:
                    if hasattr(self, "createApp"):
                        print("Calling createApp")
                        success = self.createApp()
                        if not success:
                            print("createApp returned False")
                        return success
                    else:
                        print("No createApp found")
                        return super().OnInit()
                except Exception as e:
                    traceback.print_exc()
                    return False
        
        app = DebugApp(detectors=[det1, bl_control])
        print("Starting MainLoop")
        app.MainLoop()
    except Exception as e:
        traceback.print_exc()

safe_launch()
