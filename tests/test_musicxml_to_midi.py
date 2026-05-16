from __future__ import annotations

from pathlib import Path

from parnassus_engine.musicxml_to_midi import MIDI_TICKS_PER_QUARTER, MusicXmlToMidiParser


RESOURCES = Path(__file__).parent / "resources"


def test_parser_reads_compressed_musicxml_and_writes_standard_midi() -> None:
    midi = MusicXmlToMidiParser().parse_file(RESOURCES / "wachterlied-grieg.mxl")
    midi_bytes = midi.to_bytes()

    assert len(midi.tracks) == 2
    assert sum(len(track.notes) for track in midi.tracks) == 517
    assert midi_bytes[:4] == b"MThd"
    assert int.from_bytes(midi_bytes[10:12], "big") == len(midi.tracks)
    assert int.from_bytes(midi_bytes[12:14], "big") == MIDI_TICKS_PER_QUARTER


def test_generated_midi_matches_reference_note_timeline() -> None:
    generated = MusicXmlToMidiParser().to_midi_bytes(RESOURCES / "wachterlied-grieg.mxl")
    reference = (RESOURCES / "wachterlied-grieg.mid").read_bytes()

    generated_notes = _notes_from_midi(generated)
    reference_notes = _notes_from_midi(reference)

    assert len(generated_notes) == len(reference_notes)
    for generated_note, reference_note in zip(generated_notes, reference_notes, strict=True):
        assert generated_note[:2] == reference_note[:2]
        assert abs(generated_note[2] - reference_note[2]) <= 1


def _notes_from_midi(data: bytes) -> list[tuple[int, int, int]]:
    track_count = int.from_bytes(data[10:12], "big")
    cursor = 8 + int.from_bytes(data[4:8], "big")
    notes: list[tuple[int, int, int]] = []

    for _ in range(track_count):
        assert data[cursor : cursor + 4] == b"MTrk"
        track_length = int.from_bytes(data[cursor + 4 : cursor + 8], "big")
        cursor += 8
        track_end = cursor + track_length
        absolute_tick = 0
        running_status: int | None = None
        active_notes: dict[tuple[int, int], list[int]] = {}

        while cursor < track_end:
            delta, cursor = _read_variable_length_quantity(data, cursor)
            absolute_tick += delta
            status = data[cursor]
            if status < 0x80:
                if running_status is None:
                    raise AssertionError("MIDI file used running status before a status byte")
                status = running_status
            else:
                cursor += 1
                running_status = status

            if status == 0xFF:
                cursor += 1
                payload_length, cursor = _read_variable_length_quantity(data, cursor)
                cursor += payload_length
                running_status = None
            elif status in (0xF0, 0xF7):
                payload_length, cursor = _read_variable_length_quantity(data, cursor)
                cursor += payload_length
                running_status = None
            else:
                command = status & 0xF0
                channel = status & 0x0F
                first_data_byte = data[cursor]
                cursor += 1
                second_data_byte = None
                if command not in (0xC0, 0xD0):
                    second_data_byte = data[cursor]
                    cursor += 1

                note_key = (channel, first_data_byte)
                if command == 0x90 and second_data_byte:
                    active_notes.setdefault(note_key, []).append(absolute_tick)
                elif command in (0x80, 0x90) and active_notes.get(note_key):
                    start = active_notes[note_key].pop(0)
                    notes.append((start, first_data_byte, absolute_tick + 1))

    return sorted(notes)


def _read_variable_length_quantity(data: bytes, cursor: int) -> tuple[int, int]:
    value = 0
    while True:
        byte = data[cursor]
        cursor += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, cursor
