import json
import struct
import time
from typing import Any, Dict, Iterable


class HumanReadablePackager:
    """
    Builds the UDP packet (bytes) for the app (compact JSON, UTF-8).

    ``motor_data`` is a list of per-motor data dicts (``MotorWidget.get_data()``
    snapshots) — never live widgets, so packets can be built off the GUI thread.
    """

    @staticmethod
    def build_payload(motor_data: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "timestamp": time.time(),
            "motors": list(motor_data),
        }

    @classmethod
    def build_packet(cls, motor_data: Iterable[Dict[str, Any]]) -> bytes:
        """Return compact JSON encoded as UTF-8 bytes."""
        payload = cls.build_payload(motor_data)
        return json.dumps(payload, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def preview(packet_bytes: bytes) -> str:
        """Human-readable preview for GUI consoles."""
        try:
            return packet_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return packet_bytes.hex(" ")


class STMPackager:
    """
    Builds a binary (bytes) packet for STM with this layout (little-endian / LSB-first):

        8   : id (0x69)
        16  : sim_counter (uint16, increments each build)
        24  : sim_state (24-bit bitfield, see mapping below)
        16  : m1_angle (int16, milli-degrees)
        16  : m2_angle (int16)
        16  : m3_angle (int16)
        16  : m4_angle (int16)
        16  : m5_angle (int16)
        16  : m6_angle (int16)
        32  : rgb_data (uint32)
        8   : fan1_duty_cycle (0x69)
        8   : fan2_duty_cycle (0x69)
        8   : fan3_duty_cycle (0x69)
        8   : vibrator1_duty_cycle (0x45)
        8   : vibrator2_duty_cycle (0xFF)

    sim_state bit mapping (24 bits total):
      For motor i in [1..6], switches [-5, 0, 180, 185] occupy 4 bits:
        bit_index = (i-1)*4 + switch_index
        where switch_index: -5 -> 0, 0 -> 1, 180 -> 2, 185 -> 3.
      Example: M1 -5° = bit0, M1 0° = bit1, M1 180° = bit2, M1 185° = bit3, etc.

    Notes:
    - Angles are packed as int16 after multiplying by 100.
      Example: 90.00° → 9000
    - All multibyte fields are little-endian.
    """

    ID = 0x69
    FAN_DC = 0x69
    VIBRATOR1_DC = 0x45
    VIBRATOR2_DC = 0xFF

    def __init__(self):
        self._sim_counter = 0  # uint16 counter

    def reset_counter(self) -> None:
        self._sim_counter = 0

    def build_packet(self, motor_data: Iterable[Dict[str, Any]], rgb_data: int = 0x00000000) -> bytes:
        """*motor_data*: per-motor data dicts (``MotorWidget.get_data()``
        snapshots), never live widgets — safe to call off the GUI thread."""
        motors = list(motor_data)
        if len(motors) != 6:
            raise ValueError(f"STMPackager expects 6 motors, got {len(motors)}")

        packet = bytearray()

        # +8: id
        packet += struct.pack("<B", self.ID)

        # +16: sim_counter
        packet += struct.pack("<H", self._sim_counter & 0xFFFF)

        self._sim_counter += 1
        if self._sim_counter > 0xFFFF:
            self._sim_counter = 0

        # +24: sim_state (24-bit)
        sim_state = self._build_sim_state_24(motors)
        packet += sim_state.to_bytes(3, byteorder="little", signed=False)

        # +16 × 6: angles (int16 LE, milli-degrees)
        for data in motors:
            angle = self._read_angle_mdeg(data)
            # clamp to signed 16-bit range
            if angle < -32768:
                angle = -32768
            elif angle > 32767:
                angle = 32767
            packet += struct.pack("<h", int(angle))

        # +32: rgb_data (uint32)
        packet += struct.pack("<I", rgb_data & 0xFFFFFFFF)

        # +8×5: fixed duty cycles
        packet += struct.pack(
            "<BBBBB",
            self.FAN_DC,
            self.FAN_DC,
            self.FAN_DC,
            self.VIBRATOR1_DC,
            self.VIBRATOR2_DC,
        )

        return bytes(packet)

    # ---------------- internal helpers ---------------- #

    @staticmethod
    def _read_angle_mdeg(data: Dict[str, Any]) -> int:
        """Get motor angle * 100 (milli-degrees) from a motor-data dict."""
        angle_deg = float(data.get("angle_deg", 0.0))
        return int(round(angle_deg * 100))

    @staticmethod
    def _switch_on(data: Dict[str, Any], key: str) -> bool:
        es = data.get("end_switches", {})
        return bool(es.get(key, False))

    def _build_sim_state_24(self, motors: list[Dict[str, Any]]) -> int:
        switch_keys = ["-5", "0", "180", "185"]
        state = 0
        for motor_idx, data in enumerate(motors):
            for switch_idx, key in enumerate(switch_keys):
                bit_index = motor_idx * 4 + switch_idx
                if self._switch_on(data, key):
                    state |= (1 << bit_index)
        return state
