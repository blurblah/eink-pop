"""목표 판정에서 무변경·재생·초기화·손상 로그를 구분하는 검증."""
import hashlib
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
import zlib
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'host'))
import analyze_painter_run as a
import painter_usb as p


class Tests(unittest.TestCase):
    def fixture(self,d,step=.1,same=False,version=5,patch=False):
        prefix=Path(d)/'run';wire=p.packet(p.START,12,0,b'\x0c',version=version);frames=[];previous=bytes(16000)
        for i in range(12):
            payload=bytes([1 if same else i+1])*16000
            if patch:
                payload=bytearray(previous);payload[i]=i+1;payload=bytes(payload)
            encoded=p.encode_patch(previous,payload) if patch and i else None
            blob=p.packet(p.PATCH if encoded is not None else p.FRAME,12,i+1,
                          encoded if encoded is not None else payload,version=version);wire+=blob
            previous=payload
            frames.append(dict(sequence=i+1,source_sequence=i+40,bitmap_sha256=hashlib.sha256(payload).hexdigest(),
                ack=dict(session='12',seq=str(i+1),crc=f'{zlib.crc32(blob[:-4])&0xffffffff:08x}',
                         result='unchanged' if same and i else 'applied',cycles=str(int(not same or i==0)),
                         clear_us=1000000 if i==0 else 0,rx_us=10000,decode_us=0,prepare_us=1000,paint_us=80000,
                         display_us=1081000 if i==0 else 81000,received_monotonic=100+i*step)))
        obs=dict(status='통과',serial_closed=True,after_done_seconds=10,session=12,input_mode='captured_replay',
                 frames=frames,done=dict(reason='limit',frames='12',applied='1' if same else '12',poweroff_returned='1'))
        Path(str(prefix)+'-tx.bin').write_bytes(wire)
        Path(str(prefix)+'-observation.json').write_text(json.dumps(obs))
        return prefix,obs
    def test_timing_initial_clear_excluded_and_mode_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            prefix,_=self.fixture(d,step=.09);r=a.analyze(prefix)
            self.assertTrue(r['meets_single_run_timing']);self.assertEqual(r['input_mode'],'captured_replay')
            self.assertAlmostEqual(r['ack_interval_ms']['mean'],90);self.assertEqual(r['ack_interval_ms']['count'],11)
            self.assertEqual(r['first_display_ms'],1081)
    def test_slow_or_unchanged_cannot_pass(self):
        for step,same in [(.13,False),(.01,True)]:
            with self.subTest(step=step,same=same),tempfile.TemporaryDirectory() as d:
                prefix,_=self.fixture(d,step,same);self.assertFalse(a.analyze(prefix)['meets_single_run_timing'])
    def test_old_v4_and_patched_v5(self):
        for version,patch in [(4,False),(5,True)]:
            with self.subTest(version=version),tempfile.TemporaryDirectory() as d:
                prefix,_=self.fixture(d,step=.09,version=version,patch=patch)
                result=a.analyze(prefix);self.assertTrue(result['meets_single_run_timing'])
                if patch:self.assertLess(result['wire_bytes'],17000)
    def test_driver_timing_validation(self):
        with tempfile.TemporaryDirectory() as d:
            prefix,obs=self.fixture(d,step=.09,patch=True)
            obs['timing_diagnostics']=True
            for f in obs['frames']:
                f['driver_timing']=dict(seq=f['sequence'],cycle=f['sequence'],passes=19,
                    discovery_us=1000,rowloop_us=1000,settle_us=76000,neutral_us=1000,total_us=79000)
            path=Path(str(prefix)+'-observation.json');path.write_text(json.dumps(obs))
            r=a.analyze(prefix);self.assertEqual(r['driver_timing']['passes'],[19]*11)
            obs['frames'][1]['driver_timing']['total_us']=81000;path.write_text(json.dumps(obs))
            with self.assertRaises(ValueError):a.analyze(prefix)
    def test_corrupt_packet_and_early_done(self):
        for fault in ('crc','truncated','sequence','no_off','quiet','cycle','backward','nan'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as d:
                prefix,obs=self.fixture(d)
                tx=Path(str(prefix)+'-tx.bin');raw=tx.read_bytes()
                if fault=='crc':tx.write_bytes(raw[:-1]+bytes([raw[-1]^1]))
                elif fault=='truncated':tx.write_bytes(raw[:-1])
                elif fault=='sequence':obs['frames'][1]['sequence']=9
                elif fault=='no_off':obs['done']['poweroff_returned']='0'
                elif fault=='quiet':obs['after_done_seconds']=1
                elif fault=='cycle':obs['frames'][1]['ack']['cycles']='0'
                elif fault=='backward':obs['frames'][1]['ack']['received_monotonic']=99
                elif fault=='nan':obs['frames'][1]['ack']['received_monotonic']=math.nan
                Path(str(prefix)+'-observation.json').write_text(json.dumps(obs))
                with self.assertRaises(ValueError):a.analyze(prefix)
    def test_fast_speed_does_not_meet_normal_target(self):
        with tempfile.TemporaryDirectory() as d:
            prefix,obs=self.fixture(d,step=.09);obs['quality']='FAST'
            Path(str(prefix)+'-observation.json').write_text(json.dumps(obs))
            r=a.analyze(prefix);self.assertTrue(r['meets_numeric_timing'])
            self.assertFalse(r['meets_single_run_timing']);self.assertEqual(r['quality'],'FAST')

    def test_nearest_rank(self):
        r=a.distribution(list(range(1,12)));self.assertEqual(r['p95'],11)

    def test_host_times_and_source_skips(self):
        for fault in (None,'reversed','missing','nan'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as d:
                prefix,obs=self.fixture(d,step=.09)
                for i,f in enumerate(obs['frames']):
                    end=f['ack']['received_monotonic'];f['source_sequence']=40+3*i
                    f.update(read_started_monotonic=end-(1 if i==0 else .085),
                             read_finished_monotonic=end-.08,
                             transmission=dict(send_started_monotonic=end-.079,send_finished_monotonic=end-.078))
                if fault=='reversed':obs['frames'][1]['read_finished_monotonic']=999
                elif fault=='missing':del obs['frames'][1]['transmission']
                elif fault=='nan':obs['frames'][1]['read_started_monotonic']=math.nan
                Path(str(prefix)+'-observation.json').write_text(json.dumps(obs))
                if fault:
                    with self.assertRaises(ValueError):a.analyze(prefix)
                else:
                    r=a.analyze(prefix);self.assertEqual(r['skipped_source_frames'],22)
                    self.assertEqual(r['source_steps'],[3]*11)
                    self.assertAlmostEqual(r['host_timing']['read_to_ack_ms']['mean'],85)
                    self.assertAlmostEqual(r['host_timing']['first_read_to_ack_ms'],1000)


if __name__=='__main__':unittest.main()
