import socket
import time

from PyQt6.QtCore import QThread, pyqtSignal


SEND_INTERVALS = [
    ("1 ms", 1),
    ("10 ms", 10),
    ("50 ms", 50),
    ("100 ms", 100),
    ("500 ms", 500),
    ("1 s", 1000),
    ("2 s", 2000),
    ("5 s", 5000),
    ("10 s", 10000),
]


class UdpSenderThread(QThread):
    """Sends UDP packets built from per-motor data snapshots.

    Qt widgets are not thread-safe, so this thread never touches them. The GUI
    owns the widgets and pushes their state in via ``set_target`` /
    ``set_interval`` / ``update_motor_data`` (each a single assignment of an
    immutable object, safe under the GIL); the send loop reads the latest
    values every cycle.
    """

    sent_packet = pyqtSignal(str)  # keep as str for GUI preview
    log = pyqtSignal(str)

    def __init__(self, host, port, interval_ms, motor_data, logger, packager):
        super().__init__()
        self._target = (str(host).strip(), int(port))
        self._interval_ms = int(interval_ms)
        self._motor_data = list(motor_data)
        self.logger = logger
        self.packager = packager  # must return bytes from build_packet()
        self._running = False
        self._sock = None

    # ---- setters, called from the GUI thread while the loop runs ----
    def set_target(self, host: str, port: int):
        self._target = (str(host).strip(), int(port))

    def set_interval(self, interval_ms: int):
        self._interval_ms = int(interval_ms)

    def update_motor_data(self, motor_data: list):
        """Replace the snapshot (list of ``MotorWidget.get_data()`` dicts)
        the next packets are built from."""
        self._motor_data = list(motor_data)

    def run(self):
        self._running = True
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.logger.info("UDP sender thread started.")

        while self._running:
            try:
                host, port = self._target
                interval_ms = self._interval_ms

                # Build bytes packet from the latest motor-data snapshot
                packet_bytes = self.packager.build_packet(self._motor_data)

                # Send raw bytes
                self._sock.sendto(packet_bytes, (host, port))

                # Emit a preview for the GUI (utf-8 if possible, else hex)
                preview = getattr(self.packager, "preview", None)
                if callable(preview):
                    self.sent_packet.emit(preview(packet_bytes))
                else:
                    # Fallback preview
                    try:
                        self.sent_packet.emit(packet_bytes.decode("utf-8"))
                    except UnicodeDecodeError:
                        self.sent_packet.emit(packet_bytes.hex(" "))

                # precise-ish sleep
                target = time.perf_counter() + (interval_ms / 1000.0)
                remaining = target - time.perf_counter()
                if remaining > 0:
                    time.sleep(remaining)

            except Exception as e:
                self.logger.exception(f"Error in sender thread: {e}")
                time.sleep(0.1)

        try:
            if self._sock:
                self._sock.close()
        finally:
            self._sock = None
            self.logger.info("UDP sender thread stopped.")

    def stop(self):
        self._running = False
