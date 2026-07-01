"""Main application window.

The falling-notes piano fills the window and is always visible; all the
controls (file selector, track list, output dropdown, transport, progress
bar) live in a drawer that slides out from the left. Timers poll the engine
to animate the piano (~60 fps) and drive the progress bar, time labels, and
button states. Each track row has two independent toggles — an eye (show on
the piano) and a speaker (play audio) — so any set can be shown and any set
played; the highlighted row is the target for edits.
"""

import copy

from PySide6.QtCore import (
    Qt,
    QEasingCurve,
    QEvent,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSettings,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPolygonF,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSlider,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from midiplay import devices, edits, smf
from midiplay.engine import PlaybackEngine, PlayerState
from midiplay.piano_view import TRACK_COLORS, PianoRollView

# Note-less tracks (e.g. a conductor/tempo track) are shown greyed out.
EMPTY_TRACK_COLOR = QColor(0x80, 0x80, 0x80)

# Per-track state stored on each list item: independent "show on the piano"
# (eye) and "play audio" (speaker) toggles, painted by TrackToggleDelegate.
SHOW_ROLE = Qt.ItemDataRole.UserRole + 1
PLAY_ROLE = Qt.ItemDataRole.UserRole + 2
# Colour used for an icon whose toggle is off (dim grey).
ICON_OFF_COLOR = QColor(0x88, 0x88, 0x88, 130)

# Discrete playback-speed choices, shown as a row of buttons (1× = recorded
# tempo). Each is (multiplier, button label).
SPEED_OPTIONS = (
    (0.25, "0.25×"),
    (0.5, "0.5×"),
    (0.75, "0.75×"),
    (1.0, "1×"),
    (1.5, "1.5×"),
    (2.0, "2×"),
)
DEFAULT_SPEED = 1.0


def format_time(seconds: float) -> str:
    """Seconds -> M:SS (minutes are unbounded)."""
    total = max(0, int(round(seconds)))
    minutes, secs = divmod(total, 60)
    return f"{minutes}:{secs:02d}"


class SeekSlider(QSlider):
    """A horizontal slider where clicking or dragging anywhere on the groove
    jumps the handle to that position. Emits seekStarted while the user holds
    it, seekMoved as they scrub, and seekFinished on release."""

    seekStarted = Signal()
    seekMoved = Signal(int)
    seekFinished = Signal(int)

    def __init__(self) -> None:
        super().__init__(Qt.Orientation.Horizontal)
        self._dragging = False

    def _value_from_pos(self, x: float) -> int:
        span = self.maximum() - self.minimum()
        ratio = min(1.0, max(0.0, x / max(1, self.width())))
        return self.minimum() + round(ratio * span)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            self._dragging = True
            value = self._value_from_pos(event.position().x())
            self.setValue(value)
            self.seekStarted.emit()
            self.seekMoved.emit(value)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._dragging:
            value = self._value_from_pos(event.position().x())
            self.setValue(value)
            self.seekMoved.emit(value)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._dragging and event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            value = self._value_from_pos(event.position().x())
            self.setValue(value)
            self.seekFinished.emit(value)
            event.accept()
        else:
            super().mouseReleaseEvent(event)


def _hline() -> QFrame:
    """A thin horizontal separator between sections."""
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Sunken)
    return line


class _ResizeGrip(QWidget):
    """A thin vertical strip the user drags to resize a drawer. Reports each
    horizontal drag delta (global px) to a callback."""

    def __init__(self, parent, on_drag) -> None:
        super().__init__(parent)
        self._on_drag = on_drag
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        self._last_x = None

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._last_x = event.globalPosition().toPoint().x()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._last_x is not None:
            x = event.globalPosition().toPoint().x()
            self._on_drag(x - self._last_x)
            self._last_x = x
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._last_x is not None and event.button() == Qt.MouseButton.LeftButton:
            self._last_x = None
            event.accept()
        else:
            super().mouseReleaseEvent(event)


