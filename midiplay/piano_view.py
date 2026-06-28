"""Synthesia-style falling-notes view.

A custom-painted widget: a piano keyboard along the bottom and notes falling
toward it. Notes reach the keyboard (the "hit line") exactly when they sound.
Only the visible time window is drawn each frame. The view can show a single
track or all tracks color-coded (with the played track emphasized).
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, QPointF, QRectF, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QVBoxLayout, QWidget

from midiplay.smf import Note

# MIDI note classes that are white keys (C D E F G A B).
WHITE_CLASSES = {0, 2, 4, 5, 7, 9, 11}
NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")

# Look-ahead (seconds of future notes visible) bounds and frame rate.
LOOKAHEAD_MIN = 1.0
LOOKAHEAD_MAX = 10.0
FRAME_MS = 16   # ~60 fps
EDGE_PX = 6     # how close to a note's edge counts as a resize grab


def note_name(pitch: int) -> str:
    """MIDI pitch -> name with octave, e.g. 60 -> 'C4'."""
    return f"{NOTE_NAMES[pitch % 12]}{pitch // 12 - 1}"

# Colors
BG_COLOR = QColor(0x1E, 0x1E, 0x24)
HIT_LINE_COLOR = QColor(0xFF, 0xFF, 0xFF, 0xB0)
HIT_GLOW = QColor(0x7E, 0xB0, 0xFF)
GUIDE_LINE = QColor(0xFF, 0xFF, 0xFF, 0x12)
NOTE_WHITE = QColor(0x4C, 0x8B, 0xF5)   # note that lands on a white key
NOTE_BLACK = QColor(0x2E, 0x5C, 0xC8)   # note that lands on a black key
NOTE_ACTIVE = QColor(0xA8, 0xCB, 0xFF)  # note currently crossing the hit line
NOTE_BORDER = QColor(0x10, 0x20, 0x40)
NOTE_TEXT = QColor(0x0C, 0x18, 0x2E)    # label drawn on a falling note
SELECTION_COLOR = QColor(0xFF, 0xFF, 0xFF)  # outline on selected notes
LEGEND_BG = QColor(0x12, 0x12, 0x18, 0xCC)
LEGEND_BORDER = QColor(0xFF, 0xFF, 0xFF, 0x33)
LEGEND_KEY = QColor(0x9F, 0xC4, 0xFF)
LEGEND_TEXT = QColor(0xCC, 0xCC, 0xCC)

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
WHITE_KEY = QColor(0xF4, 0xF4, 0xF4)
BLACK_KEY = QColor(0x20, 0x20, 0x20)
ACTIVE_KEY = QColor(0x6F, 0xA8, 0xFF)   # key whose note is sounding
KEY_BORDER = QColor(0x55, 0x55, 0x55)
LABEL_COLOR = QColor(0x70, 0x70, 0x70)

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
    """A note prepared for drawing: the note plus its base color and whether
    it belongs to a non-emphasized track (drawn dimmer)."""

    note: Note
    color: QColor
    dim: bool


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
    notesEditRequested = Signal(object)    # {id: (start_tick, end_tick, pitch)}
    noteAddRequested = Signal(object)      # (pitch, start_tick)
    playPauseRequested = Signal()

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
        self._show_legend = True  # toggled with 'H'

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
            _VNote(n, NOTE_WHITE if _is_white(n.pitch) else NOTE_BLACK, dim=False)
            for n in notes
        ]
        self._duration = duration
        self._selected.clear()
        self._overlay.clear()
        self._drag = None
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
                self._vnotes.append(_VNote(n, color, dim))
                pitches.append(n.pitch)
        self._duration = duration
        self._selected.clear()
        self._overlay.clear()
        self._drag = None
        self._set_range(pitches)
        self.update()

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
        if event.button() == Qt.MouseButton.LeftButton:
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
        """Select a note (Ctrl toggles), and arm a move/resize drag."""
        pos = event.position()
        self.setFocus()
        if pos.y() > self.height() - self._keyboard_height():
            return  # on the keyboard, not the note area
        ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        vnote = self._hit_test(pos)
        if vnote is None:
            if not ctrl:
                self._selected.clear()
            self.update()
            return
        note_id = vnote.note.id
        if ctrl:
            self._selected ^= {note_id}
            self.update()
            return
        if note_id not in self._selected:
            self._selected = {note_id}
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
        notes_bottom = self.height() - self._keyboard_height()
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
        notes_bottom = self.height() - self._keyboard_height()
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
        if no_snap:
            return max(0, int(round(tick)))
        grid = self._grid_ticks()
        return max(0, int(round(tick / grid)) * grid)

    def _grid_ticks(self) -> int:
        tpb = self._time_map.ticks_per_beat if self._time_map else 480
        return max(1, tpb // 4)  # 1/16-note grid

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
                changes[nid] = new  # (start_tick, end_tick, pitch)
        self._drag = None
        if changes:
            self.notesEditRequested.emit(changes)
        else:
            self._overlay.clear()
            self.update()

    def mouseDoubleClickEvent(self, event) -> None:
        """Double-click empty grid to add a note (snapped, on the played track)."""
        pos = event.position()
        in_notes = pos.y() <= self.height() - self._keyboard_height()
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._time_map is not None
            and in_notes
            and self._hit_test(pos) is None
        ):
            pitch = self._pitch_at_x(pos.x())
            no_snap = bool(event.modifiers() & Qt.KeyboardModifier.AltModifier)
            start_tick = self._start_tick_at_y(pos.y(), no_snap)
            self.noteAddRequested.emit((pitch, start_tick))
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def _start_tick_at_y(self, y: float, no_snap: bool) -> int:
        notes_bottom = self.height() - self._keyboard_height()
        seconds = self._position + (notes_bottom - y) / notes_bottom * self._look_ahead
        return self._snap_tick(self._time_map.seconds_to_ticks(seconds), no_snap)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self._selected:
            self.notesDeleteRequested.emit(set(self._selected))
            event.accept()
        elif event.key() == Qt.Key.Key_H:
            self._show_legend = not self._show_legend
            self.update()
            event.accept()
        elif event.key() == Qt.Key.Key_Space:
            self.playPauseRequested.emit()
            event.accept()
        else:
            super().keyPressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._zooming:
            # Drag up = zoom in (less look-ahead); drag down = zoom out.
            dy = event.position().y() - self._zoom_start_y
            self.set_look_ahead(self._zoom_start_look * (1.0 + dy / 250.0))
            event.accept()
        elif self._drag is not None:
            self._update_drag(event)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.MiddleButton and self._zooming:
            self._zooming = False
            self.unsetCursor()
            event.accept()
        elif event.button() == Qt.MouseButton.LeftButton and self._drag is not None:
            self._finish_drag()
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    # -- Geometry ---------------------------------------------------------
    def _keyboard_height(self) -> int:
        return min(110, max(60, int(self.height() * 0.18)))

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
        notes_bottom = height - kb_height  # the hit line / top of the keyboard

        painter.fillRect(0, 0, width, height, BG_COLOR)

        active = {
            v.note.pitch
            for v in self._vnotes
            if v.note.start <= self._position < v.note.end
        }
        geo, _white_w = self._key_geometry(width)
        self._draw_guides(painter, geo, notes_bottom)
        self._draw_notes(painter, geo, notes_bottom, active)
        self._draw_hit_line(painter, width, notes_bottom)
        self._draw_keyboard(painter, geo, notes_bottom, kb_height, active)
        self._draw_legend(painter, width)

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

        painter.setPen(QPen(LEGEND_BORDER, 1))
        painter.setBrush(LEGEND_BG)
        painter.drawRoundedRect(QRectF(x, y, panel_w, panel_h), 6, 6)

        painter.setFont(self._legend_font)
        text_y = y + pad + metrics.ascent()
        for key, desc in LEGEND:
            painter.setPen(QPen(LEGEND_KEY))
            painter.drawText(QPointF(x + pad, text_y), key)
            painter.setPen(QPen(LEGEND_TEXT))
            painter.drawText(QPointF(x + pad + key_w + gap, text_y), desc)
            text_y += line_h

    def _draw_guides(self, painter, geo, notes_bottom) -> None:
        """Faint vertical line at each C, for pitch reference."""
        painter.setPen(QPen(GUIDE_LINE, 1))
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
            painter.setPen(QPen(NOTE_BORDER, 1))
            painter.setBrush(self._note_color(vnote, is_active, velocity))
            painter.drawRoundedRect(rect, 3, 3)
            if note.id in self._selected:
                painter.setPen(QPen(SELECTION_COLOR, 2))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(rect, 3, 3)
            # Note name (or the live velocity while Shift-dragging).
            if not vnote.dim and rect.height() >= 14 and rect.width() >= 20:
                if vel_drag and note.id in self._overlay:
                    self._draw_note_label(painter, rect, f"v{velocity}")
                else:
                    self._draw_note_label(painter, rect, note_name(pitch))

    def _draw_note_label(self, painter, rect, text) -> None:
        """Bold note name with a white outline, centered in the note."""
        metrics = QFontMetricsF(self._label_font)
        x = rect.center().x() - metrics.horizontalAdvance(text) / 2
        baseline = rect.center().y() - metrics.height() / 2 + metrics.ascent()
        path = QPainterPath()
        path.addText(QPointF(x, baseline), self._label_font, text)
        painter.setPen(QPen(SELECTION_COLOR, 2.0))  # white outline
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        painter.fillPath(path, NOTE_TEXT)            # dark fill on top

    def _hit_test(self, pos):
        """The editable (non-dim) vnote under a point, or None."""
        notes_bottom = self.height() - self._keyboard_height()
        geo, _ = self._key_geometry(self.width())
        for vnote in reversed(self._vnotes):  # topmost first
            if vnote.dim:
                continue
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
        top_color = QColor(HIT_GLOW)
        top_color.setAlpha(0)
        bottom_color = QColor(HIT_GLOW)
        bottom_color.setAlpha(70)
        glow.setColorAt(0.0, top_color)
        glow.setColorAt(1.0, bottom_color)
        painter.fillRect(QRectF(0, notes_bottom - 18, width, 18), glow)

        painter.setPen(QPen(HIT_LINE_COLOR, 2))
        painter.drawLine(0, int(notes_bottom), width, int(notes_bottom))

    def _draw_keyboard(self, painter, geo, notes_bottom, kb_height, active) -> None:
        # White keys first (full height), then black keys on top (shorter).
        painter.setPen(QPen(KEY_BORDER, 1))
        for pitch, (x, w, is_black) in geo.items():
            if not is_black:
                painter.setBrush(ACTIVE_KEY if pitch in active else WHITE_KEY)
                painter.drawRect(QRectF(x, notes_bottom, w, kb_height))
        self._draw_c_labels(painter, geo, notes_bottom, kb_height)
        for pitch, (x, w, is_black) in geo.items():
            if is_black:
                painter.setBrush(ACTIVE_KEY if pitch in active else BLACK_KEY)
                painter.drawRect(QRectF(x, notes_bottom, w, kb_height * 0.62))

    def _draw_c_labels(self, painter, geo, notes_bottom, kb_height) -> None:
        painter.setPen(QPen(LABEL_COLOR))
        for pitch, (x, w, is_black) in geo.items():
            if pitch % 12 == 0:  # C — label with its octave (MIDI: 60 = C4)
                rect = QRectF(x, notes_bottom + kb_height - 16, w, 14)
                painter.drawText(
                    rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                    f"C{pitch // 12 - 1}",
                )


class PianoView(QWidget):
    """Standalone window: a PianoRollView animated from the playback engine.

    It simply polls the engine's position each frame, so it follows Play,
    Pause, Stop, Restart, Seek, and Speed with no extra wiring."""

    notes_delete_requested = Signal(object)
    notes_edit_requested = Signal(object)
    note_add_requested = Signal(object)
    play_pause_requested = Signal()

    def __init__(self, engine) -> None:
        super().__init__()
        self.setWindowTitle("Piano View")
        self.resize(820, 480)
        self._engine = engine

        # The full window is the piano roll; zoom via the scroll wheel.
        self._roll = PianoRollView()
        self._roll.notesDeleteRequested.connect(self.notes_delete_requested)
        self._roll.notesEditRequested.connect(self.notes_edit_requested)
        self._roll.noteAddRequested.connect(self.note_add_requested)
        self._roll.playPauseRequested.connect(self.play_pause_requested)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._roll)

        # Middle-drag in the view scrubs the song; seek the engine on release.
        self._roll.scrubFinished.connect(self._engine.seek)

        self._timer = QTimer(self)
        self._timer.setInterval(FRAME_MS)
        self._timer.timeout.connect(self._tick)

        self.refresh_notes()

    def refresh_notes(self) -> None:
        """Single-track view: reload notes from the engine."""
        self._roll.set_track_notes(self._engine.notes(), self._engine.duration())

    def set_multi_notes(self, per_track, duration, primary_index) -> None:
        """All-tracks view: show every track color-coded."""
        self._roll.set_multi_notes(per_track, duration, primary_index)

    def set_time_map(self, time_map) -> None:
        self._roll.set_time_map(time_map)

    # Live preview driven by the main window's seek slider.
    def preview_begin(self) -> None:
        self._roll.begin_scrub()

    def preview_position(self, seconds: float) -> None:
        self._roll.set_scrub_position(seconds)

    def preview_end(self) -> None:
        self._roll.end_scrub()

    def _tick(self) -> None:
        self._roll.set_position(self._engine.position())

    # Animate only while visible.
    def showEvent(self, event) -> None:
        self._timer.start()
        super().showEvent(event)

    def hideEvent(self, event) -> None:
        self._timer.stop()
        super().hideEvent(event)
