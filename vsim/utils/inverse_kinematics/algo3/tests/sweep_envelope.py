"""
sweep_envelope.py — Sweep ROD_LENGTH × P_RAD × B_RAD and dump DOF envelopes to CSV.

For every combination in the requested ranges this builds a geometry config,
runs ``config.compute_envelope.compute_envelope`` on it, and writes one CSV row
per combination with the symmetric per-axis DOF limits (the same quantity the
platform actually uses: ``min(|neg|, |pos|)`` derated by ``DERATE``).

ARM_LENGTH and the joint angle offsets (THETA_P/B/S) are held at their defaults —
only the three swept radii/length vary.

Run (defaults sweep around the current geometry):
    python3 -m tests.sweep_envelope

Custom ranges (start:stop:step, inclusive of stop) and output path:
    python3 -m tests.sweep_envelope \
        --rod 200:700:50 --p-rad 200:500:50 --b-rad 450:700:50 \
        --out tests/envelope_sweep.csv

Emit raw (undecorated) limits instead of the 80%-derated ones:
    python3 -m tests.sweep_envelope --raw


python3 -m tests.sweep_envelope --rod 200:700:50 --p-rad 200:500:50 --b-rad 450:700:50 --out tests/envelope_sweep.csv
"""

import argparse
import csv
import itertools
import math
import sys
import time

from config.compute_envelope import DERATE, compute_envelope
from config.platform_constants import geometry_config

# DOFs in output order, with their unit. Translations in mm, rotations in deg.
_DOFS = [
    ("x", "mm"), ("y", "mm"), ("z", "mm"),
    ("roll", "deg"), ("pitch", "deg"), ("yaw", "deg"),
]


def parse_range(spec: str) -> list[float]:
    """Parse a ``start:stop:step`` spec into an inclusive list of floats."""
    try:
        start, stop, step = (float(p) for p in spec.split(":"))
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"range must be START:STOP:STEP (e.g. 400:900:50), got {spec!r}"
        )
    if step <= 0:
        raise argparse.ArgumentTypeError(f"step must be > 0, got {step}")
    values, v = [], start
    # +step/2 guards float drift so STOP is included when it lands on a step.
    while v <= stop + step / 2:
        values.append(round(v, 6))
        v += step
    return values


def symmetric_limit(neg: float, pos: float, dof_unit: str, derate: float) -> float:
    """Symmetric reachable limit for one DOF, derated, in mm or degrees."""
    sym = min(abs(neg), abs(pos)) * derate
    return math.degrees(sym) if dof_unit == "deg" else sym


def hexagon_side(radius: float, edge_offset: float = 0.0) -> float:
    """Edge length (mm) of the regular hexagonal plate — all 6 edges equal.

    Mounts sit at the middle of each side, so *radius* is the apothem
    (center→side-midpoint) and ``side = 2·radius/√3``. A nonzero *edge_offset*
    (radians) means the mount is shifted that far along the edge from the
    midpoint (the platform's ±THETA_P pairs), lowering the apothem to
    ``radius·cos(edge_offset)``.
    """
    apothem = radius * math.cos(edge_offset)
    return 2 * apothem / math.sqrt(3)


def sweep(rods, p_rads, b_rads, derate: float):
    """Yield (rod, p_rad, b_rad, status, {dof: limit}) for each combination."""
    base = geometry_config()
    for rod, p_rad, b_rad in itertools.product(rods, p_rads, b_rads):
        cfg = dict(base)
        cfg["rod_length"] = rod
        cfg["p_rad"] = p_rad
        cfg["b_rad"] = b_rad
        try:
            limits = compute_envelope(cfg)
        except ValueError:
            # Geometry infeasible at home (e.g. rod too short to close the loop).
            yield rod, p_rad, b_rad, "invalid", {}
            continue
        row = {
            dof: symmetric_limit(*limits[dof], unit, derate)
            for dof, unit in _DOFS
        }
        yield rod, p_rad, b_rad, "ok", row


def parse_weights(spec: str) -> dict[str, float]:
    """Parse ``x=1,roll=2,yaw=0`` into a per-DOF weight dict.

    Every DOF starts at weight 1.0; the spec overrides named ones. Set a DOF to
    0 to drop it from the score (e.g. ``--weights yaw=0`` ignores yaw).
    """
    weights = {dof: 1.0 for dof, _ in _DOFS}
    if not spec:
        return weights
    for part in spec.split(","):
        key, sep, val = part.partition("=")
        key = key.strip().lower()
        if not sep or key not in weights:
            raise argparse.ArgumentTypeError(
                f"weight must be DOF=NUM with DOF in {list(weights)}, got {part!r}"
            )
        weights[key] = float(val)
    return weights


