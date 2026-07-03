# vsim/utils/remote_control/vorosim/core.py
"""
Vorosim remote-control widget.

A self-contained panel for accepting a single inbound UDP peer and logging the
datagrams it sends. Exposes:

- an editable port field (default sourced from config.json -> ``vorosim_port``)
- a label showing this machine's current IP address
- a Start/Stop button that runs a listener accepting a *single* connection
- a label reflecting the listener status (idle / listening / connected / ...)
- a console (similar to the main window's consoles) that logs incoming packets
- a toggle to enable/disable logging of incoming packets
- a toggle to allow motor control: when enabled, raw value packets are unpacked
  according to ``remote_signals_mapping`` and pushed to the platform/motors

Protocol from the Vorosim client (over the single UDP peer):
- "Vorosim: v{version}"  — handshake (text)
- "Signals: a,b,c"       — names of the streamed signals, in order (text, once)
- raw little-endian float32 packet per update — one value per announced signal

Drop ``VorosimWidget`` into vsim/core.py's layout.
"""

import math
import socket
import struct
import time

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QEvent
from PyQt6.QtGui import QIntValidator, QDoubleValidator
from PyQt6.QtWidgets import (
    QGroupBox, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QLineEdit, QTextEdit, QCheckBox, QSlider,
)

from vsim.utils.assetto_corsa import AssettoCorsaReaderThread


DEFAULT_VOROSIM_PORT = 6767
DEFAULT_AC_INTERVAL_MS = 10

# Max lines kept in the incoming-packets console (append-only QTextEdit). Caps
# its document so streaming packets/previews can't grow RAM without bound.
MAX_CONSOLE_LINES = 1000


def get_local_ip() -> str:
    """Best-effort local IPv4 address of this machine.

    Opens a throwaway UDP socket to a public address so the OS picks the
    outbound interface; no packets are actually sent. Falls back to loopback
    when offline.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def _decode_preview(data: bytes) -> str:
    """Render a datagram for the console: readable text when it decodes as
    printable UTF-8 (e.g. the "Vorosim: v1.0.0" handshake), else a hex dump."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data.hex(" ").upper()
    # text only if every char is printable or standard whitespace, else hex
    if text and all(ch.isprintable() or ch in "\r\n\t" for ch in text):
        return text
    return data.hex(" ").upper()


class VorosimListenerThread(QThread):
    """UDP listener that locks onto a *single* peer and streams the datagrams it
    receives. UDP is connectionless, so "single connection" is emulated: the
    first datagram's sender becomes the bound peer and datagrams from any other
    address are ignored. Stops when ``stop()`` is called.
    """

    status_changed = pyqtSignal(str)     # human-readable listener status
    client_connected = pyqtSignal(str)   # "host:port" of the locked peer
    packet_received = pyqtSignal(str)     # preview of an incoming packet
    datagram_received = pyqtSignal(bytes)  # raw bytes of an incoming packet

    def __init__(self, host: str, port: int, logger=None):
        super().__init__()
        self.host = host
        self.port = port
        self.logger = logger
        self._running = False
        self._sock = None

    def run(self):
        self._running = True
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind((self.host, self.port))
            self._sock.settimeout(0.25)
            self.status_changed.emit(f"Listening on {self.host}:{self.port}")
            if self.logger:
                self.logger.info(f"Vorosim listener started on {self.host}:{self.port}")
        except Exception as e:
            self.status_changed.emit(f"Bind failed: {e}")
            if self.logger:
                self.logger.exception(f"Vorosim listener bind failed: {e}")
            self._running = False

        # --- lock onto the first sender, then accept only its datagrams ---
        peer_addr = None
        while self._running:
            try:
                data, addr = self._sock.recvfrom(64 * 1024)
            except socket.timeout:
                continue
            except OSError:
                break

            if peer_addr is None:
                peer_addr = addr
                peer = f"{addr[0]}:{addr[1]}"
                self.client_connected.emit(peer)
                self.status_changed.emit(f"Client connected: {peer}")
                if self.logger:
                    self.logger.info(f"Vorosim client connected: {peer}")
            elif addr != peer_addr:
                # already locked onto a single peer; drop everyone else
                if self.logger:
                    self.logger.warning(
                        f"Vorosim ignoring datagram from {addr[0]}:{addr[1]} "
                        f"(locked to {peer_addr[0]}:{peer_addr[1]})"
                    )
                continue

            peer = f"{peer_addr[0]}:{peer_addr[1]}"
            preview = f"[{len(data)} bytes from {peer}]\n{_decode_preview(data)}"
            self.packet_received.emit(preview)
            self.datagram_received.emit(data)

        self._cleanup()

    def _cleanup(self):
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self.status_changed.emit("Stopped")

    def stop(self):
        self._running = False


