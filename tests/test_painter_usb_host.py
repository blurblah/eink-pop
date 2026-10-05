"""기존 직렬 I/O 위의 v4 상태·CRC·최신 읽기·종료 계약 검증."""
import json
import hashlib
from pathlib import Path
import struct
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zlib
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'host'))
import painter_usb as p
from painter_patch import decode as decode_patch
from painter_rle import decode as decode_rle
from painter_lz import decode as decode_lz

class Peer:
    def __init__(self, fault='', stop=False, diagnostics=False, quality='NORMAL', white_pair=False,black_pair=0):
        self.fault=fault; self.pending=bytearray(); self.queue=[]; self.sent=[]
        self.diagnostics=diagnostics
        self.acks=0;self.n=0;self.applied=0;self.total=2;self.previous=bytes(16000)
        ready=p.READY.copy()
        ready['timing']=str(int(diagnostics));ready['quality']=quality
        if black_pair and fault!='black_missing' or fault=='black_unexpected':ready['black']='pair2'
        if white_pair and fault!='white_missing' or fault=='white_unexpected':ready['white']='pair2' if white_pair==2 else 'pair'
        if fault=='ready':ready['version']='3'
        if fault=='old_ready':ready['version']='6'
        if fault=='lz_missing':ready.pop('lz')
        if fault=='lz_disabled':ready['lz']='0'
        if fault=='rle_missing':ready.pop('rle')
        if fault=='rle_disabled':ready['rle']='0'
        self.emit('READY',ready)
    def emit(self,marker,v):self.queue.append('PAINTER_USB_'+marker+' '+' '.join(f'{k}={x}' for k,x in v.items()))
    def line(self,deadline):
        if not self.queue:
            if self.fault=='partial': self.pending.extend(b'x')
            raise p.ResponseTimeout('시험 마감')
        line=self.queue.pop(0)
        if line.startswith('PAINTER_USB_ACK'):self.acks+=1
        return line,dict(received_monotonic=time.monotonic(),line=line)
    def write_all(self,wire,deadline):
        self.sent.append(wire)
        magic,version,kind,flags,session,seq,n=p.HEADER.unpack(wire[:20])
        assert magic==b'EPK1' and version==7 and flags==0 and n==len(wire)-24
        assert zlib.crc32(wire[:-4])&0xffffffff==struct.unpack('<I',wire[-4:])[0]
        if kind==p.START:
            self.total=wire[20]
            self.emit('ACCEPT',dict(session=session+(self.fault=='session'),max_frames=self.total+(self.fault=='limit')))
            self.emit('RX',dict(session=session))
        elif kind==p.STOP:
            self.emit('DONE',dict(session=session,reason='stop',frames=self.n,applied=self.applied,poweroff_returned=int(self.n>0)))
        else:
            assert self.acks==self.n
            self.n+=1;payload=wire[20:-4]
            if kind==p.PATCH:payload=decode_patch(self.previous,payload)
            elif kind==p.RLE:payload=decode_rle(payload)
            elif kind==p.LZ:payload=decode_lz(payload)
            changed=payload!=self.previous
            v=dict(session=session,seq=seq,crc=f'{zlib.crc32(wire[:-4])&0xffffffff:08x}',result='applied' if self.n==1 or changed else 'unchanged',rx_us=100,decode_us=0,display_us=400,clear_us=100 if self.n==1 else 0,prepare_us=10 if changed else 0,paint_us=200 if changed else 0,cycles=int(changed),elapsed_us=self.n*400)
            self.applied+=self.n==1 or changed
            self.previous=payload
            if self.fault=='crc':v['crc']='00000000'
            if self.fault=='sequence':v['seq']=99
            if self.fault=='cycle':v['cycles']=0
            if self.fault=='timing':v['paint_us']=1000
            if self.fault=='initial_clear':v['clear_us']=0
            if self.fault=='repeat_clear' and self.n==2:v['clear_us']=100
            if self.fault=='result':v['result']='unchanged'
            if self.fault=='reboot':self.queue.append('ESP-ROM: reboot')
            if self.fault=='error':self.emit('ERROR',dict(reason='paint_timeout'))
            if self.diagnostics and changed and self.fault!='timing_missing':
                t=dict(session=session,seq=seq,cycle=self.applied,passes=19,discovery_us=10,
                       rowloop_us=40,settle_us=30,neutral_us=10,total_us=100)
                if self.fault=='timing_seq':t['seq']=99
                if self.fault=='timing_sum':t['total_us']=1
                if self.fault=='timing_over':t['total_us']=300
                self.emit('TIMING',t)
            self.emit('ACK',v)
            if self.n==self.total:
                self.emit('DONE',dict(session=session,reason='limit',frames=self.n+(self.fault=='done_count'),applied=self.applied,poweroff_returned=0 if self.fault=='off' else 1))
                if self.fault=='extra':self.queue.append('late output')
        return dict(bytes=len(wire),bytes_written=len(wire),send_started_monotonic=time.monotonic())

