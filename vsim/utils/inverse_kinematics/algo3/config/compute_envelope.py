"""
compute_envelope.py — Auto-compute DOF working envelope from current platform config.

For each DOF, binary-searches the maximum positive and negative excursion where
all six servos stay within their physical limits. Applies the same 80% derating
used for the hardcoded limits in platform_constants.py.

Run:
    python3 -m config.compute_envelope
    python3 -m config.compute_envelope --base-rod 800 --rod 600
"""

import argparse
import math

from ..controller.controller import StewartPlatformKinematics, build_default_config


DERATE     = 0.80   # match convention in platform_constants.py
TOLERANCE  = 0.01   # binary search stops when range < this (mm or rad)

# Upper bounds for the binary search per DOF
_SEARCH_MAX = {
    "x":     2000.0,
    "y":     2000.0,
    "z":     2000.0,
    "roll":  math.pi / 2,
    "pitch": math.pi / 2,
    "yaw":   math.pi / 2,
}

_ANGULAR = {"roll", "pitch", "yaw"}


def find_limit(kin: StewartPlatformKinematics, dof: str,
               sign: float, tol: float = TOLERANCE) -> float:
    """Binary search for the largest reachable excursion along *dof*.

    Returns a **signed** limit (``sign`` × magnitude), so the positive and
    negative searches yield ``pos >= 0`` and ``neg <= 0`` respectively and
    ``(neg, pos)`` is an ordered interval.
    """
    bound = _SEARCH_MAX[dof]

    def try_val(v: float) -> bool:
        kin.x = kin.y = kin.z = 0.0
        kin.roll = kin.pitch = kin.yaw = 0.0
        setattr(kin, dof, sign * v)
        return kin.calc()[0] == 0

    if try_val(bound):
        return sign * bound  # limit is beyond search range

    lo, hi = 0.0, bound
    while hi - lo > tol:
        mid = (lo + hi) / 2
        if try_val(mid):
            lo = mid
        else:
            hi = mid
    return sign * lo


def compute_envelope(config: dict) -> dict[str, tuple[float, float]]:
    """Return raw (underated) symmetric limits for each DOF."""
    # Use unclamped limits so the binary search can exceed the hardcoded envelope
    unlimited = dict(config)
    unlimited.update({
        "x_lim": (-1e9, 1e9), "y_lim": (-1e9, 1e9), "z_lim": (-1e9, 1e9),
        "a_lim": (-1e9, 1e9), "b_lim": (-1e9, 1e9), "c_lim": (-1e9, 1e9),
    })
    try:
        kin = StewartPlatformKinematics(unlimited)
    except ValueError as e:
        raise ValueError(f"Invalid geometry: {e}") from e

    limits = {}
    for dof in ("x", "y", "z", "roll", "pitch", "yaw"):
        pos = find_limit(kin, dof, +1.0)
        neg = find_limit(kin, dof, -1.0)
        limits[dof] = (neg, pos)
    return limits


def format_val(v: float, is_angular: bool) -> str:
    if is_angular:
        return f"{math.degrees(v):+.2f}°"
    return f"{v:+.2f} mm"


def print_envelope(limits: dict, derate: float = DERATE) -> None:
    col = 46
    print(f"\n{'DOF':<8} {'Raw limit':>12}  {'Derated ({:.0%})'.format(derate):>14}")
    print("─" * col)
    for dof, (neg, pos) in limits.items():
        ang = dof in _ANGULAR
        sym = min(abs(neg), abs(pos))
        derated = sym * derate
        raw_str    = f"±{format_val(sym, ang).lstrip('+')}"
        derated_str = f"±{format_val(derated, ang).lstrip('+')}"
        print(f"{dof.upper():<8} {raw_str:>12}  {derated_str:>14}")
    print("─" * col)


def compare_envelope(old: dict, new: dict, derate: float = DERATE) -> None:
    col = 66
    print(f"\n{'DOF':<8} {'Old derated':>14}  {'New derated':>14}  {'Change':>12}")
    print("─" * col)
    for dof in ("x", "y", "z", "roll", "pitch", "yaw"):
        ang = dof in _ANGULAR
        old_sym = min(abs(old[dof][0]), abs(old[dof][1])) * derate
        new_sym = min(abs(new[dof][0]), abs(new[dof][1])) * derate
        delta = new_sym - old_sym
        sign  = "+" if delta >= 0 else ""
        old_str   = f"±{format_val(old_sym, ang).lstrip('+')}"
        new_str   = f"±{format_val(new_sym, ang).lstrip('+')}"
        delta_str = f"{sign}{format_val(abs(delta), ang).lstrip('+')} {'▲' if delta > 0 else '▼' if delta < 0 else '='}"
        print(f"{dof.upper():<8} {old_str:>14}  {new_str:>14}  {delta_str:>12}")
    print("─" * col)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Compute Stewart platform DOF envelope from current config."
    )
    parser.add_argument("--rod",      type=float, help="Override rod length (mm)")
    parser.add_argument("--arm",      type=float, help="Override arm length (mm)")
    parser.add_argument("--p-rad",    type=float, help="Override platform radius (mm)")
    parser.add_argument("--b-rad",    type=float, help="Override base radius (mm)")
    parser.add_argument("--base-rod", type=float, help="Baseline rod length for comparison (mm)")
    args = parser.parse_args()

    base_config = build_default_config()
    if args.base_rod is not None:
        base_config["rod_length"] = args.base_rod

    # --- Baseline ---
    print("Computing baseline envelope …")
    baseline = compute_envelope(base_config)
    print(f"  arm={base_config['arm_length']} mm  "
          f"rod={base_config['rod_length']} mm  "
          f"P_rad={base_config['p_rad']} mm  "
          f"B_rad={base_config['b_rad']} mm")
    print_envelope(baseline)

    # --- Override ---
    if any(v is not None for v in (args.rod, args.arm, args.p_rad, args.b_rad)):
        new_config = dict(base_config)
        if args.rod   is not None: new_config["rod_length"] = args.rod
        if args.arm   is not None: new_config["arm_length"] = args.arm
        if args.p_rad is not None: new_config["p_rad"]      = args.p_rad
        if args.b_rad is not None: new_config["b_rad"]      = args.b_rad

        print("\nComputing envelope with overrides …")
        print(f"  arm={new_config['arm_length']} mm  "
              f"rod={new_config['rod_length']} mm  "
              f"P_rad={new_config['p_rad']} mm  "
              f"B_rad={new_config['b_rad']} mm")
        try:
            new_limits = compute_envelope(new_config)
            print_envelope(new_limits)
            print("\nComparison (baseline → override):")
            compare_envelope(baseline, new_limits)
        except ValueError as e:
            print(f"\nERROR: {e}")
