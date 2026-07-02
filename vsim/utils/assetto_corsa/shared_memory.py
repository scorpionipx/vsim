"""
Assetto Corsa shared-memory reader.

Assetto Corsa publishes live telemetry through three Windows named
shared-memory blocks (memory-mapped files):

    acpmf_physics    fast-changing physics (pitch, angular velocity, accG, ...)
    acpmf_graphics   session/HUD state (lap times, position, flags, ...)
    acpmf_static     constants for the session (car/track, max rpm, ...)

This module maps those blocks with ctypes structures matching the Kunos
shared-memory layout for the original *Assetto Corsa*. Assetto Corsa
Competizione extends the same blocks with extra trailing fields, so the
prefix we map (and every field used here) stays valid for ACC too.

The :class:`ACSharedMemory` reader exposes a ``get_signal(path)`` resolver so
callers can request fields by dotted path exactly the way the old UDP Vorosim
client announced them, e.g. ``"physics.pitch"`` or
``"physics.localAngularVel[1]"``. This lets ``remote_signals_mapping`` from
config.json drive the direct reader unchanged.

Notes:
- Angles (pitch/roll/heading) are radians; angular velocities are rad/s. The
  GUI per-axis transforms (gain/offset) convert them to servo degrees, so no
  unit conversion happens here.
- ``acpmf_*`` blocks only exist while Assetto Corsa is running. Opening fails
  cleanly (raising ``OSError``/``FileNotFoundError``) until the sim is up.
"""

from __future__ import annotations

import ctypes
import mmap
import os
import re
from typing import Any, Dict, Iterable, Optional


_IS_WINDOWS = os.name == "nt"

if _IS_WINDOWS:
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _FILE_MAP_READ = 0x0004

    _OpenFileMapping = _kernel32.OpenFileMappingW
    _OpenFileMapping.restype = wintypes.HANDLE
    _OpenFileMapping.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]

    _MapViewOfFile = _kernel32.MapViewOfFile
    _MapViewOfFile.restype = ctypes.c_void_p
    _MapViewOfFile.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t,
    ]

    _UnmapViewOfFile = _kernel32.UnmapViewOfFile
    _UnmapViewOfFile.restype = wintypes.BOOL
    _UnmapViewOfFile.argtypes = [ctypes.c_void_p]

    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.restype = wintypes.BOOL
    _CloseHandle.argtypes = [wintypes.HANDLE]


# Windows shared-memory block names, in priority order per section. The
# classic ``acpmf_*`` names are used by the original Assetto Corsa and Assetto
# Corsa Competizione; Assetto Corsa EVO publishes the same physics layout under
# ``acevo_pmf_*``. We try each candidate and attach to whichever exists, so the
# reader works across all three titles without configuration.
PHYSICS_TAGS = ("acpmf_physics", "acevo_pmf_physics")
GRAPHICS_TAGS = ("acpmf_graphics", "acevo_pmf_graphics")
STATIC_TAGS = ("acpmf_static", "acevo_pmf_static")


