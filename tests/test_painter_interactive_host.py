"""v8 주기정리의 기준 이미지 전환·사용자 STOP·상한 검증."""
import sys,time,struct,unittest,zlib
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'host'))
import painter_usb as p
import usb_frame as usb
from test_painter_usb_host import Peer

class InteractivePeer(Peer):
 def __init__(self,refresh=False,white=False,timeout=False):
  super().__init__(diagnostics=True);self.refresh=refresh;self.white=white;self.timeout=timeout
  self.queue=[self.queue[0].replace('version=7','version=8')+' normal_gap_us=2000 normal_direct=13 play_max=6000 screen_limit_ms=600000 refresh_ms=30000']
 def write_all(self,wire,deadline):
  self.sent.append(wire);_,v,k,_,session,seq,n=p.HEADER.unpack(wire[:20]);data=wire[20:-4]
  assert v==8 and wire==p.packet(k,session,seq,data,version=8)
  if k==p.START:
   self.total=int.from_bytes(data,'little');self.emit('ACCEPT',dict(session=session,max_frames=self.total));self.emit('RX',dict(session=session))
  elif k==p.STOP:self.emit('DONE',dict(session=session,reason='stop',frames=self.n,applied=self.applied,poweroff_returned=int(self.n>0)))
  else:
   assert self.acks==self.n;self.n+=1
   if k==p.PATCH:data=__import__('painter_patch').decode(self.previous,data)
   elif k==p.RLE:data=__import__('painter_rle').decode(data)
   elif k==p.LZ:data=__import__('painter_lz').decode(data)
   refresh=self.refresh and self.n==2
   if refresh:self.emit('REFRESH',dict(session=session,seq=seq));self.previous=bytes(16000)
   changed=data!=self.previous;applied=self.n==1 or refresh or changed;self.applied+=applied
   self.previous=data
   if changed:self.emit('TIMING',dict(session=session,seq=seq,cycle=self.n,passes=13,discovery_us=10,rowloop_us=40,settle_us=30,neutral_us=10,total_us=100))
   self.emit('ACK',dict(session=session,seq=seq,crc=f'{zlib.crc32(wire[:-4])&0xffffffff:08x}',result='applied' if applied else 'unchanged',rx_us=100,decode_us=0,display_us=400,clear_us=100 if self.n==1 or refresh else 0,prepare_us=10 if changed else 0,paint_us=200 if changed else 0,cycles=int(changed),elapsed_us=600000000 if self.timeout else self.n*400))
   if self.timeout or self.n==self.total:self.emit('DONE',dict(session=session,reason='time_limit' if self.timeout else 'limit',frames=self.n,applied=self.applied,poweroff_returned=1))
  return dict(bytes=len(wire),bytes_written=len(wire),send_started_monotonic=time.monotonic())

class Tests(unittest.TestCase):
 def run_peer(self,peer,frames=2,stop=False):
  reads=0
  def reader(_):
   nonlocal reads
   reads+=1;data=bytes(16000) if peer.white and reads==2 else b'\x55'*16000
   return b'raw',data,dict(source_sequence=reads)
  with patch.object(p,'SerialIO',return_value=peer) as factory:
   result=p.run_session(-1,'unused',frames,123,{},frame_reader=reader,after_done=0,timing_diagnostics=True,normal_gap_2ms=True,normal_direct13=True,interactive=True,stop_requested=lambda:stop and peer.n==1)
  self.assertEqual(factory.call_args.kwargs,dict(max_events=12128,max_raw=12128*514))
  return result
 def test_refresh_same_and_white(self):
  for white in (False,True):
   r=self.run_peer(InteractivePeer(refresh=True,white=white));self.assertEqual(r['done']['frames'],'2');self.assertEqual(r['done']['applied'],'2')
   self.assertIn('periodic_refresh',r['frames'][1]);self.assertEqual(r['frames'][1]['ack']['cycles'],str(int(not white)))
 def test_stop_and_time_limit(self):
  self.assertEqual(self.run_peer(InteractivePeer(),frames=6000,stop=True)['done']['reason'],'stop')
  self.assertEqual(self.run_peer(InteractivePeer(timeout=True),frames=6000)['done']['reason'],'time_limit')
 def test_bounds_and_identity(self):
  for n in (0,6001):
   with self.assertRaises(p.TransferError):p.packet(p.START,123,0,n.to_bytes(2,'little'),version=8)
  with self.assertRaises(p.TransferError):p.packet(p.START,123,0,b'\x0c',version=8)
  peer=Peer()
  with patch.object(p,'SerialIO',return_value=peer),self.assertRaises(p.TransferError):p.run_session(-1,'unused',6000,123,{},normal_gap_2ms=True,normal_direct13=True,interactive=True)
  self.assertEqual(peer.sent,[])

class SerialLogBounds(unittest.TestCase):
 def test_full_interactive_response_budget(self):
  # 실제 SerialIO 읽기 경로로 기존 두 제한(4096줄/1MiB)을 모두 넘긴다.
  data=(b'x'*200+b'\n')*12128
  chunks=iter(data[i:i+1024] for i in range(0,len(data),1024))
  meta={};io=usb.SerialIO(9,None,None,meta,max_events=12128,max_raw=12128*514)
  with patch.object(usb.select,'select',return_value=([9],[],[])),patch.object(usb.os,'read',side_effect=lambda *_:next(chunks)):
   for _ in range(12128):io.line(time.monotonic()+1)
  self.assertGreater(io.raw_count,usb.MAX_RAW)
  self.assertEqual(len(meta['responses']),12128)
  io.pending.extend(b'overflow\n')
  with self.assertRaisesRegex(usb.TransferError,'개수 상한'):io.line(time.monotonic()+1)
 def test_default_and_raw_caps_remain(self):
  io=usb.SerialIO(9,None,None,{'responses':[{}]*usb.MAX_EVENTS})
  io.pending.extend(b'overflow\n')
  with self.assertRaisesRegex(usb.TransferError,'개수 상한'):io.line(time.monotonic()+1)
  io=usb.SerialIO(9,None,None,{},max_raw=4)
  with patch.object(usb.select,'select',return_value=([9],[],[])),patch.object(usb.os,'read',return_value=b'12345'):
   with self.assertRaisesRegex(usb.TransferError,'바이트 상한'):io.line(time.monotonic()+1)

if __name__=='__main__':unittest.main()
