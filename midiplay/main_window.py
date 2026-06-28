"""Main application window.

Lays out the UI (file selector, track list, output dropdown, transport,
progress bar) and connects it to the playback engine. A QTimer polls the
engine to drive the progress bar, time labels, and button states.
"""

import copy

from PySide6.QtCore import Qt, QSettings, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
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
    QVBoxLayout,
    QWidget,
)

from midiplay import devices, edits, smf
from midiplay.engine import PlaybackEngine, PlayerState
from midiplay.piano_view import TRACK_COLORS, PianoView

# Note-less tracks (e.g. a conductor/tempo track) are shown greyed out.
EMPTY_TRACK_COLOR = QColor(0x80, 0x80, 0x80)


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


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("MIDI Track Player")
        self.resize(560, 520)

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
        self._piano_window = None     # the falling-notes window, once opened

        # Editing state: snapshot-based undo/redo + unsaved-changes tracking.
        self._undo_stack: list = []
        self._redo_stack: list = []
        self._dirty = False
        self._save_path = None        # set once the user has chosen a Save target

        self._build_menus()

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        layout.addLayout(self._build_top())
        layout.addWidget(_hline())
        layout.addLayout(self._build_middle())
        layout.addWidget(_hline())
        layout.addLayout(self._build_bottom())

        self.setCentralWidget(root)
        self.statusBar().showMessage("No file loaded")

        self._refresh_devices()
        self._restore_last_device()
        self._restore_output_channel()

        # Poll the engine to drive the progress bar, time labels, and which
        # transport buttons are enabled.
        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(50)  # ms
        self._ui_timer.timeout.connect(self._update_ui)
        self._ui_timer.start()
        self._update_ui()

        self._update_title()
        self._update_edit_actions()

        # Spacebar toggles play/pause (buttons are set NoFocus so they don't
        # swallow it). The Piano View forwards its own Space via a signal.
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

        col.addWidget(QLabel("Tracks"))
        self.track_list = QListWidget()
        self.track_list.setEnabled(False)  # enabled once a file is loaded
        self.track_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.track_list.currentRowChanged.connect(self._on_track_changed)
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

        self.piano_button = QPushButton("Piano View")
        self.piano_button.clicked.connect(self._open_piano_view)
        self.all_tracks_check = QCheckBox("All tracks")
        self.all_tracks_check.toggled.connect(self._refresh_piano_view)
        self.loop_check = QCheckBox("Loop")
        self.loop_check.toggled.connect(self.engine.set_loop)
        self.mute_check = QCheckBox("Mute")
        self.mute_check.toggled.connect(self.engine.set_muted)
        row.addWidget(self.piano_button)
        row.addWidget(self.all_tracks_check)
        row.addWidget(self.loop_check)
        row.addWidget(self.mute_check)
        row.addStretch(1)

        self.speed_slider = QSlider(Qt.Orientation.Horizontal)
        self.speed_slider.setRange(50, 150)   # percent
        self.speed_slider.setValue(100)
        self.speed_slider.setSingleStep(5)
        self.speed_slider.setPageStep(10)
        self.speed_slider.setFixedWidth(140)
        self.speed_slider.valueChanged.connect(self._on_speed_changed)
        self.speed_label = QLabel("100%")
        row.addWidget(QLabel("Speed:"))
        row.addWidget(self.speed_slider)
        row.addWidget(self.speed_label)
        return row

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
        self._update_title()
        self._update_edit_actions()

    def _populate_tracks(self, select_row: int | None = None) -> None:
        """Fill the track list from the loaded file. Selects `select_row`
        (clamped) if given, else the first track that contains notes."""
        self.track_infos = smf.track_infos(self.midi_file)

        self.track_list.clear()
        for info in self.track_infos:
            item = QListWidgetItem(info.label())
            item.setData(Qt.ItemDataRole.UserRole, info.index)
            # Color the title to match the All-tracks view; grey out empties.
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

    def selected_track_index(self) -> int | None:
        """0-based index of the currently selected track, or None."""
        item = self.track_list.currentItem()
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole)

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

    def _on_track_changed(self, _row: int = -1) -> None:
        """User picked a different track: stop and load it into the engine so
        the total time shows and seeking works before the first Play."""
        self.engine.stop()
        track = self.selected_track_index()
        if self.midi_file is not None and track is not None:
            self.engine.set_track(self.midi_file, track)
            self._prepared_key = (id(self.midi_file), track)
        self._refresh_piano_view()
        self._update_edit_actions()

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
        track = self.selected_track_index()
        if track is None:
            self.statusBar().showMessage("Select a track to play")
            return False
        if not self._ensure_port():
            return False

        key = (id(self.midi_file), track)
        if self._prepared_key != key:
            self.engine.set_track(self.midi_file, track)
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

    def _on_speed_changed(self, percent: int) -> None:
        self.speed_label.setText(f"{percent}%")
        self.engine.set_speed(percent / 100.0)

    # -- Seeking ----------------------------------------------------------
    def _on_seek_started(self) -> None:
        self._seeking = True
        if self._piano_window is not None:
            self._piano_window.preview_begin()

    def _on_seek_moved(self, value: int) -> None:
        # Live time readout while scrubbing (audio jumps only on release).
        duration = self.engine.duration()
        seconds = value / 1000 * duration
        self.current_time_label.setText(format_time(seconds))
        if self._piano_window is not None:
            self._piano_window.preview_position(seconds)  # live falling-notes update

    def _on_seek_finished(self, value: int) -> None:
        self._seeking = False
        duration = self.engine.duration()
        if duration > 0:
            self.engine.seek(value / 1000 * duration)
        if self._piano_window is not None:
            self._piano_window.preview_end()

    # -- Piano (falling-notes) view --------------------------------------
    def _open_piano_view(self) -> None:
        if self._piano_window is None:
            self._piano_window = PianoView(self.engine)
            self._piano_window.notes_delete_requested.connect(self._delete_notes)
            self._piano_window.notes_edit_requested.connect(self._edit_notes)
            self._piano_window.note_add_requested.connect(self._add_note)
            self._piano_window.play_pause_requested.connect(self._toggle_play_pause)
        self._refresh_piano_view()
        self._piano_window.show()
        self._piano_window.raise_()
        self._piano_window.activateWindow()

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
        """Push the right content into the piano view based on the All-tracks
        toggle. Called when the track, file, or toggle changes."""
        if self._piano_window is None:
            return
        if self.midi_file is not None:
            self._piano_window.set_time_map(smf.TimeMap(self.midi_file))
        if self.all_tracks_check.isChecked() and self.midi_file is not None:
            per_track = smf.extract_all_notes(self.midi_file)
            self._piano_window.set_multi_notes(
                per_track, self.engine.duration(), self.selected_track_index()
            )
        else:
            self._piano_window.refresh_notes()

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
            and self.selected_track_index() is not None
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
        self.act_delete = edit_menu.addAction("Delete Track", self._edit_delete)

    def _track_context_menu(self, pos) -> None:
        item = self.track_list.itemAt(pos)
        if item is None:
            return
        self.track_list.setCurrentItem(item)
        menu = QMenu(self)
        menu.addAction("Rename…", self._edit_rename)
        menu.addAction("Transpose…", self._edit_transpose)
        menu.addAction("Change Instrument…", self._edit_instrument)
        menu.addSeparator()
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
            self._apply_file_edit(lambda m: edits.delete_track(m, index))

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
        self.track_list.blockSignals(True)
        if self.track_list.count() == len(self.track_infos):
            for i, info in enumerate(self.track_infos):
                item = self.track_list.item(i)
                item.setText(info.label())
                item.setData(Qt.ItemDataRole.UserRole, info.index)
                item.setForeground(
                    TRACK_COLORS[info.index % len(TRACK_COLORS)]
                    if info.has_notes else EMPTY_TRACK_COLOR
                )
        else:
            row = max(0, min(self.track_list.currentRow(), len(self.track_infos) - 1))
            self._populate_tracks(select_row=row)
        self.track_list.blockSignals(False)

        track = self.selected_track_index()
        self._prepared_key = None
        if track is not None and self.midi_file is not None:
            self.engine.set_track(self.midi_file, track)
            self._prepared_key = (id(self.midi_file), track)
        self._refresh_piano_view()
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
        if self._piano_window is not None:
            self._piano_window.close()
        self.engine.stop()
        self._close_port()
        super().closeEvent(event)
