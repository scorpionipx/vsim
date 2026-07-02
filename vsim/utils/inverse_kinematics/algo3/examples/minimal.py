"""
minimal.py — Smallest possible integration example.

Shows the whole contract: give the controller a pose, get servo angles back.
A host program (game telemetry, a motion cue generator, etc.) would call
``pose_to_angles(...)`` and forward the returned angles to the real platform.

Running this directly sweeps every axis through its full working envelope
(min → max → min) and prints the six servo angles at each step.

Run:
    python3 -m examples.minimal
"""

import math

from ..controller.controller import (
    StewartPlatformKinematics,
    build_default_config,
    waveform_triangle,
)

# Built lazily on first use (no work at import time), then reused for
# every pose.
_kin: StewartPlatformKinematics | None = None


def pose_to_angles(
    x: float, y: float, z: float,
    roll: float, pitch: float, yaw: float,
) -> tuple[int, list[float] | None]:
    """Convert a 6-DOF pose to six servo angles.

    Translations in mm, rotations in radians.
    Returns ``(status, angles_deg)``; ``status == 0`` means success and
    ``angles_deg`` holds six angles in degrees (``None`` on any error).
    """
    global _kin
    if _kin is None:
        _kin = StewartPlatformKinematics(build_default_config())
    _kin.x, _kin.y, _kin.z = x, y, z
    _kin.roll, _kin.pitch, _kin.yaw = roll, pitch, yaw
    return _kin.calc()


if __name__ == "__main__":
    # ---- Select which axes to sweep --------------------------------------
    # "all", a single axis ("x"), or a list (["x", "z", "yaw"]).
    # Order is preserved; invalid names are reported and skipped.
    RUN_AXES = "all"

    N_STEPS = 16  # sweep resolution (N_STEPS + 1 points per leg)
    # ----------------------------------------------------------------------

    # Per-axis sweep: drive each DOF from its min limit up to its max and back
    # (min → max → min), holding the other five DOFs at zero. The working
    # limits come straight from the config the controller was built with.
    cfg = build_default_config()

    # DOF name → (config limit key, angular?)
    AXES = {
        "x":     ("x_lim", False),
        "y":     ("y_lim", False),
        "z":     ("z_lim", False),
        "roll":  ("a_lim", True),
        "pitch": ("b_lim", True),
        "yaw":   ("c_lim", True),
    }

    if RUN_AXES == "all":
        selected = list(AXES)
    elif isinstance(RUN_AXES, str):
        selected = [RUN_AXES]
    else:
        selected = list(RUN_AXES)

    for dof in selected:
        if dof not in AXES:
            print(f"\n[skip] unknown axis {dof!r} "
                  f"(choose from: {', '.join(AXES)})")
            continue
        lim_key, is_angular = AXES[dof]
        lo, hi = cfg[lim_key]
        unit = "deg" if is_angular else "mm"
        disp = math.degrees if is_angular else (lambda v: v)

        print(f"\n=== {dof.upper()} sweep: "
              f"{disp(lo):+.2f} → {disp(hi):+.2f} → {disp(lo):+.2f} {unit} ===")

        for val in waveform_triangle(lo, hi, N_STEPS):
            pose = {"x": 0.0, "y": 0.0, "z": 0.0,
                    "roll": 0.0, "pitch": 0.0, "yaw": 0.0}
            pose[dof] = val
            status, angles = pose_to_angles(**pose)

            if status == 0:
                angle_str = "  ".join(f"{a:7.2f}" for a in angles)
                print(f"  {dof:5s} = {disp(val):+8.2f} {unit}  →  {angle_str}")
            else:
                print(f"  {dof:5s} = {disp(val):+8.2f} {unit}  →  [error {status}]")
