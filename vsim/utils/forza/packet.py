"""
Forza Horizon 5 "Data Out" telemetry packet.

Forza Horizon 5 pushes telemetry over UDP (Settings -> HUD & Gameplay -> Data
Out: switch it on, set the target IP and port). Unlike Assetto Corsa there is
no shared memory to map -- the game sends one datagram per physics tick
(~60 Hz) and we just decode it.

Packet layout
-------------
Every modern Forza title starts with the same 232-byte "sled" block introduced
by Forza Motorsport 7. Horizon 4 and 5 then insert 12 bytes of their own before
the FM7 "dash" block, giving 324 bytes total for FH5:

    [   0 .. 231 ]  FM7 sled block             (same across all Forza titles)
    [ 232 .. 243 ]  Horizon-only 12-byte block (contents undocumented)
    [ 244 .. 322 ]  FM7 dash block
    [ 323        ]  FH5 trailing byte          (contents undocumented)

This matters for how far each field should be trusted: everything this
application needs for motion cueing -- ``Yaw``, ``Pitch``, ``Roll`` and the
three ``AngularVelocity*`` components, at offsets 44..67 -- lives in the sled
block, *before* the Horizon-specific gap. Those offsets are stable and well
established. The dash-block fields (``Speed``, ``Gear``, lap times, ...) sit
after the undocumented 12-byte gap, so if a dash field ever reads back as
nonsense that gap is the thing to re-measure; the sled fields are unaffected.

Units (matching Assetto Corsa, so the GUI transform pipeline is unchanged):

- ``Yaw`` / ``Pitch`` / ``Roll``        radians
- ``AngularVelocity*``                  radians per second
- ``Acceleration*`` / ``Velocity*``     m/s^2 and m/s in car-local axes
                                        (X = right, Y = up, Z = forward)
- ``Speed``                             m/s
- ``IsRaceOn``                          1 while driving, 0 in menus / paused

Four-wheel arrays are ordered front-left, front-right, rear-left, rear-right
and use the same ``name[index]`` path syntax as the Assetto Corsa reader, e.g.
``fh5.TireSlipRatio[0]``.
"""

from __future__ import annotations

import ctypes
from typing import Dict, Iterable, List, Optional


# Prefix every signal path carries, mirroring Assetto Corsa's "physics." /
# "graphics." sections. Keeps FH5 paths unambiguous inside a single
# remote_signals_mapping that may also hold Assetto Corsa paths.
SIGNAL_PREFIX = "fh5"

# Order of the four-wheel arrays, for readable labels.
WHEEL_ORDER = ("FL", "FR", "RL", "RR")

# Default Data Out port. Forza does not pick one for you -- whatever is typed
# into the game's Data Out screen is where it sends -- so this is only a
# convention (and the value that ships in config.json as ``forza_port``).
DEFAULT_FORZA_PORT = 5300


