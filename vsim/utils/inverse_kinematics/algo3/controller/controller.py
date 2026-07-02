"""
Stewart Platform Controller — single source of algorithm truth.

All other modules (tests, examples) import from here.

Physical geometry and DOF limits live in config/platform_constants.py.
This module owns the IK solver class, the waveform generators, and the
single-DOF test runner.
"""

import logging
import math

from ..config.platform_constants import (
    NB_SERVOS,
    geometry_config, dof_envelope,
    GEOM_TO_MOTOR,
    N_STEPS,
)

_LOG = logging.getLogger(__name__)


# =============================================================================
# DEFAULT CONFIG BUILDER
# =============================================================================

def build_default_config() -> dict:
    """Return the default platform configuration dict.

    Single source of truth: the geometry comes from ``geometry_config`` and the
    DOF limits from ``dof_envelope`` (recomputed from that geometry on first use
    each run), so the envelope can never drift out of sync with the geometry.
    """
    cfg = geometry_config()
    env = dof_envelope()
    cfg["x_lim"] = env["x"]
    cfg["y_lim"] = env["y"]
    cfg["z_lim"] = env["z"]
    cfg["a_lim"] = env["roll"]
    cfg["b_lim"] = env["pitch"]
    cfg["c_lim"] = env["yaw"]
    return cfg


# =============================================================================
# KINEMATICS CLASS
# =============================================================================

