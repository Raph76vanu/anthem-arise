import struct
import unittest

from game_asset_explorer.frostbite_sequence import inspect_eclipse_sequences
from game_asset_explorer.geometry import MeshFormatError


def _block(type_hash, size, key):
    raw = bytearray(size)
    raw[:8] = b"GD.DATAl"
    struct.pack_into("<I", raw, 8, size)
    struct.pack_into("<I", raw, 32, type_hash)
    raw[72:80] = key
    return raw


class SequenceReferenceTests(unittest.TestCase):
    def test_resolves_authored_slots_and_rejects_broken_key(self):
        controller_key, curve_key, init_key = b"CONTROL!", b"BLENDKEY", b"INITKEY!"
        controller = _block(0x2FA96633, 192, controller_key)
        controller[112:122] = b"EXM_idle\0\0"
        curve = _block(0x0F66838D, 188, curve_key)
        struct.pack_into("<f", curve, 0xA8, 8.0)
        init = _block(0xA30D82B5, 112, init_key)
        sequence = _block(0x9089AB84, 0x250, b"SEQUENCE")
        sequence[0x88:0x96] = b"SEQ_test\0" + bytes(5)
        struct.pack_into("<IIQ", sequence, 0x1A0, 1, 1, 0x1B0)
        struct.pack_into("<Q", sequence, 0x1C0, 0x1C8)
        struct.pack_into("<I", sequence, 0x1D8 + 16, 0x6D77D289)
        sequence[0x1F8:0x210] = controller_key + curve_key + init_key
        raw = bytes(controller + curve + init + sequence)
        decoded = inspect_eclipse_sequences(raw)
        self.assertEqual(decoded[0].name, "SEQ_test")
        self.assertEqual(decoded[0].slots[0].controller, "EXM_idle")
        self.assertEqual(decoded[0].slots[0].curve_scalar, 8.0)
        sequence[0x200:0x208] = b"MISSING!"
        with self.assertRaisesRegex(MeshFormatError, "missing asset"):
            inspect_eclipse_sequences(bytes(controller + curve + init + sequence))


if __name__ == "__main__":
    unittest.main()