class FH5Packet(ctypes.Structure):
    """Byte-exact layout of an FH5 "Dash" format datagram (324 bytes)."""

    _pack_ = 1
    _fields_ = [
        # ---------------- FM7 sled block (offset 0, 232 bytes) --------------
        ("IsRaceOn", ctypes.c_int32),                        #   0
        ("TimestampMS", ctypes.c_uint32),                    #   4
        ("EngineMaxRpm", ctypes.c_float),                    #   8
        ("EngineIdleRpm", ctypes.c_float),                   #  12
        ("CurrentEngineRpm", ctypes.c_float),                #  16
        ("AccelerationX", ctypes.c_float),                   #  20  right
        ("AccelerationY", ctypes.c_float),                   #  24  up
        ("AccelerationZ", ctypes.c_float),                   #  28  forward
        ("VelocityX", ctypes.c_float),                       #  32
        ("VelocityY", ctypes.c_float),                       #  36
        ("VelocityZ", ctypes.c_float),                       #  40
        ("AngularVelocityX", ctypes.c_float),                #  44  pitch rate
        ("AngularVelocityY", ctypes.c_float),                #  48  yaw rate
        ("AngularVelocityZ", ctypes.c_float),                #  52  roll rate
        ("Yaw", ctypes.c_float),                             #  56
        ("Pitch", ctypes.c_float),                           #  60
        ("Roll", ctypes.c_float),                            #  64
        ("NormalizedSuspensionTravel", ctypes.c_float * 4),  #  68
        ("TireSlipRatio", ctypes.c_float * 4),               #  84
        ("WheelRotationSpeed", ctypes.c_float * 4),          # 100
        ("WheelOnRumbleStrip", ctypes.c_int32 * 4),          # 116
        ("WheelInPuddleDepth", ctypes.c_float * 4),          # 132
        ("SurfaceRumble", ctypes.c_float * 4),               # 148
        ("TireSlipAngle", ctypes.c_float * 4),               # 164
        ("TireCombinedSlip", ctypes.c_float * 4),            # 180
        ("SuspensionTravelMeters", ctypes.c_float * 4),      # 196
        ("CarOrdinal", ctypes.c_int32),                      # 212
        ("CarClass", ctypes.c_int32),                        # 216
        ("CarPerformanceIndex", ctypes.c_int32),             # 220
        ("DrivetrainType", ctypes.c_int32),                  # 224
        ("NumCylinders", ctypes.c_int32),                    # 228

        # ------------- Horizon-only block (offset 232, 12 bytes) ------------
        # Present in FH4/FH5, absent in Forza Motorsport. Not documented by the
        # game; kept as opaque padding so the dash block lands on the right
        # offset.
        ("HorizonPadding", ctypes.c_uint8 * 12),             # 232

        # ---------------- FM7 dash block (offset 244, 79 bytes) -------------
        ("PositionX", ctypes.c_float),                       # 244
        ("PositionY", ctypes.c_float),                       # 248
        ("PositionZ", ctypes.c_float),                       # 252
        ("Speed", ctypes.c_float),                           # 256  m/s
        ("Power", ctypes.c_float),                           # 260  W
        ("Torque", ctypes.c_float),                          # 264  Nm
        ("TireTemp", ctypes.c_float * 4),                    # 268
        ("Boost", ctypes.c_float),                           # 284
        ("Fuel", ctypes.c_float),                            # 288
        ("DistanceTraveled", ctypes.c_float),                # 292
        ("BestLap", ctypes.c_float),                         # 296
        ("LastLap", ctypes.c_float),                         # 300
        ("CurrentLap", ctypes.c_float),                      # 304
        ("CurrentRaceTime", ctypes.c_float),                 # 308
        ("LapNumber", ctypes.c_uint16),                      # 312
        ("RacePosition", ctypes.c_uint8),                    # 314
        ("Accel", ctypes.c_uint8),                           # 315  0..255
        ("Brake", ctypes.c_uint8),                           # 316  0..255
        ("Clutch", ctypes.c_uint8),                          # 317  0..255
        ("HandBrake", ctypes.c_uint8),                       # 318  0..255
        ("Gear", ctypes.c_uint8),                            # 319  0 = reverse
        ("Steer", ctypes.c_int8),                            # 320  -127..127
        ("NormalizedDrivingLine", ctypes.c_int8),            # 321
        ("NormalizedAIBrakeDifference", ctypes.c_int8),      # 322

        # FH5 sends one byte more than FM7's dash format. Undocumented.
        ("Fh5Trailing", ctypes.c_uint8),                     # 323
    ]


# Opaque blocks, excluded from the signal set.
_PADDING_FIELDS = ("HorizonPadding", "Fh5Trailing")

FH5_PACKET_SIZE = ctypes.sizeof(FH5Packet)              # 324
FH4_PACKET_SIZE = FH5_PACKET_SIZE - 1                   # 323, same layout

# Everything up to (but not including) the Horizon padding. A datagram at least
# this long has valid sled fields no matter what follows, which covers every
# signal used for motion cueing.
SLED_SIZE = 232

# A plain Forza Motorsport 7 dash packet has no Horizon padding, so its dash
# block sits 12 bytes earlier. Decoding one with this structure would silently
# produce garbage for every dash field, so it is rejected by size instead.
FM7_DASH_PACKET_SIZE = 311


