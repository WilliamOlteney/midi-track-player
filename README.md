# MIDI Track Player

A minimal, reliable desktop app that loads a Standard MIDI File (`.mid`) and
plays **one or more tracks** to an external MIDI output device (e.g. a
MIDI keyboard).

Built with **PySide6** (UI), **mido** (MIDI file parsing), and
**python-rtmidi** (MIDI I/O — CoreMIDI on macOS).

## Layout

Everything lives in **one window**. A Synthesia-style **falling-notes piano**
is always visible and fills the window, with two **sliding drawers** floating
over it:

- a **left drawer** with all the controls (file, tracks, devices, transport), and
- a **right drawer** with view/appearance **settings**.

Each drawer has a **handle on its edge** to show/hide it and a **drag-grip** to
resize its width, and both are **partially transparent** so the piano shows
through (the transparency is adjustable in Settings).

## Features

- Open a `.mid` file (file dialog or **drag-and-drop** onto the window)
- Track list showing **track number · name · event count**, each row with two
  **independent toggle icons**: an **eye (👁)** to *show* the track on the piano
  and a **speaker (🔊)** to *play* its audio. They're separate, so any set of
  tracks can be shown and any set played — e.g. watch one track fall while
  hearing another. Bright in the track's colour = on, dim grey = off. Played
  tracks are merged onto a shared time axis and play in sync; by default the
  first track with notes is both shown and played. Toggling audio during
  playback keeps the playhead where it is. The **highlighted (current) row** is
  the target for edits
- Choose any connected **MIDI output** (with **Refresh** for hot-plugging)
- **Output channel** selector — keep each track's recorded channel, or force the
  played track onto one channel. Set this to your keyboard's receive channel if
  only some tracks make sound (single-channel devices ignore other channels)
- Transport: **Play / Pause / Stop / Restart** (Spacebar toggles play/pause)
- **Click-to-seek** progress bar (click or drag to jump; instrument state is
  reapplied so it sounds correct from the new position) with elapsed / total time
- **Piano** — the always-visible falling-notes view: notes fall onto a piano
  keyboard in sync with playback, keys light up as they sound, notes are
  labelled with their (bold, outlined) names. The **shown (👁)** tracks are
  drawn color-coded with the current edit-target track emphasized. **Scroll wheel**
  scrubs the song; **middle-drag** zooms the time window; the main seek bar
  updates it live. Follows Play/Pause/Seek/Speed automatically and runs at
  ~60 fps
- **Note editing in the piano** — left-click to select (Ctrl-click for
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
- **Speed** buttons (0.25× / 0.5× / 0.75× / 1× / 1.5× / 2×), **Loop**, and
  **Mute**
- **Settings drawer** (right side): **panel opacity** (how transparent the
  drawers are), piano **fall time** (seconds of upcoming notes shown),
  **note-name labels** on/off, and the **control legend** on/off. Settings are
  remembered between runs
- **Editing** (track-level): rename, transpose, change instrument, delete a
  track, or **merge the playing (🔊) tracks into one** (their events are
  interleaved in time and each keeps its channel) — via the Edit menu or
  right-click on a track. Full **undo/redo**, an
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

1. **Open** a `.mid` file (or drag one onto the window) — open the left drawer
   with the edge handle if it's hidden.
2. Turn on the **speaker (🔊)** for the track(s) you want to hear (and the
   **eye (👁)** for the ones you want to watch on the piano), then pick your
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
  piano_view.py        Synthesia-style falling-notes piano-roll widget
  main_window.py       PySide6 window: piano + left control drawer, wiring
requirements.txt
```

## How it works (the parts that matter for reliability)

- **Timing:** each played track's events are converted to absolute seconds
  using a tempo map built from *all* tracks; when several tracks are played
  they're merged into one time-ordered stream, then scheduled on a dedicated
  thread against a monotonic clock (absolute targets, so no drift).
- **No hanging notes:** Pause/Stop/Loop perform a full "panic" (note-offs +
  sustain reset + all-notes-off + all-sound-off); a natural end only releases
  genuinely-stuck notes so release tails can ring out.
