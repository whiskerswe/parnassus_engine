"""Convert a practical subset of MusicXML into standard MIDI bytes."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree
import io
import zipfile

""" AI generated code to get started with something """
MIDI_TICKS_PER_QUARTER = 480
MUSICXML_NAMESPACE = "{http://www.musicxml.org/ns/container}"


@dataclass(frozen=True)
class MidiNote:
    """A note event in absolute MIDI ticks."""

    start: int
    end: int
    pitch: int
    velocity: int = 64
    channel: int = 0


@dataclass(frozen=True)
class TempoChange:
    """A tempo event in absolute MIDI ticks."""

    tick: int
    microseconds_per_quarter: int


@dataclass
class MidiTrack:
    """A named MIDI track with note and tempo events."""

    name: str
    notes: list[MidiNote] = field(default_factory=list)
    tempos: list[TempoChange] = field(default_factory=list)


@dataclass
class MidiFile:
    """In-memory MIDI representation produced by ``MusicXmlToMidiParser``."""

    tracks: list[MidiTrack]
    ticks_per_quarter: int = MIDI_TICKS_PER_QUARTER

    def to_bytes(self) -> bytes:
        track_chunks = [self._encode_track(track) for track in self.tracks]
        header = (
            b"MThd"
            + (6).to_bytes(4, "big")
            + (1).to_bytes(2, "big")
            + len(track_chunks).to_bytes(2, "big")
            + self.ticks_per_quarter.to_bytes(2, "big")
        )
        return header + b"".join(track_chunks)

    def write(self, path: str | Path) -> None:
        Path(path).write_bytes(self.to_bytes())

    def _encode_track(self, track: MidiTrack) -> bytes:
        events: list[tuple[int, int, bytes]] = []
        if track.name:
            name = track.name.encode("utf-8")
            events.append((0, 0, b"\xff\x03" + _variable_length_quantity(len(name)) + name))

        for tempo in track.tempos:
            payload = tempo.microseconds_per_quarter.to_bytes(3, "big")
            events.append((tempo.tick, 1, b"\xff\x51\x03" + payload))

        events.append((0, 2, bytes([0xC0, 0])))

        for note in track.notes:
            velocity = max(1, min(127, note.velocity))
            channel = max(0, min(15, note.channel))
            pitch = max(0, min(127, note.pitch))
            events.append((note.start, 4, bytes([0x90 | channel, pitch, velocity])))
            note_off_tick = max(note.start, note.end - 1)
            events.append((note_off_tick, 3, bytes([0x90 | channel, pitch, 0])))

        events.sort(key=lambda event: (event[0], event[1], event[2]))

        stream = bytearray()
        previous_tick = 0
        for tick, _priority, payload in events:
            stream.extend(_variable_length_quantity(tick - previous_tick))
            stream.extend(payload)
            previous_tick = tick

        stream.extend(b"\x00\xff\x2f\x00")
        return b"MTrk" + len(stream).to_bytes(4, "big") + bytes(stream)


class MusicXmlToMidiParser:
    """Parse MusicXML score files into a MIDI representation.

    The parser intentionally focuses on deterministic score playback data:
    pitches, rests, chords, voices, staff splits, ties, tempo directions, and
    dynamics. It accepts plain MusicXML files and compressed ``.mxl`` archives.
    """

    def parse_file(self, path: str | Path) -> MidiFile:
        return self.parse_bytes(Path(path).read_bytes())

    def parse_bytes(self, data: bytes) -> MidiFile:
        root = ElementTree.fromstring(_extract_musicxml(data))
        return self.parse_element(root)

    def parse_element(self, root: ElementTree.Element) -> MidiFile:
        parts = root.findall("part")
        tracks_by_staff: dict[str, MidiTrack] = {}

        for part in parts:
            self._parse_part(part, tracks_by_staff)

        tracks = [tracks_by_staff[key] for key in sorted(tracks_by_staff, key=_staff_sort_key)]
        if not tracks:
            tracks = [MidiTrack(name="MusicXML")]
        return MidiFile(tracks=tracks)

    def to_midi_bytes(self, path: str | Path) -> bytes:
        return self.parse_file(path).to_bytes()

    def _parse_part(self, part: ElementTree.Element, tracks_by_staff: dict[str, MidiTrack]) -> None:
        divisions = 1
        measure_start_divisions = 0
        active_ties: dict[tuple[str, int, str], tuple[int, int]] = {}
        current_velocity = 64
        part_name = part.get("id", "Part")
        part_tempos: list[TempoChange] = []

        for measure in part.findall("measure"):
            cursor = measure_start_divisions
            measure_max = measure_start_divisions
            previous_start_by_voice: dict[str, int] = {}
            previous_staccato_by_voice: dict[str, bool] = {}

            for element in measure:
                if element.tag == "attributes":
                    divisions_text = _text(element, "divisions")
                    if divisions_text:
                        divisions = int(divisions_text)
                elif element.tag == "direction":
                    current_velocity = _direction_velocity(element, current_velocity)
                    tempo = _direction_tempo(element)
                    if tempo is not None:
                        tick = _to_ticks(cursor, divisions)
                        part_tempos.append(TempoChange(tick, tempo))
                elif element.tag == "backup":
                    cursor -= int(_text(element, "duration", "0"))
                elif element.tag == "forward":
                    duration = int(_text(element, "duration", "0"))
                    cursor += duration
                    measure_max = max(measure_max, cursor)
                elif element.tag == "note":
                    voice = _text(element, "voice", "1")
                    duration = int(_text(element, "duration", "0"))
                    is_chord = element.find("chord") is not None
                    start = previous_start_by_voice.get(voice, cursor) if is_chord else cursor
                    staccato = _has_staccato(element) or (
                        is_chord and previous_staccato_by_voice.get(voice, False)
                    )
                    sounding_duration = max(1, duration // 2) if staccato else duration
                    end = start + sounding_duration

                    if element.find("rest") is None:
                        staff = _text(element, "staff", "1")
                        track = tracks_by_staff.setdefault(staff, MidiTrack(f"{part_name} Staff {staff}"))
                        pitch = _midi_pitch(element)
                        tie_types = {tie.get("type") for tie in element.findall("tie")}
                        tie_key = (staff, pitch, voice)

                        if "stop" in tie_types and tie_key in active_ties:
                            tied_start, tied_velocity = active_ties[tie_key]
                            if "start" in tie_types:
                                active_ties[tie_key] = (tied_start, tied_velocity)
                            else:
                                track.notes.append(
                                    MidiNote(
                                        start=_to_ticks(tied_start, divisions),
                                        end=_to_ticks(end, divisions),
                                        pitch=pitch,
                                        velocity=tied_velocity,
                                    )
                                )
                                del active_ties[tie_key]
                        elif "start" in tie_types:
                            active_ties[tie_key] = (start, current_velocity)
                        else:
                            track.notes.append(
                                MidiNote(
                                    start=_to_ticks(start, divisions),
                                    end=_to_ticks(end, divisions),
                                    pitch=pitch,
                                    velocity=current_velocity,
                                )
                            )

                    if not is_chord:
                        previous_start_by_voice[voice] = start
                        previous_staccato_by_voice[voice] = staccato
                        cursor += duration
                    measure_max = max(measure_max, cursor, start + duration)

            measure_start_divisions = measure_max

        for staff, pitch, _voice in list(active_ties):
            start, velocity = active_ties[(staff, pitch, _voice)]
            track = tracks_by_staff.setdefault(staff, MidiTrack(f"{part_name} Staff {staff}"))
            track.notes.append(
                MidiNote(
                    start=_to_ticks(start, divisions),
                    end=_to_ticks(measure_start_divisions, divisions),
                    pitch=pitch,
                    velocity=velocity,
                )
            )

        for track in tracks_by_staff.values():
            track.tempos.extend(part_tempos)


def _extract_musicxml(data: bytes) -> bytes:
    if not zipfile.is_zipfile(io.BytesIO(data)):
        return data

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        root_file = _root_file_from_container(archive)
        return archive.read(root_file)


def _root_file_from_container(archive: zipfile.ZipFile) -> str:
    try:
        container = ElementTree.fromstring(archive.read("META-INF/container.xml"))
    except KeyError:
        return next(name for name in archive.namelist() if name.endswith((".xml", ".musicxml")))

    root_file = container.find(f".//{MUSICXML_NAMESPACE}rootfile")
    if root_file is None:
        root_file = container.find(".//rootfile")
    if root_file is None or not root_file.get("full-path"):
        raise ValueError("Compressed MusicXML archive does not declare a root file")
    return root_file.get("full-path", "")


def _text(element: ElementTree.Element, path: str, default: str | None = None) -> str | None:
    child = element.find(path)
    return child.text if child is not None and child.text is not None else default


def _direction_tempo(direction: ElementTree.Element) -> int | None:
    sound = direction.find("sound")
    if sound is None or not sound.get("tempo"):
        return None

    bpm = float(sound.get("tempo", "120"))
    return round(60_000_000 / bpm)


def _direction_velocity(direction: ElementTree.Element, current_velocity: int) -> int:
    sound = direction.find("sound")
    if sound is not None and sound.get("dynamics"):
        return max(1, min(127, round(float(sound.get("dynamics", "64")) * 127 / 127)))
    return current_velocity


def _midi_pitch(note: ElementTree.Element) -> int:
    pitch = note.find("pitch")
    if pitch is None:
        raise ValueError("Cannot compute a MIDI pitch for a rest")

    step = _text(pitch, "step")
    octave = _text(pitch, "octave")
    if step is None or octave is None:
        raise ValueError("MusicXML pitch is missing step or octave")

    alter = int(float(_text(pitch, "alter", "0")))
    semitone = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}[step]
    return (int(octave) + 1) * 12 + semitone + alter


def _has_staccato(note: ElementTree.Element) -> bool:
    return note.find("notations/articulations/staccato") is not None


def _to_ticks(divisions_value: int, divisions: int) -> int:
    return round(divisions_value * MIDI_TICKS_PER_QUARTER / divisions)


def _variable_length_quantity(value: int) -> bytes:
    if value < 0:
        raise ValueError("MIDI delta times cannot be negative")

    buffer = value & 0x7F
    value >>= 7
    while value:
        buffer <<= 8
        buffer |= (value & 0x7F) | 0x80
        value >>= 7

    output = bytearray()
    while True:
        output.append(buffer & 0xFF)
        if buffer & 0x80:
            buffer >>= 8
        else:
            return bytes(output)


def _staff_sort_key(staff: str) -> tuple[int, str]:
    return (int(staff), staff) if staff.isdigit() else (999, staff)
