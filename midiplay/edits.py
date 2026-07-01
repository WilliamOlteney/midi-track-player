"""Pure MIDI edit operations.

Each function mutates a mido.MidiFile in place. Undo/redo is handled by the
caller via snapshots, so these stay simple and side-effect-only.
"""

from __future__ import annotations

import mido

# General MIDI program names, indexed by program number (0-127).
GM_INSTRUMENTS = (
    "Acoustic Grand Piano", "Bright Acoustic Piano", "Electric Grand Piano",
    "Honky-tonk Piano", "Electric Piano 1", "Electric Piano 2", "Harpsichord",
    "Clavi", "Celesta", "Glockenspiel", "Music Box", "Vibraphone", "Marimba",
    "Xylophone", "Tubular Bells", "Dulcimer", "Drawbar Organ",
    "Percussive Organ", "Rock Organ", "Church Organ", "Reed Organ",
    "Accordion", "Harmonica", "Tango Accordion", "Acoustic Guitar (nylon)",
    "Acoustic Guitar (steel)", "Electric Guitar (jazz)",
    "Electric Guitar (clean)", "Electric Guitar (muted)", "Overdriven Guitar",
    "Distortion Guitar", "Guitar Harmonics", "Acoustic Bass",
    "Electric Bass (finger)", "Electric Bass (pick)", "Fretless Bass",
    "Slap Bass 1", "Slap Bass 2", "Synth Bass 1", "Synth Bass 2", "Violin",
    "Viola", "Cello", "Contrabass", "Tremolo Strings", "Pizzicato Strings",
    "Orchestral Harp", "Timpani", "String Ensemble 1", "String Ensemble 2",
    "Synth Strings 1", "Synth Strings 2", "Choir Aahs", "Voice Oohs",
    "Synth Voice", "Orchestra Hit", "Trumpet", "Trombone", "Tuba",
    "Muted Trumpet", "French Horn", "Brass Section", "Synth Brass 1",
    "Synth Brass 2", "Soprano Sax", "Alto Sax", "Tenor Sax", "Baritone Sax",
    "Oboe", "English Horn", "Bassoon", "Clarinet", "Piccolo", "Flute",
    "Recorder", "Pan Flute", "Blown Bottle", "Shakuhachi", "Whistle",
    "Ocarina", "Lead 1 (square)", "Lead 2 (sawtooth)", "Lead 3 (calliope)",
    "Lead 4 (chiff)", "Lead 5 (charang)", "Lead 6 (voice)", "Lead 7 (fifths)",
    "Lead 8 (bass + lead)", "Pad 1 (new age)", "Pad 2 (warm)",
    "Pad 3 (polysynth)", "Pad 4 (choir)", "Pad 5 (bowed)", "Pad 6 (metallic)",
    "Pad 7 (halo)", "Pad 8 (sweep)", "FX 1 (rain)", "FX 2 (soundtrack)",
    "FX 3 (crystal)", "FX 4 (atmosphere)", "FX 5 (brightness)",
    "FX 6 (goblins)", "FX 7 (echoes)", "FX 8 (sci-fi)", "Sitar", "Banjo",
    "Shamisen", "Koto", "Kalimba", "Bagpipe", "Fiddle", "Shanai",
    "Tinkle Bell", "Agogo", "Steel Drums", "Woodblock", "Taiko Drum",
    "Melodic Tom", "Synth Drum", "Reverse Cymbal", "Guitar Fret Noise",
    "Breath Noise", "Seashore", "Bird Tweet", "Telephone Ring", "Helicopter",
    "Applause", "Gunshot",
)


def _track_channel(track: mido.MidiTrack) -> int:
    """Channel the track plays on (first channel message), or 0."""
    for msg in track:
        if hasattr(msg, "channel"):
            return msg.channel
    return 0


def transpose_track(midi: mido.MidiFile, index: int, semitones: int) -> None:
    """Shift every note in a track by `semitones`, clamped to 0-127."""
    for msg in midi.tracks[index]:
        if msg.type in ("note_on", "note_off"):
            msg.note = max(0, min(127, msg.note + semitones))


def set_track_program(midi: mido.MidiFile, index: int, program: int) -> None:
    """Set a track's instrument. Updates existing program changes, or inserts
    one at the start if the track has none."""
    track = midi.tracks[index]
    found = False
    for msg in track:
        if msg.type == "program_change":
            msg.program = program
            found = True
    if not found:
        track.insert(
            0,
            mido.Message(
                "program_change", program=program, channel=_track_channel(track), time=0
            ),
        )


def rename_track(midi: mido.MidiFile, index: int, name: str) -> None:
    """Set a track's name (updates or inserts a track_name meta event)."""
    track = midi.tracks[index]
    for msg in track:
        if msg.is_meta and msg.type == "track_name":
            msg.name = name
            return
    track.insert(0, mido.MetaMessage("track_name", name=name, time=0))