class SPageFilePhysics(ctypes.Structure):
    """Layout of the ``acpmf_physics`` block (original Assetto Corsa)."""

    _pack_ = 4
    _fields_ = [
        ("packetId", ctypes.c_int32),
        ("gas", ctypes.c_float),
        ("brake", ctypes.c_float),
        ("fuel", ctypes.c_float),
        ("gear", ctypes.c_int32),
        ("rpms", ctypes.c_int32),
        ("steerAngle", ctypes.c_float),
        ("speedKmh", ctypes.c_float),
        ("velocity", ctypes.c_float * 3),
        ("accG", ctypes.c_float * 3),
        ("wheelSlip", ctypes.c_float * 4),
        ("wheelLoad", ctypes.c_float * 4),
        ("wheelsPressure", ctypes.c_float * 4),
        ("wheelAngularSpeed", ctypes.c_float * 4),
        ("tyreWear", ctypes.c_float * 4),
        ("tyreDirtyLevel", ctypes.c_float * 4),
        ("tyreCoreTemperature", ctypes.c_float * 4),
        ("camberRAD", ctypes.c_float * 4),
        ("suspensionTravel", ctypes.c_float * 4),
        ("drs", ctypes.c_float),
        ("tc", ctypes.c_float),
        ("heading", ctypes.c_float),
        ("pitch", ctypes.c_float),
        ("roll", ctypes.c_float),
        ("cgHeight", ctypes.c_float),
        ("damage", ctypes.c_float * 5),
        ("numberOfTyresOut", ctypes.c_int32),
        ("pitLimiterOn", ctypes.c_int32),
        ("abs", ctypes.c_float),
        ("kersCharge", ctypes.c_float),
        ("kersInput", ctypes.c_float),
        ("autoShifterOn", ctypes.c_int32),
        ("rideHeight", ctypes.c_float * 2),
        ("turboBoost", ctypes.c_float),
        ("ballast", ctypes.c_float),
        ("airDensity", ctypes.c_float),
        ("airTemp", ctypes.c_float),
        ("roadTemp", ctypes.c_float),
        ("localAngularVel", ctypes.c_float * 3),
        ("finalFF", ctypes.c_float),
        ("performanceMeter", ctypes.c_float),
        ("engineBrake", ctypes.c_int32),
        ("ersRecoveryLevel", ctypes.c_int32),
        ("ersPowerLevel", ctypes.c_int32),
        ("ersHeatCharging", ctypes.c_int32),
        ("ersIsCharging", ctypes.c_int32),
        ("kersCurrentKJ", ctypes.c_float),
        ("drsAvailable", ctypes.c_int32),
        ("drsEnabled", ctypes.c_int32),
        ("brakeTemp", ctypes.c_float * 4),
        ("clutch", ctypes.c_float),
        ("tyreTempI", ctypes.c_float * 4),
        ("tyreTempM", ctypes.c_float * 4),
        ("tyreTempO", ctypes.c_float * 4),
        ("isAIControlled", ctypes.c_int32),
        ("tyreContactPoint", (ctypes.c_float * 3) * 4),
        ("tyreContactNormal", (ctypes.c_float * 3) * 4),
        ("tyreContactHeading", (ctypes.c_float * 3) * 4),
        ("brakeBias", ctypes.c_float),
        ("localVelocity", ctypes.c_float * 3),
    ]


class SPageFileGraphics(ctypes.Structure):
    """Layout of the ``acpmf_graphics`` block (original Assetto Corsa)."""

    _pack_ = 4
    _fields_ = [
        ("packetId", ctypes.c_int32),
        ("status", ctypes.c_int32),       # AC_STATUS: 0 OFF, 1 REPLAY, 2 LIVE, 3 PAUSE
        ("session", ctypes.c_int32),      # AC_SESSION_TYPE
        ("currentTime", ctypes.c_wchar * 15),
        ("lastTime", ctypes.c_wchar * 15),
        ("bestTime", ctypes.c_wchar * 15),
        ("split", ctypes.c_wchar * 15),
        ("completedLaps", ctypes.c_int32),
        ("position", ctypes.c_int32),
        ("iCurrentTime", ctypes.c_int32),
        ("iLastTime", ctypes.c_int32),
        ("iBestTime", ctypes.c_int32),
        ("sessionTimeLeft", ctypes.c_float),
        ("distanceTraveled", ctypes.c_float),
        ("isInPit", ctypes.c_int32),
        ("currentSectorIndex", ctypes.c_int32),
        ("lastSectorTime", ctypes.c_int32),
        ("numberOfLaps", ctypes.c_int32),
        ("tyreCompound", ctypes.c_wchar * 33),
        ("replayTimeMultiplier", ctypes.c_float),
        ("normalizedCarPosition", ctypes.c_float),
        ("carCoordinates", ctypes.c_float * 3),
        ("penaltyTime", ctypes.c_float),
        ("flag", ctypes.c_int32),         # AC_FLAG_TYPE
        ("idealLineOn", ctypes.c_int32),
        ("isInPitLane", ctypes.c_int32),
        ("surfaceGrip", ctypes.c_float),
        ("mandatoryPitDone", ctypes.c_int32),
    ]


