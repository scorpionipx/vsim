import json
import os
import sys
import logging
from datetime import datetime

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QLineEdit, QComboBox, QSpinBox, QTextEdit,
    QGroupBox, QCheckBox, QDialog
)


from vsim.utils.inverse_kinematics.algo3.controller.controller import (
    StewartPlatformKinematics,
    build_default_config,
    test_x, test_y, test_z,
    test_roll, test_pitch, test_yaw,
)


from vsim.utils.log_handler.core import QtTextEditLogHandler
from vsim.utils.udp.packagers import (
    HumanReadablePackager,
    STMPackager,
)
from vsim.utils.udp.sender import (
    SEND_INTERVALS,
    UdpSenderThread,
)
from vsim.utils.udp.receiver import UdpReceiverThread
from vsim.utils.widgets.motor_widget import MotorWidget
from vsim.utils.widgets.calibration_widget import CalibrationWidget
from vsim.utils.remote_control.vorosim.core import (
    VorosimWidget,
    DEFAULT_VOROSIM_PORT,
)

from vsim.version import __version__
from PyQt6.QtCore import QObject, QThread, QTimer, QMetaObject, pyqtSignal, pyqtSlot



# Available kinematics controllers. Each entry maps a display name (shown in the
# UI dropdown) to the kinematics class, its geometry config, and the per-DOF test
# runners used to generate the motion dataset.
CONTROLLERS: dict = {
    "Algorithm 3": {
        "cls": StewartPlatformKinematics,
        "config": build_default_config(),
        "tests": {
            "x": test_x, "y": test_y, "z": test_z,
            "roll": test_roll, "pitch": test_pitch, "yaw": test_yaw,
        },
    },
}

DEFAULT_CONTROLLER = "Algorithm 3"

# DOF / waveform / step count used to build the motion dataset for whichever
# controller is active.
DATASET_DOF = "x"
DATASET_STEPS = 384

# Max lines kept in each on-screen console. Consoles are append-only QTextEdits,
# so without a cap their documents (and RAM use) grow unbounded as logs/packets
# stream in. Qt removes the oldest blocks once this limit is exceeded.
MAX_CONSOLE_LINES = 1000


