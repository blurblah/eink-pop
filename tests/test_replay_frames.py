#!/usr/bin/env python3
"""동일 비트맵 복원·PTY 재생·잘못된 대상 거부. 실제 보드 접근 없음."""
import base64
import copy
import io
import os
from pathlib import Path
import pty
import sys
import threading
import tty
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'host'))
import test_offline_display as fixtures
from test_usb_frame_host import Peer,SESSION,FAST
import replay_frames as replay

DRIVER='9b8dcdba04ed03ee70dba412667803b1dac1adfd'
CONFIG=f'USB_FRAME_STATS metric=config driver={DRIVER} source=320x200 bpp=1 msb_first=1 black=1 white=0 scale=2 x=160 y=70 refresh=initial_clear_then_delta'


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.OfflineTests();self.fixture.setUp();self.directory=self.fixture.directory/'prepared'
        self.frames,self.manifest=replay.prepare(self.fixture.prefix,self.directory)

    def tearDown(self):self.fixture.tearDown()

    def reader(self,config=CONFIG):
        meta={'responses':[{'line':config}]}
        return replay.ReplayReader(self.directory,self.manifest,meta,DRIVER)

    def test_all_payloads_and_sequences_round_trip(self):
        r=self.reader()
        for frame in self.frames:
            _,payload,info=r(None)
            self.assertEqual(payload,base64.b64decode(frame['payload']))
            self.assertEqual(info['source_sequence'],frame['source_sequence'])
        with self.assertRaisesRegex(replay.usb.TransferError,'초과'):r(None)

    def test_wrong_driver_and_geometry_rejected(self):
        for config in (CONFIG.replace(DRIVER,'0'*40),CONFIG.replace('scale=2','scale=3'),CONFIG.replace('black=1','black=0')):
            r=self.reader(config)
            with self.assertRaisesRegex(replay.usb.TransferError,'설정 불일치'):r(None)
            self.assertEqual(r.position,0)
        r=self.reader();r.metadata['responses']*=2
        with self.assertRaisesRegex(replay.usb.TransferError,'유일'):r(None)

    def test_changed_file_and_wrong_order_rejected(self):
        p=self.directory/'frame-01.ppm';raw=p.read_bytes();p.write_bytes(raw[:-1]+b'\0')
        with self.assertRaisesRegex(replay.usb.TransferError,'바뀌'):self.reader()(None)
        p.write_bytes(raw);self.manifest['frames'][0]['file']='../escape.ppm'
        with self.assertRaisesRegex(replay.usb.TransferError,'순서'):self.reader()(None)

    def test_prepare_never_overwrites_and_bad_baseline_never_opens_port(self):
        manifest=(self.directory/'manifest.json').read_bytes()
        with self.assertRaises(FileExistsError):replay.prepare(self.fixture.prefix,self.directory)
        self.assertEqual((self.directory/'manifest.json').read_bytes(),manifest)
        self.fixture.wire=self.fixture.wire[:-1];self.fixture.save()
        with patch.object(replay.usb,'open_serial') as opened:
            result=replay.main(['--baseline-prefix',str(self.fixture.prefix),'--output',str(self.fixture.directory/'bad'),'--port','unused','--driver-commit',DRIVER])
            self.assertEqual(result,1);opened.assert_not_called()

    def test_comparison_excludes_first_and_rejects_different_pixels(self):
        after=copy.deepcopy(self.frames)
        after[0]['display_us']=999999
        for frame in after[1:]:frame['display_us']=180
        result=replay.comparison(self.frames,after)
        self.assertEqual(result['samples'],2);self.assertEqual(result['mean']['display_us']['before'],200)
        self.assertAlmostEqual(result['mean']['display_us']['change_percent'],-10)
        after[1]['payload']=after[0]['payload']
        with self.assertRaisesRegex(ValueError,'화면'):replay.comparison(self.frames,after)

    def test_single_frame_is_rejected_before_preparing(self):
        output=self.fixture.directory/'single'
        with patch.object(replay,'recording',return_value=(self.frames[:1],DRIVER)):
            with self.assertRaisesRegex(ValueError,'두 장'):replay.prepare(self.fixture.prefix,output)
        self.assertFalse(output.exists())

    def exchange(self,wrong_driver=False):
        master,slave=pty.openpty();tty.setraw(slave);os.set_blocking(slave,False)
        peer=Peer(master);errors=[];metadata={};reader=replay.ReplayReader(self.directory,self.manifest,metadata,DRIVER)
        def emulate():
            try:
                config=CONFIG.replace(DRIVER,'0'*40) if wrong_driver else CONFIG
                peer.handshake(3,before_rx=lambda:peer.send(config+'\n'))
                if wrong_driver:
                    self.assertEqual(peer.packet()['type'],replay.usb.STOP)
                    peer.send(peer.done(0,0,'stop'));return
                for expected in self.frames:
                    packet=peer.packet();self.assertEqual(packet['payload'],base64.b64decode(expected['payload']))
                    peer.send(peer.ack(packet))
                peer.send(peer.done(3))
            except BaseException as e:errors.append(e)
        thread=threading.Thread(target=emulate,daemon=True);thread.start()
        try:
            args=(slave,None,3,SESSION,metadata,io.BytesIO(),io.BytesIO())
            if wrong_driver:
                with self.assertRaisesRegex(replay.usb.TransferError,'설정 불일치'):
                    replay.usb.run_session(*args,deadlines=FAST,frame_reader=reader)
            else:
                result=replay.usb.run_session(*args,deadlines=FAST,frame_reader=reader)
                self.assertEqual(result['status'],'통과');self.assertEqual(reader.position,3)
            thread.join(timeout=2);self.assertFalse(thread.is_alive())
            if errors:raise errors[0]
            return peer.packets
        finally:os.close(slave);os.close(master)

    def test_pty_sends_all_stored_pixels_once(self):
        self.assertEqual([p['type'] for p in self.exchange()],[1,2,2,2])

    def test_pty_wrong_driver_sends_stop_without_frame(self):
        self.assertEqual([p['type'] for p in self.exchange(True)],[1,3])


if __name__=='__main__':unittest.main(verbosity=2)
