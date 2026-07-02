# vsim/utils/widgets/calibration_widget.py
"""
Live geometry-calibration panel.

Exposes the four per-motor mounting-deviation arrays the solver understands
(base pivot radial/Z, platform joint radial/Z) as sliders, one column per
physical motor (M1..M6). Dragging a slider emits ``changed`` with the full set
of arrays so the host can rebuild the motion dataset in real time and the
platform settles as you tune.

The arrays match platform_constants:
    BASE_PIVOT_RADIAL_MM, BASE_PIVOT_Z_MM,
    PLATFORM_JOINT_RADIAL_MM, PLATFORM_JOINT_Z_MM
each a list of 6 floats (mm), index 0 = Motor 1 ... index 5 = Motor 6.
"""

from typing import Dict, List

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QGroupBox, QGridLayout, QVBoxLayout, QHBoxLayout, QLabel, QSlider,
    QPushButton, QWidget,
)


# Slider key  ->  (display label, config key)
_ROWS = [
    ("Base R",  "base_pivot_radial"),
    ("Base Z",  "base_pivot_z"),
    ("Joint R", "platform_joint_radial"),
    ("Joint Z", "platform_joint_z"),
]

_RANGE_MM = 30.0     # +/- travel of each slider
_RESOLUTION = 0.1    # mm per slider step
_SCALE = int(1 / _RESOLUTION)


class CalibrationWidget(QGroupBox):
    """Grid of per-motor deviation sliders. Emits ``changed`` (dict of arrays)
    whenever any value moves, and ``logRequested`` when the user asks to dump
    the current values."""

    changed = pyqtSignal(dict)
    logRequested = pyqtSignal(dict)

    def __init__(self, nb_motors: int = 6, parent=None):
        super().__init__("Geometry Calibration (mm) — tune live, then 'Log values' to keep", parent)
        self._n = nb_motors
        self._emitting = False
        # self._sliders[config_key] -> list[QSlider]; self._labels[config_key] -> list[QLabel]
        self._sliders: Dict[str, List[QSlider]] = {}
        self._labels: Dict[str, List[QLabel]] = {}

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(4)

        # column headers: motor numbers
        grid.addWidget(QLabel(""), 0, 0)
        for m in range(self._n):
            lab = QLabel(f"M{m + 1}")
            lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lab.setStyleSheet("font-weight: bold;")
            grid.addWidget(lab, 0, m + 1)

        for r, (title, key) in enumerate(_ROWS, start=1):
            row_label = QLabel(title)
            row_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(row_label, r, 0)
            self._sliders[key] = []
            self._labels[key] = []
            for m in range(self._n):
                cell = QVBoxLayout()
                cell.setSpacing(1)
                slider = QSlider(Qt.Orientation.Horizontal)
                slider.setRange(int(-_RANGE_MM * _SCALE), int(_RANGE_MM * _SCALE))
                slider.setValue(0)
                slider.setMinimumWidth(70)
                value_lbl = QLabel("0.0")
                value_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                value_lbl.setStyleSheet("font-family: Consolas, monospace; font-size: 11px;")
                slider.valueChanged.connect(self._on_slider_changed)
                cell.addWidget(slider)
                cell.addWidget(value_lbl)
                holder = QWidget()
                holder.setLayout(cell)
                grid.addWidget(holder, r, m + 1)
                self._sliders[key].append(slider)
                self._labels[key].append(value_lbl)

        buttons = QHBoxLayout()
        reset_btn = QPushButton("Reset all to 0")
        reset_btn.clicked.connect(self.reset)
        log_btn = QPushButton("Log values")
        log_btn.clicked.connect(lambda: self.logRequested.emit(self.values()))
        buttons.addStretch(1)
        buttons.addWidget(reset_btn)
        buttons.addWidget(log_btn)

        root = QVBoxLayout()
        root.addLayout(grid)
        root.addLayout(buttons)
        self.setLayout(root)

    # ------------------------------------------------------------------
    def _on_slider_changed(self, _value: int):
        # refresh the numeric labels, then emit the full set (unless we are
        # programmatically setting values in bulk)
        for key in self._sliders:
            for slider, lbl in zip(self._sliders[key], self._labels[key]):
                lbl.setText(f"{slider.value() / _SCALE:.1f}")
        if not self._emitting:
            self.changed.emit(self.values())

    def values(self) -> Dict[str, List[float]]:
        """Current deviations as the four config arrays (lists of 6 floats)."""
        return {
            key: [s.value() / _SCALE for s in sliders]
            for key, sliders in self._sliders.items()
        }

    def set_values(self, offsets: Dict[str, List[float]]):
        """Initialise the sliders from a config-style dict (no signal storm)."""
        self._emitting = True
        try:
            for key, sliders in self._sliders.items():
                vals = offsets.get(key) or [0.0] * self._n
                for i, slider in enumerate(sliders):
                    v = vals[i] if i < len(vals) else 0.0
                    slider.setValue(int(round(v * _SCALE)))
        finally:
            self._emitting = False
        self._on_slider_changed(0)

    def reset(self):
        self.set_values({key: [0.0] * self._n for _, key in _ROWS})
        self.changed.emit(self.values())
