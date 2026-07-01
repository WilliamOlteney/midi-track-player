"""MIDI device discovery and connection (inputs and outputs).

Uses mido with the python-rtmidi backend, which maps to CoreMIDI on macOS,
WinMM on Windows, and ALSA on Linux. The backend is set explicitly so
behavior is identical across platforms regardless of mido's default.
"""

from __future__ import annotations

import mido

mido.set_backend("mido.backends.rtmidi")


def list_outputs() -> list[str]:
    """Names of currently available MIDI output ports (may be empty)."""
    return mido.get_output_names()


def open_output(name: str):
    """Open a MIDI output port by name.

    Returns an open mido output port; raises OSError/ValueError if the named
    port cannot be opened. The caller is responsible for closing it.
    """
    return mido.open_output(name)


def list_inputs() -> list[str]:
    """Names of currently available MIDI input ports (may be empty)."""
    return mido.get_input_names()


def open_input(name: str):
    """Open a MIDI input port by name for recording.

    Returns an open mido input port; raises OSError/ValueError if the named
    port cannot be opened. The caller is responsible for closing it.
    """
    return mido.open_input(name)