def delete_track(midi: mido.MidiFile, index: int) -> None:
    """Remove a track."""
    del midi.tracks[index]


def add_track(midi: mido.MidiFile, name: str | None = None, channel: int | None = None) -> int:
    """Append a new empty track (named, with a program_change) and return its
    index. The channel defaults to the new track's position, so successive
    tracks get distinct channels."""
    index = len(midi.tracks)
    if channel is None:
        channel = max(0, index - 1) % 16
    if name is None:
        name = f"Track {index}"
    track = mido.MidiTrack()
    track.append(mido.MetaMessage("track_name", name=name, time=0))
    track.append(mido.Message("program_change", program=0, channel=channel, time=0))
    track.append(mido.MetaMessage("end_of_track", time=0))
    midi.tracks.append(track)
    return index


def track_program(track: mido.MidiTrack) -> int:
    """Current program of a track (first program_change), or 0."""
    for msg in track:
        if msg.type == "program_change":
            return msg.program
    return 0


def _rebuild_track(track: mido.MidiTrack, abs_events: list[tuple[int, mido.Message]]) -> None:
    """Replace a track's contents from (abs_tick, msg) pairs, recomputing the
    delta times. end_of_track is always placed last, at or after every other
    event, so added/extended notes never end up after it."""
    body = [(t, m) for t, m in abs_events
            if not (m.is_meta and m.type == "end_of_track")]
    eot = [(t, m) for t, m in abs_events
           if m.is_meta and m.type == "end_of_track"]
    body.sort(key=lambda e: e[0])

    end_tick = max([t for t, _ in body] + [t for t, _ in eot] + [0])
    eot_msg = eot[0][1] if eot else mido.MetaMessage("end_of_track")
    body.append((end_tick, eot_msg))

    del track[:]
    prev = 0
    for tick, msg in body:
        msg.time = max(0, tick - prev)
        prev = tick
        track.append(msg)


def delete_notes(midi: mido.MidiFile, index: int, note_ids: set) -> None:
    """Delete notes (by (channel, pitch, start_tick) identity) from a track,
    removing each note-on and its matching note-off."""
    track = midi.tracks[index]
    abs_events: list[tuple[int, mido.Message]] = []
    tick = 0
    for msg in track:
        tick += msg.time
        abs_events.append((tick, msg))

    remove: set[int] = set()  # positions in abs_events to drop
    for pos, (t, msg) in enumerate(abs_events):
        if msg.type == "note_on" and msg.velocity > 0 and (msg.channel, msg.note, t) in note_ids:
            remove.add(pos)
            for later_pos in range(pos + 1, len(abs_events)):
                _t, m2 = abs_events[later_pos]
                if (
                    m2.channel == msg.channel
                    and getattr(m2, "note", None) == msg.note
                    and (m2.type == "note_off" or (m2.type == "note_on" and m2.velocity == 0))
                    and later_pos not in remove
                ):
                    remove.add(later_pos)
                    break

    kept = [(t, m) for i, (t, m) in enumerate(abs_events) if i not in remove]
    _rebuild_track(track, kept)


def edit_notes(midi: mido.MidiFile, index: int, changes: dict) -> None:
    """Move/resize/re-velocity notes. `changes` maps an existing note id
    (channel, pitch, start_tick) to (new_start_tick, new_end_tick, new_pitch,
    new_velocity). Updates each note-on/note-off pair, then rebuilds."""
    track = midi.tracks[index]
    events: list[list] = []  # [abs_tick, msg] (mutable)
    tick = 0
    for msg in track:
        tick += msg.time
        events.append([tick, msg])

    used_off: set[int] = set()
    for (channel, pitch, start_tick), (new_start, new_end, new_pitch, new_vel) in changes.items():
        on_pos = None
        for i, (t, msg) in enumerate(events):
            if (
                t == start_tick
                and msg.type == "note_on"
                and msg.velocity > 0
                and msg.channel == channel
                and msg.note == pitch
            ):
                on_pos = i
                break
        if on_pos is None:
            continue
        off_pos = None
        for j in range(on_pos + 1, len(events)):
            t, msg = events[j]
            if (
                j not in used_off
                and msg.channel == channel
                and getattr(msg, "note", None) == pitch
                and (msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0))
            ):
                off_pos = j
                break
        events[on_pos][0] = new_start
        events[on_pos][1].note = new_pitch
        events[on_pos][1].velocity = new_vel
        if off_pos is not None:
            used_off.add(off_pos)
            events[off_pos][0] = new_end
            events[off_pos][1].note = new_pitch

    _rebuild_track(track, [(t, m) for t, m in events])


