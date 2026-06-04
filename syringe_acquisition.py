import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
import numpy as np

# Configuration 

# Stepper geometry: one full 360-deg revolution = STEPS_PER_REV image positions.
# At 7.5 deg per acquisition position, that's 48 positions per syringe.
DEG_PER_STEP = 7.5
STEPS_PER_REV = int(round(360.0 / DEG_PER_STEP))

# Half-step coil energisation patterns for the unipolar stepper. These were
# calibrated by the lab buggy code - one cycle of all four patterns produces
# one 7.5-deg mechanical rotation. Reverse the list to invert direction.

COIL_PATTERNS_FORWARD = np.array([51, 102, 204, 153], dtype=np.uint8)
COIL_PATTERNS_REVERSE = COIL_PATTERNS_FORWARD[::-1]

# Timing. Tune these on the bench before a real dataset run.
SUBSTEP_DELAY_S = 0.02     # delay between the 4 coil patterns within one 7.5-deg cycle
SETTLE_DELAY_S = 0.30      # delay AFTER motor stops, BEFORE camera trigger (kills vibration blur)
TRIGGER_READY_TIMEOUT_MS = 1000  # how long to wait for the camera to be ready for the next trigger
GRAB_TIMEOUT_MS = 2000     # how long to wait for the image to come back after triggering

# Camera settings. LOCKED across the whole dataset 
# Tune these once with PylonViewer until images look good, then bake the numbers in here.
CAMERA_EXPOSURE_US = 8000      # microseconds. Adjust to your lighting.
CAMERA_GAIN_DB = 0.0           # dB. Keep as low as possible to minimise sensor noise.
CAMERA_PIXEL_FORMAT = "BayerRG8"  # acA800-510uc is a colour sensor. We keep the raw Bayer.
                                  # The converter below produces a viewable BGR8 image to save.

# Output. Each run creates a subfolder named <id>_<timestamp>.
OUTPUT_ROOT = Path("syringe_dataset")


# Imports for hardware - deferred so help works without DAQ/camera attached.


def _import_hardware():
    """Import hardware modules. Fails fast with a clear message if missing."""
    try:
        import y2daq  # noqa: F401
    except ImportError as e:
        sys.exit(f"y2daq module not found ({e}). Make sure y2daq.py is on the Python path.")
    try:
        from pypylon import pylon  # noqa: F401
    except ImportError as e:
        sys.exit(f"pypylon not found ({e}). Install with: pip install pypylon")
    try:
        import cv2  # noqa: F401
    except ImportError as e:
        sys.exit(f"opencv-python not found ({e}). Install with: pip install opencv-python")


# Motor control

class Stepper:
    """Wraps y2daq.digital() to drive one 7.5-deg rotation at a time."""

    def __init__(self):
        import y2daq
        self._dio = y2daq.digital()  # opens Dev1 port0 lines 0-7

    def step_once(self, direction="forward"):
        """Drive the motor through one 4-pattern cycle = one DEG_PER_STEP position."""
        patterns = (
            COIL_PATTERNS_FORWARD if direction == "forward" else COIL_PATTERNS_REVERSE
        )
        for pattern in patterns:
            self._dio.write(np.unpackbits(np.array([pattern], dtype=np.uint8)))
            time.sleep(SUBSTEP_DELAY_S)

    def close(self):
        """De-energise all coils so the motor isn't drawing current at rest."""
        try:
            self._dio.clear()
        except Exception as e:
            print(f"Warning: failed to clear digital lines on close: {e}")

# Camera control

