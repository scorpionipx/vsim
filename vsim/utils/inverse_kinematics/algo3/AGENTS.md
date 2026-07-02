# AGENTS.md

This file provides guidance to agents when working with code in this repository.

## Project

Python 3.10+ Stewart platform inverse kinematics controller, ported from C++ (ESP32).
The codebase is organized into packages:

- `config/platform_constants.py` — Single source of truth for all physical parameters,
  servo limits, and DOF envelope limits.
  No external dependencies (stdlib + `math` only).
- `config/compute_envelope.py` — Utility that binary-searches the real DOF limits for
  any platform config and compares against a baseline. Run with
  `python3 -m config.compute_envelope [--rod N] [--arm N] [--p-rad N] [--b-rad N] [--base-rod N]`.
- `controller/controller.py` — **Single source of algorithm truth.** Contains the IK solver
  class (`StewartPlatformKinematics`), config builder (`build_default_config()`),
  waveform generators, and test runner. All other modules import from here.
  No external dependencies (stdlib + `math` only).
- `examples/minimal.py` — Smallest integration example: pose in → servo angles out.
- `tests/test_kinematics.py` — Test suite for the kinematics class.

## Run

This package is a subpackage of `voromosim_payload`. Run the modules from the
**repository root** (the directory containing the top-level `voromosim_payload/`)
using their full dotted path:

```bash
PKG=voromosim_payload.utils.inverse_kinematics.algo3

# Kinematics test suite
python3 -m $PKG.tests.test_kinematics

# Controller test runner (sweeps all DOFs, prints servo-angle tables)
python3 -m $PKG.controller.controller

# DOF envelope calculator
python3 -m $PKG.config.compute_envelope
python3 -m $PKG.config.compute_envelope --base-rod 800 --rod 600  # compare two configs

# Minimal integration example
python3 -m $PKG.examples.minimal
```

The app consumes this controller via the `CONTROLLERS` registry in the
top-level `core.py` (class `StewartPlatformKinematics`, config from
`build_default_config()`, per-DOF `test_*` runners for the motion dataset).

Test selection is done by editing `RUN_TEST` / `WAVEFORM` / `N_STEPS` variables
in the `__main__` block of each script (no CLI args, no test framework).

## Critical Rules

- **Units**: angles in radians internally (degrees only for display); distances in mm
- **Single class**: `StewartPlatformKinematics` in `controller/controller.py` is the only
  IK solver — do not create a second implementation
- **Stateful API**: set pose attributes (`kin.x`, `kin.roll`, etc.) then call `kin.calc()`
  which returns `(status, angles_deg)`
- **Error codes**: `calc()` returns status codes
  -1 (unreachable), -3 (above max), -4 (below min), -5 (singularity)
  — do not remove or renumber
- **Config**: always passed as a `dict` to `StewartPlatformKinematics.__init__()` — no global
  state inside the class. Use `build_default_config()` from `controller/controller.py`
- **Validation order**: clamp DOF inputs → check reachability
  (`BP2 > BP2_MAX`) → check servo limits (`SERVO_MIN_DEG`/`SERVO_MAX_DEG`)
- **Romanian comments** (e.g. "DE MASURAT") are physical measurement notes — preserve them
- **Do not** change physical constants, refactor kinematics math, or add
  external dependencies to `config/platform_constants.py` / `controller/controller.py`
  without explicit confirmation
- **Imports**: use package-qualified imports (e.g. `from config.platform_constants import ...`,
  `from controller.controller import ...`)
- Max line length: 100 chars. Type hints on all function signatures.
