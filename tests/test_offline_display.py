#!/usr/bin/env python3
"""오프라인 입력 검증·시간 모델·보고서 회귀. 장치와 원본 비공개 로그에 의존하지 않는다."""
import base64
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("offline", ROOT / "host/offline_display.py")
offline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(offline)


def packet(kind, sequence, payload):
    data = struct.pack("<4sBBHIII", b"EPK1", 3, kind, 0, 77, sequence, len(payload)) + payload
    return data + struct.pack("<I", zlib.crc32(data)&0xffffffff)


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.prefix = self.directory / "session"
        self.profile = json.loads(offline.PROFILE.read_text())
        images = [bytes(8000), b"\x80"+bytes(7999), b"\x01"+bytes(7999)]
        self.wire = packet(1, 0, b"\x03")
        records=[]
        for index, data in enumerate(images, 1):
            wire = packet(2, index, data)
            self.wire += wire
            records.append({"source_sequence": index*12, "bitmap_sha256": hashlib.sha256(data).hexdigest(),
                "ack": {"session":"77", "seq":str(index), "crc":f"{zlib.crc32(wire[:-4])&0xffffffff:08x}",
                        "result":"applied", "rx_us":"10", "display_us":"200",
                        "clear_us":"100" if index==1 else "0", "white_us":"50" if index==3 else "0",
                        "black_us":"50" if index>1 else "0", "elapsed_us":str(index*1000)}})
        config=f"USB_FRAME_STATS metric=config driver={self.profile['driver_commit']} source=320x200 bpp=1 msb_first=1 black=1 white=0 scale=2 x=160 y=70 refresh=initial_clear_then_delta"
        self.observation={"status":"통과", "protocol_version":3, "session":77, "frames":records,
                          "responses":[{"line":config}],
                          "done":{"session":"77","reason":"limit","frames":"3","applied":"3"}}
        self.save()

    def tearDown(self):
        self.temp.cleanup()

    def save(self):
        Path(str(self.prefix)+"-tx.bin").write_bytes(self.wire)
        Path(str(self.prefix)+"-observation.json").write_text(json.dumps(self.observation))

    def load(self):
        return offline.load_session(self.prefix, self.profile["driver_commit"])

    def test_binary_transitions_and_edge_rows(self):
        old=bytearray(8000);new=bytearray(8000)
        old[0]=128;new[-1]=1
        d=offline.transitions(old,new)
        self.assertEqual((d['white_pixels'],d['black_pixels'],d['physical_rows']),(1,1,400))
        self.assertEqual((d['first_row'],d['last_row']),(0,199))
        self.assertEqual(offline.transitions(new,new)['physical_rows'],0)
        with self.assertRaises(ValueError):offline.transitions(b'',new)

    def test_known_linear_fit(self):
        samples=[{'rows':x,'display_us':500000+x*1000} for x in (10,20,30)]
        model=offline.fit(samples)
        self.assertAlmostEqual(offline.predict(model,25),525000)
        with self.assertRaises(ValueError):offline.fit([samples[0]]*3)

    def test_real_profile_holdout_and_budget(self):
        model=offline.timing_model(self.profile)
        self.assertEqual(model['observed_bands'],[[30,34],[98,102]])
        self.assertEqual((model['holdout']['training'],model['holdout']['validation']),(6,5))
        self.assertLess(model['holdout']['max_error_us'],5000)
        self.assertGreater(model['measured_display_us'],600000)
        self.assertAlmostEqual(model['measured_interval_us']-model['pause_us'],764817.7272727273)

    def test_prediction_distinguishes_interpolation_and_unsupported(self):
        model=offline.timing_model(self.profile)
        self.assertEqual(offline.estimate(model,32)['status'],'실측 범위 내 추정')
        self.assertIn('미검증',offline.estimate(model,64)['status'])
        for rows in (0,29,103,400):self.assertIsNone(offline.estimate(model,rows)['display_us'])
        self.assertIsNone(offline.estimate(model,32,white=False)['display_us'])
        for rows in (-1,401,1.5,True):
            with self.assertRaises(ValueError):offline.estimate(model,rows)

    def test_profile_errors(self):
        for key,value in [('rows',401),('white_us',0),('display_us',1),('rx_us',-2)]:
            p=copy.deepcopy(self.profile);p['samples'][0][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):offline.timing_model(p)

    def test_packet_to_final_image(self):
        frames=self.load()
        self.assertEqual(len(frames),3)
        self.assertEqual(frames[0]['physical_rows'],0)
        self.assertEqual(frames[1]['black_pixels'],1)
        self.assertEqual((frames[2]['white_pixels'],frames[2]['black_pixels']),(1,1))
        self.assertEqual(frames[2]['interval_us'],1000)
        self.assertEqual(base64.b64decode(frames[2]['payload']),b'\x01'+bytes(7999))

    def test_corrupt_and_truncated_wire(self):
        original=self.wire
        for wire in (original[:-1],original+b'x',original[:10],original[:-1]+bytes([original[-1]^1])):
            self.wire=wire;self.save()
            with self.assertRaises(ValueError):self.load()

    def test_old_version_and_oversized_rejected(self):
        self.wire=self.wire[:4]+b'\x02'+self.wire[5:];self.save()
        with self.assertRaises(ValueError):self.load()
        self.wire=bytes(100000);self.save()
        with self.assertRaises(ValueError):self.load()

    def test_record_mismatch(self):
        original=copy.deepcopy(self.observation)
        for key,value in [('bitmap_sha256','bad'),('source_sequence',-1)]:
            self.observation=copy.deepcopy(original);self.observation['frames'][1][key]=value;self.save()
            with self.assertRaises(ValueError):self.load()
        for key,value in [('seq','5'),('crc','00000000'),('clear_us','1'),('display_us','1'),('elapsed_us','1'),('result','unchanged')]:
            self.observation=copy.deepcopy(original);self.observation['frames'][1]['ack'][key]=value;self.save()
            with self.subTest(key=key),self.assertRaises(ValueError):self.load()

    def test_incompatible_driver_never_uses_model(self):
        self.observation['responses'][0]['line']=self.observation['responses'][0]['line'].replace('scale=2','scale=3');self.save()
        with self.assertRaisesRegex(ValueError,'설정'):self.load()

    def test_done_count_rejected(self):
        self.observation['done']['frames']='2';self.save()
        with self.assertRaises(ValueError):self.load()

    def test_report_is_self_contained_and_never_overwrites(self):
        out=self.directory/'report'
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(offline.main(['--log-prefix',str(self.prefix),'--output',str(out)]),0)
        html=(out/'index.html').read_text()
        self.assertNotIn('__REPORT_DATA__',html)
        self.assertNotIn('src="http',html)
        self.assertNotIn(str(self.prefix),html)
        self.assertIn('예측 불가',html)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(offline.main(['--log-prefix',str(self.prefix),'--output',str(out)]),1)
        self.assertEqual((out/'index.html').read_text(),html)


if __name__=='__main__':unittest.main(verbosity=2)
