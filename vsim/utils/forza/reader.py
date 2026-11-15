"""
Background listener that receives Forza Horizon 5 "Data Out" telemetry.

Assetto Corsa is read by mapping its shared memory and polling on a timer; FH5
has no shared memory and instead *pushes* a UDP datagram per physics tick, so
this reader binds a socket and waits. The emitted signal is deliberately
identical in shape to the Assetto Corsa reader's -- a ``{path: value}`` dict --
so ``VorosimWidget`` feeds it through the very same mapping / transform /
smoothing / rate-limit pipeline, and one ``remote_signals_mapping`` can name
paths from either source.

Game setup: Settings -> HUD & Gameplay -> Data Out -> On, "Data Out IP Address"
= this machine's IP (shown in the panel), "Data Out IP Port" = the port
configured here. FH5 only offers the one packet format, which is what
``packet.py`` decodes.

If the game is not running, is in a menu, or is paused, the reader reports
``Waiting for Forza Horizon 5...`` and keeps listening until packets resume,
without ever raising out of the loop.
"""

from __future__ import annotations

import socket
import time

from PyQt6.QtCore import QThread, pyqtSignal

from .packet import (
    DEFAULT_FORZA_PORT,
    PacketSizeError,
    decode,
    to_signals,
    unknown_paths,
)


# How long without a usable packet before the reader reports that it is waiting.
_STALE_AFTER_S = 1.0

# Console preview throttle: at ~60 Hz an unthrottled preview would flood the GUI.
_PREVIEW_EVERY_S = 0.5

# Socket read timeout. Bounds how long stop() takes to be noticed.
_RECV_TIMEOUT_S = 0.25


class ForzaHorizonReaderThread(QThread):
    """Receives FH5 Data Out datagrams and streams named signal values."""

    signals_updated = pyqtSignal(dict)   # {signal_path: float}
    status_changed = pyqtSignal(str)     # human-readable reader status
    preview = pyqtSignal(str)            # throttled text for the console

    def __init__(self, signal_paths, port: int = DEFAULT_FORZA_PORT,
                 bind_host: str = "127.0.0.1", logger=None):
        super().__init__()
        # Empty means "emit everything"; the widget passes the fh5.* paths its
        # mapping actually references.
        self.signal_paths = list(signal_paths)
        self.port = int(port)
        self.bind_host = bind_host
        self.logger = logger
        self._running = False
        self._sock = None

    def run(self):
        self._running = True
        wanted = self.signal_paths or None

        if self.logger:
            bad = unknown_paths(self.signal_paths)
            if bad:
                self.logger.warning(
                    f"Forza Horizon reader: unknown signal path(s) {bad} - "
                    f"these will never produce a value."
                )

        sock = self._open_socket()
        if sock is None:
            # _open_socket has already reported the failure. Leaving _running
            # set here would claim a listener that does not exist.
            self._running = False
            return

        self.status_changed.emit("Waiting for Forza Horizon 5...")
        if self.logger:
            self.logger.info(
                f"Forza Horizon reader listening on {self.bind_host}:{self.port} "
                f"(signals={self.signal_paths or 'all'})"
            )

        live = False
        last_good = 0.0
        last_preview = 0.0
        warned_size = False

        while self._running:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                # No datagram this cycle. The game being closed, paused or in a
                # menu all look the same from here: stop claiming we are live.
                if live and time.monotonic() - last_good > _STALE_AFTER_S:
                    live = False
                    self.status_changed.emit("Waiting for Forza Horizon 5...")
                continue
            except OSError:
                break

            try:
                pkt = decode(data)
            except PacketSizeError as e:
                # Wrong game / wrong format: report once rather than per packet.
                if not warned_size:
                    warned_size = True
                    self.status_changed.emit(f"Unrecognised packet: {e}")
                    if self.logger:
                        self.logger.warning(
                            f"Forza Horizon reader: {e} (from {addr[0]}:{addr[1]})"
                        )
                continue

            now = time.monotonic()

            # FH5 keeps streaming while paused or sitting in a menu, with
            # IsRaceOn cleared. Hold the output in that state rather than
            # feeding a frozen pose to the rig.
            if not pkt.IsRaceOn:
                if live:
                    live = False
                    self.status_changed.emit("Forza connected - not driving")
                continue

            last_good = now
            if not live:
                live = True
                warned_size = False
                self.status_changed.emit("Reading Forza Horizon 5")

            named = to_signals(pkt, wanted)
            if named:
                self.signals_updated.emit(named)
                if now - last_preview >= _PREVIEW_EVERY_S:
                    last_preview = now
                    self.preview.emit(
                        "  ".join(f"{k}={v:+.4f}" for k, v in named.items())
                    )

        self._close_socket()
        self.status_changed.emit("Stopped")
        if self.logger:
            self.logger.info("Forza Horizon reader stopped.")

    # ------------------------------------------------------------------
    def _open_socket(self):
        """Bind the listening socket, or report why it could not be bound.

        Returns the socket, or ``None`` on failure. The status signal carries
        the reason so the panel shows a bind error instead of silently looking
        like a healthy listener.
        """
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.bind_host, self.port))
            sock.settimeout(_RECV_TIMEOUT_S)
        except OSError as e:
            if sock is not None:
                sock.close()
            self.status_changed.emit(f"Bind failed on port {self.port}: {e}")
            if self.logger:
                self.logger.exception(
                    f"Forza Horizon reader bind failed on "
                    f"{self.bind_host}:{self.port} -> {e}"
                )
            return None
        self._sock = sock
        return sock

    def _close_socket(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def stop(self):
        self._running = False
