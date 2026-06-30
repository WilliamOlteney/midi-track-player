"""Playback engine.

Plays one or more tracks' pre-timed events (merged onto a shared time axis)
to a MIDI output port on a dedicated thread, scheduling each event against a
monotonic clock so timing does not drift. Supports play / pause / stop /
restart, playback speed, looping, and
a clean "panic" (note-offs + sustain reset + all-notes-off) so notes never
hang when playback is interrupted.

Threading model:
- One worker thread sends events while PLAYING.
- Control methods (called from the GUI thread) signal the worker via an
  Event and join it before mutating shared state.
- A re-entrant lock guards the small amount of shared state; the GUI polls
  position()/state() to drive the progress UI.
"""

from __future__ import annotations

import threading
import time
from enum import Enum, auto

import mido

from midiplay import smf


class PlayerState(Enum):
    STOPPED = auto()
    PAUSED = auto()
    PLAYING = auto()


class PlaybackEngine:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._port = None
        self._timeline = smf.TrackTimeline()
        self._state = PlayerState.STOPPED

        self._speed = 1.0
        self._loop = False
        self._muted = False
        self._out_channel = None  # remap output to this channel (0-15), or None

        # Timing anchors: position == _anchor_pos + (now - _anchor_wall) * speed
        # while PLAYING; frozen at _anchor_pos otherwise.
        self._anchor_pos = 0.0
        self._anchor_wall = 0.0

        self._next_index = 0          # next event the worker will send
        self._thread: threading.Thread | None = None
        self._stop_flag = threading.Event()
        self._active_notes: set[tuple[int, int]] = set()  # (channel, note)

    # -- Configuration ----------------------------------------------------
    def set_output(self, port) -> None:
        """Set the open MIDI output port. The engine does not own/close it."""
        with self._lock:
            self._port = port

    def set_track(self, midi: mido.MidiFile, track_index: int) -> None:
        """Prepare a single track for playback (stops any current playback)."""
        self.set_tracks(midi, [track_index])

    def set_tracks(self, midi: mido.MidiFile, track_indices) -> None:
        """Prepare one or more tracks to play simultaneously (stops any
        current playback). The tracks are merged onto a shared time axis and
        streamed through the one worker, so the whole transport — play, pause,
        seek, loop, speed, panic — works unchanged for multiple tracks."""
        self.stop()
        timeline = smf.build_multi_track_timeline(midi, track_indices)
        with self._lock:
            self._timeline = timeline
            self._next_index = 0
            self._anchor_pos = 0.0
            self._state = PlayerState.STOPPED

    def set_loop(self, loop: bool) -> None:
        with self._lock:
            self._loop = loop  # read by the worker at end-of-track

    def set_muted(self, muted: bool) -> None:
        """Mute/unmute note output. Playback keeps running and time keeps
        advancing; while muted, new note-ons are suppressed and currently
        held notes are released. Other messages (CC, program, pitch) still
        pass so the instrument stays correctly configured."""
        with self._lock:
            self._muted = bool(muted)
            now_muted = self._muted
        if now_muted:
            self._panic(full=False)

    def set_output_channel(self, channel) -> None:
        """Force the played track's output onto a single MIDI channel (0-15),
        or None to keep each message's recorded channel. Useful for external
        keyboards that only listen on one channel."""
        with self._lock:
            self._out_channel = channel
        self._panic(full=True)  # release notes held on the previous channel

    def set_speed(self, speed: float) -> None:
        """Set playback speed (1.0 = recorded tempo). Continuous if playing."""
        speed = max(0.1, float(speed))
        with self._lock:
            playing = self._state == PlayerState.PLAYING
            position = self._position_locked()
        if playing:
            self._stop_flag.set()
            self._join_worker()
        with self._lock:
            self._speed = speed
            self._anchor_pos = position
            if playing:
                self._spawn_worker_locked()

    # -- Transport --------------------------------------------------------
    def play(self) -> None:
        with self._lock:
            if self._state == PlayerState.PLAYING or not self._timeline.events:
                return
            self._spawn_worker_locked()  # resumes from _anchor_pos / _next_index

    def pause(self) -> None:
        with self._lock:
            if self._state != PlayerState.PLAYING:
                return
            frozen = self._position_locked()
        self._stop_flag.set()
        self._join_worker()
        with self._lock:
            self._anchor_pos = frozen
            self._state = PlayerState.PAUSED
        self._panic()

    def stop(self) -> None:
        self._stop_flag.set()
        self._join_worker()
        with self._lock:
            self._state = PlayerState.STOPPED
            self._anchor_pos = 0.0
            self._next_index = 0
        self._panic()

    def restart(self) -> None:
        self.stop()
        self.play()

    def seek(self, seconds: float) -> None:
        """Jump to a position in seconds, preserving the current state
        (playing stays playing, paused stays paused). Held notes are
        released and controller state (program/CC/pitch) is reapplied so the
        instrument sounds correct from the new position."""
        with self._lock:
            duration = self._timeline.duration
            playing = self._state == PlayerState.PLAYING
        seconds = max(0.0, min(seconds, duration))

        if playing:
            self._stop_flag.set()
            self._join_worker()
        self._panic(full=True)

        with self._lock:
            index = self._index_at_or_after(seconds)
            self._next_index = index
            self._anchor_pos = seconds
        self._chase_controllers(index)

        if playing:
            with self._lock:
                self._spawn_worker_locked()

    # -- Status (polled by the GUI) ---------------------------------------
    def position(self) -> float:
        with self._lock:
            return self._position_locked()

    def duration(self) -> float:
        with self._lock:
            return self._timeline.duration

    def notes(self) -> list[smf.Note]:
        """The current track's notes (for the piano-roll view)."""
        with self._lock:
            timeline = self._timeline
        return smf.extract_notes(timeline)

    def state(self) -> PlayerState:
        with self._lock:
            return self._state

    # -- Internals --------------------------------------------------------
    def _position_locked(self) -> float:
        if self._state == PlayerState.PLAYING:
            elapsed = (time.perf_counter() - self._anchor_wall) * self._speed
            return min(self._anchor_pos + elapsed, self._timeline.duration)
        return self._anchor_pos

    def _spawn_worker_locked(self) -> None:
        self._stop_flag.clear()
        self._anchor_wall = time.perf_counter()
        self._state = PlayerState.PLAYING
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _join_worker(self) -> None:
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None

    def _run(self) -> None:
        """Worker loop: send each event when its scheduled time arrives."""
        while not self._stop_flag.is_set():
            with self._lock:
                index = self._next_index
                events = self._timeline.events
                muted = self._muted
                if index < len(events):
                    event_time, msg = events[index]
                    wall_target = self._anchor_wall + (
                        event_time - self._anchor_pos
                    ) / self._speed
                    port = self._port
                at_end = index >= len(events)

            if at_end:
                if self._handle_end():
                    continue
                return

            delay = wall_target - time.perf_counter()
            if delay > 0 and self._stop_flag.wait(delay):
                return  # interrupted by pause/stop

            if port is not None and not (
                muted and msg.type == "note_on" and msg.velocity > 0
            ):
                self._note_track(self._send(port, msg))

            with self._lock:
                self._next_index += 1

    def _handle_end(self) -> bool:
        """End of track. Returns True to keep looping, False to stop."""
        with self._lock:
            loop = self._loop
        if loop:
            self._panic(full=True)  # clean reset so notes don't bleed across loops
            with self._lock:
                self._next_index = 0
                self._anchor_pos = 0.0
                self._anchor_wall = time.perf_counter()
                return True
        # Natural end: only release genuinely hanging notes; leave any
        # sustained release tail to ring out instead of cutting it off.
        self._panic(full=False)
        with self._lock:
            self._state = PlayerState.STOPPED
            self._next_index = 0
            self._anchor_pos = 0.0
            return False

    def _index_at_or_after(self, seconds: float) -> int:
        """Index of the first event at or after `seconds` (end if none)."""
        for i, (event_time, _msg) in enumerate(self._timeline.events):
            if event_time >= seconds:
                return i
        return len(self._timeline.events)

    def _chase_controllers(self, index: int) -> None:
        """Reapply the latest program/control/pitch values that occur before
        `index`, so a seek lands with the instrument correctly configured.
        Notes are intentionally not retriggered."""
        port = self._port
        if port is None:
            return
        programs: dict[int, mido.Message] = {}
        controls: dict[tuple[int, int], mido.Message] = {}
        pitches: dict[int, mido.Message] = {}
        for _time, msg in self._timeline.events[:index]:
            if msg.type == "program_change":
                programs[msg.channel] = msg
            elif msg.type == "control_change":
                controls[(msg.channel, msg.control)] = msg
            elif msg.type == "pitchwheel":
                pitches[msg.channel] = msg
        for msg in (*programs.values(), *controls.values(), *pitches.values()):
            self._send(port, msg)

    def _send(self, port, msg: mido.Message) -> mido.Message:
        """Send a message, remapping its channel if an output channel is set.
        Returns the message actually sent (for note tracking)."""
        if self._out_channel is not None and hasattr(msg, "channel"):
            msg = msg.copy(channel=self._out_channel)
        try:
            port.send(msg)
        except Exception:
            pass  # a dead port shouldn't take down the thread
        return msg

    def _output_channels(self) -> set:
        """Channels that may have sounding notes (the override, or all used)."""
        if self._out_channel is not None:
            return {self._out_channel}
        return self._timeline.channels

    def _note_track(self, msg: mido.Message) -> None:
        if msg.type == "note_on" and msg.velocity > 0:
            self._active_notes.add((msg.channel, msg.note))
        elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
            self._active_notes.discard((msg.channel, msg.note))

    def _panic(self, full: bool = True) -> None:
        """Release held notes. When `full`, also reset sustain and send
        all-notes-off / all-sound-off on every channel used (immediate
        silence for pause/stop/loop). When not `full`, only the hanging
        notes are released, so a natural ending can ring out."""
        port = self._port
        if port is None:
            return
        for channel, note in list(self._active_notes):
            self._safe_send(port, "note_off", note=note, velocity=0, channel=channel)
        self._active_notes.clear()
        if not full:
            return
        for channel in self._output_channels():
            self._safe_send(port, "control_change", control=64, value=0, channel=channel)
            self._safe_send(port, "control_change", control=123, value=0, channel=channel)
            self._safe_send(port, "control_change", control=120, value=0, channel=channel)

    @staticmethod
    def _safe_send(port, msg_type: str, **kwargs) -> None:
        try:
            port.send(mido.Message(msg_type, **kwargs))
        except Exception:
            pass
