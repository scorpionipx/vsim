"""
Tests for StewartPlatformKinematics (controller/controller.py).

Run:
    python3 -m tests.test_kinematics

Exits 0 when all tests pass, 1 otherwise.
"""

import math
import random
import sys

from ..config.compute_envelope import DERATE, compute_envelope
from ..config.platform_constants import (
    HX_X_MIN, HX_X_MAX, HX_Y_MIN, HX_Y_MAX, HX_Z_MIN, HX_Z_MAX,
    HX_A_MIN, HX_A_MAX, HX_B_MIN, HX_B_MAX, HX_C_MIN, HX_C_MAX,
    GEOM_TO_MOTOR,
)
from ..controller.controller import (
    StewartPlatformKinematics,
    build_default_config,
)


_ANGULAR = ("roll", "pitch", "yaw")

# (dof_name, min_limit, max_limit) for every working-envelope axis
_DOF_LIMITS: list[tuple[str, float, float]] = [
    ("x",     HX_X_MIN, HX_X_MAX),
    ("y",     HX_Y_MIN, HX_Y_MAX),
    ("z",     HX_Z_MIN, HX_Z_MAX),
    ("roll",  HX_A_MIN, HX_A_MAX),
    ("pitch", HX_B_MIN, HX_B_MAX),
    ("yaw",   HX_C_MIN, HX_C_MAX),
]


