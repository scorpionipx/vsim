"""
Background poller that reads Assetto Corsa shared memory directly.

This replaces the external Vorosim UDP client: instead of receiving float32
packets over the network, it maps the ``acpmf_*`` blocks locally, samples the
configured signal paths on a fixed interval, and emits a ``{path: value}`` dict
identical in shape to what the UDP path produced. The widget feeds that dict
through the very same mapping/transform/smoothing pipeline.

If Assetto Corsa is not running the mapping fails; the thread reports
``Waiting for Assetto Corsa…`` and keeps retrying until the sim appears, then
again if it goes away, without ever raising out of the loop.
"""

from PyQt6.QtCore import QThread, pyqtSignal

from .shared_memory import ACSharedMemory, split_sections


class AssettoCorsaReaderThread(QThread):
    """Polls AC shared memory and streams named signal values."""

    signals_updated = pyqtSignal(dict)   # {signal_path: float}
    status_changed = pyqtSignal(str)     # human-readable reader status
    preview = pyqtSignal(str)            # throttled text for the console

    def __init__(self, signal_paths, interval_ms: int = 10, logger=None):
        super().__init__()
        self.signal_paths = list(signal_paths)
        self.interval_ms = max(1, int(interval_ms))
        self.logger = logger
        self._running = False
        # console preview is throttled so a 10 ms loop doesn't flood the GUI
        self._preview_every = max(1, int(500 / self.interval_ms))

    def run(self):
        self._running = True
        sections = split_sections(self.signal_paths)
        shm = ACSharedMemory(sections)
        tick = 0

        # liveness: AC bumps packetId every physics step. If it stops moving
        # (sim paused or closed) we hold output and report "waiting" until it
        # resumes; this many stale polls (~1 s) trips the transition.
        last_packet_id = None
        stale_polls = 0
        stale_limit = max(1, int(1000 / self.interval_ms))
        live = False

        self.status_changed.emit("Waiting for Assetto Corsa…")
        if self.logger:
            self.logger.info(
                f"Assetto Corsa reader started (signals={self.signal_paths}, "
                f"interval={self.interval_ms} ms)"
            )

        while self._running:
            # (re)attach to the shared memory; fails while the sim is not running
            if not shm.is_open:
                try:
                    shm.open()
                    last_packet_id = None
                    stale_polls = 0
                    if self.logger:
                        tags = ", ".join(
                            f"{s}->{t}" for s, t in shm.opened_tags().items()
                        )
                        self.logger.info(f"Assetto Corsa shared memory attached ({tags}).")
                except Exception:
                    if live:
                        live = False
                    self.status_changed.emit("Waiting for Assetto Corsa…")
                    self.msleep(500)  # sim not up yet — back off before retrying
                    continue

            try:
                packet_id = shm.read_packet_id()
                named = shm.read_named_signals(self.signal_paths)
            except Exception as e:
                shm.close()
                live = False
                self.status_changed.emit("Waiting for Assetto Corsa…")
                if self.logger:
                    self.logger.warning(f"Assetto Corsa read failed, reconnecting: {e}")
                self.msleep(500)
                continue

            # decide live vs. stale from packetId movement
            if packet_id is None or packet_id != last_packet_id:
                stale_polls = 0
                if not live:
                    live = True
                    self.status_changed.emit("Reading Assetto Corsa")
            else:
                stale_polls += 1
                if live and stale_polls >= stale_limit:
                    live = False
                    self.status_changed.emit("Waiting for Assetto Corsa…")
            last_packet_id = packet_id

            if live and named:
                self.signals_updated.emit(named)
                if tick % self._preview_every == 0:
                    self.preview.emit(
                        "  ".join(f"{k}={v:+.4f}" for k, v in named.items())
                    )
            tick += 1
            self.msleep(self.interval_ms)

        shm.close()
        self.status_changed.emit("Stopped")
        if self.logger:
            self.logger.info("Assetto Corsa reader stopped.")

    def stop(self):
        self._running = False