class Camera:
    """Wraps a Basler camera in software-trigger mode for deterministic single-frame capture."""

    def __init__(self):
        from pypylon import pylon
        self._pylon = pylon

        tl_factory = pylon.TlFactory.GetInstance()
        devices = tl_factory.EnumerateDevices()
        if not devices:
            raise RuntimeError("No Basler camera detected. Check USB connection and power.")

        self._cam = pylon.InstantCamera(tl_factory.CreateFirstDevice())
        self._cam.Open()

        print(f"Camera opened: {self._cam.GetDeviceInfo().GetModelName()} "
              f"(SN {self._cam.GetDeviceInfo().GetSerialNumber()})")
        
        # Lock settings

        # Pixel format first (changes available range of other parameters).
        self._cam.PixelFormat.SetValue(CAMERA_PIXEL_FORMAT)

        # Disable any auto-adjustment that might be on by default.
        for node_name in ("ExposureAuto", "GainAuto", "BalanceWhiteAuto"):
            try:
                getattr(self._cam, node_name).SetValue("Off")
            except Exception:
                pass  
        
        # Exposure. acA800 uses ExposureTime in microseconds (float).
        try:
            self._cam.ExposureTime.SetValue(float(CAMERA_EXPOSURE_US))
        except Exception:
            # Older firmware exposes ExposureTimeAbs instead.
            self._cam.ExposureTimeAbs.SetValue(float(CAMERA_EXPOSURE_US))

        # Gain. acA800 uses Gain in dB (float).
        try:
            self._cam.Gain.SetValue(float(CAMERA_GAIN_DB))
        except Exception:
            self._cam.GainRaw.SetValue(int(CAMERA_GAIN_DB))

        # Software trigger configuration 

        # Replaces all currently registered configuration handlers and sets up
        # the camera so that frames are only captured when we explicitly trigger.
        self._cam.RegisterConfiguration(
            pylon.SoftwareTriggerConfiguration(),
            pylon.RegistrationMode_ReplaceAll,
            pylon.Cleanup_Delete,
        )

        # Image converter: Bayer raw = BGR8 for saving with OpenCV 
        self._converter = pylon.ImageFormatConverter()
        self._converter.OutputPixelFormat = pylon.PixelType_BGR8packed
        self._converter.OutputBitAlignment = pylon.OutputBitAlignment_MsbAligned

        # Start the grab loop. We feed it triggers manually via trigger_and_grab().
        self._cam.StartGrabbing(pylon.GrabStrategy_OneByOne)
    
    def trigger_and_grab(self):
        """Trigger one frame, wait for it, return as a numpy BGR image."""
        pylon = self._pylon
        if not self._cam.WaitForFrameTriggerReady(TRIGGER_READY_TIMEOUT_MS,
                                                  pylon.TimeoutHandling_ThrowException):
            raise RuntimeError("Camera did not become ready for trigger in time.")
        self._cam.ExecuteSoftwareTrigger()

        result = self._cam.RetrieveResult(GRAB_TIMEOUT_MS, pylon.TimeoutHandling_ThrowException)
        try:
            if not result.GrabSucceeded():
                raise RuntimeError(f"Grab failed: {result.ErrorCode} {result.ErrorDescription}")
            converted = self._converter.Convert(result)
            img = converted.GetArray()  # H x W x 3, BGR, uint8
        finally:
            result.Release()
        return img
    
    def get_settings(self):
        """Return a dict of the locked settings, for the sidecar metadata."""
        def safe(getter):
            try:
                return getter()
            except Exception:
                return None

        info = self._cam.GetDeviceInfo()
        return {
            "model": info.GetModelName(),
            "serial": info.GetSerialNumber(),
            "pixel_format": safe(lambda: self._cam.PixelFormat.GetValue()),
            "exposure_us": safe(lambda: self._cam.ExposureTime.GetValue()),
            "gain_db": safe(lambda: self._cam.Gain.GetValue()),
            "width": safe(lambda: self._cam.Width.GetValue()),
            "height": safe(lambda: self._cam.Height.GetValue()),
        }

    def close(self):
        try:
            self._cam.StopGrabbing()
        except Exception:
            pass
        try:
            self._cam.Close()
        except Exception:
            pass

# Acquisition routine

