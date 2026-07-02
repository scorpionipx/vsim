"""Direct Assetto Corsa shared-memory integration for vsim.

Reads the sim's ``acpmf_*`` blocks locally instead of receiving the Vorosim
client's telemetry over UDP.
"""

from .reader import AssettoCorsaReaderThread
from .shared_memory import (
    ACSharedMemory,
    SPageFilePhysics,
    SPageFileGraphics,
    SPageFileStatic,
)

__all__ = [
    "AssettoCorsaReaderThread",
    "ACSharedMemory",
    "SPageFilePhysics",
    "SPageFileGraphics",
    "SPageFileStatic",
]
