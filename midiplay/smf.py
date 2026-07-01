"""Standard MIDI File loading.

Thin wrapper around mido that loads a `.mid` file and reports file-level
information. Track parsing (names, event counts) and the timed event list
for playback are added in later phases.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import mido

# Default MIDI tempo: 500000 microseconds per beat = 120 BPM.
DEFAULT_TEMPO = 500_000


class MidiLoadError(Exception):
    """Raised when a file cannot be loaded as a Standard MIDI File."""


@dataclass(frozen=True)
class TrackInfo:
    """Summary of a single track for display in the track list."""

    index: int          # 0-based position in the file
    name: str           # track name, or "" if the track has no name
    event_count: int    # messages in the track (excludes the end-of-track marker)
    note_count: int = 0  # number of note-on events (velocity > 0)

    @property
    def display_name(self) -> str:
        return self.name if self.name else "(untitled)"

    @property
    def has_notes(self) -> bool:
        return self.note_count > 0

    def label(self) -> str:
        if not self.has_notes:
            return f"Track {self.index + 1}: {self.display_name} — no notes"
        plural = "event" if self.event_count == 1 else "events"
        return (
            f"Track {self.index + 1}: {self.display_name} "
            f"— {self.event_count} {plural}"
        )


@dataclass(frozen=True)
class MidiFileInfo:
    """File-level summary of a loaded MIDI file."""

    path: str
    name: str          # basename, e.g. "song.mid"
    midi_format: int   # SMF type: 0, 1, or 2
    num_tracks: int
    ticks_per_beat: int

    def summary(self) -> str:
        return (
            f"{self.name} — format {self.midi_format}, "
            f"{self.num_tracks} track(s), {self.ticks_per_beat} ticks/beat"
        )


def load(path: str) -> mido.MidiFile:
    """Open a Standard MIDI File.

    Raises MidiLoadError with a readable message if the file is missing or
    is not a valid MIDI file.
    """
    if not os.path.isfile(path):
        raise MidiLoadError(f"File not found: {path}")
    try:
        return mido.MidiFile(path)
    except Exception as exc:  # mido raises OSError/EOFError/ValueError/etc.
        raise MidiLoadError(f"Could not read MIDI file: {exc}") from exc


def describe(midi: mido.MidiFile, path: str) -> MidiFileInfo:
    """Build a MidiFileInfo from a loaded file."""
    return MidiFileInfo(
        path=path,
        name=os.path.basename(path) or "untitled.mid",
        midi_format=midi.type,
        num_tracks=len(midi.tracks),
        ticks_per_beat=midi.ticks_per_beat,
    )


def new_file(ticks_per_beat: int = 480) -> mido.MidiFile:
    """A blank Type-1 file: a conductor track (tempo + 4/4) and one empty
    instrument track ready to edit or record into."""
    midi = mido.MidiFile(type=1, ticks_per_beat=ticks_per_beat)

    conductor = mido.MidiTrack()
    conductor.append(mido.MetaMessage("track_name", name="Tempo", time=0))
    conductor.append(mido.MetaMessage("set_tempo", tempo=DEFAULT_TEMPO, time=0))
    conductor.append(mido.MetaMessage("time_signature", numerator=4, denominator=4, time=0))
    conductor.append(mido.MetaMessage("end_of_track", time=0))
    midi.tracks.append(conductor)

    track = mido.MidiTrack()
    track.append(mido.MetaMessage("track_name", name="Track 1", time=0))
    track.append(mido.Message("program_change", program=0, channel=0, time=0))
    track.append(mido.MetaMessage("end_of_track", time=0))
    midi.tracks.append(track)
    return midi


def track_infos(midi: mido.MidiFile) -> list[TrackInfo]:
    """One TrackInfo per track, in file order.

    `name` comes from the track's first track_name meta event (mido exposes
    this as `track.name`, defaulting to "").  The event count excludes the
    automatic end_of_track marker so empty tracks read as 0 events.
    """
    infos: list[TrackInfo] = []
    for index, track in enumerate(midi.tracks):
        count = sum(
            1
            for msg in track
            if not (msg.is_meta and msg.type == "end_of_track")
        )
        notes = sum(1 for msg in track if msg.type == "note_on" and msg.velocity > 0)
        infos.append(
            TrackInfo(index=index, name=track.name or "", event_count=count, note_count=notes)
        )
    return infos


def first_track_with_notes(infos_source: mido.MidiFile) -> int:
    """Index of the first track containing a note_on (velocity > 0), or 0.

    Used to pick a sensible default selection (skips an empty conductor track).
    """
    for index, track in enumerate(infos_source.tracks):
        for msg in track:
            if msg.type == "note_on" and msg.velocity > 0:
                return index
    return 0


@dataclass
class TrackTimeline:
    """A single track flattened into absolute-time playable events.

    `events` are (seconds_from_start, message) pairs in time order, meta
    messages removed. `duration` is the time of the last event. `channels`
    are the MIDI channels the track uses (needed to reset them on stop).
    """

    events: list[tuple[float, mido.Message]] = field(default_factory=list)
    duration: float = 0.0
    channels: set[int] = field(default_factory=set)
    notes: list = field(default_factory=list)  # list[Note], paired note spans


def build_tempo_map(midi: mido.MidiFile) -> list[tuple[int, int]]:
    """Collect tempo changes from *all* tracks as sorted (abs_tick, tempo).

    Tempo in a Type-1 file usually lives in the conductor track, not the
    track being played, so the whole file must be scanned. The result always
    starts with an entry at tick 0 (the default tempo if none is set there).
    """
    changes: list[tuple[int, int]] = []
    for track in midi.tracks:
        abs_tick = 0
        for msg in track:
            abs_tick += msg.time
            if msg.type == "set_tempo":
                changes.append((abs_tick, msg.tempo))
    changes.sort(key=lambda c: c[0])
    if not changes or changes[0][0] != 0:
        changes.insert(0, (0, DEFAULT_TEMPO))
    return changes


def build_track_timeline(
    midi: mido.MidiFile, track_index: int, tempo_map: list | None = None
) -> TrackTimeline:
    """Flatten one track into absolute-time events using the global tempo map.

    Walks the track once, converting each event's absolute tick position to
    seconds by accumulating time across tempo segments. This preserves the
    recorded timing exactly, including tempo changes defined in other tracks.
    Pass a precomputed `tempo_map` to avoid rebuilding it per call.
    """
    if tempo_map is None:
        tempo_map = build_tempo_map(midi)
    ticks_per_beat = midi.ticks_per_beat
    track = midi.tracks[track_index]

    timeline = TrackTimeline()
    abs_tick = 0

    # Running state for the tick -> seconds conversion across tempo segments.
    seg_index = 1                       # next tempo change to apply
    seg_seconds = 0.0                   # seconds elapsed up to seg_tick
    seg_tick = 0                        # tick where the current tempo began
    current_tempo = tempo_map[0][1]

    # (channel, pitch) -> (start_tick, start_seconds, velocity) for open notes.
    open_notes: dict[tuple[int, int], tuple[int, float, int]] = {}

    for msg in track:
        abs_tick += msg.time
        if msg.is_meta:
            continue  # tempo is already captured globally; meta isn't sent

        # Advance through any tempo changes that occur at or before this event.
        while seg_index < len(tempo_map) and tempo_map[seg_index][0] <= abs_tick:
            change_tick, change_tempo = tempo_map[seg_index]
            seg_seconds += _ticks_to_seconds(
                change_tick - seg_tick, current_tempo, ticks_per_beat
            )
            seg_tick = change_tick
            current_tempo = change_tempo
            seg_index += 1

        seconds = seg_seconds + _ticks_to_seconds(
            abs_tick - seg_tick, current_tempo, ticks_per_beat
        )
        timeline.events.append((seconds, msg))
        if hasattr(msg, "channel"):
            timeline.channels.add(msg.channel)

        # Pair note-on/note-off into Note spans (with tick identities).
        if msg.type == "note_on" and msg.velocity > 0:
            key = (msg.channel, msg.note)
            if key in open_notes:  # retrigger: close the previous span first
                s_tick, s_sec, vel = open_notes.pop(key)
                timeline.notes.append(
                    Note(s_sec, seconds, msg.note, vel, msg.channel, s_tick, abs_tick)
                )
            open_notes[key] = (abs_tick, seconds, msg.velocity)
        elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
            key = (msg.channel, msg.note)
            if key in open_notes:
                s_tick, s_sec, vel = open_notes.pop(key)
                timeline.notes.append(
                    Note(s_sec, seconds, msg.note, vel, msg.channel, s_tick, abs_tick)
                )

    if timeline.events:
        timeline.duration = timeline.events[-1][0]
    # Close any notes still held at the end of the track.
    for (channel, pitch), (s_tick, s_sec, vel) in open_notes.items():
        timeline.notes.append(
            Note(s_sec, max(s_sec, timeline.duration), pitch, vel, channel, s_tick, abs_tick)
        )
    timeline.notes.sort(key=lambda n: n.start_tick)
    return timeline


def build_multi_track_timeline(
    midi: mido.MidiFile, track_indices, tempo_map: list | None = None
) -> TrackTimeline:
    """Flatten several tracks onto one shared time axis for simultaneous play.

    Each track is timed independently (via the global tempo map) and the
    resulting events are merged into a single time-ordered list, so the
    playback engine can stream them through one worker exactly as it does a
    single track. Channels and paired Note spans are unioned across tracks.
    A single index collapses to the same result as build_track_timeline.
    """
    if tempo_map is None:
        tempo_map = build_tempo_map(midi)

    merged = TrackTimeline()
    for track_index in track_indices:
        sub = build_track_timeline(midi, track_index, tempo_map)
        merged.events.extend(sub.events)
        merged.channels |= sub.channels
        merged.notes.extend(sub.notes)

    # Stable sort on time only (never compares the Message payloads), so
    # same-time events keep their per-track order.
    merged.events.sort(key=lambda event: event[0])
    if merged.events:
        merged.duration = merged.events[-1][0]
    merged.notes.sort(key=lambda n: n.start_tick)
    return merged


def _ticks_to_seconds(ticks: int, tempo: int, ticks_per_beat: int) -> float:
    """Convert a tick span to seconds at a given tempo (microseconds/beat)."""
    return ticks * tempo / (1_000_000.0 * ticks_per_beat)


@dataclass(frozen=True)
class Note:
    """A sounding note with absolute start/end times (seconds) and the tick
    positions it was built from (used as a stable identity for editing)."""

    start: float
    end: float
    pitch: int       # MIDI note number 0-127
    velocity: int
    channel: int
    start_tick: int = 0
    end_tick: int = 0

    @property
    def id(self) -> tuple[int, int, int]:
        """Stable identity within a track: (channel, pitch, start_tick)."""
        return (self.channel, self.pitch, self.start_tick)


def extract_notes(timeline: TrackTimeline) -> list[Note]:
    """The paired Note spans for a timeline (built during build_track_timeline)."""
    return timeline.notes


class TimeMap:
    """Bidirectional tick <-> seconds conversion using the file's tempo map.
    Used by the editor to snap dragged notes to a musical (tick) grid."""

    def __init__(self, midi: mido.MidiFile) -> None:
        self._changes = build_tempo_map(midi)  # sorted (tick, tempo), first at 0
        self.ticks_per_beat = midi.ticks_per_beat

    def tick_to_seconds(self, tick: float) -> float:
        seconds = 0.0
        last_tick = 0
        tempo = self._changes[0][1]
        for change_tick, change_tempo in self._changes[1:]:
            if change_tick >= tick:
                break
            seconds += _ticks_to_seconds(change_tick - last_tick, tempo, self.ticks_per_beat)
            last_tick = change_tick
            tempo = change_tempo
        seconds += _ticks_to_seconds(tick - last_tick, tempo, self.ticks_per_beat)
        return seconds

    def seconds_to_ticks(self, seconds: float) -> float:
        acc = 0.0
        last_tick = 0
        tempo = self._changes[0][1]
        for change_tick, change_tempo in self._changes[1:]:
            segment = _ticks_to_seconds(change_tick - last_tick, tempo, self.ticks_per_beat)
            if acc + segment >= seconds:
                break
            acc += segment
            last_tick = change_tick
            tempo = change_tempo
        seconds_per_tick = tempo / (1_000_000.0 * self.ticks_per_beat)
        extra = (seconds - acc) / seconds_per_tick if seconds_per_tick > 0 else 0.0
        return last_tick + extra


def extract_all_notes(midi: mido.MidiFile) -> list[tuple[int, list[Note]]]:
    """Notes for every track, as (track_index, notes), all on the same
    absolute-time axis (shared tempo map) so they line up when overlaid."""
    return extract_notes_for(midi, range(len(midi.tracks)))


def extract_notes_for(midi: mido.MidiFile, track_indices) -> list[tuple[int, list[Note]]]:
    """Notes for a subset of tracks, as (track_index, notes), all on the same
    absolute-time axis (shared tempo map) so they line up when overlaid."""
    tempo_map = build_tempo_map(midi)  # compute once, reuse for every track
    return [
        (index, build_track_timeline(midi, index, tempo_map).notes)
        for index in track_indices
    ]
