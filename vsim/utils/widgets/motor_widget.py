# vsim/utils/widgets/motor_widget.py

from typing import Dict
from PyQt6.QtCore import Qt, pyqtSignal, QTimer
from PyQt6.QtGui import QDoubleValidator
from PyQt6.QtWidgets import (
    QGroupBox, QLabel, QGridLayout, QSlider, QLineEdit,
    QPushButton, QHBoxLayout, QDialog, QFormLayout, QDoubleSpinBox,
)


def _as_float(value, default: float) -> float:
    """Best-effort float parse; returns *default* on None/invalid input."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class ToggleLabel(QLabel):
    """Clickable ON/OFF label with hover cursor + highlight via style."""
    toggled = pyqtSignal(bool)

    def __init__(self, text_prefix: str, parent=None):
        super().__init__(parent)
        self.setMaximumHeight(24)
        self._prefix = text_prefix
        self._on = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setProperty("on", False)
        self.setProperty("endSwitch", True)
        self.setText(f"{self._prefix}: OFF")
        self.setToolTip(self._prefix.replace('L ', 'Left ').replace('R ', 'Right '))
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self._apply_style()

    def mouseReleaseEvent(self, e):
        return

    def set_on(self, state: bool):
        if self._on == state:
            return
        self._on = state
        self.setProperty("on", state)
        self.setText(f"{self._prefix}: {'ON' if state else 'OFF'}")
        self._apply_style()
        self.toggled.emit(self._on)

    def is_on(self) -> bool:
        return self._on

    def _apply_style(self):
        self.style().unpolish(self)
        self.style().polish(self)


class MotorWidget(QGroupBox):
    angleChanged = pyqtSignal(float)   # <-- NEW

    MIN_ANGLE = 80.0
    MAX_ANGLE = 180.0
    RESOLUTION = 0.1
    SCALE = int(1 / RESOLUTION)

    # Smooth-move (Park/Neutral) speed limits, in degrees per second.
    SPEED_MIN_DPS = 1.0
    SPEED_MAX_DPS = 50.0
    DEFAULT_SPEED_DPS = 25.0
    MOVE_TICK_MS = 20               # animation tick (50 Hz)

    def __init__(self, index: int, parent=None, min_angle=None, max_angle=None, master=False,
                 motion_settings=None, save_settings_callback=None, move_dispatch=None):
        super().__init__(parent)
        self.is_master = master
        self.setMaximumHeight(160 if master else 110)
        self._index = index
        self.setTitle(f"Motor {index}")
        self._syncing = False

        self.setStyleSheet("""
            QGroupBox { font-weight: bold; }

            QLineEdit.angleEdit {
                font-size: 13px;
                min-width: 58px;
                max-width: 58px;
                padding: 2px 4px;
            }

            QLabel.rangeLabel {
                font-size: 11px;
                font-weight: normal;
                color: rgba(200,200,200,0.75);
                padding-left: 8px;
            }

            QLabel[endSwitch="true"] {
                font-size: 13px;
                padding: 3px 6px;
                border-radius: 6px;
                border: 1px solid rgba(120,120,120,0.35);
            }
            QLabel[endSwitch="true"]:hover[on="false"] {
                background: rgba(120,120,120,0.10);
            }
            QLabel[endSwitch="true"][on="true"] {
                background: rgba(50,205,50,0.18);
                border: 1px solid rgba(50,205,50,0.55);
                font-weight: 600;
            }
        """)

        self.min_angle = min_angle if min_angle is not None else self.MIN_ANGLE
        self.max_angle = max_angle if max_angle is not None else self.MAX_ANGLE

        # Home angle = midpoint of the servo range (e.g. (80+180)/2 = 130°),
        # the level/neutral position the platform sits at with all DOFs zero.
        self.home_angle = (self.min_angle + self.max_angle) / 2.0

        # Observed travel range: the min/max angle this motor has actually
        # reached. Seeded to the starting (home) angle, so both read 130° until
        # the motor moves; each new commanded angle widens the range.
        self.observed_min = self.home_angle
        self.observed_max = self.home_angle

        # Per-widget smooth-motion engine. Every motor can ease its *own* angle
        # toward a target at a capped speed, starting from wherever it currently
        # is — so Park/Neutral don't yank a lagging motor to catch up (each motor
        # ramps independently). See move_to / _move_step.
        self._move_target = None
        self._move_speed_dps = self.DEFAULT_SPEED_DPS
        self._move_timer = QTimer(self)
        self._move_timer.setInterval(self.MOVE_TICK_MS)
        self._move_timer.timeout.connect(self._move_step)

        # Master-only: Park/Neutral targets, default move speed, and the callback
        # that fans a move request out to every follower motor.
        if master:
            self.park_angle = self.min_angle
            self.neutral_angle = self.home_angle
            self.move_speed_dps = self.DEFAULT_SPEED_DPS
            # Restore persisted overrides (from config.json), clamped to range.
            ms = motion_settings or {}
            self.park_angle = self._clamp_angle(_as_float(ms.get("park_angle"), self.park_angle))
            self.neutral_angle = self._clamp_angle(_as_float(ms.get("neutral_angle"), self.neutral_angle))
            self.move_speed_dps = min(max(
                _as_float(ms.get("move_speed_dps"), self.move_speed_dps),
                self.SPEED_MIN_DPS), self.SPEED_MAX_DPS)
            self._save_settings_callback = save_settings_callback
            self._move_dispatch = move_dispatch
            self._master_config_dialog = None

        self.angle_edit = QLineEdit()
        self.angle_edit.setProperty("class", "angleEdit")
        self.angle_edit.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.angle_edit.setText(f"{self.home_angle:.1f}")
        self.angle_edit.setToolTip("Angle in degrees (0.1° resolution). Press Enter to apply.")

        validator = QDoubleValidator(self.min_angle, self.max_angle, 1, self)
        validator.setNotation(QDoubleValidator.Notation.StandardNotation)
        self.angle_edit.setValidator(validator)

        self.angle_edit.editingFinished.connect(self._on_edit_finished)
        self.angle_edit.returnPressed.connect(self._on_edit_finished)

        deg_label = QLabel("°")
        deg_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        # Live min/max readout of the angle this motor has actually reached.
        self.range_label = QLabel()
        self.range_label.setProperty("class", "rangeLabel")
        self.range_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._update_range_label()

        self.angle_slider = QSlider(Qt.Orientation.Horizontal)
        self.angle_slider.setRange(int(self.min_angle * self.SCALE), int(self.max_angle * self.SCALE))
        self.angle_slider.setValue(int(round(self.home_angle * self.SCALE)))
        self.angle_slider.setSingleStep(1)
        self.angle_slider.setPageStep(10)
        self.angle_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.angle_slider.setTickInterval(100)
        self.angle_slider.valueChanged.connect(self._on_slider_changed)
        # Separate from _on_slider_changed (which is gated by the edit/slider
        # sync guard): this records every value change — user drag, typed entry,
        # or programmatic set_angle — so the observed range can't miss a move.
        self.angle_slider.valueChanged.connect(self._record_angle)

        self.mx_status_cal = ToggleLabel("CAL")
        self.mx_status_ena = ToggleLabel("ENA")
        self.mx_status_dir = ToggleLabel("DIR")
        self.mx_status_step = ToggleLabel("STEP")
        self.mx_status_pend = ToggleLabel("PEND")
        self.mx_status_alm = ToggleLabel("ALM")
        self.mx_status_lim_0 = ToggleLabel("LIM 0")
        self.mx_status_lim_180 = ToggleLabel("LIM 180")
        for lab in (
            self.mx_status_cal,
            self.mx_status_ena,
            self.mx_status_dir,
            self.mx_status_step,
            self.mx_status_pend,
            self.mx_status_alm,
            self.mx_status_lim_0,
            self.mx_status_lim_180,
        ):
            lab.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(4)
        r = 0

        angle_label = QLabel("Angle:")
        angle_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        grid.addWidget(angle_label, r, 0)
        grid.addWidget(self.angle_edit, r, 1)
        grid.addWidget(deg_label, r, 2)
        grid.addWidget(self.angle_slider, r, 3, 1, 2)
        grid.addWidget(self.range_label, r, 5)
        r += 1

        if not master:
            grid.addWidget(self.mx_status_cal, r, 0)
            grid.addWidget(self.mx_status_ena, r, 1)
            grid.addWidget(self.mx_status_dir, r, 2)
            grid.addWidget(self.mx_status_step, r, 3)
            r += 1

            grid.addWidget(self.mx_status_pend, r, 0)
            grid.addWidget(self.mx_status_alm, r, 1)
            grid.addWidget(self.mx_status_lim_0, r, 2)
            grid.addWidget(self.mx_status_lim_180, r, 3)
            r += 1
        else:
            self.park_btn = QPushButton("Park")
            self.park_btn.setToolTip("Ease all motors to the park angle.")
            self.park_btn.clicked.connect(self.park)

            self.neutral_btn = QPushButton("Neutral")
            self.neutral_btn.setToolTip("Ease all motors to the neutral (mid) angle.")
            self.neutral_btn.clicked.connect(self.go_neutral)

            self.master_config_btn = QPushButton("Config…")
            self.master_config_btn.setToolTip("Set park/neutral angles and move speed.")
            self.master_config_btn.clicked.connect(self._open_master_config)

            btn_row = QHBoxLayout()
            btn_row.addWidget(self.park_btn)
            btn_row.addWidget(self.neutral_btn)
            btn_row.addWidget(self.master_config_btn)
            btn_row.addStretch(1)
            grid.addLayout(btn_row, r, 0, 1, 6)
            r += 1

        self.setLayout(grid)
        self._sync_edit_from_slider(self.angle_slider.value())

    def set_status_bits(self, status_byte: int):
        status_byte &= 0xFF
        mapping = [
            (self.mx_status_cal, 7, True),
            (self.mx_status_ena, 6, False),
            (self.mx_status_dir, 5, True),
            (self.mx_status_step, 4, True),
            (self.mx_status_pend, 3, False),
            (self.mx_status_alm, 2, False),
            (self.mx_status_lim_0, 1, False),
            (self.mx_status_lim_180, 0, False),
        ]

        for label, bit, active_high in mapping:
            bit_val = bool(status_byte & (1 << bit))
            label.set_on(bit_val if active_high else not bit_val)

    def _record_angle(self, slider_val: int):
        """Widen the observed min/max to include the current angle."""
        angle = slider_val / self.SCALE
        changed = False
        if angle < self.observed_min:
            self.observed_min = angle
            changed = True
        if angle > self.observed_max:
            self.observed_max = angle
            changed = True
        if changed:
            self._update_range_label()

    def _update_range_label(self):
        self.range_label.setText(
            f"min {self.observed_min:.1f}°  |  max {self.observed_max:.1f}°"
        )
        self.range_label.setToolTip(
            "Min/max angle this motor has actually reached this session."
        )

    def _clamp_angle(self, angle: float) -> float:
        if angle < self.min_angle:
            return self.min_angle
        if angle > self.max_angle:
            return self.max_angle
        return round(angle / self.RESOLUTION) * self.RESOLUTION

    def _sync_edit_from_slider(self, slider_val: int):
        angle = slider_val / self.SCALE
        self.angle_edit.setText(f"{angle:.1f}")

    def _sync_slider_from_angle(self, angle: float):
        slider_val = int(round(angle * self.SCALE))
        self.angle_slider.setValue(slider_val)

    def _on_slider_changed(self, slider_val: int):
        if self._syncing:
            return
        self._syncing = True
        try:
            self._sync_edit_from_slider(slider_val)
        finally:
            self._syncing = False

        self.angleChanged.emit(slider_val / self.SCALE)   # <-- NEW

    def _on_edit_finished(self):
        if self._syncing:
            return

        text = (self.angle_edit.text() or "").strip().replace(",", ".")
        try:
            angle = float(text)
        except ValueError:
            angle = self.angle_slider.value() / self.SCALE

        angle = self._clamp_angle(angle)

        old_angle = self.angle_slider.value() / self.SCALE

        self._syncing = True
        try:
            self.angle_edit.setText(f"{angle:.1f}")
            self._sync_slider_from_angle(angle)
        finally:
            self._syncing = False

        if angle != old_angle:
            self.angleChanged.emit(angle)   # <-- NEW

    def get_data(self) -> Dict:
        angle_val = self.angle_slider.value() / self.SCALE
        return {
            "name": self.title(),
            "angle_deg": float(f"{angle_val:.1f}"),
        }

    def set_angle(self, value: float):
        value = self._clamp_angle(float(value))
        self._syncing = True
        try:
            self.angle_edit.setText(f"{value:.1f}")
            self._sync_slider_from_angle(value)
        finally:
            self._syncing = False

    def angle(self) -> float:
        return self.angle_slider.value() / self.SCALE

    # ---------- Smooth motion (per-motor easing) ----------
    def move_to(self, target: float, speed_dps: float = None):
        """Ease this motor's own angle from its current position toward *target*
        at up to *speed_dps* degrees/second. Each motor runs its own timer, so
        motors that start further away simply take longer — none get snapped to
        catch up with the others."""
        if speed_dps is not None:
            self._move_speed_dps = min(max(float(speed_dps), self.SPEED_MIN_DPS),
                                       self.SPEED_MAX_DPS)
        self._move_target = self._clamp_angle(float(target))
        if not self._move_timer.isActive():
            self._move_timer.start()

    def _move_step(self):
        """One animation tick: step this motor's angle toward its target by at
        most move_speed * dt."""
        target = self._move_target
        if target is None:
            self._move_timer.stop()
            return

        current = self.angle()
        dt = self._move_timer.interval() / 1000.0
        max_step = max(self._move_speed_dps * dt, self.RESOLUTION)
        delta = target - current
        if abs(delta) <= max_step:
            new_angle = target
        else:
            new_angle = current + (max_step if delta > 0 else -max_step)

        self.set_angle(new_angle)

        if abs(self.angle() - target) < self.RESOLUTION / 2.0:
            self._move_target = None
            self._move_timer.stop()

    # ---------- Master: Park / Neutral ----------
    def park(self):
        """Ask every follower motor to ease to the configured park angle."""
        self._dispatch_move(self.park_angle)

    def go_neutral(self):
        """Ask every follower motor to ease to the configured neutral angle."""
        self._dispatch_move(self.neutral_angle)

    def _dispatch_move(self, target: float):
        """Fan a smooth-move request out to the follower motors (via the host
        callback). Falls back to moving this widget if no callback is wired."""
        if getattr(self, "_move_dispatch", None) is not None:
            self._move_dispatch(float(target), float(self.move_speed_dps))
        else:
            self.move_to(target, self.move_speed_dps)

    def _open_master_config(self):
        """Popup to tune the park angle, neutral angle, and move speed."""
        if self._master_config_dialog is None:
            dlg = QDialog(self)
            dlg.setWindowTitle("Master Motion Config")
            dlg.setModal(False)
            form = QFormLayout(dlg)

            self.park_spin = QDoubleSpinBox()
            self.park_spin.setRange(self.min_angle, self.max_angle)
            self.park_spin.setDecimals(1)
            self.park_spin.setSingleStep(1.0)
            self.park_spin.setSuffix(" °")
            self.park_spin.setValue(self.park_angle)
            self.park_spin.valueChanged.connect(self._on_park_changed)
            form.addRow("Park angle:", self.park_spin)

            self.neutral_spin = QDoubleSpinBox()
            self.neutral_spin.setRange(self.min_angle, self.max_angle)
            self.neutral_spin.setDecimals(1)
            self.neutral_spin.setSingleStep(1.0)
            self.neutral_spin.setSuffix(" °")
            self.neutral_spin.setValue(self.neutral_angle)
            self.neutral_spin.valueChanged.connect(self._on_neutral_changed)
            form.addRow("Neutral angle:", self.neutral_spin)

            self.speed_slider = QSlider(Qt.Orientation.Horizontal)
            self.speed_slider.setRange(int(self.SPEED_MIN_DPS), int(self.SPEED_MAX_DPS))
            self.speed_slider.setValue(int(self.move_speed_dps))
            self.speed_value_label = QLabel(f"{self.move_speed_dps:.0f} °/s")
            self.speed_slider.valueChanged.connect(self._on_speed_changed)
            speed_row = QHBoxLayout()
            speed_row.addWidget(self.speed_slider)
            speed_row.addWidget(self.speed_value_label)
            form.addRow("Move speed:", speed_row)

            self._master_config_dialog = dlg

        self._master_config_dialog.show()
        self._master_config_dialog.raise_()
        self._master_config_dialog.activateWindow()

    def _on_park_changed(self, value: float):
        self.park_angle = float(value)
        self._save_master_motion()

    def _on_neutral_changed(self, value: float):
        self.neutral_angle = float(value)
        self._save_master_motion()

    def _on_speed_changed(self, value: int):
        self.move_speed_dps = float(value)
        if getattr(self, "speed_value_label", None) is not None:
            self.speed_value_label.setText(f"{value:.0f} °/s")
        self._save_master_motion()

    def _save_master_motion(self):
        """Persist the current park/neutral/speed settings via the callback
        supplied by the host window (writes to config.json)."""
        cb = getattr(self, "_save_settings_callback", None)
        if cb:
            cb({
                "park_angle": self.park_angle,
                "neutral_angle": self.neutral_angle,
                "move_speed_dps": self.move_speed_dps,
            })