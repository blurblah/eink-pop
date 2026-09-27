#!/usr/bin/env python3
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('capture_frames',ROOT/'host/capture_frames.py')
capture=importlib.util.module_from_spec(spec);spec.loader.exec_module(capture)


def ppm(sequence,color=0):
    return f'P6\n# eink sequence {sequence}\n320 200\n255\n'.encode()+bytes([color])*192000


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.source=self.root/'latest.ppm';self.out=self.root/'frames';self.now=0
        self.source.write_bytes(ppm(1))

    def tearDown(self):self.temp.cleanup()

    def run_capture(self,on_sleep=lambda:None,seconds=.05,max_frames=10):
        def sleep(value):
            self.now+=value;on_sleep()
        with patch.object(capture.time,'monotonic',side_effect=lambda:self.now),patch.object(capture.time,'sleep',side_effect=sleep):
            return capture.capture(self.source,self.out,seconds,max_frames)

    def test_duplicate_is_not_saved_again(self):
        r=self.run_capture()
        self.assertEqual(len(r['frames']),1);self.assertEqual(r['reason'],'time_limit')
        self.assertEqual((self.out/'frame-0001.ppm').read_bytes(),ppm(1))

    def test_atomic_replacement_and_sequence_gaps(self):
        def replace():
            p=self.root/'new.tmp';p.write_bytes(ppm(4,255));p.replace(self.source)
        r=self.run_capture(replace,max_frames=2)
        self.assertEqual(r['reason'],'frame_limit')
        self.assertEqual([f['source_sequence'] for f in r['frames']],[1,4])
        self.assertEqual(r['frames'][1]['skipped_before'],2)
        self.assertEqual((self.out/'frame-0002.ppm').read_bytes(),ppm(4,255))

    def test_sequence_restart_and_same_number_change(self):
        for raw in (ppm(2,255),ppm(1),ppm(0)):
            with self.subTest(raw=raw[:40]):
                self.source.write_bytes(ppm(2))
                self.out=self.root/str(len(list(self.root.iterdir())))
                with self.assertRaises(ValueError):self.run_capture(lambda:self.source.write_bytes(raw))
                self.assertEqual(json.loads((self.out/'capture.json').read_text())['status'],'오류')
                self.source.write_bytes(ppm(1));self.now=0

    def test_existing_directory_is_preserved(self):
        self.out.mkdir();(self.out/'keep').write_text('사용자 파일')
        with self.assertRaises(FileExistsError):self.run_capture()
        self.assertEqual((self.out/'keep').read_text(),'사용자 파일')

    def test_missing_and_malformed_source(self):
        self.source.unlink()
        with self.assertRaisesRegex(ValueError,'수집하지'):self.run_capture()
        self.out=self.root/'invalid';self.now=0;self.source.write_bytes(ppm(1)[:-1])
        with self.assertRaisesRegex(ValueError,'길이'):self.run_capture()

    def test_limits(self):
        for seconds,count in ((0,1),(31,1),(float('nan'),1),(1,0),(1,361)):
            with self.subTest(seconds=seconds,count=count),self.assertRaises(ValueError):
                capture.capture(self.source,self.out,seconds,count)
        self.assertFalse(self.out.exists())


if __name__=='__main__':unittest.main(verbosity=2)