class AxisTransformWidget(QGroupBox):
    """Per-axis transform from a raw remote value to an angle.

    angle = raw * gain + offset, then clamped to [min, max]. Offset defaults to
    0 and gain to 1; min/max are optional (blank field = no clamp on that side).
    """

    # emitted when the user finishes editing any field (used to persist values)
    changed = pyqtSignal()

    def __init__(self, title: str, parent=None):
        super().__init__(title, parent)
        self.offset_edit = self._num_field("0")
        self.gain_edit = self._num_field("1")
        self.min_edit = self._num_field("")   # blank -> no lower clamp
        self.max_edit = self._num_field("")   # blank -> no upper clamp

        for edit in (self.offset_edit, self.gain_edit, self.min_edit, self.max_edit):
            edit.editingFinished.connect(self.changed)

        # Display-only tracker of the lowest/highest value actually set on this
        # axis (after transforms/smoothing/limits). Double-click to reset.
        self._set_min = None
        self._set_max = None
        self.set_range_label = QLabel("—")
        self.set_range_label.setStyleSheet(
            "font-family: Consolas, monospace; color: gray;"
        )
        self.set_range_label.setToolTip(
            "Lowest / highest value set on this axis so far. "
            "Double-click to reset."
        )
        self.set_range_label.installEventFilter(self)

        grid = QGridLayout()
        grid.setContentsMargins(6, 6, 6, 6)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(4)
        for r, (label, widget) in enumerate((
            ("Offset", self.offset_edit),
            ("Gain", self.gain_edit),
            ("Min", self.min_edit),
            ("Max", self.max_edit),
            ("Set", self.set_range_label),
        )):
            lbl = QLabel(label)
            lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(lbl, r, 0)
            grid.addWidget(widget, r, 1)
        self.setLayout(grid)

    def eventFilter(self, obj, event):
        if obj is self.set_range_label and event.type() == QEvent.Type.MouseButtonDblClick:
            self.reset_set_range()
            return True
        return super().eventFilter(obj, event)

    def record_set_value(self, value: float) -> None:
        """Track the min/max of the values set on this axis and refresh the
        display label when either peak moves."""
        changed = False
        if self._set_min is None or value < self._set_min:
            self._set_min = value
            changed = True
        if self._set_max is None or value > self._set_max:
            self._set_max = value
            changed = True
        if changed:
            self.set_range_label.setText(
                f"{self._set_min:+.2f} / {self._set_max:+.2f}"
            )

    def reset_set_range(self) -> None:
        self._set_min = None
        self._set_max = None
        self.set_range_label.setText("—")

    @staticmethod
    def _num_field(default: str) -> QLineEdit:
        edit = QLineEdit(default)
        validator = QDoubleValidator()
        validator.setNotation(QDoubleValidator.Notation.StandardNotation)
        edit.setValidator(validator)
        edit.setMaximumWidth(70)
        return edit

    @staticmethod
    def _value(edit: QLineEdit, default):
        text = edit.text().strip().replace(",", ".")
        if text == "":
            return default
        try:
            return float(text)
        except ValueError:
            return default

    def transform(self, raw: float) -> float:
        """Map a raw value to an angle, applying gain/offset and optional clamp."""
        angle = raw * self._value(self.gain_edit, 1.0) + self._value(self.offset_edit, 0.0)
        lo = self._value(self.min_edit, None)
        hi = self._value(self.max_edit, None)
        if lo is not None and angle < lo:
            angle = lo
        if hi is not None and angle > hi:
            angle = hi
        return angle

    def get_values(self) -> dict:
        """Current field values as strings (blank min/max preserved as "")."""
        return {
            "offset": self.offset_edit.text().strip(),
            "gain": self.gain_edit.text().strip(),
            "min": self.min_edit.text().strip(),
            "max": self.max_edit.text().strip(),
        }

    def set_values(self, vals: dict) -> None:
        """Load saved field values (numbers or strings; missing keys ignored)."""
        if not vals:
            return
        for key, edit in (("offset", self.offset_edit), ("gain", self.gain_edit),
                          ("min", self.min_edit), ("max", self.max_edit)):
            if key in vals and vals[key] is not None:
                edit.setText(str(vals[key]))


