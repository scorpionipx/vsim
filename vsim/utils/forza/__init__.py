"""Forza Horizon 5 "Data Out" telemetry integration for vsim.

Receives the UDP telemetry stream FH5 pushes and exposes it as the same
``{signal_path: value}`` dict the Assetto Corsa reader produces, so both
sources share one mapping / transform / smoothing pipeline.
"""

from .packet import (
    DEFAULT_FORZA_PORT,
    FH5_PACKET_SIZE,
    SIGNAL_PATHS,
    SIGNAL_PREFIX,
    FH5Packet,
    PacketSizeError,
    decode,
    owns_path,
    to_signals,
    unknown_paths,
)
from .reader import ForzaHorizonReaderThread

__all__ = [
    "DEFAULT_FORZA_PORT",
    "FH5_PACKET_SIZE",
    "SIGNAL_PATHS",
    "SIGNAL_PREFIX",
    "FH5Packet",
    "ForzaHorizonReaderThread",
    "PacketSizeError",
    "decode",
    "owns_path",
    "to_signals",
    "unknown_paths",
]