def rank(records: list[dict], weights: dict[str, float]) -> None:
    """Assign each feasible record a normalized weighted ``score`` in place.

    Each DOF is scored as a fraction of the best value seen for that DOF across
    all feasible geometries, so units (mm vs deg) and magnitudes don't bias the
    sum. The score is the weighted average of those fractions (0..1, where 1.0
    would be best-in-class on every weighted DOF at once).
    """
    feasible = [r for r in records if r["status"] == "ok"]
    maxes = {
        dof: max((r["limits"][dof] for r in feasible), default=0.0)
        for dof, _ in _DOFS
    }
    for r in feasible:
        score, wsum = 0.0, 0.0
        for dof, _ in _DOFS:
            w, m = weights[dof], maxes[dof]
            if w == 0 or m == 0:
                continue
            score += w * (r["limits"][dof] / m)
            wsum += w
        r["score"] = score / wsum if wsum else 0.0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sweep ROD_LENGTH × P_RAD × B_RAD and write DOF limits to CSV."
    )
    parser.add_argument("--rod", type=parse_range, default="400:900:50",
                        help="rod length range START:STOP:STEP mm (default 400:900:50)")
    parser.add_argument("--p-rad", type=parse_range, default="200:500:50",
                        help="platform radius range START:STOP:STEP mm (default 200:500:50)")
    parser.add_argument("--b-rad", type=parse_range, default="400:950:50",
                        help="base radius range START:STOP:STEP mm (default 400:950:50)")
    parser.add_argument("--out", default="tests/envelope_sweep.csv",
                        help="output CSV path (default tests/envelope_sweep.csv)")
    parser.add_argument("--raw", action="store_true",
                        help="emit raw (underated) limits instead of the %d%% derated ones"
                             % round(DERATE * 100))
    parser.add_argument("--weights", type=parse_weights, default="",
                        help="per-DOF score weights, e.g. 'x=1,y=1,yaw=2,roll=0' "
                             "(default: all 1.0; set a DOF to 0 to ignore it)")
    parser.add_argument("--top", type=int, default=10,
                        help="how many top-ranked geometries to print (default 10)")
    args = parser.parse_args()

    # argparse stores parsed defaults as strings only if the user didn't override;
    # normalise so both default and user-supplied values are lists.
    rods = args.rod if isinstance(args.rod, list) else parse_range(args.rod)
    p_rads = args.p_rad if isinstance(args.p_rad, list) else parse_range(args.p_rad)
    b_rads = args.b_rad if isinstance(args.b_rad, list) else parse_range(args.b_rad)

    weights = args.weights if isinstance(args.weights, dict) else parse_weights(args.weights)

    base_cfg = geometry_config()
    theta_p, theta_b = base_cfg["theta_p"], base_cfg["theta_b"]

    derate = 1.0 if args.raw else DERATE
    total = len(rods) * len(p_rads) * len(b_rads)
    print(f"Sweeping {len(rods)}×{len(p_rads)}×{len(b_rads)} = {total} combinations "
          f"({'raw' if args.raw else f'{round(DERATE*100)}% derated'} limits) "
          f"→ {args.out}")

    t0 = time.time()
    records = []
    for i, (rod, p_rad, b_rad, status, row) in enumerate(
            sweep(rods, p_rads, b_rads, derate), start=1):
        records.append({"rod": rod, "p_rad": p_rad, "b_rad": b_rad,
                        "status": status, "limits": row, "score": None})
        if i % 100 == 0 or i == total:
            ok = sum(r["status"] == "ok" for r in records)
            print(f"  {i}/{total} … ({ok} ok, {i - ok} invalid)", end="\r")

    # Score feasible rows, then order best-first (invalid rows kept at the end).
    rank(records, weights)
    records.sort(key=lambda r: (r["status"] != "ok", -(r["score"] or 0.0)))

    header = ["rod_length_mm", "p_rad_mm", "b_rad_mm",
              "p_hex_side_mm", "b_hex_side_mm", "status", "score"]
    header += [f"{dof}_{unit}" for dof, unit in _DOFS]
    with open(args.out, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for r in records:
            if r["status"] == "ok":
                score = f"{r['score']:.4f}"
                cells = [f"{r['limits'][dof]:.3f}" for dof, _ in _DOFS]
            else:
                score = ""
                cells = [""] * len(_DOFS)
            p_side = hexagon_side(r["p_rad"], theta_p)  # pairs offset along the edge
            b_side = hexagon_side(r["b_rad"])           # motors at the side midpoints
            writer.writerow([r["rod"], r["p_rad"], r["b_rad"],
                             f"{p_side:.2f}", f"{b_side:.2f}",
                             r["status"], score, *cells])

    ok = sum(r["status"] == "ok" for r in records)
    print(f"\nDone in {time.time() - t0:.1f}s — {ok} feasible, {len(records) - ok} infeasible. "
          f"Wrote {args.out}")

    active = {dof: w for dof, w in weights.items() if w}
    print(f"\nTop {args.top} by weighted DOF score (weights: "
          f"{', '.join(f'{d}={w:g}' for d, w in active.items()) or 'none'}):")
    head = f"{'score':>7}  {'rod':>5} {'p_rad':>6} {'b_rad':>6}  " \
           + "  ".join(f"{dof:>6}" for dof, _ in _DOFS)
    print(head)
    print("─" * len(head))
    for r in records[:args.top]:
        if r["status"] != "ok":
            break
        lims = "  ".join(f"{r['limits'][dof]:6.1f}" for dof, _ in _DOFS)
        print(f"{r['score']:7.4f}  {r['rod']:5g} {r['p_rad']:6g} {r['b_rad']:6g}  {lims}")

    # Regular-hexagon plate edge length per swept radius (independent of rod/DOF).
    # Mounts sit mid-side, so the radius is the apothem; the platform pairs are
    # additionally offset ±THETA_P along the edge.
    print("\nRegular hexagon plate side (mm) — all 6 edges equal, to cut the plate:")
    for label, radii, offset in (("Platform P_RAD", p_rads, theta_p),
                                 ("Base     B_RAD", b_rads, 0.0)):
        print(f"  {label} (edge offset {math.degrees(offset):.2f}°):  "
              f"{'radius':>6}  {'side':>8}")
        for radius in sorted(set(radii)):
            print(f"  {'':>{len(label)}}                   {radius:6g}  "
                  f"{hexagon_side(radius, offset):8.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
