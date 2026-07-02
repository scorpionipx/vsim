# Stewart Platform Controller

Python 3.10+ inverse kinematics controller for a 6-DOF Stewart platform driven by six rotary servo motors. Given a desired platform pose (surge, sway, heave, roll, pitch, yaw), it computes the required angle for each servo arm so that the connecting push-rods position the platform correctly.

The IK solver is a Python translation of **Algorithm 2** from Nicolas Jeanmonod's C++ ESP32 implementation (ouilogique.com), using a rotation-matrix decomposition approach.

---

## Project Structure

```
python_controller/
├── config/
│   ├── platform_constants.py   # All physical parameters, servo limits, DOF limits
│   └── compute_envelope.py     # Utility: auto-compute DOF limits from current config
├── controller/
│   └── controller.py           # IK solver, config builder, waveform generators, test runner
├── examples/
│   └── minimal.py              # Smallest integration example: pose in → servo angles out
├── tests/
│   └── test_kinematics.py      # Test suite
└── AGENTS.md                   # Guidance for AI agents working in this repo
```

`controller/controller.py` is the **single source of algorithm truth** — all other modules import from it.

---

## Requirements

- Python 3.10+ (standard library only — no external dependencies)

---

## How to Run

This is a subpackage of `voromosim_payload`. Run all commands from the
**repository root** (the directory containing the top-level `voromosim_payload/`).
The examples below use `PKG` as shorthand for the dotted package path:

```bash
PKG=voromosim_payload.utils.inverse_kinematics.algo3
```

### Kinematics test suite

```bash
python3 -m $PKG.tests.test_kinematics
```

Tests: home pose (all servos at the midpoint, 130°), single-DOF sweeps, heave symmetry, at-the-limit poses, error-code handling, combined-DOF poses, envelope consistency (the runtime-computed limits stay within what `compute_envelope.py` derives), and a forward-kinematics rod-length invariant (every solved pose is verified by independent forward geometry: each arm tip must sit exactly `ROD_LENGTH` from its platform joint).

### Envelope calculator

Computes the working DOF limits for the current physical dimensions in `platform_constants.py`. Binary-searches each axis until a servo hits its physical limit, then applies 80% derating.

```bash
# Current config only
python3 -m $PKG.config.compute_envelope

# Compare against a different dimension (e.g. rod length 600 vs 800 mm baseline)
python3 -m $PKG.config.compute_envelope --base-rod 800 --rod 600
```

Available overrides: `--rod`, `--arm`, `--p-rad`, `--b-rad`, `--base-rod`.

### Controller test runner

Sweeps all 6 DOFs through their full working envelope and prints servo angle tables.

```bash
python3 -m $PKG.controller.controller
```

Select which DOF and waveform to run by editing `RUN_TEST` and `WAVEFORM` in the `__main__` block.

---

## Basic Usage

```python
from voromosim_payload.utils.inverse_kinematics.algo3.controller.controller import (
    StewartPlatformKinematics, build_default_config,
)
import math

kin = StewartPlatformKinematics(build_default_config())

# Set the desired pose
kin.x     =  50.0               # 50 mm surge
kin.z     =  20.0               # 20 mm heave
kin.pitch =  math.radians(5.0)  # 5° pitch

# Compute servo angles
status, angles = kin.calc()

if status == 0:
    for i, a in enumerate(angles):
        print(f"Servo {i}: {a:.2f}°")
else:
    print(f"Kinematics error: {status}")
```

### Error codes

| Code | Meaning |
|:---:|---|
| `0` | Success |
| `-1` | Platform joint out of reach |
| `-3` | Servo angle above physical maximum (180°) |
| `-4` | Servo angle below physical minimum (80°) |
| `-5` | Geometric singularity |

### Waveform generators

Available in `controller.controller` for sweeping DOFs in tests or animations.

| Name | Shape |
|---|---|
| `sinusoidal` | Full sine cycle lo → hi → lo |
| `linear` | One-way ramp lo → hi |
| `triangle` | Symmetric ramp lo → hi → lo |
| `ramp_hold` | Trapezoidal: ramp, hold, ramp, hold |
| `sweep_incremental` | Staircase of discrete levels |

---

## Platform Geometry

### Physical Construction

Two coaxial regular hexagons connected by six push-rods through ball joints.

**Base** — six rotary servo motors evenly spaced 60° apart (regular hexagon, so `THETA_B` = 30°). Each base ball joint sits 676.22 mm from the centre (`B_RAD`); a 200 mm arm on every servo drives one push-rod.

**Platform** — six ball joints in three close pairs, 120° apart. Each joint is 380 mm from the centre (`P_RAD`), offset ±50 mm laterally from its pair's symmetry axis, which gives `THETA_P` = 7.56°.