class Tests(unittest.TestCase):
    def test_lz_transfer_and_version_identity(self):
        payload=bytes(range(256))*62+bytes(range(128))
        self.assertEqual(len(payload),16000)
        peer=Peer();reads=[];meta={}
        def reader(_):
            reads.append(1)
            return b'raw',payload,dict(source_sequence=len(reads))
        with patch.object(p,'SerialIO',return_value=peer):
            result=p.run_session(-1,'unused',2,123,meta,frame_reader=reader,after_done=0)
        self.assertEqual(result['frames'][0]['packet_type'],p.LZ)
        self.assertEqual(peer.previous,payload)
        for fault in ('lz_missing','lz_disabled','old_ready'):
            with self.assertRaises(p.TransferError):self.run_peer(fault)
        for version in (4,5,6):
            with self.assertRaises(p.TransferError):p.packet(p.LZ,123,1,b'abc',version=version)

    def test_black_trial_requires_exact_ready(self):
        result,_,_=self.run_peer(quality='FAST',white_pair=2,black_pair=2)
        self.assertEqual(result['black_pair'],2)
        for fault,requested in [('black_missing',2),('black_unexpected',0)]:
            with self.assertRaises(p.TransferError):self.run_peer(fault,quality='FAST',black_pair=requested)
        for invalid in (1,3):
            with self.assertRaises(p.TransferError):self.run_peer(quality='FAST',black_pair=invalid)
        with self.assertRaises(p.TransferError):self.run_peer(black_pair=2)

    def test_captured_replay_integrity_and_order(self):
        with tempfile.TemporaryDirectory() as d:
            directory=Path(d); entries=[]
            for i in range(1,4):
                raw=f'P6\n# eink sequence {i}\n320 200\n255\n'.encode()+bytes([i*60])*192000
                name=f'frame-{i:04d}.ppm'; (directory/name).write_bytes(raw)
                entries.append(dict(file=name,source_sequence=i,captured_seconds=i/12,
                                    bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest(),skipped_before=0))
            (directory/'capture.json').write_text(json.dumps(dict(schema=1,status='완료',frames=entries)))
            reader=p.capture_reader(directory,2,2)
            self.assertEqual([reader(None)[2]['source_sequence'] for _ in range(2)],[2,3])
            with self.assertRaises(p.TransferError):reader(None)
            with self.assertRaises(p.TransferError):p.capture_reader(directory,2,3)
            reader=p.capture_reader(directory,2,2)
            file=directory/'frame-0002.ppm'; file.write_bytes(file.read_bytes().replace(bytes([120])*3,bytes([119])*3,1))
            with self.assertRaises(p.TransferError):reader(None)

    def run_peer(self,fault='',stop=False,same=False,diagnostics=False,quality='NORMAL',white_pair=False,black_pair=0):
        peer=Peer(fault,diagnostics=diagnostics,quality=quality,white_pair=white_pair,black_pair=black_pair);meta={};reads=[]
        def reader(_):
            self.assertEqual(len(reads),peer.acks) # 완료 응답을 소비한 뒤에만 최신 파일 읽기
            seq=(4,9)[len(reads)];reads.append(seq)
            data=bytes([0x1b if same or len(reads)==1 else 0xe4])*16000
            return b'raw',data,dict(source_sequence=seq)
        with patch.object(p,'SerialIO',return_value=peer):
            result=p.run_session(-1,'unused',2,123,meta,frame_reader=reader,after_done=0,
                                 stop_requested=(lambda:bool(peer.sent)) if stop else (lambda:False),
                                 timing_diagnostics=diagnostics,quality=quality,white_pair=white_pair,black_pair=black_pair)
        return result,peer,reads
    def test_normal_gap_identity(self):
        for actual,requested in ((False,True),(True,False),(True,True)):
            peer=Peer(stop=True)
            if actual:peer.queue[0]+=' normal_gap_us=2000'
            meta={}
            with patch.object(p,'SerialIO',return_value=peer):
                if actual!=requested:
                    with self.assertRaises(p.TransferError):
                        p.run_session(-1,'unused',2,123,meta,normal_gap_2ms=requested)
                    self.assertEqual(peer.sent,[])
                else:
                    r=p.run_session(-1,'unused',2,123,meta,normal_gap_2ms=requested,
                                    stop_requested=lambda:bool(peer.sent),after_done=0)
                    self.assertTrue(r['normal_gap_2ms'])
        with self.assertRaises(p.TransferError):
            p.run_session(-1,'unused',2,123,{},quality='FAST',normal_gap_2ms=True)

    def test_white_trial_identity(self):
        result,_,_=self.run_peer(quality='FAST',white_pair=True)
        self.assertTrue(result['white_pair'])
        result,_,_=self.run_peer(quality='FAST',white_pair=2)
        self.assertEqual(result['white_pair'],2)
        with self.assertRaises(p.TransferError):self.run_peer(quality='FAST',white_pair=3)
        for fault,requested in [('white_missing',True),('white_unexpected',False)]:
            with self.assertRaises(p.TransferError):
                self.run_peer(fault,quality='FAST',white_pair=requested)
        with self.assertRaises(p.TransferError):self.run_peer(white_pair=True)
    def test_latest_after_ack(self):
        r,peer,reads=self.run_peer();self.assertEqual(reads,[4,9]);self.assertEqual(r['status'],'통과')
        self.assertEqual(len(peer.sent),3);self.assertEqual(r['done']['frames'],'2')
    def test_completed_driver_timing(self):
        r,_,_=self.run_peer(diagnostics=True)
        self.assertEqual([f['driver_timing']['cycle'] for f in r['frames']],[1,2])
        r,_,_=self.run_peer(same=True,diagnostics=True)
        self.assertNotIn('driver_timing',r['frames'][1])
        for fault in ('timing_missing','timing_seq','timing_sum','timing_over'):
            with self.subTest(fault=fault),self.assertRaises(p.TransferError):self.run_peer(fault,diagnostics=True)
    def test_explicit_quality(self):
        r,_,_=self.run_peer(quality='FAST');self.assertEqual(r['quality'],'FAST')
        for actual,requested in [('FAST','NORMAL'),('NORMAL','FAST')]:
            peer=Peer(quality=actual)
            with patch.object(p,'SerialIO',return_value=peer),self.assertRaises(p.TransferError):
                p.run_session(-1,'unused',2,123,{},quality=requested)
            self.assertEqual(peer.sent,[])

    def test_unchanged(self):
        r,_,_=self.run_peer(same=True);self.assertEqual(r['frames'][1]['ack']['result'],'unchanged')
    def test_sparse_patch_after_ack(self):
        peer=Peer();reads=0
        def reader(_):
            nonlocal reads
            self.assertEqual(reads,peer.acks)
            data=bytearray(b'\x1b'*16000)
            if reads:data[15999]=0xe4
            reads+=1
            return b'raw',bytes(data),dict(source_sequence=reads)
        with patch.object(p,'SerialIO',return_value=peer):
            r=p.run_session(-1,'unused',2,123,{},frame_reader=reader,after_done=0)
        self.assertEqual(peer.sent[1][5],p.RLE);self.assertEqual(peer.sent[2][5],p.PATCH)
        self.assertEqual(len(peer.sent[2]),28);self.assertEqual(r['done']['applied'],'2')
    def test_stop_before_frame(self):
        r,peer,reads=self.run_peer(stop=True);self.assertEqual(reads,[]);self.assertEqual(r['done']['poweroff_returned'],'0')
    def test_failures(self):
        for fault in ('ready','old_ready','rle_missing','rle_disabled','session','limit','crc','sequence','cycle','timing','initial_clear','repeat_clear','result','reboot','error','done_count','off','extra','partial'):
            with self.subTest(fault=fault),self.assertRaises(p.TransferError):self.run_peer(fault)
    def test_source_reversal(self):
        peer=Peer();reads=0
        def reader(_):
            nonlocal reads
            reads+=1
            return b'raw',bytes([0x1b])*16000,dict(source_sequence=9 if reads==1 else 4)
        with patch.object(p,'SerialIO',return_value=peer),self.assertRaisesRegex(p.TransferError,'역행'):
            p.run_session(-1,'unused',2,123,{},frame_reader=reader,after_done=0)
        self.assertEqual(len(peer.sent),2) # START와 첫FRAME 이후 잘못된 원본 미전송
    def test_cancel_before_start(self):
        peer=Peer()
        with patch.object(p,'SerialIO',return_value=peer):
            r=p.run_session(-1,'unused',2,123,{},stop_requested=lambda:True)
        self.assertEqual(r['status'],'시작 전 취소');self.assertEqual(peer.sent,[])
    def test_wire(self):
        for kind,seq,payload in ((p.START,0,b'\x0c'),(p.FRAME,1,bytes(16000)),(p.RLE,1,p.encode_rle(bytes(16000))),(p.STOP,2,b'')):
            wire=p.packet(kind,123,seq,payload)
            self.assertEqual(wire[:6],b'EPK1\x07'+bytes([kind]))
            self.assertEqual(struct.unpack('<I',wire[-4:])[0],zlib.crc32(wire[:-4])&0xffffffff)
        for args in ((p.FRAME,1,1,bytes(8000)),(p.START,1,0,b'\x0d'),(p.STOP,0,1,b'')):
            with self.assertRaises(p.TransferError):p.packet(*args)
        for payload in (b'',bytes(16000)):
            with self.assertRaises(p.TransferError):p.packet(p.RLE,1,1,payload)
        with self.assertRaises(p.TransferError):p.packet(p.RLE,1,1,b'\x80x',version=5)
    def test_ppm_same_conversion(self):
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'frame.ppm';rgb=bytes(range(256))*750
            f.write_bytes(b'P6\n# eink sequence 9\n320 200\n255\n'+rgb)
            raw,payload,info=p.read_frame(f)
            self.assertEqual(payload,p.convert(rgb));self.assertEqual(info['source_sequence'],9)
            f.write_bytes(raw[:-1])
            with self.assertRaises(p.TransferError):p.read_frame(f)

if __name__=='__main__':unittest.main()
