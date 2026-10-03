"""무손실 복원·길이 경계·잘린 데이터와 복원 폭주 방지 검증."""
from pathlib import Path
import random
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'host'))
from painter_rle import SIZE, encode, decode


class Tests(unittest.TestCase):
    def test_every_byte_and_run_boundaries(self):
        for value in range(256):
            data = bytes([value]) * SIZE
            encoded = encode(data)
            self.assertEqual(len(encoded), 250)
            self.assertEqual(decode(encoded), data)
        for length in (1, 2, 3, 127, 128, 129, 255, 256):
            prefix = bytes(range(256))[:length] if length <= 256 else b''
            data = prefix + b'\x71' * length + b'\xff' * (SIZE - len(prefix) - length)
            self.assertEqual(decode(encode(data)), data)

    def test_literal_fallback_and_mixed_input(self):
        rng = random.Random(716)
        data = bytes(rng.randrange(256) for _ in range(SIZE))
        self.assertIsNone(encode(data))
        for _ in range(20):
            data = bytearray()
            while len(data) < SIZE:
                part = bytes(rng.randrange(256) for _ in range(rng.randint(1, 128)))
                data.extend(part + bytes([rng.randrange(256)]) * rng.randint(1, 256))
            data = bytes(data[:SIZE])
            self.assertEqual(decode(encode(data)), data)

    def test_invalid_streams(self):
        valid = encode(b'\xff' * SIZE)
        for size in range(len(valid)):
            with self.subTest(size=size), self.assertRaises(ValueError):
                decode(valid[:size])
        for data in (b'\xff', b'\x01x', b'\x80x', b'\xffx' * 126,
                     valid + b'\x80x', bytes(SIZE)):
            with self.subTest(data=data[:8]), self.assertRaises(ValueError):
                decode(data)
        for size in (0, SIZE - 1, SIZE + 1):
            with self.assertRaises(ValueError):
                encode(bytes(size))


if __name__ == '__main__':
    unittest.main()
