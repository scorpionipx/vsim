# vsim/utils/widgets/plotter_widget.py
"""
Plotter widget: rolling chart of the named signals streaming through
VorosimWidget, from any source (UDP / Assetto Corsa / Forza Horizon 5).

Drawn with plain QPainter rather than matplotlib -- nothing else in this
project pulls in matplotlib/numpy, and keeps this widget free of that
dependency.

Wire-up (see vsim/core.py):
    self.plotter_widget = PlotterWidget(logger=self.logger)
    self.vorosim_widget.signals_received.connect(self.plotter_widget.on_signals)

Stopped by default. Plotting (buffering + drawing) and recording (logging
every received signal to a CSV file) are independent toggles -- either can
run without the other.
"""

import csv
import time
from collections import deque

from PyQt6.QtCore import Qt, QTimer, QRectF, QPointF
from PyQt6.QtGui import QColor, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import (
    QGroupBox, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QCheckBox,
    QSpinBox, QScrollArea, QWidget, QColorDialog, QFileDialog, QSizePolicy,
)


MAX_SAMPLES = 1000
DEFAULT_REFRESH_MS = 200

# Cycled for newly-discovered signals (tab10-ish palette).
_PALETTE = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
]


class _SignalRow(QWidget):
    """One row in the signal list: a checkbox (plot this signal?) plus a
    color swatch button that opens a color picker."""

    def __init__(self, name: str, color: str, on_change=None, parent=None):
        super().__init__(parent)
        self.name = name
        self.color = color
        self._on_change = on_change

        row = QHBoxLayout(self)
        row.setContentsMargins(2, 0, 2, 0)

        self.checkbox = QCheckBox(name)
        self.checkbox.toggled.connect(self._changed)
        row.addWidget(self.checkbox, 1)

        self.color_btn = QPushButton()
        self.color_btn.setFixedSize(18, 18)
        self.color_btn.setToolTip("Choose plot color")
        self.color_btn.clicked.connect(self._pick_color)
        row.addWidget(self.color_btn)

        self._apply_swatch()

    def _apply_swatch(self):
        self.color_btn.setStyleSheet(
            f"background-color: {self.color}; border: 1px solid #666;"
        )

    def _pick_color(self):
        chosen = QColorDialog.getColor(QColor(self.color), self, f"Color for {self.name}")
        if chosen.isValid():
            self.color = chosen.name()
            self._apply_swatch()
            self._changed()

    def _changed(self, *_args):
        if self._on_change:
            self._on_change()


