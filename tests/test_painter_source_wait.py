"""대화형 입력의 잠깐 멈춤·10초 상한·내용 변조·STOP을 가짜 시계로 검증."""
import time,unittest
from unittest.mock import patch
from test_painter_interactive_host import InteractivePeer
import painter_usb as p

class Tests(unittest.TestCase):
 def run_wait(self,resume=3,stop_at=None,mutate=False):
  now=[100.0];peer=InteractivePeer();metadata={}
  def read(_):
   now[0]+=.1;seq=1 if now[0]<100+resume else 2
   data=b'\x55'*16000 if seq==1 else b'\xaa'*16000
   if mutate and now[0]>100.5:data=b'\xff'*16000
   return b'raw',data,dict(source_sequence=seq)
  with patch.object(p,'SerialIO',return_value=peer),patch.object(p.time,'monotonic',side_effect=lambda:now[0]),patch.object(p.time,'sleep',side_effect=lambda s:now.__setitem__(0,now[0]+s)):
   result=p.run_session(-1,'unused',6000,123,metadata,frame_reader=read,after_done=0,timing_diagnostics=True,normal_gap_2ms=True,normal_direct13=True,interactive=True,stop_requested=lambda:now[0]>=100+stop_at if stop_at is not None else peer.previous==b'\xaa'*16000)
  return result,peer
 def test_pause_then_resume(self):
  for seconds in (3,6.2):
   with self.subTest(seconds=seconds):
    result,_=self.run_wait(resume=seconds)
    self.assertEqual(result['done']['reason'],'stop')
    repeats=[f for f in result['frames'] if f['repeated_source']]
    self.assertGreaterEqual(len(repeats),int(seconds)-1)
    self.assertTrue(all(f['ack']['result']=='unchanged' and f['ack']['cycles']=='0' for f in repeats))
    self.assertEqual(result['frames'][-1]['source_sequence'],2)
 def test_stall_bound(self):
  with self.assertRaisesRegex(p.TransferError,'10초 진행 없음'):self.run_wait(resume=100)
 def test_stop_during_wait(self):
  result,_=self.run_wait(resume=100,stop_at=1.5)
  self.assertEqual(result['done']['reason'],'stop')
 def test_same_sequence_mutation(self):
  with self.assertRaisesRegex(p.TransferError,'같은 원본 번호'):self.run_wait(mutate=True)
if __name__=='__main__':unittest.main()
