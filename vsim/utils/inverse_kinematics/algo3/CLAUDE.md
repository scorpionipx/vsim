# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

This file covers the `python_controller/` package. See `AGENTS.md` (same directory) for
the detailed coding rules — units, validation order, error-code contract, import style —
which are not repeated here.

## What this is

Python 3.10+ inverse-kinematics controller for a 6-DOF Stewart platform driven by six
rotary servos. Given a platform pose (surge/sway/heave + roll/pitch/yaw) it returns the
six servo arm angles. It is a stdlib-only (`math`) port of **Algorithm 2** from Nicolas
Jeanmonod's C++ ESP32 firmware. The ESP32 firmware itself, the CarMaker co-simulation, and
docs live in sibling top-level directories (`../ESP32`, `../CarMaker`, `../Doc`) and are out
of scope for Python work.

## Commands

Run everything from the `python_controller/` directory (package-qualified `-m` invocation
is required — there is no test framework and no CLI arg parsing for test selection):

```bash
python3 -m tests.test_kinematics          # test suite (exits non-zero on failure)
python3 -m controller.controller          # sweep DOFs, print servo-angle tables
python3 -m config.compute_envelope        # print working DOF limits for current geometry
python3 -m examples.minimal               # smallest pose-in → angles-out example
python3 -m tests.sweep_envelope           # sweep ROD/P_RAD/B_RAD → CSV (research tool)
```

Select which DOF / waveform a runner exercises by editing module-level variables in its
`__main__` block (`RUN_TEST`, `WAVEFORM`, `N_STEPS`) — not via CLI flags.

`compute_envelope` and `sweep_envelope` accept geometry overrides:
`--rod --arm --p-rad --b-rad --base-rod` (and `--base-rod N --rod M` to compare two configs).

## Architecture: the geometry → envelope → config chain

This is the key relationship to understand before changing anything; the modules form a
one-directional dependency the whole system relies on to stay self-consistent.

1. **`config/platform_constants.py`** — single source of *physical* truth. Hardcoded
   measured constants (`B_RAD`, `P_RAD`, `ROD_LENGTH`, `ARM_LENGTH`, `SERVO_MIN/MAX_DEG`,
   the `THETA_*` joint offsets) plus `geometry_config()` which packages them into a dict.
   Module-level `__getattr__` exposes DOF limits lazily so they are never stale.

2. **`config/compute_envelope.py`** — given any geometry config, binary-searches each axis
   until a servo hits its physical limit, then derates by 80% (`DERATE`). `dof_envelope()`
   in `platform_constants.py` calls this **once on first access each run** and caches it.
   This is why the working envelope can never drift out of sync with the geometry — it is
   *derived*, not stored.

3. **`controller/controller.py`** — single source of *algorithm* truth. `build_default_config()`
   merges `geometry_config()` (physical) with `dof_envelope()` (derived limits) into the
   `dict` that `StewartPlatformKinematics(cfg)` consumes. The solver holds no global
   state — all config arrives through the constructor dict. This module also owns the
   waveform generators (`WAVEFORMS` registry) and the single-DOF `test_*` runners. Tests
   and examples import from here; do not create a second IK implementation.

Practical consequence: changing a physical constant in `platform_constants.py` automatically
flows into the envelope and into every solved pose. The test suite enforces this with an
*envelope-consistency* check (runtime limits stay within what `compute_envelope` derives) and
a *forward-kinematics invariant* (every solved pose is re-verified by independent geometry —
each arm tip must sit exactly `ROD_LENGTH` from its platform joint).

## Conventions worth knowing

- Stateful solver API: set `kin.x`, `kin.roll`, … then call `kin.calc()` → `(status, angles_deg)`.
- Internally radians + mm; degrees only at display/IO boundaries.
- Romanian comments (e.g. `DE MASURAT` = "to be measured") are real measurement notes — keep them.
- The `README.md` Revision History documents geometry changes over time; update it when you
  change a physical constant so the as-built record stays accurate.
