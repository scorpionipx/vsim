import socket

from PyQt6.QtCore import QThread, pyqtSignal


class UdpReceiverThread(QThread):
    """
    UDP receiver thread.
    If a packager is provided, it may use its `preview()` method to produce
    a readable string for display in the GUI console.
    """
    received_packet = pyqtSignal(str)
    # NEW: emits 6 status bytes, one per motor (m1..m6)
    motor_statuses = pyqtSignal(list)  # [int, int, int, int, int, int]

    def __init__(self, bind_host: str, bind_port: int, logger, packager=None):
        super().__init__()
        self.bind_host = bind_host
        self.bind_port = bind_port
        self._running = False
        self._sock = None
        self.logger = logger
        self.packager = packager  # can be ReadablePackager or STMPackager

    def run(self):
        self._running = True
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        try:
            self._sock.bind((self.bind_host, self.bind_port))
            self.logger.info(f"Receiver listening on {self.bind_host}:{self.bind_port}")
        except Exception as e:
            self.logger.exception(f"Receiver bind failed on {self.bind_host}:{self.bind_port} -> {e}")
            self._running = False

        self._sock.settimeout(0.25)

        while self._running:
            try:
                data, addr = self._sock.recvfrom(64 * 1024)

                # --- Build preview based on packager ---
                preview = None
                if self.packager and hasattr(self.packager, "preview"):
                    try:
                        preview = self.packager.preview(data)
                    except Exception as e:
                        self.logger.exception(f"Packager preview failed: {e}")

                # fallback if packager missing or failed
                if not preview:
                    preview = data.hex(" ").upper()

                preview = f"[{len(data)} bytes from {addr[0]}:{addr[1]}]\n{preview}"
                self.received_packet.emit(preview)
                if len(data) >= 10:
                    status_bytes = list(data[3:9])  # m1..m6 status bytes
                    self.motor_statuses.emit(status_bytes)
                else:
                    self.logger.warning(f"RX packet too short for statuses: {len(data)} bytes (need >= 10)")

            except socket.timeout:
                continue
            except Exception as e:
                self.logger.exception(f"Error in receiver thread: {e}")

        if self._sock:
            self._sock.close()
            self._sock = None
            self.logger.info("Receiver thread stopped.")

    def stop(self):
        self._running = False
