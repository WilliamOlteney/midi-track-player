# MIDI Track Player

A minimal, reliable desktop app that loads a Standard MIDI File (`.mid`) and
plays **one or more selected tracks** to an external MIDI output device (e.g. a
MIDI keyboard).

Built with **PySide6** (UI), **mido** (MIDI file parsing), and
**python-rtmidi** (MIDI I/O — CoreMIDI on macOS).

## Features

- Open a `.mid` file (file dialog or **drag-and-drop** onto the window)
- Track list showing **track number · name · event count**
- Select **one or more tracks** to play together (Ctrl/Shift-click for a
  multi-selection); they're merged onto a shared time axis and play in sync.
  Editing applies to the **focused (current) track** only
- Choose any connected **MIDI output** (with **Refresh** for hot-plugging)
- **Output channel** selector — keep each track's recorded channel, or force the
  played track onto one channel. Set this to your keyboard's receive channel if
  only some tracks make sound (single-channel devices ignore other channels)
- Transport: **Play / Pause / Stop / Restart** (Spacebar toggles play/pause)
- **Click-to-seek** progress bar (click or drag to jump; instrument state is
  reapplied so it sounds correct from the new position) with elapsed / total time
- **Piano View** — a Synthesia-style falling-notes window: notes fall onto a
  piano keyboard in sync with playback, keys light up as they sound, notes are
  labelled with their (bold, outlined) names. **Scroll wheel** scrubs the song;
  **middle-drag** zooms the time window; the main seek bar updates it live. An
  **All tracks** toggle shows every track color-coded (played track emphasized).
  Follows Play/Pause/Seek/Speed automatically and runs at ~60 fps
- **Note editing in the Piano View** — left-click to select (Ctrl-click for
  multiple), **Delete** to remove, **drag** to move (pitch + time), drag a note's
  edge to resize, **Shift-drag** to change velocity (brightness shows it live),
  and **double-click empty grid to add** a note. Time edits snap to a 1/16 grid
  (hold **Alt** for free placement); a Shift-drag shows the live velocity value
  on the note. All undoable, and the playhead stays put. A control legend sits
  top-right (press **H** to hide/show it)
- Faithful playback: Note On/Off, velocity, program changes, control changes,
  pitch bend, sustain pedal, and **tempo changes** — with timing preserved
  exactly (tempo is read from the whole file, even when it lives in a separate
  conductor track)
- **Speed** control (50–150%), **Loop**, and **Mute**
- **Editing** (track-level): rename, transpose, change instrument, or delete a
  track via the Edit menu or right-click on a track. Full **undo/redo**, an
  unsaved-changes (`*`) indicator, and non-destructive **Save As** — the
  original file is never overwritten unless you explicitly choose it
- Remembers your last-used MIDI output
- No stuck notes: pausing/stopping releases held notes, resets sustain, and
  sends all-notes-off / all-sound-off

## Requirements

- **Python 3.12** — python-rtmidi ships prebuilt wheels for 3.12 on macOS,
  Windows, and Linux. (3.13/3.14 currently have no wheel and would need a C++
  compiler to build from source.)

## Setup

```sh
# from the project root
python3.12 -m venv .venv
source .venv/bin/activate        # macOS / Linux
# .venv\Scripts\Activate.ps1     # Windows PowerShell
pip install -r requirements.txt
```

## Run

```sh
python app.py
```

1. **Open** a `.mid` file (or drag one onto the window).
2. Pick the **track(s)** to play (Ctrl/Shift-click to select several) and your
   **MIDI Output** device.
3. Press **Play**.

## Project layout

```
app.py                 entry point — python app.py
midiplay/
  __init__.py
  smf.py               MIDI file loading, track info, tempo map + timing, notes
  devices.py           MIDI output enumeration / connection (rtmidi backend)
  engine.py            playback engine: scheduling, transport, seek, panic
  edits.py             track edit operations (rename/transpose/instrument/delete)
  piano_view.py        Synthesia-style falling-notes window
  main_window.py       PySide6 window and wiring
requirements.txt
```

## How it works (the parts that matter for reliability)

- **Timing:** each selected track's events are converted to absolute seconds
  using a tempo map built from *all* tracks; when several tracks are selected
  they're merged into one time-ordered stream, then scheduled on a dedicated
  thread against a monotonic clock (absolute targets, so no drift).
- **No hanging notes:** Pause/Stop/Loop perform a full "panic" (note-offs +
  sustain reset + all-notes-off + all-sound-off); a natural end only releases
  genuinely-stuck notes so release tails can ring out.