def _label(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def _zero_pose(kin: StewartPlatformKinematics) -> None:
    kin.x = kin.y = kin.z = 0.0
    kin.roll = kin.pitch = kin.yaw = 0.0


def _disp(dof: str, value: float) -> str:
    return f"{math.degrees(value):+.2f}°" if dof in _ANGULAR else f"{value:+.1f} mm"


def _build_unlimited() -> StewartPlatformKinematics:
    """A solver instance with the DOF clamps removed, so out-of-envelope
    poses reach the reachability / servo-limit checks instead of being
    silently clamped first."""
    cfg = build_default_config()
    cfg.update({
        "x_lim": (-1e9, 1e9), "y_lim": (-1e9, 1e9), "z_lim": (-1e9, 1e9),
        "a_lim": (-1e9, 1e9), "b_lim": (-1e9, 1e9), "c_lim": (-1e9, 1e9),
    })
    return StewartPlatformKinematics(cfg)


def _build_ideal() -> StewartPlatformKinematics:
    """Solver on the *ideal* symmetric frame, with any as-built per-motor
    mounting deviations (configured in platform_constants) zeroed out.

    The home-position and heave-symmetry invariants below are properties of a
    perfectly symmetric platform, so they must be checked against the ideal
    geometry — not the real, intentionally-asymmetric as-built frame."""
    cfg = build_default_config()
    n = cfg["nb_servos"]
    cfg.update({
        "base_pivot_radial":     [0.0] * n,
        "base_pivot_z":          [0.0] * n,
        "platform_joint_radial": [0.0] * n,
        "platform_joint_z":      [0.0] * n,
    })
    return StewartPlatformKinematics(cfg)


# ------------------------------------------------------------------
# 1. Home pose — all servos at midpoint
# ------------------------------------------------------------------
def test_home(kin: StewartPlatformKinematics) -> bool:
    """All DOFs zero → every servo at midpoint (130°)."""
    _zero_pose(kin)
    status, angles = kin.calc()
    mid = (kin.servo_min_deg + kin.servo_max_deg) / 2.0
    ok = status == 0 and all(abs(a - mid) < 0.01 for a in angles)
    print(f"  [{_label(ok)}] home → status={status}, "
          f"angles={[round(a, 2) for a in (angles or [])]}")
    return ok


# ------------------------------------------------------------------
# 2. Single-DOF — set one DOF, keep others at zero
# ------------------------------------------------------------------
def test_single_dof(
    kin: StewartPlatformKinematics,
    dof: str,
    value: float,
) -> bool:
    _zero_pose(kin)
    setattr(kin, dof, value)
    status, angles = kin.calc()
    ok = status == 0 and angles is not None
    ang_str = (", ".join(f"{a:+.2f}" for a in angles) if angles else "None")
    print(f"  [{_label(ok)}] {dof:>5s}={_disp(dof, value):>10s} → [{ang_str}]")
    return ok


# ------------------------------------------------------------------
# 3. Heave symmetry — pure heave deflects all 6 servos equally
# ------------------------------------------------------------------
def test_heave_symmetry(kin: StewartPlatformKinematics) -> bool:
    """For pure heave all 6 servos must deflect equally (platform stays level)."""
    val = 50.0
    _zero_pose(kin)

    results = []
    for sign, label in ((val, f"+{val}"), (-val, f"-{val}")):
        kin.z = sign
        status, angles = kin.calc()
        if status != 0:
            print(f"  [FAIL] z={label}: solver error {status}")
            return False
        spread = max(angles) - min(angles)
        ok_level = spread < 0.01
        results.append(ok_level)
        print(f"  [{_label(ok_level)}] z={label:>5s}: "
              f"all servos = {angles[0]:.3f}°  (spread {spread:.4f}°)")

    return all(results)


# ------------------------------------------------------------------
# 4. At the working-envelope limits — every axis end-point must solve
# ------------------------------------------------------------------
def test_at_limits(kin: StewartPlatformKinematics) -> bool:
    """Each DOF driven to exactly its HX_* min and max (others zero) must
    return status 0 — the hardcoded limits are supposed to be reachable."""
    ok = True
    for dof, lo, hi in _DOF_LIMITS:
        for edge in (lo, hi):
            _zero_pose(kin)
            setattr(kin, dof, edge)
            status, angles = kin.calc()
            this_ok = status == 0 and angles is not None
            ok = ok and this_ok
            print(f"  [{_label(this_ok)}] {dof:>5s}={_disp(dof, edge):>10s} "
                  f"→ status={status}")
    return ok


# ------------------------------------------------------------------
# 5. Error codes — out-of-range poses return the documented codes
# ------------------------------------------------------------------
def test_error_codes(kin_unlimited: StewartPlatformKinematics) -> bool:
    """With clamps removed, out-of-range poses must report a documented error
    code (-1/-3/-4/-5) and return no angles — never crash, never silently OK."""
    ok = True

    # Extreme heave — platform far above / below the base; rods cannot reach.
    for z, expected in ((5000.0, -1), (-5000.0, -1)):
        _zero_pose(kin_unlimited)
        kin_unlimited.z = z
        status, angles = kin_unlimited.calc()
        this_ok = status == expected and angles is None
        ok = ok and this_ok
        print(f"  [{_label(this_ok)}] z={z:+.0f} mm → status={status} "
              f"(expected {expected})")

    # Poses just outside the working envelope — some documented error, no angles.
    out_of_range: list[tuple[str, float]] = [
        ("z",     250.0),
        ("z",    -250.0),
        ("x",     350.0),
        ("roll",  math.radians(35)),
        ("roll",  math.radians(-30)),
    ]
    for dof, val in out_of_range:
        _zero_pose(kin_unlimited)
        setattr(kin_unlimited, dof, val)
        status, angles = kin_unlimited.calc()
        this_ok = status in (-1, -3, -4, -5) and angles is None
        ok = ok and this_ok
        print(f"  [{_label(this_ok)}] {dof:>5s}={_disp(dof, val):>10s} "
              f"→ status={status} (one of -1/-3/-4/-5)")

    return ok


# ------------------------------------------------------------------
# 6. Combined-DOF smoke test — multi-axis poses still solve
# ------------------------------------------------------------------
def test_combined_dof(kin: StewartPlatformKinematics) -> bool:
    """A few simultaneous-DOF poses inside the envelope should solve cleanly.
    Exercises the cross-terms in calc() that single-DOF tests leave at zero."""
    cases: list[tuple[dict, str]] = [
        ({"x": 20.0, "y": 20.0, "z": 20.0,
          "roll": math.radians(3), "pitch": math.radians(3), "yaw": math.radians(3)},
         "tiny all-axis"),
        ({"z": HX_Z_MAX / 4, "roll": HX_A_MAX / 4,
          "pitch": HX_B_MAX / 4, "yaw": HX_C_MAX / 4},
         "quarter-limit z+roll+pitch+yaw"),
        ({"x": HX_X_MAX / 3, "pitch": HX_B_MAX / 3},
         "third-limit surge+pitch"),
    ]
    ok = True
    for pose, name in cases:
        _zero_pose(kin)
        for dof, val in pose.items():
            setattr(kin, dof, val)
        status, angles = kin.calc()
        this_ok = status == 0 and angles is not None
        ok = ok and this_ok
        print(f"  [{_label(this_ok)}] {name:<32s} → status={status}")
    return ok


# ------------------------------------------------------------------
# 7. Envelope consistency — hardcoded limits ⊆ freshly-computed envelope
# ------------------------------------------------------------------
def test_envelope_consistency() -> bool:
    """The HX_* constants must not exceed the envelope that compute_envelope
    derives from the current geometry — guards against geometry edits that
    forget to regenerate the limits."""
    raw = compute_envelope(build_default_config())  # raw symmetric (neg, pos)
    hardcoded = {
        "x": HX_X_MAX, "y": HX_Y_MAX, "z": HX_Z_MAX,
        "roll": HX_A_MAX, "pitch": HX_B_MAX, "yaw": HX_C_MAX,
    }
    slack = 0.02  # mm or degrees — covers 2-dp rounding of the constants

    ok = True
    for dof, hard in hardcoded.items():
        derated = min(abs(raw[dof][0]), abs(raw[dof][1])) * DERATE
        ang = dof in _ANGULAR
        hard_disp    = math.degrees(hard)    if ang else hard
        derated_disp = math.degrees(derated) if ang else derated
        this_ok = hard_disp <= derated_disp + slack
        ok = ok and this_ok
        unit = "°" if ang else " mm"
        print(f"  [{_label(this_ok)}] {dof:>5s}: hardcoded ±{hard_disp:.2f}{unit} "
              f"≤ computed ±{derated_disp:.2f}{unit}")
    return ok


# ------------------------------------------------------------------
# 8. Forward-kinematics invariant — every leg must close at ROD_LENGTH
# ------------------------------------------------------------------

def _rotation_zyx(roll: float, pitch: float, yaw: float) -> list[list[float]]:
    """ZYX rotation matrix from Euler angles (same convention as the solver)."""
    ca, sa = math.cos(roll),  math.sin(roll)
    cb, sb = math.cos(pitch), math.sin(pitch)
    cc, sc = math.cos(yaw),   math.sin(yaw)
    return [
        [cc * cb,  cc * sb * sa - sc * ca,  cc * sb * ca + sc * sa],
        [sc * cb,  sc * sb * sa + cc * ca,  sc * sb * ca - cc * sa],
        [   -sb,              cb * sa,                 cb * ca     ],
    ]


def _rod_closure_error(kin: StewartPlatformKinematics,
                       angles_deg: list[float]) -> float:
    """Worst |arm_tip→platform_joint distance − ROD_LENGTH| over 6 legs (mm).

    The forward geometry is re-derived here on purpose so this check is
    independent of the IK code it validates.
    """
    R = _rotation_zyx(kin.roll, kin.pitch, kin.yaw)
    height = kin.z - kin.z_home   # z_home < 0 → platform above base
    worst = 0.0
    for sid in range(kin.nb_servos):
        # calc() returns angles in physical-motor order, so geometry slot `sid`
        # is driven by the angle for physical motor GEOM_TO_MOTOR[sid].
        servo_angle = angles_deg[GEOM_TO_MOTOR[sid] - 1]
        # Arm tip: the arm swings in the vertical plane oriented by
        # theta_s; servo angle 90° = arm horizontal.
        swing = math.radians(servo_angle) - math.pi / 2
        ts = kin.theta_s[sid]
        bx, by = kin.b_coords[sid]
        tip = (
            bx + kin.arm_length * math.cos(swing) * math.cos(ts),
            by + kin.arm_length * math.cos(swing) * math.sin(ts),
            kin.arm_length * math.sin(swing),
        )
        # Platform joint in the world frame
        px, py = kin.p_coords[sid]
        joint = (
            kin.x + R[0][0] * px + R[0][1] * py,
            kin.y + R[1][0] * px + R[1][1] * py,
            height + R[2][0] * px + R[2][1] * py,
        )
        worst = max(worst, abs(math.dist(tip, joint) - kin.rod_length))
    return worst


def test_rod_length_invariant(kin: StewartPlatformKinematics) -> bool:
    """For every successful solve, each arm tip must sit exactly ROD_LENGTH
    from its platform joint — the physical constraint the IK inverts.
    Checks the home pose, every single-DOF envelope edge, and random
    combined poses (deterministic seed). Combined poses that the solver
    rejects are skipped; they have no angles to verify."""
    tol = 1e-6   # mm
    min_solved = 50
    rng = random.Random(42)

    poses: list[tuple[float, ...]] = [(0.0,) * 6]
    for i in range(len(_DOF_LIMITS)):
        _, lo, hi = _DOF_LIMITS[i]
        for edge in (lo, hi):
            pose = [0.0] * 6
            pose[i] = edge
            poses.append(tuple(pose))
    # Full-envelope draws exercise the extremes but mostly get rejected on
    # multi-axis combinations; half-scale draws keep the solved count high.
    for scale in (1.0, 0.5):
        for _ in range(150):
            poses.append(tuple(scale * rng.uniform(lo, hi)
                               for _, lo, hi in _DOF_LIMITS))

    solved = 0
    worst = 0.0
    for pose in poses:
        kin.x, kin.y, kin.z = pose[0], pose[1], pose[2]
        kin.roll, kin.pitch, kin.yaw = pose[3], pose[4], pose[5]
        status, angles = kin.calc()
        if status != 0:
            continue
        solved += 1
        worst = max(worst, _rod_closure_error(kin, angles))

    ok = worst < tol and solved >= min_solved
    print(f"  [{_label(ok)}] {solved}/{len(poses)} poses solved "
          f"(≥{min_solved} required), max rod-length error "
          f"{worst:.3e} mm (tol {tol:g})")
    return ok


# ==================================================================
# main
# ==================================================================
if __name__ == "__main__":
    kin = StewartPlatformKinematics(build_default_config())
    kin_unlimited = _build_unlimited()
    kin_ideal = _build_ideal()

    results: list[bool] = []

    def _record(ok: bool) -> None:
        results.append(ok)

    print("=" * 64)
    print("StewartPlatformKinematics — test suite")
    print("=" * 64)

    print("\n1. Home pose (ideal symmetric frame)")
    _record(test_home(kin_ideal))

    print("\n2. Single-DOF tests")
    dof_cases: list[tuple[str, float]] = [
        ("x",      50.0),
        ("x",     -50.0),
        ("y",      40.0),
        ("y",     -40.0),
        ("z",      80.0),
        ("z",     -80.0),
        ("roll",   math.radians(10)),
        ("roll",  math.radians(-10)),
        ("pitch",  math.radians(12)),
        ("pitch", math.radians(-12)),
        ("yaw",    math.radians(15)),
        ("yaw",   math.radians(-15)),
    ]
    for dof, val in dof_cases:
        _record(test_single_dof(kin, dof, val))

    print("\n3. Heave symmetry (ideal symmetric frame)")
    _record(test_heave_symmetry(kin_ideal))

    print("\n4. At working-envelope limits")
    _record(test_at_limits(kin))

    print("\n5. Error codes (clamps removed)")
    _record(test_error_codes(kin_unlimited))

    print("\n6. Combined-DOF smoke test")
    _record(test_combined_dof(kin))

    print("\n7. Envelope consistency")
    _record(test_envelope_consistency())

    print("\n8. Forward-kinematics rod-length invariant")
    _record(test_rod_length_invariant(kin))

    passed = sum(results)
    total = len(results)
    failed = total - passed
    print("\n" + "=" * 64)
    print(f"Results: {passed}/{total} passed, {failed} failed")
    print("=" * 64)
    sys.exit(0 if failed == 0 else 1)
