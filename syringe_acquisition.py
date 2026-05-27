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