class Drawer:
    """One edge-docked panel that slides in and out of a `host` widget, with a
    toggle handle and a drag-to-resize grip on its inner edge. `side` is 'left'
    or 'right'. The panel floats over the host's main content (the piano)."""

    MIN_W = 240
    MAX_W = 720
    GRIP = 6
    HANDLE_W = 22
    HANDLE_H = 64

    def __init__(self, host, panel, side="left", width=340, start_open=True) -> None:
        self._host = host
        self._panel = panel
        self._side = side
        self._width = width
        self._open = start_open

        panel.setParent(host)
        panel.setObjectName("drawerPanel")
        panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self._grip = _ResizeGrip(host, self._on_grip_drag)

        self._handle = QPushButton(host)
        self._handle.setFixedSize(self.HANDLE_W, self.HANDLE_H)
        self._handle.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._handle.setToolTip("Show or hide this panel")
        self._handle.clicked.connect(self.toggle)

        self._anim = QPropertyAnimation(panel, b"pos", host)
        self._anim.setDuration(200)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._anim.valueChanged.connect(lambda _pos: self._place_edge_widgets())

        self._update_handle_text()
        panel.raise_()
        self._grip.raise_()
        self._handle.raise_()

    def _open_x(self) -> int:
        return 0 if self._side == "left" else self._host.width() - self._width

    def _closed_x(self) -> int:
        return -self._width if self._side == "left" else self._host.width()

    def relayout(self) -> None:
        x = self._open_x() if self._open else self._closed_x()
        self._panel.setGeometry(x, 0, self._width, self._host.height())
        self._place_edge_widgets()

    def _edge_x(self) -> int:
        # x of the panel's inner edge (right edge for a left drawer, else left).
        return self._panel.x() + self._width if self._side == "left" else self._panel.x()

    def _place_edge_widgets(self) -> None:
        edge = self._edge_x()
        height = self._host.height()
        self._grip.setGeometry(edge - self.GRIP // 2, 0, self.GRIP, height)
        self._grip.setVisible(self._open)
        y = max(0, (height - self.HANDLE_H) // 2)
        self._handle.move(edge if self._side == "left" else edge - self.HANDLE_W, y)
        self._grip.raise_()
        self._handle.raise_()

    def _update_handle_text(self) -> None:
        if self._side == "left":
            self._handle.setText("‹" if self._open else "›")
        else:
            self._handle.setText("›" if self._open else "‹")

    def toggle(self) -> None:
        self._open = not self._open
        self._update_handle_text()
        self._grip.setVisible(self._open)
        self._anim.stop()
        self._anim.setStartValue(self._panel.pos())
        target = self._open_x() if self._open else self._closed_x()
        self._anim.setEndValue(QPoint(target, 0))
        self._anim.start()

    def _on_grip_drag(self, dx: int) -> None:
        if not self._open:
            return
        delta = dx if self._side == "left" else -dx
        self._width = max(self.MIN_W, min(self.MAX_W, self._width + delta))
        self.relayout()

    def set_panel_style(self, style: str) -> None:
        self._panel.setStyleSheet(style)


class DrawerHost(QWidget):
    """A widget whose `content` fills it, with any number of edge Drawers
    floating over the content."""

    def __init__(self, content: QWidget) -> None:
        super().__init__()
        self._content = content
        content.setParent(self)
        self._drawers: list[Drawer] = []

    def add_drawer(self, drawer: Drawer) -> None:
        self._drawers.append(drawer)
        drawer.relayout()

    def resizeEvent(self, event) -> None:
        self._content.setGeometry(0, 0, self.width(), self.height())
        for drawer in self._drawers:
            drawer.relayout()
        super().resizeEvent(event)


class TrackToggleDelegate(QStyledItemDelegate):
    """Draws each track row with two independent toggle icons in a left gutter:
    an eye (show the track on the piano) and a speaker (play its audio). Bright
    in the track's colour = on; dim grey = off. Clicking an icon flips just that
    state and emits a signal; clicking anywhere else selects the row (the edit
    target) as usual."""

    showToggled = Signal(int)  # emits the track index whose eye was clicked
    playToggled = Signal(int)  # emits the track index whose speaker was clicked

    ICON = 16      # icon box size (px)
    LEFT = 8       # left margin before the first icon
    GAP = 8        # gap between the two icons (and after them)

    def gutter(self) -> int:
        return self.LEFT + self.ICON + self.GAP + self.ICON + self.GAP

    def _icon_rects(self, rect):
        y = rect.top() + (rect.height() - self.ICON) // 2
        x = rect.left() + self.LEFT
        eye = QRect(x, y, self.ICON, self.ICON)
        spk = QRect(x + self.ICON + self.GAP, y, self.ICON, self.ICON)
        return eye, spk

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        size.setHeight(max(size.height(), 24))
        return size

    def paint(self, painter, option, index) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        widget = option.widget
        style = widget.style() if widget else QApplication.style()

        # 1) Full-row background/selection (with the text blanked out).
        text = opt.text
        opt.text = ""
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)

        # 2) The two toggle icons in the gutter.
        fg = index.data(Qt.ItemDataRole.ForegroundRole)
        base = QColor(0xDD, 0xDD, 0xDD)
        if hasattr(fg, "color"):     # a QBrush (what setForeground stores)
            base = fg.color()
        elif isinstance(fg, QColor):
            base = fg
        show = bool(index.data(SHOW_ROLE))
        play = bool(index.data(PLAY_ROLE))
        eye_rect, spk_rect = self._icon_rects(option.rect)
        self._draw_eye(painter, QRectF(eye_rect), base if show else ICON_OFF_COLOR, show)
        self._draw_speaker(painter, QRectF(spk_rect), base if play else ICON_OFF_COLOR, play)

        # 3) The track label, indented past the gutter.
        painter.save()
        if option.state & QStyle.StateFlag.State_Selected:
            painter.setPen(opt.palette.color(QPalette.ColorRole.HighlightedText))
        else:
            painter.setPen(base)
        text_rect = option.rect.adjusted(self.gutter(), 0, -6, 0)
        painter.drawText(
            text_rect,
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            text,
        )
        painter.restore()

    def editorEvent(self, event, model, option, index) -> bool:
        if (
            event.type() == QEvent.Type.MouseButtonPress
            and event.button() == Qt.MouseButton.LeftButton
        ):
            eye_rect, spk_rect = self._icon_rects(option.rect)
            pos = event.position().toPoint()
            track = index.data(Qt.ItemDataRole.UserRole)
            if eye_rect.contains(pos):
                model.setData(index, not bool(index.data(SHOW_ROLE)), SHOW_ROLE)
                self.showToggled.emit(track)
                return True   # consume so the row isn't re-selected
            if spk_rect.contains(pos):
                model.setData(index, not bool(index.data(PLAY_ROLE)), PLAY_ROLE)
                self.playToggled.emit(track)
                return True
        return super().editorEvent(event, model, option, index)

    @staticmethod
    def _draw_eye(painter, rect, color, on) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy = rect.center().x(), rect.center().y()
        w, h = rect.width(), rect.height()
        painter.setPen(QPen(color, 1.4))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        path = QPainterPath()  # an almond eye outline
        path.moveTo(cx - w * 0.42, cy)
        path.quadTo(cx, cy - h * 0.36, cx + w * 0.42, cy)
        path.quadTo(cx, cy + h * 0.36, cx - w * 0.42, cy)
        painter.drawPath(path)
        painter.setBrush(color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPointF(cx, cy), h * 0.13, h * 0.13)
        if not on:  # a slash for "hidden"
            painter.setPen(QPen(color, 1.4))
            painter.drawLine(
                QPointF(cx - w * 0.40, cy + h * 0.34),
                QPointF(cx + w * 0.40, cy - h * 0.34),
            )
        painter.restore()

    @staticmethod
    def _draw_speaker(painter, rect, color, on) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy = rect.center().x(), rect.center().y()
        w, h = rect.width(), rect.height()
        painter.setPen(QPen(color, 1.2))
        painter.setBrush(color)
        box_w, box_h = w * 0.20, h * 0.30
        left = rect.left() + w * 0.14
        cone = QPolygonF([
            QPointF(left, cy - box_h / 2),
            QPointF(left, cy + box_h / 2),
            QPointF(left + box_w, cy + box_h / 2),
            QPointF(left + box_w + w * 0.20, cy + h * 0.30),
            QPointF(left + box_w + w * 0.20, cy - h * 0.30),
            QPointF(left + box_w, cy - box_h / 2),
        ])
        painter.drawPolygon(cone)
        if on:  # two sound-wave arcs
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(color, 1.3))
            for radius in (w * 0.16, w * 0.28):
                arc = QRectF(cx + w * 0.02 - radius, cy - radius, radius * 2, radius * 2)
                painter.drawArc(arc, -50 * 16, 100 * 16)
        painter.restore()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("MIDI Track Player")
        self.resize(940, 620)

        # Currently loaded file (None until a file is opened).
        self.midi_file = None  # type: ignore[assignment]
        self.file_info = None  # type: ignore[assignment]
        self.track_infos = []  # type: ignore[var-annotated]

        # Playback engine + the open output port it sends through.
        self.engine = PlaybackEngine()
        self._port = None
        self._port_name = None
        self._prepared_key = None  # (id(midi_file), track_index) already loaded

        self._settings = QSettings()  # remembers the last output device
        self.setAcceptDrops(True)     # drag a .mid onto the window
        self._seeking = False         # True while the user drags the seek bar

        # Editing state: snapshot-based undo/redo + unsaved-changes tracking.
        self._undo_stack: list = []
        self._redo_stack: list = []
        self._dirty = False
        self._save_path = None        # set once the user has chosen a Save target
        self._pending_select_row = None    # row to select after a structural edit
        self._pending_show_indices = None  # tracks to re-show after a structural edit
        self._pending_play_indices = None  # tracks to re-play after a structural edit

        self._build_menus()

        # The always-visible piano fills the window; a left drawer holds the
        # controls and a right drawer holds view/appearance settings, both
        # floating over the piano.
        controls = QWidget()
        controls.setMinimumWidth(240)
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(12, 12, 12, 12)
        controls_layout.setSpacing(10)
        controls_layout.addLayout(self._build_top())
        controls_layout.addWidget(_hline())
        controls_layout.addLayout(self._build_middle())
        controls_layout.addWidget(_hline())
        controls_layout.addLayout(self._build_bottom())

        settings_panel = self._build_settings_panel()

        self._roll = PianoRollView()
        self._roll.notesDeleteRequested.connect(self._delete_notes)
        self._roll.notesEditRequested.connect(self._edit_notes)
        self._roll.noteAddRequested.connect(self._add_note)
        self._roll.playPauseRequested.connect(self._toggle_play_pause)
        self._roll.scrubFinished.connect(self.engine.seek)
        self._roll.lookAheadChanged.connect(self._sync_lookahead_slider)
        self._roll.legendToggled.connect(self._sync_legend_check)

        host = DrawerHost(self._roll)
        self._left_drawer = Drawer(host, controls, side="left", width=340)
        # The settings drawer starts tucked away on the right.
        self._right_drawer = Drawer(
            host, settings_panel, side="right", width=300, start_open=False
        )
        host.add_drawer(self._left_drawer)
        host.add_drawer(self._right_drawer)
        self.setCentralWidget(host)
        self.statusBar().showMessage("No file loaded")

        self._refresh_devices()
        self._restore_last_device()
        self._restore_output_channel()
        self._restore_view_settings()  # opacity, fall time, labels, legend

        # Poll the engine to drive the progress bar, time labels, and which
        # transport buttons are enabled.
        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(50)  # ms
        self._ui_timer.timeout.connect(self._update_ui)
        self._ui_timer.start()
        self._update_ui()

        # Animate the always-visible piano from the engine position (~60 fps).
        self._frame_timer = QTimer(self)
        self._frame_timer.setInterval(16)
        self._frame_timer.timeout.connect(self._tick_roll)
        self._frame_timer.start()

        self._update_title()
        self._update_edit_actions()

        # Spacebar toggles play/pause (buttons are set NoFocus so they don't
        # swallow it). The piano forwards its own Space via a signal too, but
        # this window-level shortcut normally handles it first.
        self._space_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Space), self)
        self._space_shortcut.activated.connect(self._toggle_play_pause)

    # -- Top: MIDI file selector ------------------------------------------
    def _build_top(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.file_path_edit = QLineEdit()
        self.file_path_edit.setReadOnly(True)
        self.file_path_edit.setPlaceholderText("No file loaded")
        self.open_button = QPushButton("Open…")
        self.open_button.clicked.connect(self._choose_file)

        row.addWidget(QLabel("MIDI File:"))
        row.addWidget(self.file_path_edit, stretch=1)
        row.addWidget(self.open_button)
        return row

    # -- Middle: track list + output device -------------------------------
    def _build_middle(self) -> QVBoxLayout:
        col = QVBoxLayout()

        col.addWidget(QLabel("Tracks — 👁 show on piano · 🔊 play audio"))
        self.track_list = QListWidget()
        self.track_list.setEnabled(False)  # enabled once a file is loaded
        # Each row has an eye (show) and speaker (play) toggle drawn by the
        # delegate; the highlighted (current) row is the target for edits.
        self.track_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self._track_delegate = TrackToggleDelegate(self.track_list)
        self.track_list.setItemDelegate(self._track_delegate)
        self._track_delegate.showToggled.connect(self._on_show_toggled)
        self._track_delegate.playToggled.connect(self._on_play_toggled)
        self.track_list.currentRowChanged.connect(self._on_edit_target_changed)
        self.track_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.track_list.customContextMenuRequested.connect(self._track_context_menu)
        col.addWidget(self.track_list, stretch=1)

        device_row = QHBoxLayout()
        self.device_combo = QComboBox()
        self.device_combo.currentIndexChanged.connect(self._on_device_changed)
        self.channel_combo = QComboBox()
        self.channel_combo.addItem("As recorded")  # index 0 -> keep channels
        for ch in range(1, 17):
            self.channel_combo.addItem(str(ch))     # index i -> channel i-1
        self.channel_combo.setToolTip(
            "Force the played track onto one MIDI channel — set this to your "
            "keyboard's receive channel if only some tracks make sound."
        )
        self.channel_combo.currentIndexChanged.connect(self._on_channel_changed)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self._refresh_devices)
        device_row.addWidget(QLabel("MIDI Output:"))
        device_row.addWidget(self.device_combo, stretch=1)
        device_row.addWidget(QLabel("Channel:"))
        device_row.addWidget(self.channel_combo)
        device_row.addWidget(self.refresh_button)
        col.addLayout(device_row)
        return col

    # -- Bottom: transport + progress -------------------------------------
    def _build_bottom(self) -> QVBoxLayout:
        col = QVBoxLayout()

        transport = QHBoxLayout()
        self.play_button = QPushButton("Play")
        self.pause_button = QPushButton("Pause")
        self.stop_button = QPushButton("Stop")
        self.restart_button = QPushButton("Restart")
        self.play_button.clicked.connect(self._on_play)
        self.pause_button.clicked.connect(self._on_pause)
        self.stop_button.clicked.connect(self._on_stop)
        self.restart_button.clicked.connect(self._on_restart)
        for button in (
            self.play_button,
            self.pause_button,
            self.stop_button,
            self.restart_button,
        ):
            button.setEnabled(False)  # _update_ui manages this from here on
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # let Space be a shortcut
            transport.addWidget(button)
        col.addLayout(transport)

        progress = QHBoxLayout()
        self.current_time_label = QLabel("0:00")
        self.total_time_label = QLabel("0:00")
        self.progress_slider = SeekSlider()
        self.progress_slider.setRange(0, 1000)
        self.progress_slider.setEnabled(False)  # enabled once a track is ready
        self.progress_slider.seekStarted.connect(self._on_seek_started)
        self.progress_slider.seekMoved.connect(self._on_seek_moved)
        self.progress_slider.seekFinished.connect(self._on_seek_finished)
        progress.addWidget(self.current_time_label)
        progress.addWidget(self.progress_slider, stretch=1)
        progress.addWidget(self.total_time_label)
        col.addLayout(progress)

        col.addLayout(self._build_options())
        return col

    # -- Options: loop / mute / speed -------------------------------------
    def _build_options(self) -> QHBoxLayout:
        row = QHBoxLayout()

        self.loop_check = QCheckBox("Loop")
        self.loop_check.toggled.connect(self.engine.set_loop)
        self.mute_check = QCheckBox("Mute")
        self.mute_check.toggled.connect(self.engine.set_muted)
        row.addWidget(self.loop_check)
        row.addWidget(self.mute_check)
        row.addStretch(1)

        row.addWidget(QLabel("Speed:"))
        # Exclusive group of checkable buttons — one fixed speed each.
        self.speed_group = QButtonGroup(self)
        self.speed_group.setExclusive(True)
        for value, label in SPEED_OPTIONS:
            button = QPushButton(label)
            button.setCheckable(True)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # keep Space for play/pause
            button.setFixedWidth(48)
            button.setChecked(value == DEFAULT_SPEED)
            button.clicked.connect(
                lambda _checked, v=value: self._on_speed_changed(v)
            )
            self.speed_group.addButton(button)
            row.addWidget(button)
        return row

    # -- Right drawer: view / appearance settings -------------------------
    def _build_settings_panel(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(220)
        col = QVBoxLayout(panel)
        col.setContentsMargins(12, 12, 12, 12)
        col.setSpacing(10)
        col.addWidget(QLabel("<b>Settings</b>"))

        col.addWidget(_hline())
        col.addWidget(QLabel("Appearance"))
        opacity_row = QHBoxLayout()
        self.opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.opacity_slider.setRange(40, 100)   # percent
        self.opacity_slider.setToolTip("How opaque the sliding panels are")
        self.opacity_slider.valueChanged.connect(self._on_opacity_changed)
        self.opacity_label = QLabel()
        opacity_row.addWidget(QLabel("Panel opacity"))
        opacity_row.addWidget(self.opacity_slider, stretch=1)
        opacity_row.addWidget(self.opacity_label)
        col.addLayout(opacity_row)

        col.addWidget(_hline())
        col.addWidget(QLabel("Piano"))
        fall_row = QHBoxLayout()
        self.lookahead_slider = QSlider(Qt.Orientation.Horizontal)
        self.lookahead_slider.setRange(1, 10)   # seconds of look-ahead
        self.lookahead_slider.setToolTip("How many seconds of upcoming notes are visible")
        self.lookahead_slider.valueChanged.connect(self._on_lookahead_changed)
        self.lookahead_label = QLabel()
        fall_row.addWidget(QLabel("Fall time"))
        fall_row.addWidget(self.lookahead_slider, stretch=1)
        fall_row.addWidget(self.lookahead_label)
        col.addLayout(fall_row)

        self.labels_check = QCheckBox("Note-name labels")
        self.labels_check.toggled.connect(self._on_labels_toggled)
        col.addWidget(self.labels_check)
        self.legend_check = QCheckBox("Control legend (H)")
        self.legend_check.toggled.connect(self._on_legend_toggled)
        col.addWidget(self.legend_check)

        col.addStretch(1)
        return panel

    def _panel_style(self, alpha: int, side: str) -> str:
        """Stylesheet for a drawer panel: a translucent light background so the
        piano shows faintly through, with a subtle divider on the inner edge."""
        border = "border-right" if side == "left" else "border-left"
        return (
            f"#drawerPanel {{ background: rgba(246, 247, 249, {alpha}); "
            f"{border}: 1px solid rgba(120, 120, 130, 90); }}"
        )

    def _apply_panel_opacity(self, percent: int) -> None:
        alpha = max(0, min(255, round(percent / 100 * 255)))
        self._left_drawer.set_panel_style(self._panel_style(alpha, "left"))
        self._right_drawer.set_panel_style(self._panel_style(alpha, "right"))

    def _on_opacity_changed(self, percent: int) -> None:
        self.opacity_label.setText(f"{percent}%")
        self._apply_panel_opacity(percent)
        self._settings.setValue("panel_opacity", percent)

    def _on_lookahead_changed(self, seconds: int) -> None:
        self.lookahead_label.setText(f"{seconds}s")
        self._roll.set_look_ahead(float(seconds))
        self._settings.setValue("look_ahead", seconds)

    def _sync_lookahead_slider(self, seconds: float) -> None:
        """Reflect the piano's look-ahead (e.g. changed by middle-drag zoom)
        back onto the slider without feeding the change back to the piano."""
        value = max(1, min(10, int(round(seconds))))
        self.lookahead_slider.blockSignals(True)
        self.lookahead_slider.setValue(value)
        self.lookahead_slider.blockSignals(False)
        self.lookahead_label.setText(f"{value}s")

    def _on_labels_toggled(self, on: bool) -> None:
        self._roll.set_labels_visible(on)
        self._settings.setValue("note_labels", on)

    def _on_legend_toggled(self, on: bool) -> None:
        self._roll.set_legend_visible(on)
        self._settings.setValue("legend", on)

    def _sync_legend_check(self, on: bool) -> None:
        """Reflect the piano's legend state (toggled with 'H') on the checkbox."""
        self.legend_check.blockSignals(True)
        self.legend_check.setChecked(on)
        self.legend_check.blockSignals(False)

    def _restore_view_settings(self) -> None:
        """Load persisted view settings and apply them to the widgets AND the
        piano/panels. We apply directly (not only via the widgets' signals),
        because a persisted value that equals a widget's default state emits no
        change signal and would otherwise never be applied."""
        opacity = max(40, min(100, int(self._settings.value("panel_opacity", 80))))
        look_ahead = max(1, min(10, int(self._settings.value("look_ahead", 3))))
        labels = self._settings.value("note_labels", True, type=bool)
        legend = self._settings.value("legend", True, type=bool)

        self.opacity_slider.setValue(opacity)
        self.lookahead_slider.setValue(look_ahead)
        self.labels_check.setChecked(labels)
        self.legend_check.setChecked(legend)

        self.opacity_label.setText(f"{opacity}%")
        self.lookahead_label.setText(f"{look_ahead}s")
        self._apply_panel_opacity(opacity)
        self._roll.set_look_ahead(float(look_ahead))
        self._roll.set_labels_visible(labels)
        self._roll.set_legend_visible(legend)

    # -- File loading -----------------------------------------------------
    def _choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open MIDI File",
            "",
            "MIDI files (*.mid *.midi);;All files (*)",
        )
        if path:
            self.load_file(path)

    def load_file(self, path: str) -> None:
        """Load a MIDI file and update the UI. Shared by the dialog and
        (later) drag-and-drop. Shows a message box on failure."""
        try:
            midi = smf.load(path)
            info = smf.describe(midi, path)
        except smf.MidiLoadError as exc:
            QMessageBox.warning(self, "Could not open file", str(exc))
            return

        self.engine.stop()
        self.midi_file = midi
        self.file_info = info
        self._prepared_key = None  # force the engine to reload the new file
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._dirty = False
        self._save_path = None     # don't silently overwrite the opened file
        self.file_path_edit.setText(path)
        self.statusBar().showMessage(f"Loaded: {info.summary()}")
        self._populate_tracks()
        self._sync_all(preserve_playhead=False)  # load the played/shown track(s)
        self._update_title()
        self._update_edit_actions()

    def _populate_tracks(
        self, select_row: int | None = None, show_indices=None, play_indices=None
    ) -> None:
        """Fill the track list from the loaded file. Each row carries two
        independent states — shown on the piano (eye) and played (speaker).
        `show_indices` / `play_indices` set which start on, each defaulting to
        the first track with notes. Selects `select_row` (clamped) if given,
        else the first track with notes."""
        self.track_infos = smf.track_infos(self.midi_file)
        default = {smf.first_track_with_notes(self.midi_file)}
        show = default if show_indices is None else set(show_indices)
        play = default if play_indices is None else set(play_indices)

        self.track_list.blockSignals(True)
        self.track_list.clear()
        for info in self.track_infos:
            item = QListWidgetItem(info.label())
            item.setData(Qt.ItemDataRole.UserRole, info.index)
            item.setData(SHOW_ROLE, info.index in show)
            item.setData(PLAY_ROLE, info.index in play)
            # Color the title to match the piano; grey out note-less tracks.
            if info.has_notes:
                item.setForeground(TRACK_COLORS[info.index % len(TRACK_COLORS)])
            else:
                item.setForeground(EMPTY_TRACK_COLOR)
            self.track_list.addItem(item)

        self.track_list.setEnabled(True)
        if select_row is None:
            select_row = smf.first_track_with_notes(self.midi_file)
        select_row = max(0, min(select_row, self.track_list.count() - 1))
        self.track_list.setCurrentRow(select_row)
        self.track_list.blockSignals(False)

    def selected_track_index(self) -> int | None:
        """0-based index of the highlighted (current) track — the single track
        that edits apply to. None if the list is empty."""
        item = self.track_list.currentItem()
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    def _tracks_with_role(self, role) -> list[int]:
        indices = []
        for row in range(self.track_list.count()):
            item = self.track_list.item(row)
            if bool(item.data(role)):
                indices.append(item.data(Qt.ItemDataRole.UserRole))
        return sorted(indices)

    def show_track_indices(self) -> list[int]:
        """0-based indices of the tracks shown on the piano (eye on)."""
        return self._tracks_with_role(SHOW_ROLE)

    def play_track_indices(self) -> list[int]:
        """0-based indices of the tracks that play (speaker on)."""
        return self._tracks_with_role(PLAY_ROLE)

    # -- MIDI output devices ----------------------------------------------
    def _refresh_devices(self) -> None:
        """(Re)populate the output dropdown, keeping the current choice if it
        is still present. Disables the dropdown when no outputs are found."""
        previous = self.selected_device_name()
        try:
            outputs = devices.list_outputs()
        except Exception as exc:  # backend/driver failure
            outputs = []
            self.statusBar().showMessage(f"Could not list MIDI outputs: {exc}")

        # Rebuilding the list fires currentIndexChanged; block it so we don't
        # treat a repopulate as a user device change.
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        if outputs:
            self.device_combo.addItems(outputs)
            self.device_combo.setEnabled(True)
            if previous in outputs:
                self.device_combo.setCurrentText(previous)
        else:
            self.device_combo.addItem("No MIDI outputs found")
            self.device_combo.setEnabled(False)
        self.device_combo.blockSignals(False)

    def selected_device_name(self) -> str | None:
        """Name of the chosen output port, or None if none is available."""
        if not self.device_combo.isEnabled():
            return None
        text = self.device_combo.currentText()
        return text or None

    def _on_device_changed(self, _index: int = -1) -> None:
        """User picked a different output: stop and drop the old port so the
        new one is opened on the next Play, and remember the choice."""
        self.engine.stop()
        self.engine.set_output(None)
        self._close_port()
        name = self.selected_device_name()
        if name:
            self._settings.setValue("output_device", name)

    def _on_channel_changed(self, index: int) -> None:
        channel = None if index == 0 else index - 1
        self.engine.set_output_channel(channel)
        self._settings.setValue("output_channel", index)

    def _restore_output_channel(self) -> None:
        index = int(self._settings.value("output_channel", 0))
        index = max(0, min(index, self.channel_combo.count() - 1))
        self.channel_combo.blockSignals(True)
        self.channel_combo.setCurrentIndex(index)
        self.channel_combo.blockSignals(False)
        self.engine.set_output_channel(None if index == 0 else index - 1)

    def _restore_last_device(self) -> None:
        """Reselect the output device used last time, if it's still present."""
        last = self._settings.value("output_device", "")
        if last and self.device_combo.isEnabled():
            index = self.device_combo.findText(last)
            if index >= 0:
                # Block signals so restoring isn't treated as a user change.
                self.device_combo.blockSignals(True)
                self.device_combo.setCurrentIndex(index)
                self.device_combo.blockSignals(False)

    def _on_edit_target_changed(self, *_args) -> None:
        """User highlighted a different row: it becomes the edit target, so
        re-emphasize it on the piano. Show/play sets are unaffected."""
        self._refresh_piano_view()
        self._update_edit_actions()

    def _on_show_toggled(self, _track: int) -> None:
        """User toggled a track's eye: update what the piano draws (playback is
        unaffected)."""
        self._refresh_piano_view()
        self._update_edit_actions()

    def _on_play_toggled(self, _track: int) -> None:
        """User toggled a track's speaker: reload the engine with the played
        set, keeping the playhead so toggling doesn't restart the song. Refresh
        the piano too since its time span follows the played duration."""
        self._sync_playback(preserve_playhead=True)
        self._refresh_piano_view()
        self._update_edit_actions()

    def _sync_playback(self, preserve_playhead: bool) -> None:
        """Load the played (speaker-on) tracks into the engine. When
        `preserve_playhead`, keep the current position and play state across the
        reload; otherwise start fresh from 0."""
        play = self.play_track_indices()
        if not (self.midi_file is not None and play):
            self.engine.stop()
            self._prepared_key = None
            return
        position = self.engine.position()
        was_playing = self.engine.state() == PlayerState.PLAYING
        self.engine.set_tracks(self.midi_file, play)  # stops + resets to 0
        self._prepared_key = (id(self.midi_file), tuple(play))
        if preserve_playhead:
            self.engine.seek(min(position, self.engine.duration()))
            if was_playing:
                self.engine.play()

    def _sync_all(self, preserve_playhead: bool) -> None:
        """Reload both the engine (played tracks) and the piano (shown tracks)."""
        self._sync_playback(preserve_playhead)
        self._refresh_piano_view()

    def _ensure_port(self) -> bool:
        """Make sure the selected output port is open and handed to the
        engine. Returns False (with a message) if it cannot be opened."""
        name = self.selected_device_name()
        if name is None:
            self.statusBar().showMessage("No MIDI output available")
            return False
        if self._port is not None and self._port_name == name:
            return True
        self._close_port()
        try:
            self._port = devices.open_output(name)
        except Exception as exc:
            QMessageBox.warning(
                self, "MIDI output error", f"Could not open '{name}':\n{exc}"
            )
            return False
        self._port_name = name
        self.engine.set_output(self._port)
        return True

    def _close_port(self) -> None:
        if self._port is not None:
            try:
                self._port.close()
            except Exception:
                pass
        self._port = None
        self._port_name = None

    def _prepare(self) -> bool:
        """Validate file/track/device and load the track into the engine if
        it isn't already. Returns True when playback can start."""
        if self.midi_file is None:
            self.statusBar().showMessage("Open a MIDI file first")
            return False
        tracks = self.play_track_indices()
        if not tracks:
            self.statusBar().showMessage("Enable a track's audio (🔊) to play")
            return False
        if not self._ensure_port():
            return False

        key = (id(self.midi_file), tuple(tracks))
        if self._prepared_key != key:
            self.engine.set_tracks(self.midi_file, tracks)
            self._prepared_key = key
        if self.engine.duration() <= 0:
            self.statusBar().showMessage("That track has no playable events")
            return False
        return True

    # -- Transport handlers ----------------------------------------------
    def _on_play(self) -> None:
        if self.engine.state() == PlayerState.PAUSED:
            self.engine.play()  # resume; keep current position
            return
        if self._prepare():
            # Reapply controller state for the start position now that the
            # port is open (handles seeking before the first Play).
            self.engine.seek(self.engine.position())
            self.engine.play()

    def _on_pause(self) -> None:
        self.engine.pause()

    def _toggle_play_pause(self) -> None:
        if self.engine.state() == PlayerState.PLAYING:
            self._on_pause()
        else:
            self._on_play()

    def _on_stop(self) -> None:
        self.engine.stop()

    def _on_restart(self) -> None:
        if self._prepare():
            self.engine.restart()

    def _on_speed_changed(self, multiplier: float) -> None:
        self.engine.set_speed(multiplier)

    # -- Seeking ----------------------------------------------------------
    def _on_seek_started(self) -> None:
        self._seeking = True
        self._roll.begin_scrub()

    def _on_seek_moved(self, value: int) -> None:
        # Live time readout + falling-notes preview while scrubbing (audio jumps
        # only on release).
        duration = self.engine.duration()
        seconds = value / 1000 * duration
        self.current_time_label.setText(format_time(seconds))
        self._roll.set_scrub_position(seconds)

    def _on_seek_finished(self, value: int) -> None:
        self._seeking = False
        duration = self.engine.duration()
        if duration > 0:
            self.engine.seek(value / 1000 * duration)
        self._roll.end_scrub()

    # -- Piano (falling-notes) view --------------------------------------
    def _tick_roll(self) -> None:
        """Advance the always-visible piano to the engine's position."""
        self._roll.set_position(self.engine.position())

    def _delete_notes(self, note_ids) -> None:
        """Delete the given notes from the selected track (via undo-able edit)."""
        index = self.selected_track_index()
        if index is None or not note_ids:
            return
        self._apply_track_edit(index, lambda m: edits.delete_notes(m, index, note_ids))

    def _edit_notes(self, changes) -> None:
        """Move/resize notes on the selected track (via undo-able edit)."""
        index = self.selected_track_index()
        if index is None or not changes:
            return
        self._apply_track_edit(index, lambda m: edits.edit_notes(m, index, changes))

    def _add_note(self, payload) -> None:
        """Add a note (pitch, start_tick) to the selected track."""
        index = self.selected_track_index()
        if index is None:
            return
        pitch, start_tick = payload
        self._apply_track_edit(index, lambda m: edits.add_note(m, index, pitch, start_tick))

    def _refresh_piano_view(self, *_args) -> None:
        """Show the eye-on tracks on the piano, color-coded, with the current
        edit-target row emphasized (and editable) and the rest dimmed. Called
        when the shown set, the played set, the highlighted row, or the file
        changes."""
        if self.midi_file is None:
            self._roll.set_multi_notes([], 0.0, None)
            return
        self._roll.set_time_map(smf.TimeMap(self.midi_file))
        shown = self.show_track_indices()
        # The edit target is emphasized only when it is itself shown; otherwise
        # nothing is editable, matching what's visible.
        primary = self.selected_track_index()
        per_track = smf.extract_notes_for(self.midi_file, shown)
        # Span at least the played duration, but also any shown notes that
        # extend past it (a track can be shown without being played).
        duration = self.engine.duration()
        for _index, notes in per_track:
            if notes:
                duration = max(duration, max(n.end for n in notes))
        self._roll.set_multi_notes(per_track, duration, primary)

    # -- Drag and drop ----------------------------------------------------
    def dragEnterEvent(self, event) -> None:
        if self._dropped_midi_path(event) is not None:
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        path = self._dropped_midi_path(event)
        if path:
            self.load_file(path)

    @staticmethod
    def _dropped_midi_path(event) -> str | None:
        """First .mid/.midi local file in a drag event, or None."""
        mime = event.mimeData()
        if not mime.hasUrls():
            return None
        for url in mime.urls():
            path = url.toLocalFile()
            if path.lower().endswith((".mid", ".midi")):
                return path
        return None

    # -- Periodic UI refresh ---------------------------------------------
    def _update_ui(self) -> None:
        state = self.engine.state()
        position = self.engine.position()
        duration = self.engine.duration()

        self.progress_slider.setEnabled(duration > 0)
        if self._seeking:
            pass  # the user is dragging; don't fight the handle or the label
        elif duration > 0:
            self.progress_slider.setValue(int(position / duration * 1000))
            self.current_time_label.setText(format_time(position))
        else:
            self.progress_slider.setValue(0)
            self.current_time_label.setText(format_time(position))
        self.total_time_label.setText(format_time(duration))

        ready = (
            self.midi_file is not None
            and bool(self.play_track_indices())
            and self.selected_device_name() is not None
        )
        playing = state == PlayerState.PLAYING
        paused = state == PlayerState.PAUSED
        self.play_button.setEnabled(ready and not playing)
        self.pause_button.setEnabled(playing)
        self.stop_button.setEnabled(playing or paused)
        self.restart_button.setEnabled(ready)

    # -- Editing: menus ---------------------------------------------------
    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("File")
        act_open = file_menu.addAction("Open…")
        act_open.setShortcut(QKeySequence.StandardKey.Open)
        act_open.triggered.connect(self._choose_file)
        file_menu.addSeparator()
        self.act_save = file_menu.addAction("Save")
        self.act_save.setShortcut(QKeySequence.StandardKey.Save)
        self.act_save.triggered.connect(self._save)
        self.act_save_as = file_menu.addAction("Save As…")
        self.act_save_as.setShortcut(QKeySequence.StandardKey.SaveAs)
        self.act_save_as.triggered.connect(self._save_as)

        edit_menu = self.menuBar().addMenu("Edit")
        self.act_undo = edit_menu.addAction("Undo")
        self.act_undo.setShortcut(QKeySequence.StandardKey.Undo)
        self.act_undo.triggered.connect(self._undo)
        self.act_redo = edit_menu.addAction("Redo")
        self.act_redo.setShortcut(QKeySequence.StandardKey.Redo)
        self.act_redo.triggered.connect(self._redo)
        edit_menu.addSeparator()
        self.act_rename = edit_menu.addAction("Rename Track…", self._edit_rename)
        self.act_transpose = edit_menu.addAction("Transpose Track…", self._edit_transpose)
        self.act_instrument = edit_menu.addAction("Change Instrument…", self._edit_instrument)
        edit_menu.addSeparator()
        self.act_merge = edit_menu.addAction("Merge Playing Tracks", self._edit_merge)
        self.act_merge.setToolTip(
            "Combine the tracks with audio enabled (🔊) into one (needs two or more)"
        )
        self.act_delete = edit_menu.addAction("Delete Track", self._edit_delete)

    def _track_context_menu(self, pos) -> None:
        item = self.track_list.itemAt(pos)
        if item is None:
            return
        self.track_list.setCurrentItem(item)  # right-clicked track = edit target
        menu = QMenu(self)
        menu.addAction("Rename…", self._edit_rename)
        menu.addAction("Transpose…", self._edit_transpose)
        menu.addAction("Change Instrument…", self._edit_instrument)
        menu.addSeparator()
        merge = menu.addAction("Merge Playing Tracks", self._edit_merge)
        merge.setEnabled(self.act_merge.isEnabled())
        delete = menu.addAction("Delete Track", self._edit_delete)
        delete.setEnabled(self.act_delete.isEnabled())
        menu.exec(self.track_list.mapToGlobal(pos))

    # -- Editing: operations ----------------------------------------------
    def _edit_rename(self) -> None:
        index = self.selected_track_index()
        if index is None:
            return
        current = self.midi_file.tracks[index].name or ""
        name, ok = QInputDialog.getText(self, "Rename Track", "Track name:", text=current)
        if ok:
            self._apply_track_edit(index, lambda m: edits.rename_track(m, index, name))

    def _edit_transpose(self) -> None:
        index = self.selected_track_index()
        if index is None:
            return
        semitones, ok = QInputDialog.getInt(
            self, "Transpose Track", "Semitones (+/-):", 0, -48, 48
        )
        if ok and semitones != 0:
            self._apply_track_edit(index, lambda m: edits.transpose_track(m, index, semitones))

    def _edit_instrument(self) -> None:
        index = self.selected_track_index()
        if index is None:
            return
        names = [f"{i}: {n}" for i, n in enumerate(edits.GM_INSTRUMENTS)]
        current = edits.track_program(self.midi_file.tracks[index])
        choice, ok = QInputDialog.getItem(
            self, "Change Instrument", "Instrument:", names, current, editable=False
        )
        if ok:
            program = int(choice.split(":", 1)[0])
            self._apply_track_edit(index, lambda m: edits.set_track_program(m, index, program))

    def _edit_delete(self) -> None:
        index = self.selected_track_index()
        if index is None or len(self.midi_file.tracks) <= 1:
            return
        name = self.midi_file.tracks[index].name or f"Track {index + 1}"
        if (
            QMessageBox.question(self, "Delete Track", f"Delete '{name}'?")
            == QMessageBox.StandardButton.Yes
        ):
            # Keep the surviving show/play states (indices above the deleted one
            # shift down by one).
            remap = lambda s: {(i - 1 if i > index else i) for i in s if i != index}
            self._pending_show_indices = remap(set(self.show_track_indices())) or None
            self._pending_play_indices = remap(set(self.play_track_indices())) or None
            self._apply_file_edit(lambda m: edits.delete_track(m, index))

    def _edit_merge(self) -> None:
        indices = self.play_track_indices()  # merge the tracks with audio on
        if self.midi_file is None or len(indices) < 2:
            return
        # The merged track lands at the earliest position; select it, play it,
        # and show it if any of the merged tracks were shown.
        target = min(indices)
        self._pending_select_row = target
        self._pending_play_indices = {target}
        self._pending_show_indices = self._remap_after_merge(
            set(self.show_track_indices()), indices, target
        )
        self._apply_file_edit(lambda m: edits.merge_tracks(m, indices))
        self.statusBar().showMessage(f"Merged {len(indices)} tracks into one")

    @staticmethod
    def _remap_after_merge(state: set, merged, target: int) -> set:
        """Remap a set of track indices across a merge of `merged` into a single
        track at `target` (= min(merged)). Surviving indices shift down past the
        removed tracks; `target` is included if any merged track was in the
        set."""
        merged_set = set(merged)
        out = set()
        collapsed = False
        for i in state:
            if i in merged_set:
                collapsed = True
            elif i < target:
                out.add(i)
            else:
                out.add(i - sum(1 for m in merged if m < i) + 1)
        if collapsed:
            out.add(target)
        return out

    def _apply_track_edit(self, index: int, mutator) -> None:
        """Edit affecting only one track: snapshot just that track (fast) then
        apply. Used by note edits, transpose, rename, instrument, etc."""
        if self.midi_file is None:
            return
        snap = {"kind": "track", "index": index,
                "data": copy.deepcopy(self.midi_file.tracks[index])}
        self._push_undo(snap)
        self._commit_change(lambda: mutator(self.midi_file))

    def _apply_file_edit(self, mutator) -> None:
        """Edit changing the track structure (add/delete track): snapshot the
        whole file."""
        if self.midi_file is None:
            return
        self._push_undo({"kind": "file", "data": copy.deepcopy(self.midi_file)})
        self._commit_change(lambda: mutator(self.midi_file))

    def _push_undo(self, snapshot) -> None:
        self._undo_stack.append(snapshot)
        if len(self._undo_stack) > 50:
            self._undo_stack.pop(0)
        self._redo_stack.clear()

    def _restore_snapshot(self, snapshot):
        """Apply a snapshot; return the inverse snapshot (what it replaced)."""
        if snapshot["kind"] == "track":
            i = snapshot["index"]
            inverse = {"kind": "track", "index": i,
                       "data": copy.deepcopy(self.midi_file.tracks[i])}
            self.midi_file.tracks[i] = snapshot["data"]
            return inverse
        inverse = {"kind": "file", "data": copy.deepcopy(self.midi_file)}
        self.midi_file = snapshot["data"]
        return inverse

    def _undo(self) -> None:
        if not self._undo_stack:
            return
        self._commit_change(
            lambda: self._redo_stack.append(
                self._restore_snapshot(self._undo_stack.pop())
            )
        )

    def _redo(self) -> None:
        if not self._redo_stack:
            return
        self._commit_change(
            lambda: self._undo_stack.append(
                self._restore_snapshot(self._redo_stack.pop())
            )
        )

    def _commit_change(self, change_fn) -> None:
        """Apply a model change while preserving the playhead and play state,
        so editing doesn't reset the song to the start."""
        position = self.engine.position()
        was_playing = self.engine.state() == PlayerState.PLAYING
        self.engine.stop()
        change_fn()
        self._dirty = True
        self._after_change()          # rebuilds track list + reloads engine track
        self.engine.seek(position)    # restore the playhead
        if was_playing:
            self.engine.play()

    def _after_change(self) -> None:
        """Refresh the views after a model change. Updates the track list in
        place when the track count is unchanged (note edits, rename, etc.) to
        avoid the clear/rebuild + double selection-cascade; only structural
        changes (add/delete track) do a full repopulate. Reloads the engine
        track and piano view exactly once."""
        self.track_infos = smf.track_infos(self.midi_file)
        if self.track_list.count() == len(self.track_infos):
            # In-place update (note edits, rename, transpose…): keep the ticks.
            self.track_list.blockSignals(True)
            for i, info in enumerate(self.track_infos):
                item = self.track_list.item(i)
                item.setText(info.label())
                item.setData(Qt.ItemDataRole.UserRole, info.index)
                item.setForeground(
                    TRACK_COLORS[info.index % len(TRACK_COLORS)]
                    if info.has_notes else EMPTY_TRACK_COLOR
                )
            self.track_list.blockSignals(False)
        else:
            # Structural change (add/delete/merge): rebuild, restoring the
            # show/play states and selection the edit handler asked for.
            row = self._pending_select_row
            if row is None:
                row = self.track_list.currentRow()
            row = max(0, min(row, len(self.track_infos) - 1))
            self._populate_tracks(
                select_row=row,
                show_indices=self._pending_show_indices,
                play_indices=self._pending_play_indices,
            )
        self._pending_select_row = None
        self._pending_show_indices = None
        self._pending_play_indices = None

        self._prepared_key = None
        self._sync_all(preserve_playhead=False)  # _commit_change restores it
        self._update_title()
        self._update_edit_actions()

    # -- Editing: save ----------------------------------------------------
    def _save(self) -> None:
        if self._save_path is None:
            self._save_as()  # never silently overwrite the opened file
        else:
            self._write_file(self._save_path)

    def _save_as(self) -> None:
        if self.midi_file is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save MIDI File", self._suggest_save_name(), "MIDI files (*.mid)"
        )
        if not path:
            return
        if not path.lower().endswith(".mid"):
            path += ".mid"
        if self._write_file(path):
            self._save_path = path

    def _write_file(self, path: str) -> bool:
        try:
            self.midi_file.save(path)
        except Exception as exc:
            QMessageBox.warning(self, "Save failed", str(exc))
            return False
        self._dirty = False
        self._update_title()
        self.statusBar().showMessage(f"Saved: {path}")
        return True

    def _suggest_save_name(self) -> str:
        if self.file_info is None:
            return "untitled.mid"
        stem = self.file_info.name.rsplit(".", 1)[0]
        return f"{stem}-edited.mid"

    # -- Editing: UI state ------------------------------------------------
    def _update_title(self) -> None:
        base = "MIDI Track Player"
        if self.file_info is not None:
            mark = "*" if self._dirty else ""
            base = f"{mark}{self.file_info.name} — {base}"
        self.setWindowTitle(base)

    def _update_edit_actions(self) -> None:
        has_file = self.midi_file is not None
        has_track = self.selected_track_index() is not None
        for action in (self.act_rename, self.act_transpose, self.act_instrument):
            action.setEnabled(has_track)
        self.act_delete.setEnabled(
            has_track and has_file and len(self.midi_file.tracks) > 1
        )
        self.act_merge.setEnabled(has_file and len(self.play_track_indices()) >= 2)
        self.act_save.setEnabled(has_file)
        self.act_save_as.setEnabled(has_file)
        self.act_undo.setEnabled(bool(self._undo_stack))
        self.act_redo.setEnabled(bool(self._redo_stack))

    def closeEvent(self, event) -> None:
        """Offer to save unsaved edits, then release playback resources."""
        if self._dirty:
            choice = QMessageBox.question(
                self,
                "Unsaved changes",
                "Save changes before closing?",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
            )
            if choice == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return
            if choice == QMessageBox.StandardButton.Save:
                self._save()
                if self._dirty:  # save was cancelled
                    event.ignore()
                    return
        self.engine.stop()
        self._close_port()
        super().closeEvent(event)
