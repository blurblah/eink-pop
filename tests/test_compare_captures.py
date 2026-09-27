#!/usr/bin/env python3
import base64
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'host'))
import compare_captures as compare
import usb_frame


def ppm(sequence,rgb):
    return f'P6\n# eink sequence {sequence}\n320 200\n255\n'.encode()+rgb


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.capture=self.root/'capture';self.capture.mkdir()
        self.entries=[]
        for n,seq in enumerate((2,4)):
            raw=ppm(seq,bytes([255])*192000);name=f'frame-{n+1:04d}.ppm'
            (self.capture/name).write_bytes(raw)
            self.entries.append({'file':name,'source_sequence':seq,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'captured_seconds':n*.1,'skipped_before':n})
        self.manifest={'schema':1,'status':'완료','frames':self.entries};self.save()

    def save(self):
        (self.capture/'capture.json').write_text(json.dumps(self.manifest))

    def tearDown(self):self.temp.cleanup()

    def test_solid_black_and_white(self):
        for value,expected in ((0,b'\xff'*8000),(255,b'\0'*8000)):
            for result in compare.convert(bytes([value])*192000).values():self.assertEqual(result,expected)
        with self.assertRaises(ValueError):compare.convert(b'')

    def test_existing_bayer4_matches_production_and_hp(self):
        rgb=bytearray(bytes(range(256))*750)
        for x,color in ((0,(228,0,0)),(1,(184,0,0))):
            at=(194*320+x)*3;rgb[at:at+3]=bytes(color)
        file=self.root/'palette.ppm';file.write_bytes(ppm(1,rgb))
        _,expected,_=usb_frame.read_frame(file)
        actual=compare.convert(rgb)
        self.assertEqual(actual['bayer4'],expected)
        for mode in compare.MODES:self.assertFalse(actual[mode][194*40]&128)

    def test_midtones_have_expected_coverage(self):
        results=compare.convert(bytes([112])*192000) # corrected gray=8
        self.assertEqual(sum(b.bit_count() for b in results['bayer2']),32000)
        self.assertEqual(results['threshold'],bytes(8000))
        self.assertNotEqual(results['bayer2'],results['bayer4'])

    def test_png_round_trip(self):
        rgb=bytes(range(256))*750
        raw=base64.b64decode(compare.png(rgb).split(',')[1]);self.assertEqual(raw[:8],b'\x89PNG\r\n\x1a\n')
        at=8;compressed=b''
        while at<len(raw):
            length,=struct.unpack_from('>I',raw,at);kind=raw[at+4:at+8];data=raw[at+8:at+8+length]
            crc,=struct.unpack_from('>I',raw,at+8+length);self.assertEqual(crc,zlib.crc32(kind+data)&0xffffffff)
            if kind==b'IDAT':compressed+=data
            at+=length+12
        scan=zlib.decompress(compressed);self.assertEqual(b''.join(scan[y*961+1:(y+1)*961] for y in range(200)),rgb)

    def test_manifest_integrity_and_path_boundaries(self):
        for key,value in (('file','../escape.ppm'),('sha256','bad'),('bytes',1),('source_sequence',9),('captured_seconds',float('nan')),('captured_seconds',-1),('skipped_before',5)):
            with self.subTest(key=key,value=value):
                old=self.entries[1][key];self.entries[1][key]=value;self.save()
                with self.assertRaises(ValueError):compare.load_capture(self.capture)
                self.entries[1][key]=old
        self.save()
        target=self.capture/self.entries[1]['file'];target.unlink();target.symlink_to(self.capture/self.entries[0]['file'])
        with self.assertRaisesRegex(ValueError,'심볼릭'):compare.load_capture(self.capture)

    def test_invalid_or_incomplete_capture(self):
        for key,value in (('status','오류'),('schema',2),('frames',[])):
            old=self.manifest[key];self.manifest[key]=value;self.save()
            with self.assertRaises(ValueError):compare.load_capture(self.capture)
            self.manifest[key]=old

    def test_statistics_do_not_invent_time_for_unchanged_frames(self):
        report=compare.build_report(self.capture)
        self.assertEqual(report['skipped'],1)
        for mode,s in report['summary'].items():
            self.assertEqual(s['transitions'],1);self.assertEqual(s['unchanged'],1)
            self.assertEqual(s['supported'],0);self.assertIsNone(s['supported_mean_us'])
            self.assertEqual(s['mean_changed_pixels'],0)
        single=compare.build_report(self.capture,4,4)
        self.assertEqual(single['skipped'],0);self.assertEqual(len(single['frames']),1)
        self.assertIsNone(single['summary']['bayer4']['mean_changed_pixels'])
        for start,end in ((0,None),(4,2),(5,None)):
            with self.assertRaises(ValueError):compare.build_report(self.capture,start,end)

    def test_black_coverage_is_not_black_transition_count(self):
        for entry in self.entries:
            raw=ppm(entry['source_sequence'],bytes(192000))
            (self.capture/entry['file']).write_bytes(raw)
            entry['sha256']=hashlib.sha256(raw).hexdigest()
        self.save()
        report=compare.build_report(self.capture)
        for mode in compare.MODES:
            self.assertEqual(report['summary'][mode]['mean_black_percent'],100)
            self.assertEqual(report['frames'][1]['modes'][mode]['black_pixels'],0)

    def test_self_contained_cli_and_no_overwrite(self):
        out=self.root/'report'
        args=['--capture',str(self.capture),'--output',str(out),'--start-sequence','4']
        self.assertEqual(compare.main(args),0)
        original=(out/'index.html').read_bytes()
        self.assertNotIn(b'__REPORT_DATA__',original)
        self.assertEqual(compare.main(args),1)
        self.assertEqual((out/'index.html').read_bytes(),original)


if __name__=='__main__':unittest.main(verbosity=2)