class SPageFileStatic(ctypes.Structure):
    """Layout of the ``acpmf_static`` block (original Assetto Corsa)."""

    _pack_ = 4
    _fields_ = [
        ("smVersion", ctypes.c_wchar * 15),
        ("acVersion", ctypes.c_wchar * 15),
        ("numberOfSessions", ctypes.c_int32),
        ("numCars", ctypes.c_int32),
        ("carModel", ctypes.c_wchar * 33),
        ("track", ctypes.c_wchar * 33),
        ("playerName", ctypes.c_wchar * 33),
        ("playerSurname", ctypes.c_wchar * 33),
        ("playerNick", ctypes.c_wchar * 33),
        ("sectorCount", ctypes.c_int32),
        ("maxTorque", ctypes.c_float),
        ("maxPower", ctypes.c_float),
        ("maxRpm", ctypes.c_int32),
        ("maxFuel", ctypes.c_float),
        ("suspensionMaxTravel", ctypes.c_float * 4),
        ("tyreRadius", ctypes.c_float * 4),
        ("maxTurboBoost", ctypes.c_float),
        ("deprecated_1", ctypes.c_float),
        ("deprecated_2", ctypes.c_float),
        ("penaltiesEnabled", ctypes.c_int32),
        ("aidFuelRate", ctypes.c_float),
        ("aidTireRate", ctypes.c_float),
        ("aidMechanicalDamage", ctypes.c_float),
        ("aidAllowTyreBlankets", ctypes.c_int32),
        ("aidStability", ctypes.c_float),
        ("aidAutoClutch", ctypes.c_int32),
        ("aidAutoBlip", ctypes.c_int32),
        ("hasDRS", ctypes.c_int32),
        ("hasERS", ctypes.c_int32),
        ("hasKERS", ctypes.c_int32),
        ("kersMaxJ", ctypes.c_float),
        ("engineBrakeSettingsCount", ctypes.c_int32),
        ("ersPowerControllerCount", ctypes.c_int32),
        ("trackSPlineLength", ctypes.c_float),
        ("trackConfiguration", ctypes.c_wchar * 33),
        ("ersMaxJ", ctypes.c_float),
        ("isTimedRace", ctypes.c_int32),
        ("hasExtraLap", ctypes.c_int32),
        ("carSkin", ctypes.c_wchar * 33),
        ("reversedGridPositions", ctypes.c_int32),
        ("pitWindowStart", ctypes.c_int32),
        ("pitWindowEnd", ctypes.c_int32),
    ]


# section name -> ctypes structure + candidate shared-memory tags
_SECTIONS = {
    "physics": (SPageFilePhysics, PHYSICS_TAGS),
    "graphics": (SPageFileGraphics, GRAPHICS_TAGS),
    "static": (SPageFileStatic, STATIC_TAGS),
}

# matches "field" or "field[index]" in a dotted signal path
_FIELD_RE = re.compile(r"^([A-Za-z_]\w*)(?:\[(\d+)\])?$")


def split_sections(signal_paths: Iterable[str]) -> set[str]:
    """Return the set of section names (``physics``/``graphics``/``static``)
    referenced by an iterable of dotted signal paths. Unknown sections are
    ignored; used to map only the blocks a mapping actually needs."""
    sections: set[str] = set()
    for path in signal_paths:
        section = path.split(".", 1)[0]
        if section in _SECTIONS:
            sections.add(section)
    return sections


def _resolve_field(struct: ctypes.Structure, field_path: str) -> Any:
    """Resolve ``field`` or ``field[index]`` (optionally nested with dots,
    e.g. ``accG[0]``) against a ctypes structure instance."""
    value: Any = struct
    for token in field_path.split("."):
        m = _FIELD_RE.match(token)
        if not m:
            raise KeyError(f"bad field token: {token!r}")
        name, index = m.group(1), m.group(2)
        value = getattr(value, name)
        if index is not None:
            value = value[int(index)]
    return value