class PeriodicWorker(QObject):
    tick = pyqtSignal()
    finished = pyqtSignal()

    def __init__(self, interval_ms=500):
        super().__init__()
        self.interval_ms = interval_ms
        self._timer = None

    @pyqtSlot()
    def start(self):
        """Called when the thread starts. Creates the QTimer here so it lives
        in (and is owned by) the worker thread."""
        self._timer = QTimer()
        self._timer.timeout.connect(self._do_work)
        self._timer.start(self.interval_ms)

    def _do_work(self):
        # Emit signal (safe way to communicate with GUI)
        self.tick.emit()

    @pyqtSlot()
    def stop(self):
        """Stop the timer and signal completion. MUST run in the worker thread
        (the timer is thread-affine) — callers invoke it via a queued
        connection, never directly from the GUI thread."""
        if self._timer:
            self._timer.stop()
            self._timer = None
        self.finished.emit()


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"vsim — Motor UDP Sender/Receiver v{__version__}")
        self.resize(1280, 1280)

        # Active controller. Motion is generated live from elapsed phase each
        # tick (see _advance), so smoothness is set by the tick rate and is
        # independent of any step count; speed is set by the cycle period.
        self.controller_name = DEFAULT_CONTROLLER
        self.platform = None
        self._tick_ms = 10        # playback tick interval (also the worker rate)
        self._period_s = 4.0      # seconds per full lo->hi->lo sweep cycle
        self._phase = 0.0         # current sweep phase in [0, 1)

        # Which DOF the streamed dataset sweeps, plus the live per-motor
        # geometry corrections (copied out of the controller config so the
        # calibration sliders never mutate the shared module constants).
        self._dataset_dof = DATASET_DOF
        _base_cfg = CONTROLLERS[self.controller_name]["config"]
        self._geo_offsets = {
            key: list(_base_cfg.get(key, [0.0] * 6))
            for key in ("base_pivot_radial", "base_pivot_z",
                        "platform_joint_radial", "platform_joint_z")
        }
        self._calib_dialog = None
        self._calib_widget = None
        self._config_dialog = None

        # Debounce live rebuilds so dragging a slider doesn't rebuild per pixel.
        self._rebuild_timer = QTimer(self)
        self._rebuild_timer.setSingleShot(True)
        self._rebuild_timer.setInterval(80)
        self._rebuild_timer.timeout.connect(self._rebuild_platform)

        # Feeds the running sender thread with widget-state snapshots (angles,
        # target host/port, interval). Qt widgets are not thread-safe, so the
        # sender never reads them itself — this GUI-thread timer pushes the
        # data in instead. Runs at the motion tick rate: angles change at most
        # that often, so faster sends just resend the latest snapshot.
        self._sender_feed_timer = QTimer(self)
        self._sender_feed_timer.setInterval(self._tick_ms)
        self._sender_feed_timer.timeout.connect(self._feed_sender)

        self._build_active_controller()

        # Logging setup
        self.logger = logging.getLogger("motor_app")
        self.logger.setLevel(logging.INFO)
        self.gui_log_handler = QtTextEditLogHandler(logging.INFO)
        self.gui_log_handler.emitter.message.connect(self._append_log_text)
        self.logger.addHandler(self.gui_log_handler)

        self.config = self.load_config()

        root = QVBoxLayout(self)
        root.addLayout(self._build_controls())

        min_angle = self.config.get('min_angle', None)
        max_angle = self.config.get('max_angle', None)

        # Master motor widget
        self.master_motor_widget = MotorWidget(
            "Master", min_angle=min_angle, max_angle=max_angle, master=True,
            motion_settings=self.config.get("master_motion", {}),
            save_settings_callback=self._save_master_motion_settings,
            move_dispatch=self._dispatch_master_move,
        )
        self.master_motor_widget.setTitle("Master Motor")
        self.master_motor_widget.angleChanged.connect(self._apply_master_angle)

        root.addWidget(self.master_motor_widget)

        # 6 motor widgets
        self.motor_widgets = [MotorWidget(i + 1, min_angle=min_angle, max_angle=max_angle) for i in range(6)]
        grid = QGridLayout()
        for i, mw in enumerate(self.motor_widgets):
            grid.addWidget(mw, i // 3, i % 3)
        root.addLayout(grid, stretch=2)

        # Vorosim remote-control panel (single-connection listener + log)
        self.vorosim_widget = VorosimWidget(
            port=self.config.get("vorosim_port", DEFAULT_VOROSIM_PORT),
            logger=self.logger,
            signals_mapping=self.config.get("remote_signals_mapping", {}),
            pose_callback=self.apply_remote_pose,
            ac_interval_ms=self.config.get("interval_ms", 10),
            axis_transforms=self.config.get("axis_transforms", {}),
            smoothing=self.config.get("smoothing", {}),
            save_settings_callback=self._save_vorosim_settings,
        )
        root.addWidget(self.vorosim_widget, stretch=2)

        # Consoles
        root.addLayout(self._build_consoles(), stretch=3)

        self.sender_thread = None
        self.receiver_thread = None
        self.current_file_handler = None

        self.__apply_config(self.config)

    def stop_periodic_task(self):
        worker = getattr(self, "periodic_worker", None)
        thread = getattr(self, "periodic_thread", None)
        if worker is None and thread is None:
            return  # nothing running (also called unconditionally on close)

        if worker is not None:
            # worker.stop() touches a QTimer that was created in the worker
            # thread, so it must run there. Invoke it via a queued connection
            # rather than calling it from the GUI thread (which crashes).
            QMetaObject.invokeMethod(worker, "stop", Qt.ConnectionType.QueuedConnection)

        if thread is not None and thread.isRunning():
            thread.quit()        # exit the worker event loop (after stop() runs)
            thread.wait(2000)

        self.periodic_thread = None
        self.periodic_worker = None

        self.logger.info("Periodic task stopped.")

    _DOF_LIMIT_ATTR = {
        "x": "x_lim", "y": "y_lim", "z": "z_lim",
        "roll": "a_lim", "pitch": "b_lim", "yaw": "c_lim",
    }

    def _on_periodic_tick(self):
        self._advance(self._tick_ms)

    def _command_pose_at_phase(self, phase: float):
        """Evaluate the triangle sweep at *phase* in [0,1) for the active DOF
        and push the resulting servo angles to the motors. Reads self.platform
        live, so calibration changes take effect on the next tick."""
        if self.platform is None:
            return
        lo, hi = getattr(self.platform, self._DOF_LIMIT_ATTR[self._dataset_dof])
        tri = 1.0 - abs(2.0 * phase - 1.0)        # 0 -> 1 -> 0 across the cycle
        value = lo + (hi - lo) * tri
        self.platform.x = self.platform.y = self.platform.z = 0.0
        self.platform.roll = self.platform.pitch = self.platform.yaw = 0.0
        setattr(self.platform, self._dataset_dof, value)
        status, angles = self.platform.calc()
        if status == 0 and angles is not None:
            self.set_desired_angles(angles, log=False)

    def apply_remote_pose(self, roles: dict):
        """Apply a pose received from the Vorosim remote to the platform and push
        the resulting servo angles to the motors.

        *roles* maps DOF names (a subset of roll/pitch/yaw) to values, already
        resolved from the incoming signals via remote_signals_mapping. Runs in
        the GUI thread (queued signal), so touching widgets here is safe.
        """
        if self.platform is None:
            return
        self.platform.x = self.platform.y = self.platform.z = 0.0
        self.platform.roll = self.platform.pitch = self.platform.yaw = 0.0
        for role in ("roll", "pitch", "yaw"):
            if role in roles:
                setattr(self.platform, role, float(roles[role]))
        status, angles = self.platform.calc()
        if status == 0 and angles is not None:
            self.set_desired_angles(angles, log=False)

    def _advance(self, dt_ms: float):
        """Advance the sweep phase by *dt_ms* of motion and command the pose.
        Smoothness comes from the small dt per tick; speed from the period."""
        if self._period_s > 0:
            self._phase = (self._phase + (dt_ms / 1000.0) / self._period_s) % 1.0
        self._command_pose_at_phase(self._phase)

    def start_periodic_task(self):
        if hasattr(self, "periodic_thread") and self.periodic_thread:
            if self.periodic_thread.isRunning():
                self.logger.warning("Periodic task already running.")
                return

        self.periodic_thread = QThread()
        self.periodic_worker = PeriodicWorker(interval_ms=self._tick_ms)

        self.periodic_worker.moveToThread(self.periodic_thread)

        # Start worker when thread starts
        self.periodic_thread.started.connect(self.periodic_worker.start)

        # Connect worker signal to your logic
        self.periodic_worker.tick.connect(self._on_periodic_tick)

        # Cleanup
        self.periodic_worker.finished.connect(self.periodic_thread.quit)
        self.periodic_worker.finished.connect(self.periodic_worker.deleteLater)
        self.periodic_thread.finished.connect(self.periodic_thread.deleteLater)

        self.periodic_thread.start()
        self.logger.info(f"Periodic task started ({self.periodic_worker.interval_ms} ms).")

    def set_desired_angles(self, angles: list[float], auto_start_sender: bool = False,
                           log: bool = True):
        """
        Programmatically set desired angles for the 6 motors.

        Example:
            self.set_desired_angles([10, 20, 30, 40, 50, 60])

        Args:
            angles: list of up to 6 angles
            auto_start_sender: if True, starts sender automatically if not running
            log: if False, skip the info log (used by the high-rate playback loop
                 so it doesn't flood the console at the tick rate)
        """
        if not isinstance(angles, (list, tuple)):
            raise TypeError("angles must be a list or tuple")

        if len(angles) > len(self.motor_widgets):
            raise ValueError(f"Expected at most {len(self.motor_widgets)} angles, got {len(angles)}")

        for i, angle in enumerate(angles):
            self.motor_widgets[i].set_angle(float(angle))

        if log:
            self.logger.info(f"Desired angles set programmatically: {list(angles)}")

        if auto_start_sender and not (self.sender_thread and self.sender_thread.isRunning()):
            self.start_sender()

    def _apply_master_angle(self, angle: float):
        for mw in self.motor_widgets:
            mw.set_angle(angle)

    def _dispatch_master_move(self, target: float, speed_dps: float):
        """Park/Neutral from the master: each of the 6 motors eases to *target*
        from its own current angle at *speed_dps*, so a motor sitting further
        away just takes longer rather than snapping to catch up."""
        for mw in self.motor_widgets:
            mw.move_to(target, speed_dps)

    # ---------- Controller selection ----------
    def _active_config(self) -> dict:
        """Controller config merged with the live per-motor geometry offsets."""
        cfg = dict(CONTROLLERS[self.controller_name]["config"])
        cfg.update(self._geo_offsets)
        return cfg

    def _build_active_controller(self):
        """Instantiate the active controller for the current geometry."""
        entry = CONTROLLERS[self.controller_name]
        self.platform = entry["cls"](self._active_config())
        self._phase = 0.0

    def _rebuild_platform(self):
        """Rebuild the solver from the current geometry offsets. Safe to call
        live — the running loop reads self.platform on its next tick."""
        entry = CONTROLLERS[self.controller_name]
        try:
            self.platform = entry["cls"](self._active_config())
        except Exception as e:
            self.logger.exception(f"Failed to rebuild platform: {e}")

    def _set_dataset_dof(self, dof: str):
        self._dataset_dof = dof
        self._phase = 0.0
        self._rebuild_platform()
        self.logger.info(f"Test DOF set to '{dof}'.")

    def _set_speed(self):
        secs = self.speed_combo.currentData()
        if secs:
            self._period_s = float(secs)
            self.logger.info(f"Sweep period set to {self._period_s:.1f} s.")

    def _open_calibration(self):
        """Open (or re-show) the non-modal live geometry-calibration panel."""
        if self._calib_dialog is None:
            dlg = QDialog(self)
            dlg.setWindowTitle("Geometry Calibration")
            dlg.setModal(False)
            self._calib_widget = CalibrationWidget(self.platform.nb_servos)
            self._calib_widget.set_values(self._geo_offsets)
            self._calib_widget.changed.connect(self._on_calibration_changed)
            self._calib_widget.logRequested.connect(self._log_calibration)
            lay = QVBoxLayout(dlg)
            lay.addWidget(self._calib_widget)
            self._calib_dialog = dlg
        self._calib_dialog.show()
        self._calib_dialog.raise_()
        self._calib_dialog.activateWindow()

    def _on_calibration_changed(self, offsets: dict):
        self._geo_offsets = offsets
        self._rebuild_timer.start()  # debounced rebuild

    def _log_calibration(self, offsets: dict):
        def fmt(name, key):
            vals = ", ".join(f"{v:.1f}" for v in offsets[key])
            return f"{name}: list[float] = [{vals}]"
        self.logger.info(
            "Geometry calibration — paste into platform_constants.py:\n"
            + fmt("BASE_PIVOT_RADIAL_MM", "base_pivot_radial") + "\n"
            + fmt("BASE_PIVOT_Z_MM", "base_pivot_z") + "\n"
            + fmt("PLATFORM_JOINT_RADIAL_MM", "platform_joint_radial") + "\n"
            + fmt("PLATFORM_JOINT_Z_MM", "platform_joint_z")
        )

    def set_controller(self, name: str):
        """Switch the active kinematics controller and rebuild the dataset.

        Safe to call while the app is running (e.g. from the UI dropdown).
        """
        if name not in CONTROLLERS:
            self.logger.warning(f"Unknown controller '{name}', ignoring.")
            return
        if name == self.controller_name and self.platform is not None:
            return

        self.controller_name = name
        self._build_active_controller()
        self.logger.info(f"Controller switched to '{name}'.")

    # ---------- UI Builders ----------
    def _build_controls(self):
        """Build the slim top control row.

        Only the essential run controls live here: start/stop sender, start/stop
        receiver, and a Config button. Every other setting is created up front
        by _create_control_widgets() and shown in the Config popup dialog
        (see _open_config / _build_config_body).
        """
        self._create_control_widgets()

        row = QHBoxLayout()
        row.addWidget(self.start_btn)
        row.addWidget(self.stop_btn)
        row.addWidget(self.start_recv_btn)
        row.addWidget(self.stop_recv_btn)

        self.config_btn = QPushButton("Config…")
        self.config_btn.setToolTip("Open sender/receiver/motion settings.")
        self.config_btn.clicked.connect(self._open_config)
        row.addWidget(self.config_btn)

        row.addStretch(1)
        return row

    def _create_control_widgets(self):
        """Instantiate every control widget once.

        The start/stop buttons are placed in the top row by _build_controls;
        all the others are arranged into the Config popup by _build_config_body.
        Widgets are created here (not in the dialog) so their attributes exist
        even before the dialog is first opened and so existing logic that reads
        e.g. self.host_edit / self.packager_combo keeps working unchanged.
        """
        # --- Sender target ---
        self.host_edit = QLineEdit("127.0.0.1")
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(9999)
        self.interval_combo = QComboBox()
        for label, ms in SEND_INTERVALS:
            self.interval_combo.addItem(label, ms)
        self.interval_combo.setCurrentIndex(5)  # 1s
        self.packager_combo = QComboBox()
        self.packager_combo.addItem("STM (binary)", userData="stm")
        self.packager_combo.addItem("Readable (JSON)", userData="readable")
        self.packager_combo.setCurrentIndex(0)  # default: STM

        # --- Controller / motion ---
        self.controller_combo = QComboBox()
        for name in CONTROLLERS:
            self.controller_combo.addItem(name)
        self.controller_combo.setCurrentText(self.controller_name)
        self.controller_combo.currentTextChanged.connect(self.set_controller)

        self.dof_combo = QComboBox()
        for dof in ("x", "y", "z", "roll", "pitch", "yaw"):
            self.dof_combo.addItem(dof)
        self.dof_combo.setCurrentText(self._dataset_dof)
        self.dof_combo.currentTextChanged.connect(self._set_dataset_dof)

        self.speed_combo = QComboBox()
        for label, secs in (("Slow (8s)", 8.0), ("Medium (4s)", 4.0),
                            ("Fast (2s)", 2.0), ("Very fast (1s)", 1.0)):
            self.speed_combo.addItem(label, secs)
        idx = self.speed_combo.findData(self._period_s)
        self.speed_combo.setCurrentIndex(idx if idx >= 0 else 1)
        self.speed_combo.currentIndexChanged.connect(self._set_speed)

        self.calib_btn = QPushButton("Calibrate…")
        self.calib_btn.clicked.connect(self._open_calibration)

        # --- Receiver ---
        self.loopback_check = QCheckBox("Loopback / open receiver")
        self.loopback_check.stateChanged.connect(self._toggle_loopback)
        self.recv_host_edit = QLineEdit("127.0.0.1")
        self.recv_port_spin = QSpinBox()
        self.recv_port_spin.setRange(1, 65535)
        self.recv_port_spin.setValue(9999)

        # --- Run controls (top row) ---
        self.start_btn = QPushButton("Start Sender")
        self.start_btn.clicked.connect(self.start_sender)
        self.stop_btn = QPushButton("Stop Sender")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_sender)
        self.start_recv_btn = QPushButton("Start Receiver")
        self.start_recv_btn.clicked.connect(self.start_receiver)
        self.stop_recv_btn = QPushButton("Stop Receiver")
        self.stop_recv_btn.setEnabled(False)
        self.stop_recv_btn.clicked.connect(self.stop_receiver)

        # --- Motion actions (Config popup) ---
        self.test_btn = QPushButton("Next Angles")
        self.test_btn.clicked.connect(self.next_angles)
        self.start_periodic_btn = QPushButton("Start Loop")
        self.start_periodic_btn.clicked.connect(self.start_periodic_task)
        self.stop_periodic_btn = QPushButton("Stop Loop")
        self.stop_periodic_btn.clicked.connect(self.stop_periodic_task)

    def _open_config(self):
        """Open (or re-show) the non-modal Config popup holding every setting
        that isn't a start/stop button."""
        if self._config_dialog is None:
            dlg = QDialog(self)
            dlg.setWindowTitle("Configuration")
            dlg.setModal(False)
            dlg.setLayout(self._build_config_body())
            self._config_dialog = dlg
        self._config_dialog.show()
        self._config_dialog.raise_()
        self._config_dialog.activateWindow()

    def _build_config_body(self):
        """Lay out all non-essential controls into grouped sections for the
        Config popup."""
        outer = QVBoxLayout()

        sender_group = QGroupBox("Sender")
        sg = QGridLayout()
        sg.addWidget(QLabel("Target Host:"), 0, 0)
        sg.addWidget(self.host_edit, 0, 1)
        sg.addWidget(QLabel("Target Port:"), 0, 2)
        sg.addWidget(self.port_spin, 0, 3)
        sg.addWidget(QLabel("Interval:"), 1, 0)
        sg.addWidget(self.interval_combo, 1, 1)
        sg.addWidget(QLabel("Packager:"), 1, 2)
        sg.addWidget(self.packager_combo, 1, 3)
        sender_group.setLayout(sg)
        outer.addWidget(sender_group)

        recv_group = QGroupBox("Receiver")
        rg = QGridLayout()
        rg.addWidget(self.loopback_check, 0, 0, 1, 4)
        rg.addWidget(QLabel("Recv Host:"), 1, 0)
        rg.addWidget(self.recv_host_edit, 1, 1)
        rg.addWidget(QLabel("Recv Port:"), 1, 2)
        rg.addWidget(self.recv_port_spin, 1, 3)
        recv_group.setLayout(rg)
        outer.addWidget(recv_group)

        motion_group = QGroupBox("Motion / Controller")
        mg = QGridLayout()
        mg.addWidget(QLabel("Controller:"), 0, 0)
        mg.addWidget(self.controller_combo, 0, 1)
        mg.addWidget(QLabel("Test DOF:"), 0, 2)
        mg.addWidget(self.dof_combo, 0, 3)
        mg.addWidget(QLabel("Speed:"), 1, 0)
        mg.addWidget(self.speed_combo, 1, 1)
        mg.addWidget(self.calib_btn, 1, 2, 1, 2)
        actions = QHBoxLayout()
        actions.addWidget(self.test_btn)
        actions.addWidget(self.start_periodic_btn)
        actions.addWidget(self.stop_periodic_btn)
        mg.addLayout(actions, 2, 0, 1, 4)
        motion_group.setLayout(mg)
        outer.addWidget(motion_group)

        outer.addStretch(1)
        return outer

    def next_angles(self):
        # Manual single step: advance 1/DATASET_STEPS of a cycle per click, so
        # DATASET13-_STEPS now only sets the manual step granularity (the loop's
        # smoothness is independent of it).
        self._advance(self._period_s * 1000.0 / max(1, DATASET_STEPS))

    def _build_consoles(self):
        col = QVBoxLayout()
        self.output_console = self._make_console_group("Output Console (Logs)")
        self.sent_console = self._make_console_group("Sent Packets")
        self.recv_console = self._make_console_group("Received Packets")
        col.addWidget(self.output_console["group"])
        col.addWidget(self.sent_console["group"])
        col.addWidget(self.recv_console["group"])
        return col

    def _make_console_group(self, title):
        group = QGroupBox(title)
        v = QVBoxLayout()
        top_bar = QHBoxLayout()
        clear_btn = QPushButton("Clear")
        top_bar.addStretch()
        top_bar.addWidget(clear_btn)
        v.addLayout(top_bar)
        text = QTextEdit()
        text.setReadOnly(True)
        text.setStyleSheet("QTextEdit { font-family: Consolas, monospace; }")
        # Bound memory: keep only the last MAX_CONSOLE_LINES blocks. Qt drops
        # the oldest lines as new ones arrive, so the console can't grow forever.
        text.document().setMaximumBlockCount(MAX_CONSOLE_LINES)

        v.addWidget(text)
        group.setLayout(v)
        clear_btn.clicked.connect(text.clear)
        return {"group": group, "text": text}

    # ---------- Logging / Append ----------
    # Guard against log/packet callbacks firing before the console widgets are
    # built (e.g. a log emitted during __init__): a crash in a Qt slot aborts
    # the whole app, so silently skip until the target console exists.
    def _append_log_text(self, msg):
        console = getattr(self, "output_console", None)
        if console: console["text"].append(msg)

    def _append_sent_packet(self, text):
        console = getattr(self, "sent_console", None)
        if console: console["text"].append(text)

    def _append_received_packet(self, text):
        console = getattr(self, "recv_console", None)
        if console: console["text"].append(text)

    @staticmethod
    def get_app_base_dir() -> str:
        """
        Returns the directory where:
        - the .exe lives (PyInstaller)
        - or the script is run from (dev mode)
        """
        if getattr(sys, "frozen", False):
            return os.path.dirname(sys.executable)
        return os.getcwd()

    def __apply_config(self, cfg):

        # Apply values if present
        if "target_host" in cfg:
            self.host_edit.setText(str(cfg["target_host"]))

        if "target_port" in cfg:
            self.port_spin.setValue(int(cfg["target_port"]))

        if "receiver_host" in cfg:
            self.recv_host_edit.setText(str(cfg["receiver_host"]))

        if "receiver_port" in cfg:
            self.recv_port_spin.setValue(int(cfg["receiver_port"]))

        if "interval_ms" in cfg:
            self._set_interval_from_ms(int(cfg["interval_ms"]))

    def load_config(self):
        """Load config.json from the app base dir. Always returns a dict (empty
        when the file is missing or unreadable) so callers can safely .get()."""
        base_dir = self.get_app_base_dir()
        config_path = os.path.join(base_dir, "config.json")

        if not os.path.isfile(config_path):
            self.logger.info("No config.json found, using GUI defaults.")
            return {}
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            return cfg if isinstance(cfg, dict) else {}

        except Exception as e:
            self.logger.exception(f"Failed to load config.json: {e}")
            return {}

    def save_config(self) -> None:
        """Write the in-memory config back to config.json in the app base dir
        (same location load_config reads from), preserving all keys."""
        base_dir = self.get_app_base_dir()
        config_path = os.path.join(base_dir, "config.json")
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2)
        except Exception as e:
            self.logger.exception(f"Failed to save config.json: {e}")

    def _save_vorosim_settings(self, settings: dict) -> None:
        """Persist the Vorosim Pitch/Roll/Yaw transforms and smoothing (filter)
        settings to config.json."""
        if "axis_transforms" in settings:
            self.config["axis_transforms"] = settings["axis_transforms"]
        if "smoothing" in settings:
            self.config["smoothing"] = settings["smoothing"]
        self.save_config()
        self.logger.info("Saved Vorosim settings to config.json.")

    def _save_master_motion_settings(self, settings: dict) -> None:
        """Persist the master widget's Park/Neutral angles and move speed to
        config.json (under the 'master_motion' key)."""
        self.config["master_motion"] = dict(settings)
        self.save_config()
        self.logger.info("Saved master motion settings to config.json.")

    def _set_interval_from_ms(self, interval_ms: int):
        for i in range(self.interval_combo.count()):
            if self.interval_combo.itemData(i) == interval_ms:
                self.interval_combo.setCurrentIndex(i)
                return
        self.logger.warning(f"Interval {interval_ms} ms not found in interval list.")

    # ---------- Thread Control ----------
    def start_sender(self):
        if self.sender_thread and self.sender_thread.isRunning():
            self.logger.warning("Sender already running.")
            return
        self._attach_log_file()

        # pick packager based on combo selection
        choice = self.packager_combo.currentData()
        if choice == "stm":
            packager = STMPackager()
            self.logger.info("Using STMPackager (binary).")
        else:
            packager = HumanReadablePackager()
            self.logger.info("Using ReadablePackager (JSON).")

        # The thread gets value snapshots, never the widgets themselves (Qt
        # widgets are not thread-safe); _feed_sender keeps them fresh.
        self.sender_thread = UdpSenderThread(
            host=self.host_edit.text(),
            port=self.port_spin.value(),
            interval_ms=int(self.interval_combo.currentData()),
            motor_data=[mw.get_data() for mw in self.motor_widgets],
            logger=self.logger,
            packager=packager,  # <- important: sender expects bytes from packager
        )
        self.sender_thread.sent_packet.connect(self._append_sent_packet)
        self.sender_thread.start()
        self._sender_feed_timer.start()

        # disable controls that must not change during run
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.packager_combo.setEnabled(False)

        self.logger.info("Sender started.")

    def _feed_sender(self):
        """Push the current widget state (motor angles, target, interval) into
        the running sender thread. Runs in the GUI thread on the feed timer, so
        it may safely read the widgets; the thread only sees the snapshots."""
        thread = self.sender_thread
        if thread is None:
            return
        thread.set_target(self.host_edit.text(), self.port_spin.value())
        thread.set_interval(int(self.interval_combo.currentData()))
        thread.update_motor_data([mw.get_data() for mw in self.motor_widgets])

    def stop_sender(self):
        self._sender_feed_timer.stop()
        if self.sender_thread and self.sender_thread.isRunning():
            self.sender_thread.stop()
            self.sender_thread.wait(2000)
        self.sender_thread = None

        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.packager_combo.setEnabled(True)  # re-enable selection

        self._detach_log_file()
        self.logger.info("Sender stopped.")

    def _toggle_loopback(self, state):
        if state == Qt.CheckState.Checked.value:
            self.start_receiver()
        else:
            self.stop_receiver()

    def _apply_motor_statuses(self, status_bytes: list):
        """
        status_bytes: [m1, m2, m3, m4, m5, m6] each is 0..255
        """
        for i, b in enumerate(status_bytes[:6]):
            if i < len(self.motor_widgets):
                self.motor_widgets[i].set_status_bits(int(b))

    def start_receiver(self):
        if self.receiver_thread and self.receiver_thread.isRunning():
            self.logger.warning("Receiver already running.")
            return

        host = self.recv_host_edit.text().strip()
        port = self.recv_port_spin.value()

        # Match the sender's packager selection so previews follow the active
        # protocol (STM replies are binary and preview as hex; JSON as text).
        choice = self.packager_combo.currentData()
        packager = STMPackager() if choice == "stm" else HumanReadablePackager()

        self.receiver_thread = UdpReceiverThread(host, port, self.logger, packager)
        self.receiver_thread.received_packet.connect(self._append_received_packet)
        self.receiver_thread.motor_statuses.connect(self._apply_motor_statuses)

        self.receiver_thread.start()

        self.start_recv_btn.setEnabled(False)
        self.stop_recv_btn.setEnabled(True)
        self.logger.info(f"Receiver started on {host}:{port}")

    def stop_receiver(self):
        if self.receiver_thread and self.receiver_thread.isRunning():
            self.receiver_thread.stop()
            self.receiver_thread.wait(2000)
        self.receiver_thread = None
        self.start_recv_btn.setEnabled(True)
        self.stop_recv_btn.setEnabled(False)
        self.logger.info("Receiver stopped.")

    def _attach_log_file(self):
        """Create a new log file inside 'logs' directory.

        - If running as PyInstaller exe → use the exe directory.
        - Otherwise (running from source) → use current working directory.
        """

        self._detach_log_file()

        # Determine base path
        if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
            # Running as a PyInstaller bundle
            base_path = os.path.dirname(sys.executable)
        else:
            # Running as normal script
            base_path = os.path.abspath(os.getcwd())

        # Create logs directory if missing
        logs_dir = os.path.join(base_path, "logs")
        os.makedirs(logs_dir, exist_ok=True)

        # Build filename
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        log_path = os.path.join(logs_dir, f"motor_run_{ts}.log")

        # Set up file handler
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setLevel(logging.INFO)
        fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))

        self.logger.addHandler(fh)
        self.current_file_handler = fh
        self.logger.info(f"Log file created: {log_path}")

    def _detach_log_file(self):
        if self.current_file_handler:
            self.logger.removeHandler(self.current_file_handler)
            self.current_file_handler.close()
            self.current_file_handler = None

    def closeEvent(self, e):
        self.stop_periodic_task()
        self.stop_sender()
        self.stop_receiver()
        self.vorosim_widget.stop_listener()
        self.vorosim_widget.stop_ac_reader()
        super().closeEvent(e)


# ----------------------------- Entrypoint ----------------------------- #

def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
