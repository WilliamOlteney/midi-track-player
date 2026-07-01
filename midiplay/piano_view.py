"""Synthesia-style falling-notes view.

A custom-painted widget: a piano keyboard along the bottom and notes falling
toward it. Notes reach the keyboard (the "hit line") exactly when they sound.
Only the visible time window is drawn each frame. The view can show a single
track or all tracks color-coded (with the played track emphasized).
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from PySide6.QtCore import Qt, QPointF, QRectF, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QWidget

from midiplay.smf import Note
from midiplay.theme import DEFAULT_THEME

# MIDI note classes that are white keys (C D E F G A B).
WHITE_CLASSES = {0, 2, 4, 5, 7, 9, 11}
NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")

# Look-ahead (seconds of future notes visible) bounds.
LOOKAHEAD_MIN = 1.0
LOOKAHEAD_MAX = 10.0
EDGE_PX = 6     # how close to a note's edge counts as a resize grab


def note_name(pitch: int) -> str:
    """MIDI pitch -> name with octave, e.g. 60 -> 'C4'."""
    return f"{NOTE_NAMES[pitch % 12]}{pitch // 12 - 1}"

# Per-scheme colours (background, keyboard, notes, legend, …) live in
# midiplay.theme.Theme; the view reads them from self._theme.

# Control reference shown in the top-right legend: (gesture, action).
LEGEND = (
    ("Click", "select"),
    ("Ctrl-click", "add to selection"),
    ("Drag", "move note"),
    ("Edge-drag", "resize"),
    ("Shift-drag", "velocity"),
    ("Alt", "disable snap"),
    ("Double-click", "add note"),
    ("Delete", "remove selected"),
    ("Space", "play / pause"),
    ("Wheel", "scrub"),
    ("Middle-drag", "zoom"),
    ("H", "hide / show this"),
)

# Distinct colors for the all-tracks view (cycled if there are more tracks).
TRACK_COLORS = (
    QColor(0x4C, 0x8B, 0xF5),  # blue
    QColor(0x4C, 0xC0, 0x6A),  # green
    QColor(0xF5, 0x9E, 0x42),  # orange
    QColor(0xE0, 0x5A, 0x8A),  # pink
    QColor(0xB0, 0x7B, 0xE0),  # purple
    QColor(0x42, 0xC7, 0xC7),  # teal
    QColor(0xD9, 0xC2, 0x4A),  # yellow
    QColor(0xC9, 0x6A, 0x4A),  # rust
)


@dataclass
class _VNote:
    """A note prepared for drawing: the note plus its base color, whether it
    belongs to a non-emphasized track (drawn dimmer / not the edit target), and
    which track it came from."""

    note: Note
    color: QColor
    dim: bool
    track_index: int = -1


def _is_white(pitch: int) -> bool:
    return pitch % 12 in WHITE_CLASSES


def _snap_white_down(pitch: int) -> int:
    while pitch > 0 and not _is_white(pitch):
        pitch -= 1
    return pitch


def _snap_white_up(pitch: int) -> int:
    while pitch < 127 and not _is_white(pitch):
        pitch += 1
    return pitch


class PianoRollView(QWidget):
    lookAheadChanged = Signal(float)
    scrubMoved = Signal(float)       # live song position while scrubbing
    scrubFinished = Signal(float)    # final song position on release
    notesDeleteRequested = Signal(object)  # set of note ids to delete
    notesEditRequested = Signal(object)    # {id: (start_tick, end_tick, pitch, velocity)}
    noteAddRequested = Signal(object)      # (pitch, start_tick)
    notesAddRequested = Signal(object)     # [(pitch, start_tick, length, velocity), …]
    playPauseRequested = Signal()
    legendToggled = Signal(bool)           # legend shown/hidden (e.g. via 'H')
    selectionChanged = Signal()            # the set of selected notes changed
    trackRetargetRequested = Signal(int)   # make this track the edit target

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumSize(480, 320)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)  # receive key events
        self._selected: set = set()  # selected note ids (channel, pitch, start_tick)
        self._time_map = None        # smf.TimeMap, for snapping during edits
        self._drag = None            # active move/resize gesture, or None
        self._overlay: dict = {}     # id -> (start_tick, end_tick, pitch) preview
        self._vnotes: list[_VNote] = []
        self._duration = 0.0
        self._position = 0.0
        self._look_ahead = 3.0          # seconds of future notes visible
        self._lo = 60                   # lowest pitch shown (set from notes)
        self._hi = 72                   # highest pitch shown
        self._label_font = QFont()
        self._label_font.setPointSize(8)
        self._label_font.setBold(True)
        self._legend_font = QFont()
        self._legend_font.setPointSize(8)
        self._show_legend = True   # toggled with 'H' or the settings drawer
        self._labels_visible = True  # note-name labels on the falling notes
        self._theme = DEFAULT_THEME  # colour scheme (set via set_theme)

        # Editing: grid / snap / clipboard / marquee selection.
        self._grid_denom = 16          # note value for the grid: 4, 8, 16, 32
        self._grid_mod = "straight"    # "straight" | "triplet" | "dotted"
        self._snap = True              # snap edits to the grid (Alt inverts)
        self._quant_strength = 1.0     # 0..1 pull toward the grid on Quantize
        self._clipboard: list = []     # [(pitch, rel_start_tick, length, velocity)]
        self._pending_selection = None  # selection to restore after an edit reload
        self._marquee = None           # (start, current) QPointF while box-selecting
        self._press_pos = None         # left-press point (to tell a click from a drag)

        # Scale/key lock: constrain pitch edits to a scale.
        self._scale_lock = False
        self._scale_root = 0                       # 0 = C … 11 = B
        self._scale_mask = set(range(12))          # allowed pitch classes (from root)
        # Draw mode: drag on empty grid to create a note of the dragged length.
        self._draw_mode = False
        self._draw = None                          # active draw gesture, or None
        # Velocity lane: a strip above the keyboard with draggable velocity bars.
        self._vel_lane = False
        self._vel_dragging = False
        self._vel_drag = None

        # Scrubbing: while active, the engine's position is ignored and the
        # view follows the scrub position instead.
        self._scrubbing = False
        self._scrub_start_y = 0.0
        self._scrub_start_pos = 0.0

        # Middle-drag zoom state.
        self._zooming = False
        self._zoom_start_y = 0.0
        self._zoom_start_look = 3.0

    # -- Public API -------------------------------------------------------
    def set_time_map(self, time_map) -> None:
        self._time_map = time_map

    def look_ahead(self) -> float:
        return self._look_ahead

    def set_look_ahead(self, seconds: float) -> None:
        seconds = max(LOOKAHEAD_MIN, min(LOOKAHEAD_MAX, seconds))
        if abs(seconds - self._look_ahead) < 1e-6:
            return
        self._look_ahead = seconds
        self.lookAheadChanged.emit(seconds)
        self.update()

    def labels_visible(self) -> bool:
        return self._labels_visible

    def set_labels_visible(self, visible: bool) -> None:
        self._labels_visible = bool(visible)
        self.update()

    def legend_visible(self) -> bool:
        return self._show_legend

    def set_legend_visible(self, visible: bool) -> None:
        self._show_legend = bool(visible)
        self.update()

    def set_theme(self, theme) -> None:
        self._theme = theme
        self.update()

    def set_grid(self, denom: int, mod: str = "straight") -> None:
        """Set the edit grid to a 1/`denom` note ('straight', 'triplet', or
        'dotted')."""
        self._grid_denom = int(denom)
        self._grid_mod = mod
        self.update()

    def set_snap(self, on: bool) -> None:
        self._snap = bool(on)

    def snap_enabled(self) -> bool:
        return self._snap

    def set_quantize_strength(self, strength: float) -> None:
        self._quant_strength = max(0.0, min(1.0, float(strength)))

    def set_scale(self, enabled: bool, root: int = 0, mask=None) -> None:
        """Constrain pitch edits to a scale: `root` is 0..11 (C..B) and `mask`
        the allowed pitch classes relative to the root (None = chromatic)."""
        self._scale_lock = bool(enabled)
        self._scale_root = int(root) % 12
        self._scale_mask = set(mask) if mask else set(range(12))
        self.update()

    def set_draw_mode(self, on: bool) -> None:
        self._draw_mode = bool(on)

    def set_velocity_lane(self, on: bool) -> None:
        self._vel_lane = bool(on)
        self.update()

    def _scale_allows(self, pitch: int) -> bool:
        return not self._scale_lock or ((pitch - self._scale_root) % 12) in self._scale_mask

    def _scale_snap_pitch(self, pitch: int) -> int:
        """Nearest allowed pitch under the scale lock (unchanged if lock off)."""
        if self._scale_allows(pitch):
            return pitch
        for d in range(1, 12):
            for cand in (pitch - d, pitch + d):
                if 0 <= cand <= 127 and self._scale_allows(cand):
                    return cand
        return pitch

    def _scale_step(self, pitch: int, direction: int) -> int:
        """Next allowed pitch above/below (for arrow moves under scale lock)."""
        step = 1 if direction > 0 else -1
        p = pitch + step
        while 0 <= p <= 127:
            if self._scale_allows(p):
                return p
            p += step
        return pitch

    def _set_selection(self, ids) -> None:
        """Replace the selection and notify listeners (e.g. the inspector)."""
        new = set(ids)
        if new != self._selected:
            self._selected = new
            self.selectionChanged.emit()
            self.update()

    def selected_notes(self) -> list[dict]:
        """Details of the currently-selected (editable) notes, for the inspector."""
        out = []
        for v in self._vnotes:
            if not v.dim and v.note.id in self._selected:
                n = v.note
                out.append({
                    "id": n.id, "pitch": n.pitch, "velocity": n.velocity,
                    "start_tick": n.start_tick, "end_tick": n.end_tick,
                })
        return out

    def wheelEvent(self, event) -> None:
        # Scroll wheel scrubs the song position (seek on each notch).
        step = self._look_ahead * 0.12
        if event.angleDelta().y() > 0:
            new_pos = self._position - step  # wheel up = backward
        else:
            new_pos = self._position + step  # wheel down = forward
        new_pos = max(0.0, min(new_pos, self._duration))
        self._position = new_pos          # immediate visual feedback
        self.scrubFinished.emit(new_pos)  # seek the engine
        self.update()
        event.accept()

    def set_track_notes(self, notes: list[Note], duration: float) -> None:
        """Single-track view: notes colored by white/black key."""
        self._vnotes = [
            _VNote(
                n,
                self._theme.note_white if _is_white(n.pitch) else self._theme.note_black,
                dim=False,
            )
            for n in notes
        ]
        self._duration = duration
        self._overlay.clear()
        self._drag = None
        self._apply_pending_selection()
        self._set_range([n.pitch for n in notes])
        self.update()

    def set_multi_notes(
        self,
        per_track: list[tuple[int, list[Note]]],
        duration: float,
        primary_index: int | None,
    ) -> None:
        """All-tracks view: each track gets a palette color; the primary
        (played) track is emphasized and the others are dimmed."""
        self._vnotes = []
        pitches: list[int] = []
        for track_index, notes in per_track:
            color = TRACK_COLORS[track_index % len(TRACK_COLORS)]
            dim = primary_index is not None and track_index != primary_index
            for n in notes:
                self._vnotes.append(_VNote(n, color, dim, track_index))
                pitches.append(n.pitch)
        self._duration = duration
        self._overlay.clear()
        self._drag = None
        self._apply_pending_selection()
        self._set_range(pitches)
        self.update()

    def _apply_pending_selection(self) -> None:
        """After notes are (re)loaded, restore a pending selection (set by an
        edit command so it survives the reload) or clear it."""
        valid = {v.note.id for v in self._vnotes if not v.dim}
        self._selected = (self._pending_selection or set()) & valid
        self._pending_selection = None
        self.selectionChanged.emit()

    def _set_range(self, pitches: list[int]) -> None:
        """Choose the visible keyboard range from the notes' pitches."""
        if pitches:
            lo, hi = min(pitches), max(pitches)
            # Pad slightly, then snap edges to white keys for a clean keyboard.
            self._lo = max(0, _snap_white_down(lo - 2))
            self._hi = min(127, _snap_white_up(hi + 2))
            while self._hi - self._lo < 11:  # keep at least ~an octave wide
                if self._hi < 127:
                    self._hi = _snap_white_up(self._hi + 1)
                if self._lo > 0:
                    self._lo = _snap_white_down(self._lo - 1)
        else:
            self._lo, self._hi = 60, 72

    def set_position(self, seconds: float) -> None:
        if self._scrubbing:
            return  # scrubbing overrides the engine's position
        if abs(seconds - self._position) < 1e-9:
            return  # nothing moved (e.g. paused) -> skip the repaint
        self._position = seconds
        self.update()

    # -- Scrubbing (middle-drag here, or driven by the main seek slider) ---
    def begin_scrub(self) -> None:
        self._scrubbing = True

    def set_scrub_position(self, seconds: float) -> None:
        self._position = max(0.0, min(seconds, self._duration))
        self.update()

    def end_scrub(self) -> None:
        self._scrubbing = False

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._in_vel_lane(event.position()):
            self._begin_vel_lane(event.position())
            event.accept()
        elif event.button() == Qt.MouseButton.LeftButton:
            self._left_press(event)
            event.accept()
        elif event.button() == Qt.MouseButton.MiddleButton:
            # Middle-drag now zooms the look-ahead (time window).
            self._zooming = True
            self._zoom_start_y = event.position().y()
            self._zoom_start_look = self._look_ahead
            self.setCursor(Qt.CursorShape.SizeVerCursor)
            event.accept()
        else:
            super().mousePressEvent(event)

    def _left_press(self, event) -> None:
        """Select a note (Ctrl toggles), arm a move/resize drag, or start a
        marquee (drag-box) selection on empty grid."""
        pos = event.position()
        self.setFocus()
        if pos.y() > self._notes_bottom():
            return  # on the keyboard, not the note area
        ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        vnote = self._hit_test(pos)
        if vnote is None:
            other = self._hit_test_any(pos)
            if other is not None and other.dim:
                # Clicked a note on another shown track: make it the edit target
                # and select the clicked note once it reloads emphasized.
                self._pending_selection = {other.note.id}
                self.trackRetargetRequested.emit(other.track_index)
                return
            if self._draw_mode and self._time_map is not None:
                self._begin_draw(pos, event)
                return
            # Begin a marquee; base is the current selection when Ctrl-adding.
            self._marquee = {
                "start": pos, "cur": pos,
                "base": set(self._selected) if ctrl else set(),
            }
            self.update()
            return
        note_id = vnote.note.id
        if ctrl:
            self._set_selection(self._selected ^ {note_id})
            return
        if note_id not in self._selected:
            self._set_selection({note_id})
        if self._time_map is not None:
            shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            mode = "velocity" if shift else self._edge_mode(vnote, pos)
            targets = self._selected if mode in ("move", "velocity") else {note_id}
            orig = {
                v.note.id: (v.note.start_tick, v.note.end_tick, v.note.pitch,
                            v.note.velocity)
                for v in self._vnotes
                if v.note.id in targets
            }
            self._drag = {"mode": mode, "start": pos, "orig": orig,
                          "anchor_pitch": vnote.note.pitch}
            self.setCursor(
                Qt.CursorShape.SizeAllCursor if mode == "move"
                else Qt.CursorShape.SizeVerCursor
            )
        self.update()

    def _edge_mode(self, vnote, pos) -> str:
        geo, _ = self._key_geometry(self.width())
        notes_bottom = self._notes_bottom()
        rect = self._note_rect(vnote.note, geo, notes_bottom)
        if rect is None or rect.height() < 2 * EDGE_PX + 4:
            return "move"
        if abs(pos.y() - rect.top()) <= EDGE_PX:
            return "resize_end"     # top edge controls the end time
        if abs(pos.y() - rect.bottom()) <= EDGE_PX:
            return "resize_start"   # bottom edge controls the start time
        return "move"

    def _update_drag(self, event) -> None:
        pos = event.position()
        notes_bottom = self._notes_bottom()
        if notes_bottom <= 0:
            return
        dy = pos.y() - self._drag["start"].y()
        d_seconds = -dy * self._look_ahead / notes_bottom  # drag down = earlier
        no_snap = bool(event.modifiers() & Qt.KeyboardModifier.AltModifier)
        mode = self._drag["mode"]
        grid = self._grid_ticks()
        overlay = {}
        for nid, (s_tick, e_tick, pitch, vel) in self._drag["orig"].items():
            if mode == "velocity":
                new_vel = max(1, min(127, vel + int(round(-dy * 0.5))))
                overlay[nid] = (s_tick, e_tick, pitch, new_vel)
            elif mode == "move":
                length = e_tick - s_tick
                new_start = self._shift(s_tick, d_seconds, no_snap)
                d_pitch = self._pitch_at_x(pos.x()) - self._drag["anchor_pitch"]
                new_pitch = max(self._lo, min(self._hi, pitch + d_pitch))
                new_pitch = self._scale_snap_pitch(new_pitch)  # obey scale lock
                overlay[nid] = (new_start, new_start + length, new_pitch, vel)
            elif mode == "resize_start":
                new_start = min(self._shift(s_tick, d_seconds, no_snap), e_tick - grid)
                overlay[nid] = (max(0, new_start), e_tick, pitch, vel)
            else:  # resize_end
                new_end = max(self._shift(e_tick, d_seconds, no_snap), s_tick + grid)
                overlay[nid] = (s_tick, new_end, pitch, vel)
        self._overlay = overlay
        self.update()

    def _shift(self, tick: int, d_seconds: float, no_snap: bool) -> int:
        """Shift a tick position by a time delta, snapped to the grid."""
        seconds = self._time_map.tick_to_seconds(tick) + d_seconds
        return self._snap_tick(self._time_map.seconds_to_ticks(seconds), no_snap)

    def _snap_tick(self, tick: float, no_snap: bool) -> int:
        # Alt (no_snap) inverts the persistent snap setting per-gesture.
        if no_snap or not self._snap:
            return max(0, int(round(tick)))
        grid = self._grid_ticks()
        return max(0, int(round(tick / grid)) * grid)

    def _grid_ticks(self) -> int:
        tpb = self._time_map.ticks_per_beat if self._time_map else 480
        # A 1/denom note is tpb*4/denom ticks (a beat = a 1/4 note = tpb).
        ticks = tpb * 4 / max(1, self._grid_denom)
        if self._grid_mod == "triplet":
            ticks *= 2 / 3
        elif self._grid_mod == "dotted":
            ticks *= 3 / 2
        return max(1, int(round(ticks)))

    def _pitch_at_x(self, x: float) -> int:
        geo, _ = self._key_geometry(self.width())
        for pitch, (gx, gw, is_black) in geo.items():
            if is_black and gx <= x <= gx + gw:
                return pitch
        for pitch, (gx, gw, is_black) in geo.items():
            if not is_black and gx <= x <= gx + gw:
                return pitch
        return self._lo if x < self.width() / 2 else self._hi

    def _finish_drag(self) -> None:
        self.unsetCursor()
        changes = {}
        for nid, new in self._overlay.items():
            old = self._drag["orig"].get(nid)
            if old is not None and tuple(new) != tuple(old):
                changes[nid] = new  # (start_tick, end_tick, pitch, velocity)
        self._drag = None
        if changes:
            self._emit_changes(changes)
        else:
            self._overlay.clear()
            self.update()

    def _emit_changes(self, changes: dict) -> None:
        """Emit a note edit and remember where each edited note moves to, so the
        selection follows it after the track is rebuilt and reloaded."""
        pending = set()
        for sid in self._selected:
            if sid in changes:
                new_start, _new_end, new_pitch, _vel = changes[sid]
                pending.add((sid[0], new_pitch, new_start))  # (channel, pitch, start)
            else:
                pending.add(sid)
        self._pending_selection = pending
        self.notesEditRequested.emit(changes)

    def mouseDoubleClickEvent(self, event) -> None:
        """Double-click empty grid to add a note (snapped, on the played track)."""
        pos = event.position()
        in_notes = pos.y() <= self._notes_bottom()
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._time_map is not None
            and in_notes
            and self._hit_test(pos) is None
        ):
            pitch = self._scale_snap_pitch(self._pitch_at_x(pos.x()))
            no_snap = bool(event.modifiers() & Qt.KeyboardModifier.AltModifier)
            start_tick = self._start_tick_at_y(pos.y(), no_snap)
            self.noteAddRequested.emit((pitch, start_tick))
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def _start_tick_at_y(self, y: float, no_snap: bool) -> int:
        notes_bottom = self._notes_bottom()
        seconds = self._position + (notes_bottom - y) / notes_bottom * self._look_ahead
        return self._snap_tick(self._time_map.seconds_to_ticks(seconds), no_snap)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        mods = event.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)

        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self._selected:
            self.notesDeleteRequested.emit(set(self._selected))
        elif ctrl and key == Qt.Key.Key_A:
            self.select_all()
        elif key == Qt.Key.Key_Escape:
            self.clear_selection()
        elif ctrl and key == Qt.Key.Key_C:
            self.copy_selection()
        elif ctrl and key == Qt.Key.Key_X:
            self.cut_selection()
        elif ctrl and key == Qt.Key.Key_V:
            self.paste_clipboard()
        elif ctrl and key == Qt.Key.Key_D:
            self.duplicate_selection()
        elif key == Qt.Key.Key_Q:
            self.quantize_selection()
        elif key == Qt.Key.Key_L and self._selected:
            self.legato_selection()
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Right) and self._selected:
            step = self._grid_ticks() * (1 if key == Qt.Key.Key_Right else -1)
            self.nudge_selection(step, 0)
        elif key in (Qt.Key.Key_Up, Qt.Key.Key_Down) and self._selected:
            semis = (12 if shift else 1) * (1 if key == Qt.Key.Key_Up else -1)
            self.nudge_selection(0, semis)
        elif key in (Qt.Key.Key_BracketLeft, Qt.Key.Key_Comma) and self._selected:
            self.scale_length_selection(0.5)
        elif key in (Qt.Key.Key_BracketRight, Qt.Key.Key_Period) and self._selected:
            self.scale_length_selection(2.0)
        elif key == Qt.Key.Key_H:
            self._show_legend = not self._show_legend
            self.legendToggled.emit(self._show_legend)
            self.update()
        elif key == Qt.Key.Key_Space:
            self.playPauseRequested.emit()
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._zooming:
            # Drag up = zoom in (less look-ahead); drag down = zoom out.
            dy = event.position().y() - self._zoom_start_y
            self.set_look_ahead(self._zoom_start_look * (1.0 + dy / 250.0))
            event.accept()
        elif self._vel_dragging:
            self._vel_lane_paint(event.position())
            event.accept()
        elif self._draw is not None:
            self._update_draw(event)
            event.accept()
        elif self._drag is not None:
            self._update_drag(event)
            event.accept()
        elif self._marquee is not None:
            self._marquee["cur"] = event.position()
            self.update()
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.MiddleButton and self._zooming:
            self._zooming = False
            self.unsetCursor()
            event.accept()
        elif event.button() == Qt.MouseButton.LeftButton and self._vel_dragging:
            self._finish_vel_lane()
            event.accept()
        elif event.button() == Qt.MouseButton.LeftButton and self._draw is not None:
            self._finish_draw()
            event.accept()
        elif event.button() == Qt.MouseButton.LeftButton and self._drag is not None:
            self._finish_drag()
            event.accept()
        elif event.button() == Qt.MouseButton.LeftButton and self._marquee is not None:
            self._finish_marquee()
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    # -- Drag-to-draw a note ---------------------------------------------
    def _begin_draw(self, pos, event) -> None:
        no_snap = bool(event.modifiers() & Qt.KeyboardModifier.AltModifier)
        pitch = self._scale_snap_pitch(self._pitch_at_x(pos.x()))
        anchor = self._start_tick_at_y(pos.y(), no_snap)
        grid = self._grid_ticks()
        self._draw = {"pitch": pitch, "anchor": anchor, "lo": anchor, "hi": anchor + grid}
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.update()

    def _update_draw(self, event) -> None:
        no_snap = bool(event.modifiers() & Qt.KeyboardModifier.AltModifier)
        cur = self._start_tick_at_y(event.position().y(), no_snap)
        anchor = self._draw["anchor"]
        grid = self._grid_ticks()
        lo, hi = min(anchor, cur), max(anchor, cur)
        if hi - lo < grid:
            hi = lo + grid
        self._draw["lo"], self._draw["hi"] = lo, hi
        self.update()

    def _finish_draw(self) -> None:
        d = self._draw
        self._draw = None
        self.unsetCursor()
        length = d["hi"] - d["lo"]
        if length <= 0:
            self.update()
            return
        channel = self._primary_channel()
        if channel is not None:
            self._pending_selection = {(channel, d["pitch"], d["lo"])}
        self.notesAddRequested.emit([(d["pitch"], d["lo"], length, 90)])

    # -- Velocity lane ----------------------------------------------------
    def _in_vel_lane(self, pos) -> bool:
        if not self._vel_lane:
            return False
        top = self._notes_bottom()
        return top < pos.y() <= top + self._lane_height()

    def _vel_lane_notes(self) -> list[Note]:
        """Notes shown in the lane: the selection if any, else the visible
        editable notes — in time order."""
        sel = self._selected_editable()
        if sel:
            return sorted(sel, key=lambda n: n.start_tick)
        window = self._position + self._look_ahead
        visible = [
            v.note for v in self._vnotes
            if not v.dim and v.note.end > self._position and v.note.start < window
        ]
        return sorted(visible, key=lambda n: n.start_tick)

    def _begin_vel_lane(self, pos) -> None:
        notes = self._vel_lane_notes()
        if not notes:
            return
        self._vel_drag = {"notes": notes, "overlay": {}}
        self._vel_dragging = True
        self._vel_lane_paint(pos)

    def _vel_lane_paint(self, pos) -> None:
        notes = self._vel_drag["notes"]
        count = len(notes)
        if count == 0:
            return
        idx = max(0, min(count - 1, int(pos.x() / max(1, self.width()) * count)))
        top = self._notes_bottom()
        lane_h = self._lane_height()
        frac = max(0.0, min(1.0, (top + lane_h - pos.y()) / max(1, lane_h)))
        velocity = max(1, min(127, int(round(1 + frac * 126))))
        self._vel_drag["overlay"][notes[idx].id] = velocity
        self.update()

    def _finish_vel_lane(self) -> None:
        drag = self._vel_drag
        self._vel_drag = None
        self._vel_dragging = False
        by_id = {n.id: n for n in drag["notes"]}
        changes = {}
        for nid, vel in drag["overlay"].items():
            n = by_id.get(nid)
            if n is not None and vel != n.velocity:
                changes[nid] = (n.start_tick, n.end_tick, n.pitch, vel)
        if changes:
            self._emit_changes(changes)
        else:
            self.update()

    def _finish_marquee(self) -> None:
        m = self._marquee
        self._marquee = None
        rect = QRectF(m["start"], m["cur"]).normalized()
        if rect.width() < 3 and rect.height() < 3:
            # A click, not a drag: clear the selection (base is empty unless Ctrl).
            self._set_selection(m["base"])
            return
        enclosed = {v.note.id for v in self._marquee_hits(rect)}
        self._set_selection(m["base"] | enclosed)

    def _marquee_hits(self, rect: QRectF):
        """Editable vnotes whose on-screen rect intersects the marquee rect."""
        notes_bottom = self._notes_bottom()
        geo, _ = self._key_geometry(self.width())
        hits = []
        for vnote in self._vnotes:
            if vnote.dim:
                continue
            r = self._note_rect(vnote.note, geo, notes_bottom)
            if r is not None and rect.intersects(r):
                hits.append(vnote)
        return hits

    # -- Editing commands (selection is preserved across the reload) ------
    def _editable_notes(self) -> list[Note]:
        return [v.note for v in self._vnotes if not v.dim]

    def _selected_editable(self) -> list[Note]:
        return [n for n in self._editable_notes() if n.id in self._selected]

    def _primary_channel(self):
        for v in self._vnotes:
            if not v.dim:
                return v.note.channel
        return None

    def select_all(self) -> None:
        self._set_selection({n.id for n in self._editable_notes()})

    def clear_selection(self) -> None:
        self._set_selection(set())

    def quantize_selection(self) -> None:
        """Pull selected note starts toward the grid by the strength setting
        (length preserved)."""
        grid = self._grid_ticks()
        strength = self._quant_strength
        changes = {}
        for n in self._selected_editable():
            target = int(round(n.start_tick / grid)) * grid
            new_start = max(0, int(round(n.start_tick + (target - n.start_tick) * strength)))
            length = n.end_tick - n.start_tick
            changes[n.id] = (new_start, new_start + length, n.pitch, n.velocity)
        if changes:
            self._emit_changes(changes)

    def nudge_selection(self, d_tick: int, d_pitch: int) -> None:
        changes = {}
        for n in self._selected_editable():
            new_start = max(0, n.start_tick + d_tick)
            length = n.end_tick - n.start_tick
            if d_pitch and self._scale_lock and abs(d_pitch) == 1:
                # A semitone arrow under scale lock steps to the next scale note.
                new_pitch = self._scale_step(n.pitch, d_pitch)
            else:
                new_pitch = self._scale_snap_pitch(max(0, min(127, n.pitch + d_pitch)))
            changes[n.id] = (new_start, new_start + length, new_pitch, n.velocity)
        if changes:
            self._emit_changes(changes)

    def scale_length_selection(self, factor: float) -> None:
        grid = self._grid_ticks()
        changes = {}
        for n in self._selected_editable():
            length = max(grid, int(round((n.end_tick - n.start_tick) * factor)))
            changes[n.id] = (n.start_tick, n.start_tick + length, n.pitch, n.velocity)
        if changes:
            self._emit_changes(changes)

    def legato_selection(self) -> None:
        """Extend each selected note to the start of the next note in time."""
        starts = sorted({n.start_tick for n in self._editable_notes()})
        changes = {}
        for n in self._selected_editable():
            later = [s for s in starts if s > n.start_tick]
            if later and later[0] > n.start_tick:
                changes[n.id] = (n.start_tick, later[0], n.pitch, n.velocity)
        if changes:
            self._emit_changes(changes)

    def set_velocity_selection(self, velocity: int) -> None:
        velocity = max(1, min(127, int(velocity)))
        changes = {}
        for n in self._selected_editable():
            changes[n.id] = (n.start_tick, n.end_tick, n.pitch, velocity)
        if changes:
            self._emit_changes(changes)

    def ramp_velocity_selection(self, v0: int, v1: int) -> None:
        notes = sorted(self._selected_editable(), key=lambda n: n.start_tick)
        if not notes:
            return
        changes = {}
        span = max(1, len(notes) - 1)
        for i, n in enumerate(notes):
            v = int(round(v0 + (v1 - v0) * i / span))
            changes[n.id] = (n.start_tick, n.end_tick, n.pitch, max(1, min(127, v)))
        self._emit_changes(changes)

    def humanize_selection(self, time_ticks: int = 15, vel_amount: int = 12) -> None:
        changes = {}
        for n in self._selected_editable():
            dt = random.randint(-time_ticks, time_ticks)
            dv = random.randint(-vel_amount, vel_amount)
            new_start = max(0, n.start_tick + dt)
            length = n.end_tick - n.start_tick
            vel = max(1, min(127, n.velocity + dv))
            changes[n.id] = (new_start, new_start + length, n.pitch, vel)
        if changes:
            self._emit_changes(changes)

    # -- Clipboard --------------------------------------------------------
    def copy_selection(self) -> None:
        notes = self._selected_editable()
        if not notes:
            return
        base = min(n.start_tick for n in notes)
        self._clipboard = [
            (n.pitch, n.start_tick - base, n.end_tick - n.start_tick, n.velocity)
            for n in notes
        ]

    def cut_selection(self) -> None:
        if not self._selected:
            return
        self.copy_selection()
        self.notesDeleteRequested.emit(set(self._selected))

    def paste_clipboard(self) -> None:
        if not self._clipboard or self._time_map is None:
            return
        base = self._snap_tick(
            self._time_map.seconds_to_ticks(self._position), no_snap=False
        )
        self._add_notes_at(base)

    def duplicate_selection(self) -> None:
        notes = self._selected_editable()
        if not notes:
            return
        min_s = min(n.start_tick for n in notes)
        max_e = max(n.end_tick for n in notes)
        span = max_e - min_s
        payload = [
            (n.pitch, n.start_tick - min_s, n.end_tick - n.start_tick, n.velocity)
            for n in notes
        ]
        self._add_notes_at(min_s + span, payload)

    def _add_notes_at(self, base_tick: int, payload=None) -> None:
        """Add notes (clipboard or `payload` of (pitch, rel_start, len, vel)) at
        base_tick, then select the new copies once they reload."""
        payload = self._clipboard if payload is None else payload
        notes = [(p, base_tick + rs, ln, vel) for (p, rs, ln, vel) in payload]
        channel = self._primary_channel()
        if channel is not None:
            self._pending_selection = {
                (channel, p, base_tick + rs) for (p, rs, _ln, _vel) in payload
            }
        self.notesAddRequested.emit(notes)

    # -- Geometry ---------------------------------------------------------
    def _keyboard_height(self) -> int:
        return min(110, max(60, int(self.height() * 0.18)))

    def _lane_height(self) -> int:
        """Height of the velocity lane (0 when it's off)."""
        return 56 if self._vel_lane else 0

    def _notes_bottom(self) -> int:
        """Y of the hit line — bottom of the falling-notes area (above the
        velocity lane and keyboard)."""
        return self.height() - self._keyboard_height() - self._lane_height()

    def _key_geometry(self, width: float) -> tuple[dict[int, tuple[float, float, bool]], float]:
        """Map each pitch in range to (x, key_width, is_black)."""
        white_pitches = [p for p in range(self._lo, self._hi + 1) if _is_white(p)]
        num_white = max(1, len(white_pitches))
        white_w = width / num_white
        white_index = {p: i for i, p in enumerate(white_pitches)}

        geo: dict[int, tuple[float, float, bool]] = {}
        for pitch, i in white_index.items():
            geo[pitch] = (i * white_w, white_w, False)

        black_w = white_w * 0.62
        for pitch in range(self._lo, self._hi + 1):
            if not _is_white(pitch):
                left_white = pitch - 1  # the white key just below any black key
                if left_white in white_index:
                    center = (white_index[left_white] + 1) * white_w
                    geo[pitch] = (center - black_w / 2, black_w, True)
        return geo, white_w

    def _time_to_y(self, t: float, notes_bottom: float) -> float:
        """Time -> y. The playhead time maps to the hit line (notes_bottom);
        future times map upward toward the top of the widget."""
        return notes_bottom - ((t - self._position) / self._look_ahead) * notes_bottom

    # -- Painting ---------------------------------------------------------
    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        width = self.width()
        height = self.height()
        kb_height = self._keyboard_height()
        notes_bottom = self._notes_bottom()  # hit line (above the velocity lane)

        painter.fillRect(0, 0, width, height, self._theme.bg)

        active = {
            v.note.pitch
            for v in self._vnotes
            if v.note.start <= self._position < v.note.end
        }
        geo, _white_w = self._key_geometry(width)
        self._draw_grid(painter, width, notes_bottom)
        self._draw_guides(painter, geo, notes_bottom)
        self._draw_notes(painter, geo, notes_bottom, active)
        self._draw_note_preview(painter, geo, notes_bottom)
        self._draw_marquee(painter)
        self._draw_hit_line(painter, width, notes_bottom)
        self._draw_vel_lane(painter, width, notes_bottom)
        keyboard_top = notes_bottom + self._lane_height()  # below the velocity lane
        self._draw_keyboard(painter, geo, keyboard_top, kb_height, active)
        self._draw_legend(painter, width)

    def _draw_note_preview(self, painter, geo, notes_bottom) -> None:
        """The translucent note being drawn (drag-to-draw)."""
        if self._draw is None or self._time_map is None:
            return
        start = self._time_map.tick_to_seconds(self._draw["lo"])
        end = self._time_map.tick_to_seconds(self._draw["hi"])
        rect = self._rect_for(start, end, self._draw["pitch"], geo, notes_bottom)
        if rect is None:
            return
        fill = QColor(self._theme.hit_glow)
        fill.setAlpha(150)
        painter.setPen(QPen(self._theme.selection, 1))
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, 3, 3)

    def _draw_vel_lane(self, painter, width, notes_bottom) -> None:
        if not self._vel_lane:
            return
        lane_h = self._lane_height()
        lane = QRectF(0, notes_bottom, width, lane_h)
        # A shade clearly distinct from the piano background either way.
        bg = self._theme.bg.lighter(150) if self._theme.dark else self._theme.bg.darker(112)
        painter.fillRect(lane, bg)
        painter.setPen(QPen(self._theme.key_border, 1))
        painter.drawLine(0, int(notes_bottom), width, int(notes_bottom))

        notes = self._vel_lane_notes()
        count = len(notes)
        if count == 0:
            return
        overlay = self._vel_drag["overlay"] if self._vel_drag else {}
        color = next((v.color for v in self._vnotes if not v.dim), self._theme.hit_glow)
        slot = width / count
        for i, n in enumerate(notes):
            vel = overlay.get(n.id, n.velocity)
            h = (vel / 127.0) * (lane_h - 6)
            x = i * slot + slot * 0.15
            w = max(2.0, slot * 0.7)
            bar = QRectF(x, notes_bottom + lane_h - h - 3, w, h)
            c = QColor(color)
            if n.id in self._selected:
                c = c.lighter(140)
            painter.fillRect(bar, c)

    def _draw_grid(self, painter, width, notes_bottom) -> None:
        """Horizontal beat/bar lines (and grid subdivisions when not too dense)
        across the falling-notes area. Assumes 4/4."""
        if self._time_map is None or notes_bottom <= 0:
            return
        grid = self._grid_ticks()
        tpb = self._time_map.ticks_per_beat
        beat, bar = tpb, tpb * 4
        end_sec = self._position + self._look_ahead
        start_tick = self._time_map.seconds_to_ticks(self._position)
        tick = max(0, int(start_tick // grid) * grid)
        # Pixels between adjacent grid lines — skip sub-lines if they'd be dense.
        sub_px = notes_bottom * (self._time_map.tick_to_seconds(tick + grid)
                                 - self._time_map.tick_to_seconds(tick)) / self._look_ahead
        g = self._theme.guide
        sub_c = QColor(g.red(), g.green(), g.blue(), min(255, g.alpha()))
        beat_c = QColor(g.red(), g.green(), g.blue(), min(255, g.alpha() * 2 + 12))
        bar_c = QColor(g.red(), g.green(), g.blue(), min(255, g.alpha() * 4 + 30))
        guard = 0
        while guard < 20000:
            guard += 1
            sec = self._time_map.tick_to_seconds(tick)
            if sec > end_sec:
                break
            y = self._time_to_y(sec, notes_bottom)
            if 0 <= y <= notes_bottom:
                on_bar = tick % bar == 0
                on_beat = tick % beat == 0
                if on_bar or on_beat or sub_px >= 7:
                    painter.setPen(QPen(bar_c if on_bar else beat_c if on_beat else sub_c, 1))
                    painter.drawLine(0, int(y), width, int(y))
                    if on_bar:
                        painter.setPen(QPen(self._theme.key_label))
                        painter.drawText(QPointF(3, y - 2), str(tick // bar + 1))
            tick += grid

    def _draw_marquee(self, painter) -> None:
        if self._marquee is None:
            return
        rect = QRectF(self._marquee["start"], self._marquee["cur"]).normalized()
        fill = QColor(self._theme.selection)
        fill.setAlpha(40)
        painter.setPen(QPen(self._theme.selection, 1, Qt.PenStyle.DashLine))
        painter.setBrush(fill)
        painter.drawRect(rect)

    def _draw_legend(self, painter, width) -> None:
        if not self._show_legend:
            return
        metrics = QFontMetricsF(self._legend_font)
        pad, gap = 8.0, 12.0
        line_h = metrics.height() + 2
        key_w = max(metrics.horizontalAdvance(k) for k, _ in LEGEND)
        desc_w = max(metrics.horizontalAdvance(d) for _, d in LEGEND)
        panel_w = pad * 2 + key_w + gap + desc_w
        panel_h = pad * 2 + line_h * len(LEGEND)
        x = width - panel_w - 10
        y = 10.0

        painter.setPen(QPen(self._theme.legend_border, 1))
        painter.setBrush(self._theme.legend_bg)
        painter.drawRoundedRect(QRectF(x, y, panel_w, panel_h), 6, 6)

        painter.setFont(self._legend_font)
        text_y = y + pad + metrics.ascent()
        for key, desc in LEGEND:
            painter.setPen(QPen(self._theme.legend_key))
            painter.drawText(QPointF(x + pad, text_y), key)
            painter.setPen(QPen(self._theme.legend_text))
            painter.drawText(QPointF(x + pad + key_w + gap, text_y), desc)
            text_y += line_h

    def _draw_guides(self, painter, geo, notes_bottom) -> None:
        """Faint vertical line at each C, for pitch reference."""
        painter.setPen(QPen(self._theme.guide, 1))
        for pitch, (x, _w, _is_black) in geo.items():
            if pitch % 12 == 0:  # C
                painter.drawLine(int(x), 0, int(x), int(notes_bottom))

    def _rect_for(self, start, end, pitch, geo, notes_bottom):
        """On-screen rect for a (start_sec, end_sec, pitch), or None if hidden."""
        if end <= self._position or start >= self._position + self._look_ahead:
            return None
        rect_geo = geo.get(pitch)
        if rect_geo is None:
            return None
        x, w, _is_black = rect_geo
        top = max(0.0, self._time_to_y(end, notes_bottom))
        bottom = min(notes_bottom, self._time_to_y(start, notes_bottom))
        if bottom - top < 1:
            bottom = top + 1
        return QRectF(x + 0.5, top, max(1.0, w - 1.0), bottom - top)

    def _note_rect(self, note, geo, notes_bottom):
        return self._rect_for(note.start, note.end, note.pitch, geo, notes_bottom)

    def _effective(self, note):
        """(start_sec, end_sec, pitch, velocity) honoring any live drag overlay."""
        override = self._overlay.get(note.id)
        if override is not None and self._time_map is not None:
            s_tick, e_tick, pitch, vel = override
            return (
                self._time_map.tick_to_seconds(s_tick),
                self._time_map.tick_to_seconds(e_tick),
                pitch,
                vel,
            )
        return (note.start, note.end, note.pitch, note.velocity)

    def _draw_notes(self, painter, geo, notes_bottom, _active) -> None:
        painter.setFont(self._label_font)
        vel_drag = self._drag is not None and self._drag.get("mode") == "velocity"
        for vnote in self._vnotes:
            note = vnote.note
            start, end, pitch, velocity = self._effective(note)
            rect = self._rect_for(start, end, pitch, geo, notes_bottom)
            if rect is None:
                continue
            is_active = start <= self._position < end
            painter.setPen(QPen(self._theme.note_border, 1))
            painter.setBrush(self._note_color(vnote, is_active, velocity))
            painter.drawRoundedRect(rect, 3, 3)
            if note.id in self._selected:
                painter.setPen(QPen(self._theme.selection, 2))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(rect, 3, 3)
            # Note name (or the live velocity while Shift-dragging).
            if not vnote.dim and rect.height() >= 14 and rect.width() >= 20:
                if vel_drag and note.id in self._overlay:
                    self._draw_note_label(painter, rect, f"v{velocity}")
                elif self._labels_visible:
                    self._draw_note_label(painter, rect, note_name(pitch))

    def _draw_note_label(self, painter, rect, text) -> None:
        """Bold note name with a contrasting outline, centered in the note."""
        metrics = QFontMetricsF(self._label_font)
        x = rect.center().x() - metrics.horizontalAdvance(text) / 2
        baseline = rect.center().y() - metrics.height() / 2 + metrics.ascent()
        path = QPainterPath()
        path.addText(QPointF(x, baseline), self._label_font, text)
        painter.setPen(QPen(self._theme.selection, 2.0))  # outline
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        painter.fillPath(path, self._theme.note_text)     # fill on top

    def _hit_test(self, pos):
        """The editable (non-dim) vnote under a point, or None."""
        notes_bottom = self._notes_bottom()
        geo, _ = self._key_geometry(self.width())
        for vnote in reversed(self._vnotes):  # topmost first
            if vnote.dim:
                continue
            rect = self._note_rect(vnote.note, geo, notes_bottom)
            if rect is not None and rect.contains(pos):
                return vnote
        return None

    def _hit_test_any(self, pos):
        """The topmost vnote under a point on any shown track (dim or not)."""
        notes_bottom = self._notes_bottom()
        geo, _ = self._key_geometry(self.width())
        for vnote in reversed(self._vnotes):
            rect = self._note_rect(vnote.note, geo, notes_bottom)
            if rect is not None and rect.contains(pos):
                return vnote
        return None

    @staticmethod
    def _note_color(vnote: _VNote, is_active: bool, velocity: int) -> QColor:
        base = vnote.color
        # Brightness scales with velocity across the full range: a soft note
        # approaches black, a loud one is the full track color.
        f = 0.10 + 0.90 * max(0, min(127, velocity)) / 127.0
        color = QColor(int(base.red() * f), int(base.green() * f), int(base.blue() * f))
        if is_active:
            color = color.lighter(150)
        color.setAlpha(110 if vnote.dim else 255)
        return color

    def _draw_hit_line(self, painter, width, notes_bottom) -> None:
        # Soft glow fading up into the falling area, then the line itself.
        glow = QLinearGradient(0, notes_bottom - 18, 0, notes_bottom)
        top_color = QColor(self._theme.hit_glow)
        top_color.setAlpha(0)
        bottom_color = QColor(self._theme.hit_glow)
        bottom_color.setAlpha(70)
        glow.setColorAt(0.0, top_color)
        glow.setColorAt(1.0, bottom_color)
        painter.fillRect(QRectF(0, notes_bottom - 18, width, 18), glow)

        painter.setPen(QPen(self._theme.hit_line, 2))
        painter.drawLine(0, int(notes_bottom), width, int(notes_bottom))

    def _draw_keyboard(self, painter, geo, notes_bottom, kb_height, active) -> None:
        # White keys first (full height), then black keys on top (shorter).
        theme = self._theme
        painter.setPen(QPen(theme.key_border, 1))
        for pitch, (x, w, is_black) in geo.items():
            if not is_black:
                painter.setBrush(theme.key_active if pitch in active else theme.key_white)
                painter.drawRect(QRectF(x, notes_bottom, w, kb_height))
        self._draw_c_labels(painter, geo, notes_bottom, kb_height)
        for pitch, (x, w, is_black) in geo.items():
            if is_black:
                painter.setBrush(theme.key_active if pitch in active else theme.key_black)
                painter.drawRect(QRectF(x, notes_bottom, w, kb_height * 0.62))

    def _draw_c_labels(self, painter, geo, notes_bottom, kb_height) -> None:
        painter.setPen(QPen(self._theme.key_label))
        for pitch, (x, w, is_black) in geo.items():
            if pitch % 12 == 0:  # C — label with its octave (MIDI: 60 = C4)
                rect = QRectF(x, notes_bottom + kb_height - 16, w, 14)
                painter.drawText(
                    rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                    f"C{pitch // 12 - 1}",
                )