class _MappedBlock:
    """A single attached shared-memory block (read-only view).

    On Windows it attaches to an *existing* named mapping via
    ``OpenFileMapping`` — which fails when the block does not exist (sim not
    running) and, unlike ``mmap(-1, ...)``, never *creates* a phantom mapping
    that could collide with the one Assetto Corsa makes at launch. On other
    platforms (dev only — AC is Windows-only) it falls back to ``mmap``.
    """

    def __init__(self, tags, size: int):
        self.size = size
        self.tag = None       # the candidate name that actually attached
        self._handle = None   # Windows mapping handle
        self._addr = None     # Windows mapped view address
        self._mm = None       # POSIX mmap fallback

        tags = (tags,) if isinstance(tags, str) else tuple(tags)

        if _IS_WINDOWS:
            last_err = 2  # ERROR_FILE_NOT_FOUND
            for tag in tags:
                handle = _OpenFileMapping(_FILE_MAP_READ, False, tag)
                if not handle:
                    last_err = ctypes.get_last_error()
                    continue
                addr = _MapViewOfFile(handle, _FILE_MAP_READ, 0, 0, size)
                if not addr:
                    last_err = ctypes.get_last_error()
                    _CloseHandle(handle)
                    continue
                self._handle = handle
                self._addr = addr
                self.tag = tag
                return
            raise FileNotFoundError(
                f"none of the shared-memory blocks {tags} found "
                f"(err {last_err}) — is Assetto Corsa running?"
            )
        else:
            # dev fallback (AC is Windows-only): just try the first name
            self.tag = tags[0]
            self._mm = mmap.mmap(-1, size, tags[0], mmap.ACCESS_READ)

    def read_bytes(self) -> bytes:
        if _IS_WINDOWS:
            return ctypes.string_at(self._addr, self.size)
        self._mm.seek(0)
        return self._mm.read(self.size)

    def close(self) -> None:
        if _IS_WINDOWS:
            if self._addr:
                _UnmapViewOfFile(self._addr)
                self._addr = None
            if self._handle:
                _CloseHandle(self._handle)
                self._handle = None
        elif self._mm is not None:
            try:
                self._mm.close()
            except (OSError, ValueError):
                pass
            self._mm = None


class ACSharedMemory:
    """Reader for the Assetto Corsa shared-memory blocks.

    Attaches only the blocks requested via ``sections`` (default: all).
    Call :meth:`open` before reading; :meth:`close` (or use as a context
    manager) to release the mappings. Reads return freshly copied structures,
    so callers never hold a view into the live mapping.
    """

    def __init__(self, sections: Optional[Iterable[str]] = None):
        wanted = set(sections) if sections is not None else set(_SECTIONS)
        self._wanted = [s for s in _SECTIONS if s in wanted] or list(_SECTIONS)
        self._maps: Dict[str, _MappedBlock] = {}

    # ---------- lifecycle ----------
    def open(self) -> None:
        """Attach every requested block. Raises ``FileNotFoundError`` if
        Assetto Corsa is not running (the named blocks do not exist). Partial
        attaches are rolled back."""
        try:
            for section in self._wanted:
                if section in self._maps:
                    continue
                struct_cls, tags = _SECTIONS[section]
                self._maps[section] = _MappedBlock(tags, ctypes.sizeof(struct_cls))
        except Exception:
            self.close()
            raise

    @property
    def is_open(self) -> bool:
        return bool(self._maps)

    def opened_tags(self) -> Dict[str, str]:
        """Map each attached section to the block name it actually attached to
        (e.g. ``acpmf_physics`` for classic AC, ``acevo_pmf_physics`` for EVO).
        Useful for logging which title was detected."""
        return {section: block.tag for section, block in self._maps.items()}

    def close(self) -> None:
        for block in self._maps.values():
            block.close()
        self._maps.clear()

    def __enter__(self) -> "ACSharedMemory":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------- reads ----------
    def read_section(self, section: str) -> ctypes.Structure:
        """Read and return a copy of one block's structure."""
        if section not in self._maps:
            raise KeyError(f"section not mapped: {section!r}")
        struct_cls, _ = _SECTIONS[section]
        return struct_cls.from_buffer_copy(self._maps[section].read_bytes())

    def read_packet_id(self) -> Optional[int]:
        """Read the ``packetId`` of a mapped block that carries one
        (physics/graphics). AC increments it every physics step, so callers can
        watch it to tell a *live* sim from a paused/closed one. Returns ``None``
        when no suitable block is mapped."""
        for section in ("physics", "graphics"):
            if section in self._maps:
                return int(self.read_section(section).packetId)
        return None

    def read_named_signals(self, signal_paths: Iterable[str]) -> Dict[str, float]:
        """Resolve a collection of dotted signal paths to numeric values.

        Sections are read once and shared across all paths that reference them.
        Paths that fail to resolve (bad field, unmapped section) are skipped so
        a single typo never stops the rest of the mapping from updating.
        """
        paths = list(signal_paths)
        cache: Dict[str, ctypes.Structure] = {}
        out: Dict[str, float] = {}
        for path in paths:
            section, _, field_path = path.partition(".")
            if not field_path or section not in self._maps:
                continue
            try:
                if section not in cache:
                    cache[section] = self.read_section(section)
                out[path] = float(_resolve_field(cache[section], field_path))
            except (KeyError, AttributeError, IndexError, ValueError, TypeError):
                continue
        return out