def add_note(
    midi: mido.MidiFile,
    index: int,
    pitch: int,
    start_tick: int,
    length: int | None = None,
    velocity: int = 90,
) -> None:
    """Add a note to a track. Length defaults to one beat. The note uses the
    track's existing channel (or 0)."""
    track = midi.tracks[index]
    if length is None:
        length = midi.ticks_per_beat
    channel = _track_channel(track)

    abs_events: list[tuple[int, mido.Message]] = []
    tick = 0
    for msg in track:
        tick += msg.time
        abs_events.append((tick, msg))
    abs_events.append(
        (start_tick, mido.Message("note_on", note=pitch, velocity=velocity, channel=channel))
    )
    abs_events.append(
        (start_tick + length, mido.Message("note_off", note=pitch, velocity=0, channel=channel))
    )
    _rebuild_track(track, abs_events)


def add_notes(midi: mido.MidiFile, index: int, notes) -> None:
    """Add several notes to a track in one pass (e.g. paste/duplicate).

    `notes` is an iterable of (pitch, start_tick, length, velocity). Each is
    clamped to sane ranges; all use the track's existing channel (or 0)."""
    track = midi.tracks[index]
    channel = _track_channel(track)
    beat = midi.ticks_per_beat

    abs_events: list[tuple[int, mido.Message]] = []
    tick = 0
    for msg in track:
        tick += msg.time
        abs_events.append((tick, msg))

    for pitch, start_tick, length, velocity in notes:
        pitch = max(0, min(127, int(pitch)))
        start_tick = max(0, int(start_tick))
        length = max(1, int(length) if length else beat)
        velocity = max(1, min(127, int(velocity)))
        abs_events.append(
            (start_tick, mido.Message("note_on", note=pitch, velocity=velocity, channel=channel))
        )
        abs_events.append(
            (start_tick + length, mido.Message("note_off", note=pitch, velocity=0, channel=channel))
        )
    _rebuild_track(track, abs_events)


def insert_recorded(midi: mido.MidiFile, index: int, timed_msgs) -> None:
    """Merge recorded (abs_tick, message) events into a track (overdub).

    Each message is placed at its tick with the track's channel; note-ons still
    held at the end (e.g. a key down when recording stopped) are closed with a
    note-off at the last tick so nothing hangs. Meta messages are ignored."""
    track = midi.tracks[index]
    channel = _track_channel(track)

    abs_events: list[tuple[int, mido.Message]] = []
    tick = 0
    for msg in track:
        tick += msg.time
        abs_events.append((tick, msg))

    max_tick = max([t for t, _ in abs_events] + [0])
    open_notes: dict[int, int] = {}  # pitch -> number of note-ons still open
    for t, msg in timed_msgs:
        if msg.is_meta:
            continue
        m = msg.copy(time=0)
        if hasattr(m, "channel"):
            m.channel = channel
        t = max(0, int(t))
        max_tick = max(max_tick, t)
        abs_events.append((t, m))
        if m.type == "note_on" and m.velocity > 0:
            open_notes[m.note] = open_notes.get(m.note, 0) + 1
        elif m.type == "note_off" or (m.type == "note_on" and m.velocity == 0):
            if open_notes.get(m.note):
                open_notes[m.note] -= 1

    for pitch, count in open_notes.items():
        for _ in range(count):
            abs_events.append(
                (max_tick, mido.Message("note_off", note=pitch, velocity=0, channel=channel))
            )
    _rebuild_track(track, abs_events)


def merge_tracks(midi: mido.MidiFile, indices) -> int:
    """Merge two or more tracks into one, replacing them with a single track
    at the position of the earliest selected. Returns the merged track's index.

    Every track's events are placed on a shared absolute-tick timeline and
    interleaved in time order; each message keeps its own channel, so the
    combined track can carry several channels at once. Duplicate track-name
    meta events are dropped (the first track's name is kept). A no-op that
    returns the sole index when fewer than two distinct tracks are given.
    """
    indices = sorted(set(indices))
    if len(indices) < 2:
        return indices[0] if indices else 0

    abs_events: list[tuple[int, mido.Message]] = []
    name_kept = False
    for track_index in indices:
        tick = 0
        for msg in midi.tracks[track_index]:
            tick += msg.time
            if msg.is_meta and msg.type == "end_of_track":
                continue  # _rebuild_track re-adds a single terminator
            if msg.is_meta and msg.type == "track_name":
                if name_kept:
                    continue  # keep only the first track's name
                name_kept = True
            abs_events.append((tick, msg))

    merged = mido.MidiTrack()
    _rebuild_track(merged, abs_events)

    target = indices[0]
    for track_index in reversed(indices):  # delete high-to-low to keep indices valid
        del midi.tracks[track_index]
    midi.tracks.insert(target, merged)
    return target
