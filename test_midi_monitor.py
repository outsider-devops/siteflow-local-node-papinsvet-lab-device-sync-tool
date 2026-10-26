"""CoreMIDI ABI regression checks; no hardware or MIDI transmission required."""
import ctypes
import struct
import unittest
from unittest.mock import patch

import midi_monitor as midi


class PacketCaptureTests(unittest.TestCase):
    def decode(self, architecture, packets):
        raw = bytearray(struct.pack('<I', len(packets)))
        for data in packets:
            raw.extend(struct.pack('<QH', 0, len(data)))
            raw.extend(data)
            if architecture == 'arm64':
                raw.extend(bytes((-len(raw)) % 4))
        memory = ctypes.create_string_buffer(bytes(raw))
        packet_list = ctypes.cast(memory, ctypes.POINTER(midi._MIDIPacketList))
        with patch.object(midi.platform, 'machine', return_value=architecture):
            return [event['bytes'] for event in midi._packet_events(packet_list, 'Traktor')]

    def test_apple_packet_struct_packing(self):
        self.assertEqual(midi._MIDIPacketList.packet.offset, 4)
        self.assertEqual(midi._MIDIPacket.length.offset, 8)
        self.assertEqual(midi._MIDIPacket.data.offset, 10)

    def test_intel_and_arm_packet_boundaries(self):
        packets = [[0xB0, 4, 0], [0xB0, 4, 127, 0x90, 60, 127],
                   [0xF8], [0x80, 60, 0, 0xC0, 7, 0xE0, 0, 64]]
        expected = [[0xB0, 4, 0], [0xB0, 4, 127], [0x90, 60, 127],
                    [0xF8], [0x80, 60, 0], [0xC0, 7], [0xE0, 0, 64]]
        for architecture in ('x86_64', 'arm64'):
            with self.subTest(architecture=architecture):
                self.assertEqual(self.decode(architecture, packets), expected)

    def test_variable_packet_can_exceed_declared_256_byte_array(self):
        messages = [[0xB0, 4, i] for i in range(120)]
        for architecture in ('x86_64', 'arm64'):
            with self.subTest(architecture=architecture):
                self.assertEqual(self.decode(architecture, [sum(messages, [])]), messages)

    def test_orphan_bytes_are_not_exposed_as_midi_commands(self):
        self.assertEqual(midi._packet_messages([0] * 64), [])
        self.assertEqual(midi._packet_messages([0, 0xB0, 4, 0, 0xB0]), [[0xB0, 4, 0]])


if __name__ == '__main__':
    unittest.main()