def run_acquisition(syringe_id, label, notes, direction="forward"):
    """Capture one full revolution for one syringe. Returns the output directory."""
    import cv2

    if not syringe_id or not syringe_id.replace("_", "").replace("-", "").isalnum():
        raise ValueError("syringe_id must be non-empty alphanumeric (underscores/dashes allowed)")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = OUTPUT_ROOT / f"{syringe_id}_{timestamp}"
    out_dir.mkdir(parents=True, exist_ok=False)
    print(f"Output directory: {out_dir}")

    stepper = None
    camera = None
    captured_files = []
    try:
        stepper = Stepper()
        camera = Camera()

        # Warm-up grab: the first frame after StartGrabbing can have stale
        # exposure settings. Trigger once and discard.
        try:
            _ = camera.trigger_and_grab()
            print("Warm-up frame captured and discarded.")
        except Exception as e:
            print(f"Warm-up grab failed (continuing anyway): {e}")

        print(f"Starting {STEPS_PER_REV}-frame capture for syringe '{syringe_id}'...")
        t_start = time.time()

        for i in range(STEPS_PER_REV):
            angle_deg = i * DEG_PER_STEP

            # The first frame (i=0) is captured at the starting position before
            # any movement, so we move AFTER capture, not before.
            time.sleep(SETTLE_DELAY_S)
            img = camera.trigger_and_grab()

            fname = f"{syringe_id}_{label}_a{angle_deg:05.1f}_{i:03d}.png"
            fpath = out_dir / fname
            # PNG with no compression for sub-pixel defect fidelity.
            cv2.imwrite(str(fpath), img, [cv2.IMWRITE_PNG_COMPRESSION, 0])
            captured_files.append(fname)
            print(f"  [{i+1:2d}/{STEPS_PER_REV}] angle={angle_deg:5.1f} deg -> {fname}")

            # Move to the next angular position (unless this was the last frame).
            if i < STEPS_PER_REV - 1:
                stepper.step_once(direction=direction)

        elapsed = time.time() - t_start
        print(f"Capture complete in {elapsed:.1f} s ({elapsed / STEPS_PER_REV:.2f} s/frame).")

        # Sidecar metadata: everything needed to reproduce or audit this run.
        metadata = {
            "syringe_id": syringe_id,
            "label": label,
            "notes": notes or "",
            "timestamp": timestamp,
            "direction": direction,
            "deg_per_step": DEG_PER_STEP,
            "steps_per_revolution": STEPS_PER_REV,
            "substep_delay_s": SUBSTEP_DELAY_S,
            "settle_delay_s": SETTLE_DELAY_S,
            "camera": camera.get_settings(),
            "files": captured_files,
        }
        with open(out_dir / "metadata.json", "w") as f:
            json.dump(metadata, f, indent=2)
        print(f"Wrote {out_dir / 'metadata.json'}")

        return out_dir

    finally:
        if camera is not None:
            camera.close()
        if stepper is not None:
            stepper.close()

# Optional GUI mode (for rig calibration and one-off tests).


def run_gui():
    """Minimal GUI: enter ID + label, click Capture, watch the progress in the terminal."""
    import tkinter as tk
    from tkinter import messagebox, ttk

    root = tk.Tk()
    root.title("Syringe acquisition")
    root.geometry("360x240")

    frm = ttk.Frame(root, padding=12)
    frm.pack(fill="both", expand=True)

    ttk.Label(frm, text="Syringe ID:").grid(row=0, column=0, sticky="w")
    id_var = tk.StringVar(value="SYR_001")
    ttk.Entry(frm, textvariable=id_var, width=24).grid(row=0, column=1, pady=4)

    ttk.Label(frm, text="Label:").grid(row=1, column=0, sticky="w")
    label_var = tk.StringVar(value="good")
    ttk.Combobox(
        frm, textvariable=label_var, width=22,
        values=["good", "defect_flash", "defect_print", "defect_surface", "defect_warp"],
    ).grid(row=1, column=1, pady=4)

    ttk.Label(frm, text="Notes:").grid(row=2, column=0, sticky="w")
    notes_var = tk.StringVar(value="")
    ttk.Entry(frm, textvariable=notes_var, width=24).grid(row=2, column=1, pady=4)

    ttk.Label(frm, text="Direction:").grid(row=3, column=0, sticky="w")
    direction_var = tk.StringVar(value="forward")
    ttk.Combobox(
        frm, textvariable=direction_var, width=22,
        values=["forward", "reverse"], state="readonly",
    ).grid(row=3, column=1, pady=4)

    status_var = tk.StringVar(value="Ready.")
    ttk.Label(frm, textvariable=status_var, foreground="gray").grid(
        row=5, column=0, columnspan=2, pady=8
    )

    
    def on_capture():
        status_var.set("Capturing... see terminal for progress.")
        root.update_idletasks()
        try:
            out_dir = run_acquisition(
                syringe_id=id_var.get().strip(),
                label=label_var.get().strip(),
                notes=notes_var.get().strip(),
                direction=direction_var.get(),
            )
            status_var.set(f"Done. Saved to {out_dir}")
            messagebox.showinfo("Capture complete", f"Saved {STEPS_PER_REV} images to:\n{out_dir}")
        except Exception as e:
            status_var.set(f"Error: {e}")
            messagebox.showerror("Capture failed", str(e))

    ttk.Button(frm, text=f"Capture {STEPS_PER_REV} frames", command=on_capture).grid(
        row=4, column=0, columnspan=2, pady=10
    )

    root.mainloop()
