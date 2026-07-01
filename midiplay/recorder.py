"""MIDI recording.

Captures messages from a MIDI input port on a background thread while the
transport plays, timestamping each with the song position (seconds) taken from
a callback, and optionally echoing them to an output port so the player hears
what they're performing (MIDI "thru" / monitoring). The captured events are
turned into track edits by the caller (see edits.insert_recorded).
"""

from __future__ import annotations

import threading
import time


class Recorder:
    def __init__(self) -> None:
        self._input = None
        self._thru = None            # output port for monitoring, or None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._events: list = []      # (seconds, mido.Message)
        self._pos_fn = None
        self._lock = threading.Lock()

    def set_input(self, port) -> None:
        """Set the open MIDI input port (the recorder does not own/close it)."""
        self._input = port

    def set_thru(self, port) -> None:
        """Set the output port to echo input to while recording (or None)."""
        self._thru = port

    def recording(self) -> bool:
        return self._thread is not None

    def start(self, position_fn) -> None:
        """Begin capturing. `position_fn()` returns the current song position in
        seconds, used to timestamp each incoming message."""
        if self._thread is not None or self._input is None:
            return
        self._pos_fn = position_fn
        with self._lock:
            self._events = []
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        port = self._input
        while not self._stop.is_set():
            for msg in port.iter_pending():
                seconds = self._pos_fn() if self._pos_fn else 0.0
                with self._lock:
                    self._events.append((seconds, msg))
                if self._thru is not None and not msg.is_meta:
                    try:
                        self._thru.send(msg)
                    except Exception:
                        pass  # a dead port shouldn't kill recording
            time.sleep(0.001)

    def stop(self) -> list:
        """Stop capturing and return the captured [(seconds, message), …]."""
        if self._thread is None:
            return []
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not threading.current_thread():
            thread.join(timeout=2.0)
        with self._lock:
            events = list(self._events)
            self._events = []
        return events