class _PlotCanvas(QWidget):
    """Minimal line-chart canvas: draws whatever series it is handed, with
    no plotting-library dependency."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(300, 200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._series: list[tuple[str, str, list]] = []  # (name, color, [(x, y), ...])

    def set_series(self, series: list[tuple[str, str, list]]):
        self._series = series
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        painter.fillRect(rect, self.palette().base())

        margin_left, margin_bottom, margin_top, margin_right = 52, 24, 10, 10
        plot = QRectF(
            margin_left, margin_top,
            max(1, rect.width() - margin_left - margin_right),
            max(1, rect.height() - margin_top - margin_bottom),
        )
        painter.setPen(QPen(QColor(140, 140, 140)))
        painter.drawRect(plot)

        plotted = [(n, c, pts) for n, c, pts in self._series if pts]
        if not plotted:
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter,
                              "No signals selected" if not self._series else "Waiting for data...")
            return

        all_x = [x for _, _, pts in plotted for x, _ in pts]
        all_y = [y for _, _, pts in plotted for _, y in pts]
        x_min, x_max = min(all_x), max(all_x)
        y_min, y_max = min(all_y), max(all_y)
        if x_min == x_max:
            x_max = x_min + 1
        if y_min == y_max:
            y_min, y_max = y_min - 1, y_max + 1
        pad = (y_max - y_min) * 0.08
        y_min -= pad
        y_max += pad

        def to_px(x, y):
            px = plot.left() + (x - x_min) / (x_max - x_min) * plot.width()
            py = plot.bottom() - (y - y_min) / (y_max - y_min) * plot.height()
            return px, py

        # horizontal gridlines + y-axis labels
        painter.setPen(QPen(QColor(220, 220, 220)))
        for i in range(5):
            frac = i / 4
            y = plot.bottom() - frac * plot.height()
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
        painter.setPen(QPen(QColor(120, 120, 120)))
        for i in range(5):
            frac = i / 4
            y = plot.bottom() - frac * plot.height()
            val = y_min + frac * (y_max - y_min)
            painter.drawText(
                QRectF(0, y - 8, margin_left - 6, 16),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                f"{val:.3g}",
            )

        # series lines
        for _name, color, pts in plotted:
            if len(pts) < 2:
                continue
            pen = QPen(QColor(color))
            pen.setWidthF(1.6)
            painter.setPen(pen)
            painter.drawPolyline(QPolygonF([QPointF(*to_px(x, y)) for x, y in pts]))

        # legend, top-right inside the plot area
        painter.setPen(QPen(QColor(80, 80, 80)))
        ly = plot.top() + 6
        for name, color, _pts in plotted:
            swatch = QRectF(plot.right() - 110, ly, 10, 10)
            painter.fillRect(swatch, QColor(color))
            painter.drawText(
                QRectF(plot.right() - 96, ly - 3, 92, 16),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                name,
            )
            ly += 16


class PlotterWidget(QGroupBox):
    """Rolling chart (last MAX_SAMPLES points) of any signals fed to it via
    ``on_signals``, plus an optional CSV recorder of every received signal.

    Stopped by default. Plotting and recording are independent: either can
    run without the other.
    """

    def __init__(self, logger=None, parent=None):
        super().__init__("Plotter", parent)
        self.logger = logger

        self._plotting = False
        self._recording = False
        self._record_file = None
        self._record_writer = None
        self._record_fields: list[str] = []

        self._tick = 0
        self._series: dict[str, deque] = {}     # signal name -> deque[(tick, value)]
        self._rows: dict[str, _SignalRow] = {}   # signal name -> row widget
        self._next_color = 0

        self._timer = QTimer(self)
        self._timer.setInterval(DEFAULT_REFRESH_MS)
        self._timer.timeout.connect(self._redraw)

        root = QVBoxLayout()
        root.addLayout(self._build_controls())

        body = QHBoxLayout()
        body.addWidget(self._build_signal_list())
        self._canvas = _PlotCanvas()
        body.addWidget(self._canvas, 1)
        root.addLayout(body)

        self.setLayout(root)

    # ---------- UI builders ----------
    def _build_controls(self):
        row = QHBoxLayout()

        self.start_btn = QPushButton("Start Plotting")
        self.start_btn.setCheckable(True)
        self.start_btn.toggled.connect(self._on_start_toggle)
        row.addWidget(self.start_btn)

        self.record_btn = QPushButton("Start Recording")
        self.record_btn.setCheckable(True)
        self.record_btn.setToolTip(
            "Log every received signal (not just the plotted ones) to a CSV "
            "file, independent of whether plotting is running."
        )
        self.record_btn.toggled.connect(self._on_record_toggle)
        row.addWidget(self.record_btn)

        row.addSpacing(16)
        row.addWidget(QLabel("Refresh (ms):"))
        self.refresh_spin = QSpinBox()
        self.refresh_spin.setRange(50, 5000)
        self.refresh_spin.setSingleStep(50)
        self.refresh_spin.setValue(DEFAULT_REFRESH_MS)
        self.refresh_spin.valueChanged.connect(self._timer.setInterval)
        row.addWidget(self.refresh_spin)

        row.addSpacing(16)
        self.record_status_label = QLabel("")
        self.record_status_label.setStyleSheet("color: gray;")
        row.addWidget(self.record_status_label)

        row.addStretch(1)
        return row

    def _build_signal_list(self):
        self._signal_list_layout = QVBoxLayout()
        self._signal_list_layout.setContentsMargins(4, 4, 4, 4)
        self._signal_list_layout.addStretch(1)

        container = QWidget()
        container.setLayout(self._signal_list_layout)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(container)
        scroll.setFixedWidth(180)
        return scroll

    # ---------- start/stop plotting ----------
    def _on_start_toggle(self, checked: bool):
        self._plotting = checked
        self.start_btn.setText("Stop Plotting" if checked else "Start Plotting")
        if checked:
            self._timer.start()
        else:
            self._timer.stop()

    # ---------- record to file ----------
    def _on_record_toggle(self, checked: bool):
        if checked:
            path, _filter = QFileDialog.getSaveFileName(
                self, "Record signals to CSV", "", "CSV files (*.csv)"
            )
            if not path:
                self.record_btn.setChecked(False)
                return
            try:
                self._record_file = open(path, "w", newline="", encoding="utf-8")
            except OSError as e:
                if self.logger:
                    self.logger.exception(f"Plotter: failed to open record file: {e}")
                self.record_btn.setChecked(False)
                return
            self._record_writer = csv.writer(self._record_file)
            self._record_fields = []  # header written lazily, from the first row
            self._recording = True
            self.record_btn.setText("Stop Recording")
            self.record_status_label.setText(f"Recording to {path}")
            if self.logger:
                self.logger.info(f"Plotter: recording signals to {path}")
        else:
            self._recording = False
            self.record_btn.setText("Start Recording")
            self.record_status_label.setText("")
            if self._record_file:
                try:
                    self._record_file.close()
                except OSError:
                    pass
            self._record_file = None
            self._record_writer = None
            self._record_fields = []

    # ---------- incoming data ----------
    def on_signals(self, named: dict):
        """Slot: receives every {signal_name: value} dict as it arrives from
        any source. Cheap no-op unless plotting or recording is active."""
        if not named:
            return
        if self._recording:
            self._record_row(named)
        if self._plotting:
            self._buffer_row(named)

    def _ensure_row(self, name: str):
        if name in self._rows:
            return
        color = _PALETTE[self._next_color % len(_PALETTE)]
        self._next_color += 1
        row = _SignalRow(name, color, on_change=self._redraw)
        self._rows[name] = row
        self._series[name] = deque(maxlen=MAX_SAMPLES)
        # insert before the trailing stretch
        self._signal_list_layout.insertWidget(self._signal_list_layout.count() - 1, row)

    def _buffer_row(self, named: dict):
        self._tick += 1
        for name, value in named.items():
            self._ensure_row(name)
            try:
                self._series[name].append((self._tick, float(value)))
            except (TypeError, ValueError):
                continue

    def _record_row(self, named: dict):
        # Fieldnames are captured from the first row and held fixed for the
        # rest of the file; a signal set that changes mid-run is unusual for
        # a single UDP/AC/Forza session, and this keeps the CSV columns
        # stable rather than emitting a second header block partway through.
        if not self._record_fields:
            self._record_fields = list(named.keys())
            self._record_writer.writerow(["timestamp"] + self._record_fields)
        row = [f"{time.time():.3f}"]
        row.extend(named.get(f, "") for f in self._record_fields)
        self._record_writer.writerow(row)
        self._record_file.flush()

    # ---------- redraw ----------
    def _redraw(self):
        series = [
            (name, row.color, list(self._series.get(name, ())))
            for name, row in self._rows.items()
            if row.checkbox.isChecked()
        ]
        self._canvas.set_series(series)

    def closeEvent(self, e):
        if self._recording:
            self.record_btn.setChecked(False)  # closes the file via the toggle handler
        super().closeEvent(e)
