#!/usr/bin/env python3
"""실제 호스트 전송 루프를 PTY 상대와 검증한다. 보드·게임·GUI는 사용하지 않는다."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import pty
import select
import struct
import tempfile
import threading
import time
import tty
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("usb_frame", ROOT / "host/usb_frame.py")
usb = importlib.util.module_from_spec(SPEC)
import sys
sys.modules[SPEC.name] = usb
SPEC.loader.exec_module(usb)
REAL_WRITE = os.write
SESSION = 123456789
FAST = usb.Deadlines(ready=.3, accept=.3, diagnostic=.4, ack=.2, done=.2,
                     receive_window=.8, pause=.025, after_done=.04)


def ppm(sequence=1, color=0):
    return f"P6\n# eink sequence {sequence}\n320 200\n255\n".encode() + bytes([color]) * 192000


def crc_reference(data):
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xEDB88320 if crc & 1 else 0)
    return crc ^ 0xFFFFFFFF


class Peer:
    def __init__(self, fd):
        self.fd = fd
        self.packets = []

    def send(self, text, fragment=0):
        raw = text.encode() if isinstance(text, str) else text
        while raw:
            count = REAL_WRITE(self.fd, raw[:fragment] if fragment else raw)
            raw = raw[count:]

    def read_exact(self, length):
        result = b""
        deadline = time.monotonic() + 1.5
        while len(result) < length:
            if time.monotonic() >= deadline:
                raise AssertionError("호스트 패킷 대기 시간초과")
            if select.select([self.fd], [], [], .02)[0]:
                result += os.read(self.fd, length - len(result))
        return result

    def packet(self):
        header = self.read_exact(20)
        magic, version, kind, reserved, session, seq, length = struct.unpack("<4sBBHIII", header)
        assert magic == b"EPK1" and version == 1 and reserved == 0
        assert session == SESSION and length in (0, 1, 8000)
        tail = self.read_exact(length + 4)
        payload, crc = tail[:-4], struct.unpack("<I", tail[-4:])[0]
        assert crc_reference(header + payload) == crc
        value = {"type": kind, "session": session, "seq": seq, "payload": payload,
                 "crc": f"{crc:08x}", "received_monotonic": time.monotonic()}
        self.packets.append(value)
        return value

    def quiet(self, duration=.025):
        assert not select.select([self.fd], [], [], duration)[0], "ACK 전에 다음 패킷이 전송됨"

    def handshake(self, frames=1, before_rx=None):
        self.send("USB_FRAME_READY version=1 panel=untouched\r\n", fragment=3)
        start = self.packet()
        assert start["type"] == usb.START and start["seq"] == 0 and start["payload"] == bytes([frames])
        self.send(f"USB_FRAME_ACCEPT session={SESSION} max_frames={frames}\n")
        self.send("USB_FRAME_STATS metric=delay_sample sample=1 elapsed_cycles=120020\n")
        self.quiet()  # RX가 오기 전에는 프레임을 보내지 않는다.
        if before_rx:
            before_rx()
        self.send("USB_FRAME_DELAY_DONE status=pass samples=199\n")
        self.send(f"USB_FRAME_RX session={SESSION}\r\n")

    def ack(self, frame, result="applied", changes=None):
        fields = {"session": str(SESSION), "seq": str(frame["seq"]), "crc": frame["crc"],
                  "result": result, "rx_us": "100", "display_us": "500",
                  "white_us": "200", "black_us": "250", "elapsed_us": str(1000 * frame["seq"])}
        if changes:
            fields.update(changes)
        return "USB_FRAME_ACK " + " ".join(f"{k}={v}" for k, v in fields.items()) + "\n"

    def done(self, frames, applied=None, reason="limit"):
        applied = frames if applied is None else applied
        return f"USB_FRAME_DONE session={SESSION} reason={reason} frames={frames} applied={applied}\n"


class HostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="eink-usb-host-")
        self.directory = Path(self.temp.name)
        self.frame = self.directory / "latest.ppm"
        self.frame.write_bytes(ppm())

    def tearDown(self):
        self.temp.cleanup()

    def replace(self, sequence, color):
        temporary = self.directory / "latest.tmp"
        temporary.write_bytes(ppm(sequence, color))
        os.replace(temporary, self.frame)

    def exchange(self, emulate, count=1, error=None, deadlines=FAST, stop=None, patch_write=None):
        master, slave = pty.openpty()
        tty.setraw(slave)
        os.set_blocking(slave, False)
        peer = Peer(master)
        errors = []

        def target():
            try:
                emulate(peer)
            except BaseException as failure:
                errors.append(failure)

        worker = threading.Thread(target=target, daemon=True)
        metadata, raw, tx = {}, io.BytesIO(), io.BytesIO()
        worker.start()
        try:
            manager = mock.patch.object(usb.os, "write", patch_write) if patch_write else contextlib.nullcontext()
            with manager:
                arguments = (slave, self.frame, count, SESSION, metadata, raw, tx)
                options = {"deadlines": deadlines, "stop_requested": stop or (lambda: False),
                           "first_prefix": self.directory / "capture"}
                if error:
                    with self.assertRaisesRegex(usb.TransferError, error):
                        usb.run_session(*arguments, **options)
                else:
                    result = usb.run_session(*arguments, **options)
                    self.assertEqual(result["status"], "통과")
                    self.assertGreaterEqual(result["after_done_seconds"], deadlines.after_done)
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive(), "모의 보드 스레드가 종료되지 않음")
            if errors:
                raise errors[0]
            self.assertEqual(tx.getvalue().count(b"EPK1\x01\x01"), 1)
            return metadata, peer, raw.getvalue(), tx.getvalue()
        finally:
            os.close(slave)
            with contextlib.suppress(OSError):
                os.close(master)

    def test_exact_threshold_packing_and_fd_metadata(self):
        colors = [(0,0,0), (127,127,127), (128,128,128), (255,255,255),
                  (255,0,0), (0,255,0), (0,0,255), (255,255,0)]
        rgb = bytes(value for color in colors for value in color) * 8000
        self.frame.write_bytes(b"P6\n# eink sequence 17\n320 200\n255\n" + rgb)
        old_mtime = self.frame.stat().st_mtime_ns
        real_fstat = os.fstat

        def replaced_fstat(fd):
            self.replace(99, 255)
            return real_fstat(fd)

        with mock.patch.object(usb.os, "fstat", replaced_fstat):
            raw, payload, evidence = usb.read_frame(self.frame)
        self.assertEqual(payload, bytes([0xCA]) * 8000)
        self.assertEqual(evidence["source_sequence"], 17)
        self.assertEqual(evidence["file_mtime_ns"], old_mtime)
        self.assertEqual(evidence["file_bytes"], len(raw))
        self.assertEqual(usb.pbm(payload), b"P4\n320 200\n" + payload)

    def test_invalid_ppm(self):
        base = ppm()
        variants = [base[:-1], base+b"x", base.replace(b"P6", b"P3", 1),
                    base.replace(b"320 200", b"320 201", 1), base.replace(b"255\n", b"256\n", 1),
                    base.replace(b"sequence 1", b"sequence 0", 1), base.replace(b"sequence 1", b"sequence 01", 1),
                    base.replace(b"# eink sequence 1\n", b"", 1)]
        for value in variants:
            with self.subTest(length=len(value), prefix=value[:45]):
                self.frame.write_bytes(value)
                with self.assertRaises(usb.TransferError):
                    usb.read_frame(self.frame)

    def test_packet_crc_and_sizes(self):
        self.assertEqual(crc_reference(b"123456789"), 0xCBF43926)
        for kind, seq, payload, length in [(usb.START,0,b"\x0c",25), (usb.FRAME,1,b"\0"*8000,8024), (usb.STOP,2,b"",24)]:
            wire = usb.packet(kind, SESSION, seq, payload)
            self.assertEqual(len(wire), length)
            self.assertEqual(struct.unpack("<I",wire[-4:])[0], crc_reference(wire[:-4]))
        for count in (0,13):
            with self.assertRaises(usb.TransferError):
                usb.packet(usb.START, SESSION, 0, bytes([count]))

    def test_preview_never_opens_board(self):
        target = self.directory / "preview.pbm"
        with mock.patch.object(usb, "open_serial", side_effect=AssertionError("보드 접근")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(usb.main(["--frame-path",str(self.frame),"--preview",str(target)]),0)
        self.assertEqual(target.read_bytes(), b"P4\n320 200\n" + b"\xff"*8000)
        self.assertEqual(list(self.directory.glob("*-observation.json")), [])

    def test_single_frame_fresh_after_rx_and_fragmented_ack(self):
        def emulate(peer):
            peer.handshake(before_rx=lambda:self.replace(2,255))
            frame = peer.packet()
            self.assertEqual(frame["payload"], b"\0"*8000)
            peer.send(peer.ack(frame) + peer.done(1), fragment=7)
        metadata, _, raw, _ = self.exchange(emulate)
        self.assertEqual(metadata["frames"][0]["source_sequence"],2)
        self.assertIn(b"USB_FRAME_ACK",raw)
        self.assertEqual((self.directory/"capture-first.ppm").read_bytes(),ppm(2,255))

    def test_queued_ready_in_partial_and_next_read(self):
        for kind in ("partial","next_read","together"):
            with self.subTest(kind=kind):
                for file in self.directory.glob("capture-*"):file.unlink()
                def emulate(peer):
                    ready="USB_FRAME_READY version=1 panel=untouched\n"
                    peer.send(ready + ("USB_FRAME_RE" if kind=="partial" else ready if kind=="together" else ""))
                    peer.packet()
                    if kind=="partial":peer.send("ADY version=1 panel=untouched\n")
                    elif kind=="next_read":peer.send(ready)
                    peer.send(f"USB_FRAME_ACCEPT session={SESSION} max_frames=1\nUSB_FRAME_DELAY_DONE status=pass\nUSB_FRAME_RX session={SESSION}\n")
                    frame=peer.packet();peer.send(peer.ack(frame)+peer.done(1))
                metadata,peer,_,_=self.exchange(emulate)
                self.assertEqual(metadata["start_packets_sent"],1)
                self.assertEqual([p["type"] for p in peer.packets],[usb.START,usb.FRAME])

    def test_latest_a_b_c_and_real_250ms_pause(self):
        def emulate(peer):
            peer.handshake(frames=2)
            a = peer.packet()
            self.assertEqual(a["payload"], b"\xff"*8000)
            self.replace(2,127); self.replace(3,255)
            peer.quiet()
            peer.send(peer.ack(a))
            sent = time.monotonic()
            c = peer.packet()
            self.assertGreaterEqual(c["received_monotonic"]-sent,.250)
            self.assertEqual(c["payload"], b"\0"*8000)
            peer.send(peer.ack(c)+peer.done(2))
        timings = usb.Deadlines(ready=.3,accept=.3,diagnostic=.4,ack=.2,done=.2,receive_window=.8,pause=.250,after_done=.04)
        metadata, peer, _, _ = self.exchange(emulate,count=2,deadlines=timings)
        self.assertEqual([f["source_sequence"] for f in metadata["frames"]],[1,3])
        self.assertEqual([p["seq"] for p in peer.packets],[0,1,2])

    def test_same_source_sequence_unchanged_counts(self):
        def emulate(peer):
            peer.handshake(frames=2)
            a=peer.packet(); peer.send(peer.ack(a))
            b=peer.packet(); self.assertEqual(a["payload"],b["payload"])
            peer.send(peer.ack(b,"unchanged")+peer.done(2,applied=1))
        metadata,_,_,_=self.exchange(emulate,count=2)
        self.assertEqual([f["source_sequence"] for f in metadata["frames"]],[1,1])

    def test_short_writes_and_eagain(self):
        writes=0
        def short_write(fd,data):
            nonlocal writes
            writes+=1
            if writes%7==0:raise BlockingIOError()
            return REAL_WRITE(fd,data[:13])
        def emulate(peer):
            peer.handshake(); frame=peer.packet(); peer.send(peer.ack(frame)+peer.done(1))
        metadata,_,_,wire=self.exchange(emulate,patch_write=short_write)
        self.assertGreater(writes,600)
        self.assertEqual(len(wire),25+8024)
        self.assertEqual(metadata["frames"][0]["transmission"]["bytes_written"],8024)

    def test_write_deadline_without_retry(self):
        master,slave=pty.openpty()
        try:
            io_state=usb.SerialIO(slave,io.BytesIO(),io.BytesIO(),{})
            with mock.patch.object(usb.os,"write",side_effect=BlockingIOError()):
                with self.assertRaisesRegex(usb.ResponseTimeout,"송신 시간초과"):
                    io_state.write_all(b"one packet",time.monotonic()+.03)
            self.assertEqual(io_state.metadata["writes"][0]["bytes_written"],0)
            self.assertEqual(len(io_state.metadata["writes"]),1)
        finally:
            os.close(master);os.close(slave)

    def test_residual_ready_does_not_extend_accept_deadline(self):
        def emulate(peer):
            ready="USB_FRAME_READY version=1 panel=untouched\n"
            peer.send(ready);peer.packet()
            until=time.monotonic()+.4
            while time.monotonic()<until:
                peer.send(ready);time.sleep(.02)
            peer.quiet()
        metadata,peer,_,_=self.exchange(emulate,error="시간초과")
        self.assertEqual(len(peer.packets),1)
        self.assertEqual(metadata["start_packets_sent"],1)

    def test_stop_waits_for_inflight_ack(self):
        stop=threading.Event()
        def emulate(peer):
            peer.handshake(frames=3); frame=peer.packet()
            stop.set(); peer.quiet(.05)
            peer.send(peer.ack(frame))
            command=peer.packet()
            self.assertEqual((command["type"],command["seq"]),(usb.STOP,2))
            peer.send(peer.done(1,reason="stop"))
        metadata,_,_,_=self.exchange(emulate,count=3,stop=stop.is_set)
        self.assertEqual(metadata["stop_packets_sent"],1)
        self.assertEqual(len(metadata["frames"]),1)

    def test_invalid_latest_stops_without_sending_frame(self):
        def emulate(peer):
            peer.handshake(frames=2); frame=peer.packet()
            self.frame.write_bytes(b"broken")
            peer.send(peer.ack(frame))
            command=peer.packet();self.assertEqual(command["type"],usb.STOP)
            peer.send(peer.done(1,reason="stop"))
        metadata,_,_,_=self.exchange(emulate,count=2,error="PPM 헤더")
        self.assertEqual(len(metadata["frames"]),1)

    def test_stop_before_first_frame_has_no_frame_packet(self):
        stop=threading.Event()
        def emulate(peer):
            peer.handshake(before_rx=stop.set)
            command=peer.packet()
            self.assertEqual((command["type"],command["seq"]),(usb.STOP,1))
            peer.send(peer.done(0,reason="stop"))
        metadata,_,_,_=self.exchange(emulate,stop=stop.is_set)
        self.assertEqual(metadata["frames"],[])

    def test_ack_rejection(self):
        changes=[{"session":"9"},{"seq":"2"},{"crc":"ffffffff"},{"result":"other"},
                 {"rx_us":"-1"},{"seq":"01"},{"white_us":"999"},{"elapsed_us":"-1"}]
        for change in changes:
            with self.subTest(change=change):
                for file in self.directory.glob("capture-*"):file.unlink()
                def emulate(peer):
                    peer.handshake();frame=peer.packet();peer.send(peer.ack(frame,changes=change));peer.quiet()
                metadata,peer,_,_=self.exchange(emulate,error="ACK|정수|session")
                self.assertEqual(len(peer.packets),2)
                self.assertEqual(metadata["status"],"오류")

    def test_missing_duplicate_and_premature_ack(self):
        for kind in ("missing","duplicate","premature"):
            with self.subTest(kind=kind):
                for file in self.directory.glob("capture-*"):file.unlink()
                def emulate(peer):
                    if kind=="premature":
                        peer.send("USB_FRAME_READY version=1 panel=untouched\n");peer.packet()
                        peer.send(f"USB_FRAME_ACCEPT session={SESSION} max_frames=1\nUSB_FRAME_DELAY_DONE status=pass\nUSB_FRAME_RX session={SESSION}\nUSB_FRAME_ACK seq=1\n")
                        peer.quiet();return
                    peer.handshake();frame=peer.packet();line=peer.ack(frame)
                    if kind=="missing":line=line.replace(" rx_us=100","")
                    else:line=line.rstrip()+" seq=1\n"
                    peer.send(line);peer.quiet()
                self.exchange(emulate,error="필드|추가 응답")

    def test_wrong_accept_rx_and_delay_failure(self):
        for kind in ("accept","rx","delay"):
            with self.subTest(kind=kind):
                def emulate(peer):
                    peer.send("USB_FRAME_READY version=1 panel=untouched\n");peer.packet()
                    peer.send(f"USB_FRAME_ACCEPT session={9 if kind=='accept' else SESSION} max_frames=1\n")
                    if kind!="accept":
                        peer.send(f"USB_FRAME_DELAY_DONE status={'fail' if kind=='delay' else 'pass'}\n")
                        if kind=="rx":peer.send("USB_FRAME_RX session=9\n")
                    peer.quiet()
                self.exchange(emulate,error="session|지연 검사")

    def test_no_ack_partial_ack_reboot_and_eof(self):
        for kind in ("none","partial","reboot","eof"):
            with self.subTest(kind=kind):
                for file in self.directory.glob("capture-*"):file.unlink()
                def emulate(peer):
                    peer.handshake();peer.packet()
                    if kind=="partial":peer.send("USB_FRAME_ACK session=123")
                    elif kind=="reboot":peer.send("ESP-ROM:esp32s3\n")
                    elif kind=="eof":os.close(peer.fd)
                    if kind in ("none","partial"):time.sleep(.25)
                self.exchange(emulate,error="시간초과|재부팅|연결 종료|읽기 실패")

    def test_done_validation_and_post_done_activity(self):
        for kind in ("session","count","reason","post","post_stats"):
            with self.subTest(kind=kind):
                for file in self.directory.glob("capture-*"):file.unlink()
                def emulate(peer):
                    peer.handshake();frame=peer.packet();peer.send(peer.ack(frame))
                    line=peer.done(1)
                    if kind=="session":line=line.replace(str(SESSION),"9")
                    elif kind=="count":line=line.replace("frames=1","frames=0")
                    elif kind=="reason":line=line.replace("reason=limit","reason=stop")
                    peer.send(line)
                    if kind in ("post","post_stats"):
                        time.sleep(.01)
                        peer.send("USB_FRAME_STATS metric=unexpected\n" if kind=="post_stats" else "USB_FRAME_READY version=1 panel=untouched\n")
                self.exchange(emulate,error="session|DONE|STOP")

    def test_line_buffer_and_error_response(self):
        for line in ("X"*513,"USB_FRAME_ERROR reason=bad_crc\n","USB_FRAME_READY version=2 panel=untouched\n"):
            with self.subTest(line=line[:40]):
                def emulate(peer):
                    peer.send("USB_FRAME_READY version=1 panel=untouched\n");peer.packet();peer.send(line)
                self.exchange(emulate,error="상한|펌웨어 오류|예상하지 않은 응답")

    def test_ready_after_accept_is_rejected(self):
        def emulate(peer):
            peer.send("USB_FRAME_READY version=1 panel=untouched\n");peer.packet()
            peer.send(f"USB_FRAME_ACCEPT session={SESSION} max_frames=1\nUSB_FRAME_READY version=1 panel=untouched\n")
        self.exchange(emulate,error="예상 응답")

    def test_premature_time_limit_is_rejected(self):
        def emulate(peer):
            peer.handshake();peer.packet();peer.send(peer.done(0,reason="time_limit"))
        metadata,_,_,_=self.exchange(emulate,error="30초 경과 근거")
        self.assertNotIn("ack",metadata["frames"][0])

    def test_time_limit_after_ack(self):
        def emulate(peer):
            peer.handshake(frames=2);frame=peer.packet()
            peer.send(peer.ack(frame,changes={"elapsed_us":"30000001"})+peer.done(1,reason="time_limit"))
        metadata,_,_,_=self.exchange(emulate,count=2)
        self.assertEqual(metadata["done"]["reason"],"time_limit")

    def test_raw_and_event_log_bounds(self):
        for limit in ("MAX_RAW","MAX_EVENTS"):
            with self.subTest(limit=limit):
                def emulate(peer):
                    peer.send("USB_FRAME_READY version=1 panel=untouched\n");peer.packet()
                    peer.send("USB_FRAME_STATS metric=diagnostic\n"*8)
                with mock.patch.object(usb,limit,64 if limit=="MAX_RAW" else 4):
                    self.exchange(emulate,error="로그.*상한")


if __name__ == "__main__":
    unittest.main(verbosity=2)
