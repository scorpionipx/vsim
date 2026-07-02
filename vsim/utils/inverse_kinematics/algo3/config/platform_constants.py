"""
Stewart Platform Constants

Single source of truth for all platform physical parameters, servo limits,
DOF envelope limits, and 2D drawing layout constants.

Equivalent to a C/C++ header file — import from here, never redefine.
"""

import math

# =============================================================================
# SERVO [S] CONFIGURATION
# =============================================================================

NB_SERVOS: int = 6

# Servo angular range (radians)
SERVO_FULL_ANGULAR_RANGE: float = math.pi       # 180 degrees
SERVO_HALF_ANGULAR_RANGE: float = math.pi / 2   # 90 degrees

# Physical servo angular limits (degrees)
SERVO_MIN_DEG: float = 80.0    # Minimum servo arm angle
SERVO_MAX_DEG: float = 180.0   # Maximum servo arm angle

# =============================================================================
# PHYSICAL DIMENSIONS [ARM / ROD / PLATFORM / BASE] (mm)
# =============================================================================

ARM_LENGTH: float = 200.0      
ROD_LENGTH: float = 675.0

P_RAD: float      = 380.0    
B_RAD: float      = 676.22   


# =============================================================================
# JOINT ANGLE OFFSETS [P / B / S] (radians)
# =============================================================================

THETA_P: float = math.radians(7.56)   # Platform [P] joint angular offset
                                       # from symmetry axis
                                       # asin(50 / P_RAD) = atan(50 / 376.70)
                                       # 50 mm lateral offset, P_RAD = 380
THETA_B: float = math.radians(30)     # Base [B] servo pinion angular offset
                                       # from symmetry axis

# Servo [S] arm orientations relative to X axis (radians), one per servo
# Even-side motors: arm extends in +side direction → CCW tangential
#   (motor_angle + 90)
# Odd-side motors:  arm extends in -side direction → CW tangential
#   (motor_angle - 90)
THETA_S: list[float] = [
    math.radians(150),   # S0: Motor 3 at  60°, side 2 (even) →  60+90 = 150
    math.radians(-90),   # S1: Motor 2 at   0°, side 1 (odd)  →   0-90 = -90
    math.radians(30),    # S2: Motor 1 at 300°, side 0 (even) → 300+90 =  30
    math.radians(150),   # S3: Motor 6 at 240°, side 5 (odd)  → 240-90 = 150
    math.radians(-90),   # S4: Motor 5 at 180°, side 4 (even) → 180+90 = -90
    math.radians(30),    # S5: Motor 4 at 120°, side 3 (odd)  → 120-90 =  30
]

# Mapping from solver geometry index (the order of _P/_B/THETA_S above, 0..5) to
# the physical motor number (1..6) that geometry slot drives. Read straight off
# the THETA_S comments. The solver uses this to reorder its output so that
# angles[i] drives physical motor i+1 — without it, a pure translation command
# comes out as a rotation on the real platform (X→pitch, Y→roll, etc.).
#
# VERIFY ON HARDWARE: command one motor at a time from home and confirm which
# physical motor moves; if it differs, edit this list (it is the single source
# of truth for the output ordering).
GEOM_TO_MOTOR: list[int] = [3, 2, 1, 6, 5, 4]

# =============================================================================
# MEASURED PER-MOTOR MOUNTING DEVIATIONS
#
# The geometry above is the *ideal* symmetric frame (all base pivots at exactly
# B_RAD, all platform joints at exactly P_RAD/THETA_P, all coplanar). A real,
# hand-built platform deviates from that, and those deviations are what cause
# the cross-axis coupling (e.g. yaw inducing a Z rise) that a symmetric model
# cannot otherwise represent.
#
# Enter measured deviations here, one entry per PHYSICAL MOTOR (index 0 = Motor
# 1 ... index 5 = Motor 6). All default to 0.0 = ideal. The solver applies them
# on top of the symmetric layout (see _precompute_geometry).
#
#   *_RADIAL_MM : displacement of the pivot/joint along its own radius from the
#                 center, +outward / -inward (mm).
#   *_Z_MM      : vertical displacement, +up / -down (mm). Base Z is in the base
#                 frame; platform-joint Z is in the platform body frame (this is
#                 the joint non-coplanarity term).
#
# Example — the measured "Motor 3 mounted 15 mm toward the base center":
#   BASE_PIVOT_RADIAL_MM[2] = -15.0   # Motor 3 -> list index 2; inward -> negative
BASE_PIVOT_RADIAL_MM:     list[float] = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
BASE_PIVOT_Z_MM:          list[float] = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
PLATFORM_JOINT_RADIAL_MM: list[float] = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
PLATFORM_JOINT_Z_MM:      list[float] = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

