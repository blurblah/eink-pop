"""v9의 실제 방 식별·CRC 플래그·정리 응답·PPM 호환 검증."""
import tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from test_painter_interactive_host import InteractivePeer
import painter_usb as p
from usb_frame import TransferError

class RoomPeer(InteractivePeer):
 def __init__(self,periodic=False,omit=False,bad_reason=False):
  super().__init__();self.periodic=periodic;self.omit=omit;self.bad_reason=bad_reason
  self.queue[0]=self.queue[0].replace('version=8','version=9')+' room_refresh=1'
 def write_all(self,wire,deadline):
  self.sent.append(wire);_,v,k,flags,session,seq,n=p.HEADER.unpack(wire[:20]);data=wire[20:-4]
  assert v==9 and wire==p.packet(k,session,seq,data,version=9,room_refresh=bool(flags))
  if k==p.START:
   self.total=int.from_bytes(data,'little');self.emit('ACCEPT',dict(session=session,max_frames=self.total));self.emit('RX',dict(session=session))
  elif k==p.STOP:self.emit('DONE',dict(session=session,reason='stop',frames=self.n,applied=self.applied,poweroff_returned=int(self.n>0)))
  else:
   assert self.acks==self.n;self.n+=1
   if k==p.PATCH:data=__import__('painter_patch').decode(self.previous,data)
   elif k==p.RLE:data=__import__('painter_rle').decode(data)
   elif k==p.LZ:data=__import__('painter_lz').decode(data)
   periodic=self.periodic and seq==2
   refresh=(flags or periodic) and not self.omit
   if refresh:
    reason='both' if flags and periodic else 'room' if flags else 'periodic'
    self.emit('REFRESH',dict(session=session,seq=seq,reason='bad' if self.bad_reason else reason));self.previous=bytes(16000)
   changed=data!=self.previous;applied=self.n==1 or refresh or changed;self.applied+=int(applied);self.previous=data
   if changed:self.emit('TIMING',dict(session=session,seq=seq,cycle=self.n,passes=13,discovery_us=10,rowloop_us=40,settle_us=30,neutral_us=10,total_us=100))
   self.emit('ACK',dict(session=session,seq=seq,crc=wire[-4:][::-1].hex(),result='applied' if applied else 'unchanged',rx_us=1,decode_us=int(k!=p.FRAME),display_us=200,clear_us=10 if seq==1 or refresh else 0,prepare_us=int(changed),paint_us=100 if changed else 0,cycles=int(changed),elapsed_us=seq*100))
   if self.n==self.total:self.emit('DONE',dict(session=session,reason='limit',frames=self.n,applied=self.applied,poweroff_returned=1))
  return dict(bytes=len(wire),bytes_written=len(wire),send_started_monotonic=time.monotonic())

class Tests(unittest.TestCase):
 def run_peer(self,scenes,peer=None,white=False,stop=False):
  peer=peer or RoomPeer();read=iter(enumerate(scenes,1))
  def reader(_):
   seq,scene=next(read);data=bytes(16000) if white and seq==2 else b'\x55'*16000
   return b'raw',data,dict(source_sequence=seq*3,scene=scene)
  with patch.object(p,'SerialIO',return_value=peer) as factory:
   result=p.run_session(-1,'unused',len(scenes),123,{},frame_reader=reader,after_done=0,timing_diagnostics=True,normal_gap_2ms=True,normal_direct13=True,interactive=True,room_refresh=True,stop_requested=lambda:stop and peer.n==2)
  self.assertEqual(factory.call_args.kwargs,dict(max_events=18128,max_raw=18128*514))
  return result,peer
 def test_room_level_revisit_and_unchanged(self):
  result,peer=self.run_peer([(1,1),(1,1),(1,2),(1,2),(2,2),(1,1)])
  self.assertEqual([f['room_refresh_requested'] for f in result['frames']],[False,False,True,False,True,True])
  self.assertEqual([f['ack']['cycles'] for f in result['frames']],['1','0','1','0','1','1'])
 def test_periodic_overlap_white_and_stop(self):
  for same in (True,False):
   for white in (True,False):
    result,_=self.run_peer([(1,1),(1,1) if same else (1,2)],RoomPeer(periodic=True),white)
    self.assertEqual(result['frames'][1]['refresh']['reason'],'periodic' if same else 'both')
    self.assertEqual(result['frames'][1]['ack']['cycles'],str(int(not white)))
  result,_=self.run_peer([(1,1),(1,2),(1,3)],stop=True)
  self.assertEqual(result['done']['reason'],'stop')
 def test_missing_metadata_and_refresh_rejected(self):
  for scenes,peer in [([(1,1),None],RoomPeer()), ([(1,25)],RoomPeer()), ([(1,1),(1,2)],RoomPeer(omit=True)), ([(1,1),(1,2)],RoomPeer(bad_reason=True))]:
   with self.assertRaises(TransferError):self.run_peer(scenes,peer)
 def test_wire_flags_rejected(self):
  for version,kind,seq,data in [(8,p.FRAME,2,bytes(16000)),(9,p.FRAME,1,bytes(16000)),(9,p.STOP,2,b''),(9,p.START,0,b'\x02\x00')]:
   with self.assertRaises(TransferError):p.packet(kind,123,seq,data,version=version,room_refresh=True)
 def test_ppm_legacy_scene_and_malformed(self):
  with tempfile.TemporaryDirectory() as d:
   path=Path(d)/'latest.ppm'
   for header,scene in [(b'',None),(b'# eink scene 1 24\n',(1,24)),(b'# eink scene 0 1\n',(0,1))]:
    path.write_bytes(b'P6\n# eink sequence 9\n'+header+b'320 200\n255\n'+bytes(192000))
    _,_,info=p.read_frame(path,lambda rgb:bytes(16000));self.assertEqual(info['scene'],scene)
   for header in (b'# eink scene 1 0\n',b'# eink scene 16 1\n',b'# eink scene 1 25\n',b'# eink scene 1\n'):
    path.write_bytes(b'P6\n# eink sequence 9\n'+header+b'320 200\n255\n'+bytes(192000))
    with self.assertRaises(TransferError):p.read_frame(path)
if __name__=='__main__':unittest.main()