class VorosimWidget(QGroupBox):
    """Panel for accepting a single Vorosim connection and logging its packets."""

    def __init__(self, port: int = DEFAULT_VOROSIM_PORT, logger=None,
                 signals_mapping: dict = None, pose_callback=None,
                 ac_interval_ms: int = DEFAULT_AC_INTERVAL_MS,
                 axis_transforms: dict = None, smoothing: dict = None,
                 rate_limit: dict = None,
                 save_settings_callback=None, parent=None):
        super().__init__("Vorosim Remote Control", parent)
        self.logger = logger
        self._listener = None
        self._logging_enabled = True

        # Persisted Vorosim settings: initial values loaded from config, and a
        # callback to write changes (axis transforms + smoothing) back to
        # config.json.
        self._axis_transforms = dict(axis_transforms or {})
        self._smoothing_init = dict(smoothing or {})
        self._rate_limit_init = dict(rate_limit or {})
        self._save_settings_callback = save_settings_callback

        # Direct Assetto Corsa shared-memory reader (bypasses the UDP path).
        self._ac_reader = None
        self._ac_interval_ms = max(1, int(ac_interval_ms))

        # Motor control: maps incoming signal name -> role (e.g. pitch/yaw/roll).
        # pose_callback(roles: dict) applies the roles to the platform/motors.
        self._mapping = dict(signals_mapping or {})
        self._pose_callback = pose_callback
        self._motor_control_enabled = False
        self._signal_order: list[str] = []  # names announced by "Signals:" msg

        # Low-pass (EMA) smoothing of the per-axis angles. Per-role state holds
        # the last filtered value; cleared when smoothing is off or on reconnect.
        self._smoothing_enabled = False
        self._filter_state: dict[str, float] = {}

        # Rate limiting (speed / acceleration caps) of the per-axis angles,
        # applied after smoothing. Per-role state holds the last commanded
        # position, velocity and timestamp; cleared on toggle or reconnect.
        self._rate_state: dict[str, dict] = {}

        root = QVBoxLayout()
        root.addLayout(self._build_controls(port))
        root.addLayout(self._build_ac_controls())
        root.addLayout(self._build_transforms())
        root.addLayout(self._build_smoothing())
        root.addLayout(self._build_rate_limit())
        root.addWidget(self._build_console())
        self.setLayout(root)

    # ---------- UI builders ----------
    def _build_controls(self, port: int):
        row = QHBoxLayout()

        # The legacy UDP remote (Vorosim client over the network) is hidden by
        # default now that Assetto Corsa is read directly. This button reveals
        # the listener controls on demand.
        self.udp_toggle_btn = QPushButton("Enable remote via UDP")
        self.udp_toggle_btn.setCheckable(True)
        self.udp_toggle_btn.toggled.connect(self._on_udp_toggle)
        row.addWidget(self.udp_toggle_btn)

        port_label = QLabel("Port:")
        row.addWidget(port_label)
        self.port_edit = QLineEdit(str(port))
        self.port_edit.setValidator(QIntValidator(1, 65535, self))
        self.port_edit.setMaximumWidth(80)
        row.addWidget(self.port_edit)

        ip_title = QLabel("IP Address:")
        row.addWidget(ip_title)
        self.ip_label = QLabel(get_local_ip())
        self.ip_label.setStyleSheet("font-family: Consolas, monospace; font-weight: bold;")
        self.ip_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(self.ip_label)

        self.start_btn = QPushButton("Start Listener")
        self.start_btn.clicked.connect(self._toggle_listener)
        row.addWidget(self.start_btn)

        status_title = QLabel("Status:")
        row.addWidget(status_title)
        self.status_label = QLabel("Idle")
        self.status_label.setStyleSheet("font-weight: bold;")
        row.addWidget(self.status_label)

        # widgets toggled by "Enable remote via UDP" — hidden until enabled
        self._udp_widgets = [
            port_label, self.port_edit, ip_title, self.ip_label,
            self.start_btn, status_title, self.status_label,
        ]
        for w in self._udp_widgets:
            w.setVisible(False)

        row.addStretch(1)
        return row

    def _on_udp_toggle(self, checked: bool):
        """Show/hide the legacy UDP listener controls. Turning the section off
        also stops any running listener so it can't keep accepting datagrams
        while hidden."""
        for w in self._udp_widgets:
            w.setVisible(checked)
        self.udp_toggle_btn.setText(
            "Disable remote via UDP" if checked else "Enable remote via UDP"
        )
        if not checked:
            self.stop_listener()

    def _build_ac_controls(self):
        """Controls for reading Assetto Corsa shared memory directly, with no
        UDP hop. Reuses the same mapping / transforms / smoothing as the UDP
        path; only the source of the signal values differs."""
        row = QHBoxLayout()

        self.ac_start_btn = QPushButton("Read Assetto Corsa (direct)")
        self.ac_start_btn.setToolTip(
            "Read AC's acpmf_* shared memory locally instead of receiving the "
            "Vorosim client's telemetry over UDP. Requires Assetto Corsa to be "
            "running on this machine."
        )
        self.ac_start_btn.clicked.connect(self._toggle_ac_reader)
        row.addWidget(self.ac_start_btn)

        row.addWidget(QLabel("Status:"))
        self.ac_status_label = QLabel("Idle")
        self.ac_status_label.setStyleSheet("font-weight: bold;")
        row.addWidget(self.ac_status_label)

        row.addStretch(1)
        return row

    def _build_transforms(self):
        """Row of per-axis (Pitch/Roll/Yaw) raw-value -> angle transforms.

        Initial values are loaded from the persisted ``axis_transforms`` config;
        edits are written back via ``save_settings_callback``."""
        row = QHBoxLayout()
        self._transforms: dict[str, AxisTransformWidget] = {}
        for role, title in (("pitch", "Pitch"), ("roll", "Roll"), ("yaw", "Yaw")):
            w = AxisTransformWidget(title)
            w.set_values(self._axis_transforms.get(role))
            w.changed.connect(self._persist_settings)
            self._transforms[role] = w
            row.addWidget(w)
        row.addStretch(1)
        return row

    def _persist_settings(self):
        """Collect the Pitch/Roll/Yaw transforms and smoothing settings and
        hand them to the save callback (which writes config.json). No-op
        without a callback."""
        if not self._save_settings_callback:
            return
        settings = {
            "axis_transforms": {
                role: w.get_values() for role, w in self._transforms.items()
            },
            "smoothing": {
                "enabled": self.smooth_toggle.isChecked(),
                "strength": self.smooth_slider.value(),
            },
            "rate_limit": {
                "speed_enabled": self.speed_limit_toggle.isChecked(),
                "max_speed_dps": self.max_speed_edit.text().strip(),
                "accel_enabled": self.accel_limit_toggle.isChecked(),
                "max_accel_dps2": self.max_accel_edit.text().strip(),
            },
        }
        try:
            self._save_settings_callback(settings)
        except Exception as e:
            if self.logger:
                self.logger.exception(f"Failed to persist Vorosim settings: {e}")

    def _build_smoothing(self):
        """Toggle + slider for the low-pass (EMA) smoothing of output angles.

        Initial state is loaded from the persisted ``smoothing`` config; changes
        are written back via ``save_settings_callback``."""
        enabled = bool(self._smoothing_init.get("enabled", False))
        strength = max(0, min(100, int(self._smoothing_init.get("strength", 50))))

        row = QHBoxLayout()
        self.smooth_toggle = QCheckBox("Smooth (low-pass)")
        self.smooth_toggle.setChecked(enabled)
        self._smoothing_enabled = enabled
        self.smooth_toggle.setToolTip(
            "Exponential low-pass filter on the output angles to reduce jitter."
        )
        self.smooth_toggle.stateChanged.connect(self._on_smooth_toggle)
        self.smooth_toggle.stateChanged.connect(self._persist_settings)
        row.addWidget(self.smooth_toggle)

        row.addWidget(QLabel("Strength:"))
        self.smooth_slider = QSlider(Qt.Orientation.Horizontal)
        self.smooth_slider.setRange(0, 100)
        self.smooth_slider.setValue(strength)
        self.smooth_slider.setMaximumWidth(220)
        self.smooth_slider.setToolTip("0 = no smoothing, 100 = maximum smoothing")
        self.smooth_slider.valueChanged.connect(self._on_smooth_slider)
        # persist only when the drag ends, so a drag doesn't rewrite per pixel
        self.smooth_slider.sliderReleased.connect(self._persist_settings)
        row.addWidget(self.smooth_slider)

        self.smooth_value_label = QLabel()
        self.smooth_value_label.setStyleSheet("font-family: Consolas, monospace;")
        row.addWidget(self.smooth_value_label)

        row.addStretch(1)
        self._update_smooth_label()
        return row

    def _build_rate_limit(self):
        """Toggles + fields for the per-axis speed and acceleration caps applied
        to the output angles (after smoothing).

        Initial state is loaded from the persisted ``rate_limit`` config;
        changes are written back via ``save_settings_callback``."""
        init = self._rate_limit_init

        def _num_field(value) -> QLineEdit:
            edit = QLineEdit(str(value))
            validator = QDoubleValidator(0.0, 100000.0, 2)
            validator.setNotation(QDoubleValidator.Notation.StandardNotation)
            edit.setValidator(validator)
            edit.setMaximumWidth(70)
            edit.editingFinished.connect(self._persist_settings)
            return edit

        row = QHBoxLayout()

        self.speed_limit_toggle = QCheckBox("Limit speed")
        self.speed_limit_toggle.setChecked(bool(init.get("speed_enabled", False)))
        self.speed_limit_toggle.setToolTip(
            "Cap how fast each output angle may change (°/s). Normal motion "
            "passes through; only spikes get slowed down."
        )
        self.speed_limit_toggle.stateChanged.connect(self._on_rate_limit_toggle)
        self.speed_limit_toggle.stateChanged.connect(self._persist_settings)
        row.addWidget(self.speed_limit_toggle)

        row.addWidget(QLabel("Max speed (°/s):"))
        self.max_speed_edit = _num_field(init.get("max_speed_dps", 60))
        row.addWidget(self.max_speed_edit)

        row.addSpacing(16)

        self.accel_limit_toggle = QCheckBox("Limit accel")
        self.accel_limit_toggle.setChecked(bool(init.get("accel_enabled", False)))
        self.accel_limit_toggle.setToolTip(
            "Cap how fast each output angle's speed may change (°/s²). Softens "
            "jerk: the platform ramps up and brakes instead of snapping."
        )
        self.accel_limit_toggle.stateChanged.connect(self._on_rate_limit_toggle)
        self.accel_limit_toggle.stateChanged.connect(self._persist_settings)
        row.addWidget(self.accel_limit_toggle)

        row.addWidget(QLabel("Max accel (°/s²):"))
        self.max_accel_edit = _num_field(init.get("max_accel_dps2", 200))
        row.addWidget(self.max_accel_edit)

        row.addStretch(1)
        return row

    def _build_console(self):
        group = QGroupBox("Incoming Packets")
        v = QVBoxLayout()

        top_bar = QHBoxLayout()
        self.log_toggle = QCheckBox("Log incoming packets")
        self.log_toggle.setChecked(True)
        self.log_toggle.stateChanged.connect(self._on_log_toggle)
        top_bar.addWidget(self.log_toggle)

        self.motor_control_toggle = QCheckBox("Allow motor control")
        self.motor_control_toggle.setChecked(False)
        self.motor_control_toggle.setToolTip(
            "When enabled, unpack received values per remote_signals_mapping "
            "and drive the platform/motors."
        )
        self.motor_control_toggle.stateChanged.connect(self._on_motor_control_toggle)
        top_bar.addWidget(self.motor_control_toggle)

        top_bar.addStretch(1)
        clear_btn = QPushButton("Clear")
        top_bar.addWidget(clear_btn)
        v.addLayout(top_bar)

        self.console = QTextEdit()
        self.console.setReadOnly(True)
        self.console.setStyleSheet("QTextEdit { font-family: Consolas, monospace; }")
        # Bound memory: keep only the last MAX_CONSOLE_LINES blocks so the
        # console can't grow without limit as packets stream in.
        self.console.document().setMaximumBlockCount(MAX_CONSOLE_LINES)
        v.addWidget(self.console)

        clear_btn.clicked.connect(self.console.clear)
        group.setLayout(v)
        return group

    # ---------- listener control ----------
    def port(self) -> int:
        try:
            return int(self.port_edit.text())
        except (TypeError, ValueError):
            return DEFAULT_VOROSIM_PORT

    def _toggle_listener(self):
        if self._listener and self._listener.isRunning():
            self.stop_listener()
        else:
            self.start_listener()

    def start_listener(self):
        if self._listener and self._listener.isRunning():
            return

        # bind on all interfaces so the advertised IP (and loopback) both work
        self._listener = VorosimListenerThread("0.0.0.0", self.port(), self.logger)
        self._listener.status_changed.connect(self._set_status)
        self._listener.packet_received.connect(self._append_packet)
        self._listener.datagram_received.connect(self._on_datagram)
        self._listener.finished.connect(self._on_listener_finished)
        self._listener.start()

        self.start_btn.setText("Stop Listener")
        self.port_edit.setEnabled(False)

    def stop_listener(self):
        if self._listener and self._listener.isRunning():
            self._listener.stop()
            self._listener.wait(2000)

    def _on_listener_finished(self):
        self.start_btn.setText("Start Listener")
        self.port_edit.setEnabled(True)
        self._listener = None

    # ---------- Assetto Corsa direct reader ----------
    def _toggle_ac_reader(self):
        if self._ac_reader and self._ac_reader.isRunning():
            self.stop_ac_reader()
        else:
            self.start_ac_reader()

    def start_ac_reader(self):
        if self._ac_reader and self._ac_reader.isRunning():
            return
        if not self._mapping:
            self._set_ac_status("No remote_signals_mapping configured")
            if self.logger:
                self.logger.warning(
                    "Assetto Corsa reader: remote_signals_mapping is empty; "
                    "nothing to read."
                )
            return

        # the mapping keys are the dotted shared-memory paths to sample
        self._ac_reader = AssettoCorsaReaderThread(
            list(self._mapping.keys()), self._ac_interval_ms, self.logger
        )
        self._ac_reader.signals_updated.connect(self.apply_named_signals)
        self._ac_reader.status_changed.connect(self._set_ac_status)
        self._ac_reader.preview.connect(self._append_ac_preview)
        self._ac_reader.finished.connect(self._on_ac_reader_finished)
        self._ac_reader.start()

        self.ac_start_btn.setText("Stop Assetto Corsa")

    def stop_ac_reader(self):
        if self._ac_reader and self._ac_reader.isRunning():
            self._ac_reader.stop()
            self._ac_reader.wait(2000)

    def _on_ac_reader_finished(self):
        self.ac_start_btn.setText("Read Assetto Corsa (direct)")
        self._ac_reader = None

    def _set_ac_status(self, text: str):
        self.ac_status_label.setText(text)

    def _append_ac_preview(self, text: str):
        if self._logging_enabled:
            self.console.append(f"[AC] {text}")

    # ---------- slots ----------
    def _set_status(self, text: str):
        self.status_label.setText(text)

    def _on_log_toggle(self, state):
        self._logging_enabled = state == Qt.CheckState.Checked.value

    def _append_packet(self, text: str):
        if self._logging_enabled:
            self.console.append(text)

    def _on_motor_control_toggle(self, state):
        self._motor_control_enabled = state == Qt.CheckState.Checked.value

    # ---------- smoothing (low-pass filter) ----------
    def _on_smooth_toggle(self, state):
        self._smoothing_enabled = state == Qt.CheckState.Checked.value
        # start clean so re-enabling doesn't snap from a stale value
        self._filter_state.clear()

    def _on_smooth_slider(self, _value):
        self._update_smooth_label()

    def _smoothing_alpha(self) -> float:
        """EMA coefficient in (0, 1]. Slider 0 -> 1.0 (passthrough), 100 -> heavy
        smoothing. Smaller alpha tracks the input more slowly."""
        return max(0.01, 1.0 - self.smooth_slider.value() / 100.0)

    def _update_smooth_label(self):
        self.smooth_value_label.setText(
            f"{self.smooth_slider.value():>3d}%  (α={self._smoothing_alpha():.2f})"
        )

    def _smooth(self, role: str, value: float) -> float:
        alpha = self._smoothing_alpha()
        prev = self._filter_state.get(role)
        filtered = value if prev is None else prev + alpha * (value - prev)
        self._filter_state[role] = filtered
        return filtered

    # ---------- rate limiting (speed / acceleration caps) ----------
    def _on_rate_limit_toggle(self, _state):
        # start clean so re-enabling doesn't chase a stale position
        self._rate_state.clear()

    @staticmethod
    def _limit_value(edit: QLineEdit) -> float:
        """Parse a limit field. Blank or non-positive means "no limit" (inf)."""
        try:
            v = float(edit.text().strip().replace(",", "."))
        except ValueError:
            return math.inf
        return v if v > 0 else math.inf

    def _rate_limit(self, role: str, target: float) -> float:
        """Move the commanded angle toward *target* no faster than the enabled
        speed (°/s) and acceleration (°/s²) caps allow. dt comes from wall time
        between packets, clamped so a stream pause can't let a jump through."""
        speed_on = self.speed_limit_toggle.isChecked()
        accel_on = self.accel_limit_toggle.isChecked()
        if not speed_on and not accel_on:
            return target

        now = time.monotonic()
        st = self._rate_state.get(role)
        if st is None:
            self._rate_state[role] = {"pos": target, "vel": 0.0, "t": now}
            return target
        dt = min(max(now - st["t"], 1e-3), 0.1)
        st["t"] = now

        err = target - st["pos"]
        vel = err / dt  # velocity that would reach the target this tick
        if accel_on:
            a_max = self._limit_value(self.max_accel_edit)
            if math.isfinite(a_max):
                # never faster than what can still brake to a stop at target
                brake = math.sqrt(2.0 * a_max * abs(err))
                vel = max(-brake, min(brake, vel))
                # velocity may only change by a_max*dt per tick
                dv = a_max * dt
                vel = max(st["vel"] - dv, min(st["vel"] + dv, vel))
        if speed_on:
            v_max = self._limit_value(self.max_speed_edit)
            vel = max(-v_max, min(v_max, vel))

        st["vel"] = vel
        st["pos"] += vel * dt
        return st["pos"]

    # ---------- incoming data handling ----------
    def _on_datagram(self, data: bytes):
        """Route an incoming datagram: handshake / signal-list (text) update the
        decode state; everything else is treated as a raw float32 value packet."""
        if data.startswith(b"Vorosim:"):
            # (re)connection handshake — the signal order will be re-announced
            self._signal_order = []
            self._filter_state.clear()
            self._rate_state.clear()
            return
        if data.startswith(b"Signals:"):
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                return
            self._signal_order = [
                n.strip() for n in text[len("Signals:"):].split(",") if n.strip()
            ]
            if self.logger:
                self.logger.info(f"Vorosim signal order: {self._signal_order}")
            return
        self._handle_values(data)

    def _handle_values(self, data: bytes):
        """Unpack a raw little-endian float32 packet, map values to roles via
        ``remote_signals_mapping``, and apply them to the platform/motors."""
        if not self._signal_order:
            return

        count = len(data) // 4
        if count == 0:
            return
        try:
            values = struct.unpack(f"<{count}f", data[:count * 4])
        except struct.error:
            return

        # position in the packet == position in the announced signal list
        named = {
            name: values[i]
            for i, name in enumerate(self._signal_order)
            if i < len(values)
        }
        self.apply_named_signals(named)

    def apply_named_signals(self, named: dict):
        """Map a ``{signal_name: value}`` dict to roles via
        ``remote_signals_mapping``, transform/smooth each axis, and push the
        resulting pose to the platform/motors.

        Shared by both sources: the UDP Vorosim client (``_handle_values``) and
        the direct Assetto Corsa reader (``AssettoCorsaReaderThread``). Runs in
        the GUI thread (queued signal), so touching widgets here is safe.
        """
        if not self._motor_control_enabled or not self._pose_callback:
            return
        if not self._mapping:
            return

        # raw signal value -> role, transformed into an angle per-axis
        roles = {}
        for sig_name, role in self._mapping.items():
            if sig_name not in named:
                continue
            raw = named[sig_name]
            tr = self._transforms.get(role)
            angle = tr.transform(raw) if tr else raw
            if self._smoothing_enabled:
                angle = self._smooth(role, angle)
            angle = self._rate_limit(role, angle)
            roles[role] = angle
            if tr:
                tr.record_set_value(angle)
        if roles:
            self._pose_callback(roles)

    def closeEvent(self, e):
        self.stop_listener()
        self.stop_ac_reader()
        super().closeEvent(e)