# =============================================================================
# CHAIR MOUNTING ANGLE
# =============================================================================
# Angle the chair is bolted down rotated relative to the platform's native +Y
# forward (radians). Lets the chair clear the ball joints while the rider still
# commands in chair coordinates: the solver rotates surge/sway and roll/pitch
# by this angle into platform axes at the start of calc() (heave and yaw are on
# the Z axis the rotation spins about, so they are unaffected).
#   0        = chair aligned with the platform
#   positive = chair rotated CCW looking down +Z (use a negative value for CW)
MOUNT_ANGLE: float = math.radians(90)

# =============================================================================
# DOF WORKING ENVELOPE LIMITS [HX]
# Per-axis limits = 80% of each axis's single-axis maximum (where all six
# servos stay inside SERVO_MIN_DEG..SERVO_MAX_DEG).
#
# These are NOT hardcoded: they are recomputed from the geometry above on the
# first access each run (cached thereafter), so they can never go stale when
# ARM_LENGTH / ROD_LENGTH / P_RAD / B_RAD / THETA_P/B/S change. Access them as
# normal module attributes (HX_X_MIN … HX_C_MAX) — served lazily via
# __getattr__ below — or call dof_envelope() directly for the full dict.
# =============================================================================

# HX_* attribute name → (DOF name, index into the (neg, pos) envelope tuple).
_HX_ATTR_MAP: dict[str, tuple[str, int]] = {
    "HX_X_MIN": ("x", 0),     "HX_X_MAX": ("x", 1),
    "HX_Y_MIN": ("y", 0),     "HX_Y_MAX": ("y", 1),
    "HX_Z_MIN": ("z", 0),     "HX_Z_MAX": ("z", 1),
    "HX_A_MIN": ("roll", 0),  "HX_A_MAX": ("roll", 1),
    "HX_B_MIN": ("pitch", 0), "HX_B_MAX": ("pitch", 1),
    "HX_C_MIN": ("yaw", 0),   "HX_C_MAX": ("yaw", 1),
}

_ENVELOPE_CACHE: dict[str, tuple[float, float]] | None = None


def geometry_config() -> dict:
    """Geometry-only config dict (placeholder ±1e9 DOF limits).

    Single source for the physical geometry. ``build_default_config`` adds the
    computed envelope on top of this; ``dof_envelope`` feeds it to the solver
    with the limits overridden so the binary search can find the true maxima.
    """
    return {
        "nb_servos":                NB_SERVOS,
        "arm_length":               ARM_LENGTH,
        "rod_length":               ROD_LENGTH,
        "z_home":                   0.0,   # recomputed in the solver __init__
        "p_rad":                    P_RAD,
        "b_rad":                    B_RAD,
        "theta_p":                  THETA_P,
        "theta_b":                  THETA_B,
        "theta_s":                  THETA_S,
        "servo_half_angular_range": SERVO_HALF_ANGULAR_RANGE,
        "servo_min_deg":            SERVO_MIN_DEG,
        "servo_max_deg":            SERVO_MAX_DEG,
        "base_pivot_radial":        BASE_PIVOT_RADIAL_MM,
        "base_pivot_z":             BASE_PIVOT_Z_MM,
        "platform_joint_radial":    PLATFORM_JOINT_RADIAL_MM,
        "platform_joint_z":         PLATFORM_JOINT_Z_MM,
        "mount_angle":              MOUNT_ANGLE,
        "x_lim": (-1e9, 1e9), "y_lim": (-1e9, 1e9), "z_lim": (-1e9, 1e9),
        "a_lim": (-1e9, 1e9), "b_lim": (-1e9, 1e9), "c_lim": (-1e9, 1e9),
    }


def dof_envelope() -> dict[str, tuple[float, float]]:
    """Return the derated DOF envelope for the current geometry (cached).

    Maps each DOF name to a ``(neg, pos)`` tuple in mm (x/y/z) or rad
    (roll/pitch/yaw). Computed once per run on first call. Imports are deferred
    to avoid a circular import (the solver module imports from here).
    """
    global _ENVELOPE_CACHE
    if _ENVELOPE_CACHE is None:
        from .compute_envelope import compute_envelope, DERATE
        raw = compute_envelope(geometry_config())
        # Symmetric per-axis limits: derate the smaller of the two reachable
        # half-ranges so +max == -max (matches compute_envelope.print_envelope).
        _ENVELOPE_CACHE = {}
        for dof, (neg, pos) in raw.items():
            lim = min(abs(neg), abs(pos)) * DERATE
            _ENVELOPE_CACHE[dof] = (-lim, lim)
    return _ENVELOPE_CACHE


def __getattr__(name: str):
    """PEP 562 lazy attribute hook: serve HX_* from the computed envelope.

    Keeps ``from config.platform_constants import HX_X_MIN`` working for every
    consumer without hardcoding the values.
    """
    entry = _HX_ATTR_MAP.get(name)
    if entry is not None:
        dof, idx = entry
        return dof_envelope()[dof][idx]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# =============================================================================
# TEST / WAVEFORM DEFAULTS
# =============================================================================

N_STEPS: int = 64   # Default number of waveform steps