**Push-rods** — 675 mm between ball joints.

### Coordinate System

Origin at base centre. X right, Y forward, Z up. Rotations follow the right-hand rule: roll around X, pitch around Y, yaw around Z.

### Derived Parameters

| Parameter | Value | Derivation |
|---|:---:|---|
| `B_RAD` | 676.22 mm | Centre → base ball joint (measured) |
| `THETA_B` | 30° | Regular-hexagon offset (six evenly-spaced motors) |
| `P_RAD` | 380 mm | √((apothem+tab)² + lateral²) = √(376.70² + 50²) |
| `THETA_P` | 7.56° | asin(50 / 380) = atan(50 / 376.70) |
| `Z_HOME` | −639.48 mm | Auto-computed so all servos read 130° at home |
| `ARM_LENGTH` | 200 mm | Servo pivot → push-rod ball joint |
| `ROD_LENGTH` | 675 mm | Between ball joints |

### Servo Arm Orientations (THETA_S)

Even-numbered base sides: arm extends CCW (motor angle + 90°).
Odd-numbered base sides: arm extends CW (motor angle − 90°).

| Index | Motor | Side | THETA_S |
|:---:|:---:|:---:|:---:|
| S0 | M3 | 2 (even) | 150° |
| S1 | M2 | 1 (odd) | −90° |
| S2 | M1 | 0 (even) | 30° |
| S3 | M6 | 5 (odd) | 150° |
| S4 | M5 | 4 (even) | −90° |
| S5 | M4 | 3 (odd) | 30° |

---

## Working Envelope

Safe single-axis operating limits where all servos stay within 80–180°. Each is 80% of that axis's single-axis maximum for margin. Regenerate with `python3 -m config.compute_envelope`.

| DOF | Limit |
|---|:---:|
| Surge (x) | ±133.09 mm |
| Sway (y) | ±125.61 mm |
| Heave (z) | ±107.25 mm |
| Roll | ±15.47° |
| Pitch | ±16.03° |
| Yaw | ±20.81° |

Combined multi-axis poses have a smaller safe envelope.

---

## Revision History

**June 2026 (measured geometry + cleanup)** — Updated to as-built measurements: `ROD_LENGTH` 660 → 675 mm, `P_RAD` 407.91 → 380 mm, `THETA_P` 4.92° → 7.56° (±50 mm lateral offset), `B_RAD` 676.22 mm confirmed; `ARM_LENGTH` 200 mm. Servo band 60–180° → 80–180° (home 120° → 130°), `Z_HOME` auto-computed to −639.48 mm. DOF working-envelope limits are now **computed from the geometry at runtime** (first access each run) instead of hardcoded, so they can never drift out of sync. Removed the `visualization/` package (2D/3D matplotlib views, measurement guide) and the `plans/` folder; the controller is now pure standard library.

**June 2026** — Push-rod length 600 → 660 mm; `Z_HOME` −482.23 → −570.85 mm (auto-computed); DOF working-envelope limits regenerated from `compute_envelope.py`. Added `examples/minimal.py` integration example. Test suite now exits non-zero on failure and includes a forward-kinematics rod-length invariant check over random in-envelope poses.

**May 2026 (geometry update + cleanup)** — Platform hexagon side 420 → 400 mm and push-rod length 800 → 600 mm; `P_RAD` 425.17 → 407.91 mm, `THETA_P` 4.72° → 4.92°, `Z_HOME` −754.99 → −482.23 mm (auto-computed), DOF working-envelope limits regenerated from `compute_envelope.py`. Also: `waveform_triangle` now emits exactly `n_steps + 1` symmetric points; test suite expanded (at-the-limit, error-code, combined-DOF, and envelope-consistency tests).

**May 2026** — Consolidated dual kinematics implementations into a single `StewartPlatformKinematics` class in `controller/controller.py`. Removed `StewartPlatformKinematicsAlgo2` and the `kinematics/` module. API changed from functional (`calc_servo_angles(x, y, z, ...)`) to stateful (set attributes, call `calc()`). Numerical output verified identical before and after.

**April 2026** — Geometry parameters corrected against the reference drawing:
- `P_RAD` 365.4 → 425.17 mm (added 60 mm mounting tab)
- `THETA_P` 5.50° → 4.72° (recalculated for updated radius)
- `B_RAD` 606.22 → 676.22 mm (added 70 mm shaft)
- `THETA_S` corrected for odd-side motors (`motor_angle − 90°`)
- `Z_HOME` retuned to −754.990 mm (all servos read 120° at home)
- DOF limits recalculated; heave gained 41% range