class StewartPlatformKinematics:
    """Inverse-kinematics solver for a 6-DOF Stewart platform.

    Algorithm 2 — rotation-matrix decomposition approach.
    Translated from *Hexapod_KinematicsCalcServoAnglesAlgo2.h* (ESP32 C++).

    Usage::

        cfg = build_default_config()
        kin = StewartPlatformKinematics(cfg)

        kin.x     =  10.0   # mm
        kin.pitch =   0.05  # rad
        status, angles = kin.calc()
    """

    def __init__(self, platform_config: dict) -> None:
        cfg = platform_config

        # Physical geometry
        self.nb_servos: int                  = cfg["nb_servos"]
        self.arm_length: float               = cfg["arm_length"]
        self.rod_length: float               = cfg["rod_length"]
        self.z_home: float                   = cfg["z_home"]
        self.p_rad: float                    = cfg["p_rad"]
        self.b_rad: float                    = cfg["b_rad"]
        self.theta_p: float                  = cfg["theta_p"]
        self.theta_b: float                  = cfg["theta_b"]
        self.theta_s: list[float]            = cfg["theta_s"]
        self.servo_half_angular_range: float = cfg["servo_half_angular_range"]
        self.servo_min_deg: float            = cfg["servo_min_deg"]
        self.servo_max_deg: float            = cfg["servo_max_deg"]

        # Measured per-motor mounting deviations (motor order, default zero =
        # ideal symmetric frame). Applied on top of the symmetric layout in
        # _precompute_geometry. See platform_constants for the conventions.
        z6 = [0.0] * self.nb_servos
        self.base_pivot_radial: list[float]     = cfg.get("base_pivot_radial", z6)
        self.base_pivot_z: list[float]          = cfg.get("base_pivot_z", z6)
        self.platform_joint_radial: list[float] = cfg.get("platform_joint_radial", z6)
        self.platform_joint_z: list[float]      = cfg.get("platform_joint_z", z6)

        # Chair mounting angle (rad). Lets the chair be bolted down rotated
        # relative to the platform's native +Y forward (e.g. to clear the ball
        # joints) while the rider still commands in chair coordinates. The pose
        # is rotated by this angle into platform axes at the start of calc().
        # 0.0 = chair aligned with the platform; positive = CCW looking down +Z.
        self.mount_angle: float = cfg.get("mount_angle", 0.0)

        # DOF envelope limits
        self.x_lim: tuple[float, float] = cfg["x_lim"]
        self.y_lim: tuple[float, float] = cfg["y_lim"]
        self.z_lim: tuple[float, float] = cfg["z_lim"]
        self.a_lim: tuple[float, float] = cfg["a_lim"]
        self.b_lim: tuple[float, float] = cfg["b_lim"]
        self.c_lim: tuple[float, float] = cfg["c_lim"]

        self._precompute_geometry()
        self.z_home = self._compute_z_home()

        _LOG.info("Z_HOME = %.4f", self.z_home)

        # 6-DOF pose — default is the home position
        self.x: float     = 0.0   # Surge  (mm)
        self.y: float     = 0.0   # Sway   (mm)
        self.z: float     = 0.0   # Heave  (mm)
        self.roll: float  = 0.0   # Roll   (rad)
        self.pitch: float = 0.0   # Pitch  (rad)
        self.yaw: float   = 0.0   # Yaw    (rad)

    # ------------------------------------------------------------------
    # Public geometry accessors
    # ------------------------------------------------------------------

    @property
    def b_coords(self) -> list[list[float]]:
        """Base servo pivot XY coordinates (6 × [x, y])."""
        return self._B

    @property
    def p_coords(self) -> list[list[float]]:
        """Platform joint XY coordinates in body frame (6 × [x, y])."""
        return self._P

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def calc(self) -> tuple[int, list[float] | None]:
        """Compute servo angles for the current 6-DOF pose.

        Reads ``self.x``, ``self.y``, ``self.z``,
        ``self.roll``, ``self.pitch``, ``self.yaw``.

        Returns:
            ``(status, angles_deg)``

            *status*:
                ``0``  — OK
                ``-1`` — joint unreachable (BP² > BP²_MAX)
                ``-3`` — servo angle above motor max
                ``-4`` — servo angle below motor min
                ``-5`` — geometric singularity

            *angles_deg*:
                list of *nb_servos* servo angles in **degrees**,
                or ``None`` when *status* ≠ 0.
        """
        # Map the chair-frame pose into platform axes. The chair may be bolted
        # down rotated by ``mount_angle`` relative to the platform's native +Y.
        # Translations (surge/sway) and tilts (roll/pitch) are planar vectors in
        # the same XY plane, so both rotate by the same angle; heave (Z) and yaw
        # (about Z) are unaffected. mount_angle == 0 is the identity.
        if self.mount_angle != 0.0:
            cm = math.cos(self.mount_angle)
            sm = math.sin(self.mount_angle)
            in_x,    in_y     = self.x,    self.y
            in_roll, in_pitch = self.roll, self.pitch
            in_x,  in_y  = in_x * cm - in_y * sm,  in_x * sm + in_y * cm
            in_roll, in_pitch = (in_roll * cm - in_pitch * sm,
                                 in_roll * sm + in_pitch * cm)
        else:
            in_x,    in_y     = self.x,    self.y
            in_roll, in_pitch = self.roll, self.pitch

        x     = self._clamp(in_x,     *self.x_lim, "x")
        y     = self._clamp(in_y,     *self.y_lim, "y")
        z     = self._clamp(self.z,   *self.z_lim, "z")
        roll  = self._clamp(in_roll,  *self.a_lim, "roll")
        pitch = self._clamp(in_pitch, *self.b_lim, "pitch")
        yaw   = self._clamp(self.yaw, *self.c_lim, "yaw")

        cosA = math.cos(roll)
        cosB = math.cos(pitch)
        cosC = math.cos(yaw)
        sinA = math.sin(roll)
        sinB = math.sin(pitch)
        sinC = math.sin(yaw)

        ARM2 = self.arm_length ** 2
        ROD2 = self.rod_length ** 2

        angles_deg: list[float] = []

        for sid in range(self.nb_servos):
            cosD = self._cosD[sid]
            sinD = self._sinD[sid]

            # cos/sin(yaw + D) via angle-addition identity
            cosCpD = cosC * cosD - sinC * sinD
            sinCpD = sinC * cosD + cosC * sinD

            Px = self._P[sid][0]
            Py = self._P[sid][1]
            Pz = self._Pz[sid]
            Bx = self._B[sid][0]
            By = self._B[sid][1]
            Bz = self._Bz[sid]

            # Vector from base pivot to platform joint in servo local frame.
            # The Pz / Bz terms carry the platform-joint / base-pivot heights;
            # they vanish for the ideal coplanar frame (Pz = Bz = 0).
            BP_x = (
                x * cosD - y * sinD
                - Bx * cosD + By * sinD
                + Px * cosB * cosCpD
                + Py * (cosC * (sinA * sinB * cosD - cosA * sinD)
                        - sinC * (cosA * cosD + sinA * sinB * sinD))
                + Pz * (sinB * cosA * cosCpD + sinA * sinCpD)
            )
            BP_y = (
                x * sinD + y * cosD
                - Bx * sinD - By * cosD
                + Px * cosB * sinCpD
                + Py * (cosA * cosCpD + sinA * sinB * sinCpD)
                + Pz * (sinB * cosA * sinCpD - sinA * cosCpD)
            )
            BP_z = (
                z - self.z_home
                - Px * sinB
                + Py * sinA * cosB
                + Pz * cosA * cosB
                - Bz
            )

            BP_x2 = BP_x ** 2
            BP_y2 = BP_y ** 2
            BP_z2 = BP_z ** 2
            BP2   = BP_x2 + BP_y2 + BP_z2

            if BP2 > self._BP2_MAX:
                return -1, None

            i0 = BP_x2 + BP_z2
            i1 = i0 + BP_y2 - ROD2
            i2 = i0 - BP_y2 + ROD2

            i3 = -(BP_z2 * (ARM2 ** 2 + i1 ** 2 - 2 * ARM2 * i2))
            if i3 < 0:
                return -5, None

            i4 = math.sqrt(i3)

            i5 = (ARM2 * BP_z2
                  + BP_x2 * BP_z2
                  + BP_y2 * BP_z2
                  + BP_z2 ** 2
                  - BP_z2 * ROD2
                  - BP_x * i4)

            i6 = (BP_z * (ARM2 * BP_x
                          + BP_x ** 3
                          + BP_x * BP_y2
                          + BP_x * BP_z2
                          - BP_x * ROD2
                          + i4))
            # Exact-zero compare is intentional: this only guards the literal
            # divide-by-zero below; a near-zero i6 is fine, math.atan handles
            # arbitrarily large ratios and saturates towards ±pi/2.
            if i6 == 0.0:
                return -5, None

            # atan (not atan2) is correct here: the result is offset by +pi/2
            # to give the servo angle, and the valid servo band (80-180 deg)
            # keeps the raw atan within its (-pi/2, pi/2) principal branch.
            # Angles that would need the other branch fall outside the servo
            # limits and are rejected by the min/max checks below.
            angle_rad = math.atan(i5 / i6) + self.servo_half_angular_range
            angle_deg = math.degrees(angle_rad)

            if angle_deg < self.servo_min_deg:
                return -4, None
            if angle_deg > self.servo_max_deg:
                return -3, None

            angles_deg.append(angle_deg)

        # Reorder from solver geometry index to physical motor order so that
        # angles[i] drives physical motor i+1 (see GEOM_TO_MOTOR).
        return 0, [angles_deg[i] for i in self._motor_to_geom]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compute_z_home(self) -> float:
        """Compute Z_HOME from current instance dimensions.

        At home pose all DOFs are 0 and each servo sits at the midpoint of
        its angular range.  The platform height is determined by the arm
        tip position and the rod length.

        Returns a negative value by the solver's sign convention: the joint
        height enters the math as ``BP_z = z - z_home``, so a negative
        ``z_home`` places the home platform a positive distance *above* the
        base.
        """
        home_angle_deg = (self.servo_min_deg + self.servo_max_deg) / 2.0
        home_angle_rad = math.radians(home_angle_deg)
        swing = home_angle_rad - math.pi / 2   # angle above horizontal

        cos_swing = math.cos(swing)
        sin_swing = math.sin(swing)

        Px = self._P[0][0]
        Py = self._P[0][1]
        Bx = self._B[0][0]
        By = self._B[0][1]
        ts = self.theta_s[0]

        dx = Px - Bx - self.arm_length * cos_swing * math.cos(ts)
        dy = Py - By - self.arm_length * cos_swing * math.sin(ts)

        horiz_sq = dx ** 2 + dy ** 2
        vert_sq  = self.rod_length ** 2 - horiz_sq

        if vert_sq < 0:
            raise ValueError(
                f"Cannot compute Z_HOME: rod too short "
                f"(rod²={self.rod_length**2:.1f} < horiz²={horiz_sq:.1f})"
            )

        return -(self.arm_length * sin_swing + math.sqrt(vert_sq))

    def _precompute_geometry(self) -> None:
        """Precompute static geometric tables."""
        AXIS1 =  math.pi / 6   #  30°
        AXIS2 = -math.pi / 2   # -90°
        AXIS3 =  AXIS1          #  30° (Y-mirror via sign on X component)

        p  = self.p_rad
        b  = self.b_rad
        tp = self.theta_p
        tb = self.theta_b

        self._P: list[list[float]] = [
            [ p * math.cos(AXIS1 + tp),  p * math.sin(AXIS1 + tp)],
            [ p * math.cos(AXIS1 - tp),  p * math.sin(AXIS1 - tp)],
            [ p * math.cos(AXIS2 + tp),  p * math.sin(AXIS2 + tp)],
            [-p * math.cos(AXIS2 + tp),  p * math.sin(AXIS2 + tp)],
            [-p * math.cos(AXIS3 - tp),  p * math.sin(AXIS3 - tp)],
            [-p * math.cos(AXIS3 + tp),  p * math.sin(AXIS3 + tp)],
        ]

        self._B: list[list[float]] = [
            [ b * math.cos(AXIS1 + tb),  b * math.sin(AXIS1 + tb)],
            [ b * math.cos(AXIS1 - tb),  b * math.sin(AXIS1 - tb)],
            [ b * math.cos(AXIS2 + tb),  b * math.sin(AXIS2 + tb)],
            [-b * math.cos(AXIS2 + tb),  b * math.sin(AXIS2 + tb)],
            [-b * math.cos(AXIS3 - tb),  b * math.sin(AXIS3 - tb)],
            [-b * math.cos(AXIS3 + tb),  b * math.sin(AXIS3 + tb)],
        ]

        # Apply measured per-motor mounting deviations on top of the ideal
        # symmetric layout. Offsets are indexed by physical motor, so each
        # geometry slot `sid` uses the entry for the motor it drives,
        # GEOM_TO_MOTOR[sid]. Radial offset runs along the pivot/joint's own
        # radius (+out/-in); _Pz/_Bz hold the vertical (height) offsets, which
        # are 0 for the ideal coplanar frame. All default to zero -> no change.
        self._Pz: list[float] = [0.0] * self.nb_servos
        self._Bz: list[float] = [0.0] * self.nb_servos
        for sid in range(self.nb_servos):
            m = GEOM_TO_MOTOR[sid] - 1
            self._B[sid] = self._apply_radial(self._B[sid], self.base_pivot_radial[m])
            self._Bz[sid] = self.base_pivot_z[m]
            self._P[sid] = self._apply_radial(self._P[sid], self.platform_joint_radial[m])
            self._Pz[sid] = self.platform_joint_z[m]

        mts = [-ts for ts in self.theta_s]
        self._sinD: list[float] = [math.sin(m) for m in mts]
        self._cosD: list[float] = [math.cos(m) for m in mts]

        # _motor_to_geom[m] = geometry index that drives physical motor m+1.
        # Used to reorder calc() output into physical motor order.
        self._motor_to_geom: list[int] = [
            GEOM_TO_MOTOR.index(m + 1) for m in range(self.nb_servos)
        ]

        self._BP2_MAX: float = (self.arm_length + self.rod_length) ** 2

    @staticmethod
    def _apply_radial(xy: list[float], radial_mm: float) -> list[float]:
        """Shift a 2D point by ``radial_mm`` along its own radius from origin
        (+outward / -inward). Returns the point unchanged at the origin."""
        if radial_mm == 0.0:
            return xy
        x, y = xy
        r = math.hypot(x, y)
        if r == 0.0:
            return xy
        return [x + radial_mm * x / r, y + radial_mm * y / r]

    @staticmethod
    def _clamp(value: float, lo: float, hi: float, name: str) -> float:
        """Clamp *value* to [lo, hi], logging a warning if applied."""
        if value < lo:
            _LOG.warning("%s = %.4f below min %.4f, clamped.", name, value, lo)
            return lo
        if value > hi:
            _LOG.warning("%s = %.4f above max %.4f, clamped.", name, value, hi)
            return hi
        return value


