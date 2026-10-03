"""진단 입력이12개 전이와 동일 목표 참조를 실제 전송 경로에서 보존하는지 검사."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'host'))
from compare_captures import load_capture
from painter_usb import capture_reader
from prepare_painter_clip import unpack
from prepare_painter_transitions import prepare


class TransitionsTest(unittest.TestCase):
    def test_wire_pixels_and_all_transitions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'capture'
            manifest = prepare(path, 6)
            self.assertEqual(len(load_capture(path)), 12)
            reader = capture_reader(path, 1, 12)
            frames = [unpack(reader(None)[1]) for _ in range(12)]
            expected_changes = set()
            for entry in manifest['regions']:
                source, target = entry['source'], entry['target']
                for name in ('reference', 'probe'):
                    x, y, w, h = entry[name]
                    indices = {row * 320 + col for row in range(y, y+h) for col in range(x, x+w)}
                    if name == 'probe' and source != target:
                        expected_changes.update(indices)
                    for index, frame in enumerate(frames):
                        expected = source if name == 'probe' and index % 2 == 0 else target
                        self.assertEqual({frame[i] for i in indices}, {expected})
            for before, after in zip(frames, frames[1:]):
                changed = {i for i, (a, b) in enumerate(zip(before, after)) if a != b}
                self.assertEqual(changed, expected_changes)
                counts = {(a, b): 0 for a in range(4) for b in range(4) if a != b}
                for i in changed:
                    counts[before[i], after[i]] += 1
                self.assertEqual(set(counts.values()), {24 * 24})

    def test_limits_and_preserve_existing_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for value in (0, 7, True, 1.5):
                with self.assertRaises(ValueError):
                    prepare(root / 'invalid', value)
            self.assertFalse((root / 'invalid').exists())
            prepare(root / 'capture')
            with self.assertRaises(FileExistsError):
                prepare(root / 'capture')


if __name__ == '__main__':
    unittest.main()