class PacketSizeError(ValueError):
    """Raised for a datagram whose length is not an FH5 Data Out packet."""


def decode(data: bytes) -> FH5Packet:
    """Decode a datagram into an :class:`FH5Packet`.

    Accepts the 324-byte FH5 packet and the byte-identical 323-byte FH4 form,
    and tolerates a longer datagram (a future title appending fields keeps
    working) by ignoring the excess. Raises :class:`PacketSizeError` for
    anything else -- notably the 311-byte Forza Motorsport dash packet, which
    uses the same field names at different offsets and would otherwise decode
    to silent garbage.
    """
    n = len(data)
    if n == FM7_DASH_PACKET_SIZE:
        raise PacketSizeError(
            f"{n}-byte packet is Forza Motorsport 'Dash' format, which lacks "
            f"the Horizon 12-byte block; select Forza Horizon 5 Data Out"
        )
    if n < FH4_PACKET_SIZE:
        raise PacketSizeError(
            f"{n}-byte packet is too short for FH5 Data Out "
            f"(expected {FH5_PACKET_SIZE}); is something else sending to "
            f"this port?"
        )
    if n < FH5_PACKET_SIZE:
        # 323-byte FH4 form: pad the single missing trailing byte so
        # from_buffer_copy sees a whole structure.
        data = data + b"\x00" * (FH5_PACKET_SIZE - n)
    return FH5Packet.from_buffer_copy(data[:FH5_PACKET_SIZE])


def _flat_field_names() -> List[str]:
    """Signal names (without the section prefix) in declaration order.

    Derived from ``_fields_`` so it cannot drift out of sync with the structure:
    scalars contribute ``Name``, four-wheel arrays contribute ``Name[0]`` ..
    ``Name[3]``. Opaque padding is skipped.
    """
    names: List[str] = []
    for name, ctype in FH5Packet._fields_:
        if name in _PADDING_FIELDS:
            continue
        length = getattr(ctype, "_length_", None)
        if length is None:
            names.append(name)
        else:
            names.extend(f"{name}[{i}]" for i in range(length))
    return names


# Every signal path this reader can produce, e.g. "fh5.Pitch",
# "fh5.TireSlipRatio[0]". Useful for validating remote_signals_mapping.
SIGNAL_PATHS: List[str] = [f"{SIGNAL_PREFIX}.{n}" for n in _flat_field_names()]
_SIGNAL_PATH_SET = frozenset(SIGNAL_PATHS)


def to_signals(pkt: FH5Packet,
               signal_paths: Optional[Iterable[str]] = None) -> Dict[str, float]:
    """Flatten a packet into ``{signal_path: float}``.

    With *signal_paths* given, only those paths are produced and unknown ones
    are skipped, so a typo in ``remote_signals_mapping`` never stops the rest of
    the mapping from updating -- the same contract as the Assetto Corsa reader.
    With ``None``, every field is produced.
    """
    wanted = None if signal_paths is None else set(signal_paths)
    out: Dict[str, float] = {}
    for name, ctype in FH5Packet._fields_:
        if name in _PADDING_FIELDS:
            continue
        length = getattr(ctype, "_length_", None)
        if length is None:
            path = f"{SIGNAL_PREFIX}.{name}"
            if wanted is None or path in wanted:
                out[path] = float(getattr(pkt, name))
        else:
            values = getattr(pkt, name)
            for i in range(length):
                path = f"{SIGNAL_PREFIX}.{name}[{i}]"
                if wanted is None or path in wanted:
                    out[path] = float(values[i])
    return out


def owns_path(path: str) -> bool:
    """True when *path* addresses this reader (i.e. starts with ``fh5.``)."""
    return path.split(".", 1)[0] == SIGNAL_PREFIX


def unknown_paths(paths: Iterable[str]) -> List[str]:
    """The ``fh5.*`` paths in *paths* that name no field of the packet.

    Used to warn about typos in ``remote_signals_mapping`` at start-up rather
    than leaving the operator wondering why an axis never moves.
    """
    return [p for p in paths if owns_path(p) and p not in _SIGNAL_PATH_SET]