# =============================================================================
# WAVEFORM GENERATORS
# =============================================================================

def waveform_sinusoidal(lo: float, hi: float, n_steps: int) -> list[float]:
    """Full sine cycle sweeping [lo, hi]."""
    mid = (hi + lo) / 2
    amp = (hi - lo) / 2
    return [mid + amp * math.sin(2 * math.pi * i / n_steps) for i in range(n_steps + 1)]


def waveform_linear(lo: float, hi: float, n_steps: int) -> list[float]:
    """One-way linear ramp from lo to hi."""
    return [lo + (hi - lo) * i / n_steps for i in range(n_steps + 1)]


def waveform_triangle(lo: float, hi: float, n_steps: int) -> list[float]:
    """Triangle wave: lo → hi → lo, with exactly n_steps + 1 points."""
    up_steps   = max(1, n_steps // 2)
    down_steps = max(1, n_steps - up_steps)
    up   = [lo + (hi - lo) * i / up_steps   for i in range(up_steps + 1)]       # lo … hi
    down = [hi - (hi - lo) * i / down_steps for i in range(1, down_steps + 1)]  # … lo
    return up + down


def waveform_ramp_hold(lo: float, hi: float, n_steps: int) -> list[float]:
    """Trapezoidal: ramp lo→hi, hold, ramp hi→lo, hold (each phase = n_steps/4).

    Emits exactly ``n_steps + 1`` points; the final slice only matters for
    degenerate n_steps < 3 where the three minimum-length phases overshoot.
    """
    q         = max(1, n_steps // 4)
    ramp_up   = [lo + (hi - lo) * i / q for i in range(q)]
    hold_hi   = [hi] * q
    ramp_down = [hi - (hi - lo) * i / q for i in range(q)]
    hold_lo   = [lo] * max(0, n_steps - 3 * q + 1)
    return (ramp_up + hold_hi + ramp_down + hold_lo)[: n_steps + 1]


def waveform_sweep_incremental(lo: float, hi: float, n_steps: int,
                                n_levels: int = 8) -> list[float]:
    """Staircase: n_levels discrete positions from lo to hi.

    Emits exactly ``n_steps + 1`` points (matching the other waveforms) by
    bucketing the output indices into the discrete levels. Starts at *lo*,
    ends at *hi*. ``n_levels`` is clamped to at least 2 (a single level has
    no lo→hi span to step through).
    """
    n_levels = max(2, n_levels)
    n_points = n_steps + 1
    result   = []
    for i in range(n_points):
        lvl = min(i * n_levels // n_points, n_levels - 1)
        result.append(lo + (hi - lo) * lvl / (n_levels - 1))
    return result


WAVEFORMS: dict = {
    "sinusoidal":        waveform_sinusoidal,
    "linear":            waveform_linear,
    "triangle":          waveform_triangle,
    "ramp_hold":         waveform_ramp_hold,
    "sweep_incremental": waveform_sweep_incremental,
}


# =============================================================================
# TEST RUNNER
# =============================================================================

def _run_dof_test(
    kin: StewartPlatformKinematics,
    dof: str,
    lo: float,
    hi: float,
    waveform_func,
    n_steps: int,
    verbose: bool = True,
) -> list[list[float] | None]:
    """Run a single-DOF sweep; all other DOFs held at zero.

    Returns the per-step servo angles (one ``list[float]`` per waveform point,
    or ``None`` for any step the solver rejected). The returned dataset is what
    the host app consumes to drive the motors. When *verbose* is True (default)
    a servo-angle table is printed; pass False for quiet (e.g. live) rebuilds.
    """
    is_angular = dof in ("roll", "pitch", "yaw")
    unit       = "deg" if is_angular else "mm"
    lo_disp    = math.degrees(lo) if is_angular else lo
    hi_disp    = math.degrees(hi) if is_angular else hi

    values = waveform_func(lo, hi, n_steps)

    if verbose:
        print(f"\nDOF: {dof.upper():6s}  |  Range: {lo_disp:+.1f} to {hi_disp:+.1f} {unit}"
              f"  |  Waveform: {waveform_func.__name__}  |  Points: {len(values)}")
        print("=" * 92)
        servo_header = "  ".join(f"  M{i + 1}[deg]" for i in range(NB_SERVOS))
        print(f"{'Step':>4}  {'Input':>9}  {servo_header}")
        print("-" * 92)

    dataset: list[list[float] | None] = []

    for step, val in enumerate(values):
        kin.x = kin.y = kin.z = 0.0
        kin.roll = kin.pitch = kin.yaw = 0.0
        setattr(kin, dof, val)
        val_disp = math.degrees(val) if is_angular else val

        status, angles = kin.calc()
        dataset.append(angles)

        if verbose:
            if status == 0:
                angle_str = "  ".join(f"{a:+9.3f}" for a in angles)
                print(f"{step:>4}  {val_disp:>+9.3f}  {angle_str}")
            else:
                print(f"{step:>4}  {val_disp:>+9.3f}  [error {status}]")

    if verbose:
        print("=" * 92)
    return dataset


def test_x(kin: StewartPlatformKinematics, waveform_func, n_steps: int = N_STEPS, verbose: bool = True) -> list:
    return _run_dof_test(kin, "x", *kin.x_lim, waveform_func, n_steps, verbose)

def test_y(kin: StewartPlatformKinematics, waveform_func, n_steps: int = N_STEPS, verbose: bool = True) -> list:
    return _run_dof_test(kin, "y", *kin.y_lim, waveform_func, n_steps, verbose)

def test_z(kin: StewartPlatformKinematics, waveform_func, n_steps: int = N_STEPS, verbose: bool = True) -> list:
    return _run_dof_test(kin, "z", *kin.z_lim, waveform_func, n_steps, verbose)

def test_roll(kin: StewartPlatformKinematics, waveform_func, n_steps: int = N_STEPS, verbose: bool = True) -> list:
    return _run_dof_test(kin, "roll", *kin.a_lim, waveform_func, n_steps, verbose)

def test_pitch(kin: StewartPlatformKinematics, waveform_func, n_steps: int = N_STEPS, verbose: bool = True) -> list:
    return _run_dof_test(kin, "pitch", *kin.b_lim, waveform_func, n_steps, verbose)

def test_yaw(kin: StewartPlatformKinematics, waveform_func, n_steps: int = N_STEPS, verbose: bool = True) -> list:
    return _run_dof_test(kin, "yaw", *kin.c_lim, waveform_func, n_steps, verbose)


TEST_FUNCTIONS: dict = {
    "x":     test_x,
    "y":     test_y,
    "z":     test_z,
    "roll":  test_roll,
    "pitch": test_pitch,
    "yaw":   test_yaw,
}


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    # RUN_TEST : "x" | "y" | "z" | "roll" | "pitch" | "yaw" | "all"
    # WAVEFORM : "sinusoidal" | "linear" | "triangle" | "ramp_hold" | "sweep_incremental"
    RUN_TEST = "all"
    WAVEFORM = "triangle"

    kin = StewartPlatformKinematics(build_default_config())
    waveform_func = WAVEFORMS[WAVEFORM]

    print("Stewart Platform Kinematics")

    if RUN_TEST == "all":
        for fn in TEST_FUNCTIONS.values():
            fn(kin, waveform_func, N_STEPS)
    else:
        TEST_FUNCTIONS[RUN_TEST](kin, waveform_func, N_STEPS)
