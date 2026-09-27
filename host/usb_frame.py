#!/usr/bin/env python3
"""SDLPoP 최신 PPM을 고정 점무늬 흑백으로 보내는 제한 USB v3 시험 도구."""

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import select
import signal
import struct
import sys
import termios
import time
import zlib


HEADER = struct.Struct("<4sBBHIII")
VERSION = 3
START, FRAME, STOP = 1, 2, 3
PIXEL_BYTES = 8000
BAYER = ((0, 8, 2, 10), (12, 4, 14, 6), (3, 11, 1, 9), (15, 7, 13, 5))
MAX_LINE = 512
MAX_RAW = 1024 * 1024
MAX_EVENTS = 4096
ROOT = Path(__file__).resolve().parents[1]


class TransferError(RuntimeError):
    """재시도하지 않는 입력·전송·프로토콜 오류."""


class ResponseTimeout(TransferError):
    pass


@dataclass(frozen=True)
class Deadlines:
    ready: float = 15.0
    accept: float = 5.0
    diagnostic: float = 30.0
    ack: float = 10.0
    done: float = 5.0
    receive_window: float = 4.5  # 보드의 5초 제한보다 먼저 호스트 쓰기를 끝낸다.
    pause: float = 0.250
    after_done: float = 10.0


def stamp():
    return datetime.now(timezone.utc).isoformat()


def read_frame(path):
    """원자 교체되는 파일 하나를 열어 동일 fd의 내용·mtime을 확보한다."""
    started = time.monotonic()
    try:
        with Path(path).open("rb") as source:
            raw = source.read(192000 + 129)
            info = os.fstat(source.fileno())
    except OSError as error:
        raise TransferError(f"PPM 읽기 실패: {type(error).__name__}") from error
    match = re.match(rb"P6\n# eink sequence ([1-9][0-9]*)\n320 200\n255\n", raw)
    if not match or match.end() > 128 or len(raw) != match.end() + 192000:
        raise TransferError("PPM 헤더·해상도·RGB 데이터 크기 불일치")
    rgb = memoryview(raw)[match.end():]
    packed = bytearray(PIXEL_BYTES)
    for pixel in range(320 * 200):
        offset = pixel * 3
        red, green, blue = rgb[offset:offset + 3]
        gray = ((77 * red + 150 * green + 29 * blue) >> 8) >> 4
        y, x = divmod(pixel, 320)
        # 사용자 확인 정지 이미지와 같은 규칙. 생명력 두 색은 중간톤 보정보다 우선한다.
        if x < 69 and 194 <= y < 199 and (red, green, blue) == (228, 0, 0):
            gray = 15
        elif x < 69 and 194 <= y < 199 and (red, green, blue) == (184, 0, 0):
            gray = 12
        elif 1 <= gray <= 13:
            gray += 1
        # 화면 좌표에 고정한다. 정지 배경이 프레임 번호 때문에 달라지지 않는다.
        if gray * 32 < (2 * BAYER[y % 4][x % 4] + 1) * 15:
            packed[pixel // 8] |= 0x80 >> (x % 8)
    payload = bytes(packed)
    evidence = {
        "source_sequence": int(match.group(1)),
        "read_at": stamp(), "read_started_monotonic": started,
        "read_finished_monotonic": time.monotonic(),
        "file_mtime_ns": info.st_mtime_ns, "file_bytes": info.st_size,
        "ppm_sha256": hashlib.sha256(raw).hexdigest(),
        "bitmap_sha256": hashlib.sha256(payload).hexdigest(),
        "bpp": 1, "payload_bytes": PIXEL_BYTES, "pixel_order": "msb_first",
        "conversion": "corrected_gray_bayer4_fixed", "black": 1, "white": 0,
    }
    return raw, payload, evidence


def pgm(payload):
    if len(payload) != PIXEL_BYTES:
        raise TransferError("1bpp 데이터는 8000바이트여야 합니다")
    pixels = bytearray(320 * 200)
    for index, value in enumerate(payload):
        for bit in range(8):
            pixels[index * 8 + bit] = 0 if value & (0x80 >> bit) else 255
    return b"P5\n320 200\n255\n" + pixels


def packet(kind, session, sequence, payload=b""):
    if not 1 <= session <= 0xFFFFFFFF or not 0 <= sequence <= 0xFFFFFFFF:
        raise TransferError("패킷 session/sequence 범위 오류")
    valid = (
        kind == START and sequence == 0 and len(payload) == 1 and 1 <= payload[0] <= 12
        or kind == FRAME and sequence > 0 and len(payload) == PIXEL_BYTES
        or kind == STOP and sequence > 0 and not payload
    )
    if not valid:
        raise TransferError("패킷 종류·순서·길이 오류")
    body = HEADER.pack(b"EPK1", VERSION, kind, 0, session, sequence, len(payload)) + payload
    return body + struct.pack("<I", zlib.crc32(body) & 0xFFFFFFFF)


def open_serial(port):
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        attrs = termios.tcgetattr(fd)
        attrs[0] = attrs[1] = attrs[3] = 0
        attrs[2] = termios.CLOCAL | termios.CREAD | termios.CS8
        attrs[4] = attrs[5] = termios.B115200
        attrs[6][termios.VMIN] = attrs[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        fcntl.ioctl(fd, termios.TIOCMBIC, struct.pack("I", termios.TIOCM_RTS))
        fcntl.ioctl(fd, termios.TIOCMBIS, struct.pack("I", termios.TIOCM_DTR))
        return fd
    except BaseException:
        os.close(fd)
        raise


class SerialIO:
    """동일 fd의 짧은 읽기/쓰기를 누적한다. 보드 재접속·재전송은 하지 않는다."""

    def __init__(self, fd, raw_log, tx_log, metadata):
        self.fd, self.raw_log, self.tx_log, self.metadata = fd, raw_log, tx_log, metadata
        self.pending = bytearray()
        self.raw_count = 0

    def write_all(self, data, deadline):
        evidence = {"send_started_at": stamp(), "send_started_monotonic": time.monotonic(),
                    "bytes": len(data), "bytes_written": 0}
        self.metadata.setdefault("writes", []).append(evidence)
        while evidence["bytes_written"] < len(data):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ResponseTimeout("패킷 송신 시간초과; 재전송하지 않습니다")
            _, writable, _ = select.select([], [self.fd], [], min(remaining, 0.1))
            if not writable:
                continue
            try:
                count = os.write(self.fd, data[evidence["bytes_written"]:])
            except BlockingIOError:
                continue
            except OSError as error:
                raise TransferError(f"직렬 쓰기 실패: {type(error).__name__}") from error
            if count <= 0:
                raise TransferError("직렬 쓰기 진행 없음")
            if self.tx_log is not None:
                at = evidence["bytes_written"]
                self.tx_log.write(data[at:at + count])
                self.tx_log.flush()
            evidence["bytes_written"] += count
        evidence.update(send_finished_at=stamp(), send_finished_monotonic=time.monotonic())
        return evidence

    def line(self, deadline):
        while True:
            newline = self.pending.find(b"\n")
            if newline >= 0:
                if newline > MAX_LINE:
                    raise TransferError("응답 줄 512바이트 상한 초과")
                raw = bytes(self.pending[:newline]).removesuffix(b"\r")
                del self.pending[:newline + 1]
                try:
                    text = raw.decode("ascii")
                except UnicodeDecodeError as error:
                    raise TransferError("ASCII가 아닌 제어 응답") from error
                events = self.metadata.setdefault("responses", [])
                if len(events) >= MAX_EVENTS:
                    raise TransferError("응답 로그 개수 상한 초과")
                event = {"received_at": stamp(), "received_monotonic": time.monotonic(), "line": text}
                events.append(event)
                return text, event
            if len(self.pending) > MAX_LINE:
                raise TransferError("개행 없는 응답 줄 상한 초과")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ResponseTimeout("응답 시간초과")
            readable, _, _ = select.select([self.fd], [], [], min(remaining, 0.1))
            if not readable:
                continue
            try:
                data = os.read(self.fd, 1024)
            except BlockingIOError:
                continue
            except OSError as error:
                raise TransferError(f"직렬 읽기 실패: {type(error).__name__}") from error
            if not data:
                raise TransferError("직렬 연결 종료")
            self.raw_count += len(data)
            if self.raw_count > MAX_RAW:
                raise TransferError("수신 원본 로그 1MiB 상한 초과")
            if self.raw_log is not None:
                self.raw_log.write(data)
                self.raw_log.flush()
            self.pending.extend(data)


def fields_of(line):
    pieces = line.split(" ")
    if not pieces or not pieces[0] or any(not part for part in pieces):
        raise TransferError("응답 공백 형식 오류")
    fields = {}
    for part in pieces[1:]:
        if part.count("=") != 1:
            raise TransferError("응답 key=value 형식 오류")
        key, value = part.split("=", 1)
        if not key or not value or key in fields:
            raise TransferError("응답 필드 누락·중복")
        fields[key] = value
    return pieces[0], fields


def unsigned(fields, key):
    value = fields.get(key, "")
    if not re.fullmatch(r"0|[1-9][0-9]{0,19}", value) or int(value) > 0xFFFFFFFFFFFFFFFF:
        raise TransferError(f"응답 정수 오류: {key}")
    return int(value)


def exact(fields, keys):
    if set(fields) != set(keys.split()):
        raise TransferError("응답 필드 구성 불일치")


def run_session(fd, frame_path, frames, session, metadata, raw_log=None, tx_log=None,
                deadlines=Deadlines(), stop_requested=lambda: False, first_prefix=None,
                frame_reader=None):
    """열린 직렬 fd로 한 세션만 수행한다. 테스트도 동일 함수를 PTY에서 실행한다."""
    if not 1 <= frames <= 12 or not 1 <= session <= 0xFFFFFFFF:
        raise TransferError("세션·프레임 제한 범위 오류")
    io = SerialIO(fd, raw_log, tx_log, metadata)
    metadata.update(session=session, max_frames=frames, frames=[], started_at=stamp(),
                    start_packets_sent=0, stop_packets_sent=0, status="진행 중",
                    protocol_version=VERSION, bpp=1, payload_bytes=PIXEL_BYTES,
                    pixel_order="msb_first", black=1, white=0,
                    conversion="corrected_gray_bayer4_fixed")
    started = False
    acknowledged = applied = 0
    delay_passed = False
    previous_elapsed = 0

    def response(deadline, boot=False, allow_stats=True, allow_empty=False):
        while True:
            line, event = io.line(deadline)
            lower = line.lower()
            if any(token in lower for token in ("guru meditation", "panic", "abort()", "assert failed", "watchdog", "brownout")):
                raise TransferError("펌웨어 중단·전원 오류 로그")
            if started and ("ESP-ROM:" in line or "rst:0x" in line):
                raise TransferError("START 뒤 재부팅 로그")
            if boot and not line.startswith("USB_FRAME_"):
                continue
            if allow_empty and line == "":
                # ACCEPT 전 남은 빈 줄만 허용한다. 마감·응답 로그 상한은 유지한다.
                continue
            marker, values = fields_of(line)
            if marker == "USB_FRAME_ERROR":
                raise TransferError("펌웨어 오류: " + values.get("reason", "누락"))
            if marker == "USB_FRAME_STATS" and allow_stats:
                continue
            return marker, values, event

    def expect(marker, deadline):
        actual, values, event = response(deadline)
        if actual != marker:
            raise TransferError(f"예상 응답 {marker}, 실제 {actual}")
        return values, event

    def match_session(values):
        if unsigned(values, "session") != session:
            raise TransferError("응답 session 불일치")

    def finish(values, event, stopped=False):
        exact(values, "session reason frames applied")
        match_session(values)
        if unsigned(values, "frames") != acknowledged or unsigned(values, "applied") != applied:
            raise TransferError("DONE 완료·적용 프레임 수 불일치")
        reason = values["reason"]
        if reason == "limit":
            if acknowledged != frames or stopped:
                raise TransferError("DONE limit의 프레임 수·상태 불일치")
        elif reason == "stop":
            if not stopped:
                raise TransferError("STOP 없이 DONE stop 수신")
        elif reason == "time_limit":
            first_send = metadata["frames"][0]["transmission"]["send_started_monotonic"] if metadata["frames"] else None
            host_elapsed = 0 if first_send is None else time.monotonic() - first_send
            if previous_elapsed < 30000000 and host_elapsed < 30:
                raise TransferError("DONE time_limit의 30초 경과 근거 없음")
        else:
            raise TransferError("DONE 오류 또는 알 수 없는 종료 사유")
        metadata["done"] = {**values, **event}
        until = event["received_monotonic"] + deadlines.after_done
        while True:
            try:
                marker, _, _ = response(until, allow_stats=False)
            except ResponseTimeout:
                if io.pending:
                    raise TransferError("종료 관찰 끝에 불완전한 응답 존재")
                break
            raise TransferError("DONE 뒤 추가 실행 응답: " + marker)
        metadata.update(status="통과", finished_at=stamp(),
                        after_done_seconds=time.monotonic() - event["received_monotonic"])
        return metadata

    try:
        marker, values, _ = response(time.monotonic() + deadlines.ready, boot=True)
        if marker != "USB_FRAME_READY" or values != {"version": str(VERSION), "panel": "untouched"}:
            raise TransferError("READY 버전·상태 불일치")
        if stop_requested():
            metadata.update(status="시작 전 취소", finished_at=stamp())
            return metadata
        io.write_all(packet(START, session, 0, bytes([frames])), time.monotonic() + deadlines.accept)
        metadata["start_packets_sent"] = 1
        started = True
        accept_end = time.monotonic() + deadlines.accept
        while True:
            marker, values, _ = response(accept_end, allow_empty=True)
            if marker == "USB_FRAME_READY" and values == {"version": str(VERSION), "panel": "untouched"}:
                # START 이전에 커널·부분 줄에 남은 READY를 허용한다. 마감·START는 갱신하지 않는다.
                continue
            if marker != "USB_FRAME_ACCEPT":
                raise TransferError("ACCEPT 전 예상하지 않은 응답: " + marker)
            break
        exact(values, "session max_frames")
        match_session(values)
        if unsigned(values, "max_frames") != frames:
            raise TransferError("ACCEPT 프레임 한도 불일치")
        diagnostic_end = time.monotonic() + deadlines.diagnostic
        values, _ = expect("USB_FRAME_DELAY_DONE", diagnostic_end)
        if values.get("status") != "pass":
            raise TransferError("화면 전 지연 검사 실패")
        delay_passed = True
        values, ready_event = expect("USB_FRAME_RX", diagnostic_end)
        exact(values, "session")
        match_session(values)
        metadata["delay_passed"] = delay_passed
        stop_reason = None
        while True:
            receive_deadline = ready_event["received_monotonic"] + deadlines.receive_window
            if not acknowledged and io.pending:
                raise TransferError("RX 뒤 FRAME 송신 전에 추가 응답 존재")
            # ACK 이후 250ms 동안 DONE·오류도 읽어 다음 프레임보다 먼저 처리한다.
            if acknowledged:
                pause_end = ready_event["received_monotonic"] + deadlines.pause
                try:
                    marker, values, event = response(pause_end)
                except ResponseTimeout:
                    pass
                else:
                    if marker == "USB_FRAME_DONE":
                        return finish(values, event)
                    raise TransferError("ACK 뒤 예상하지 않은 응답: " + marker)
            if stop_requested():
                stop_reason = "사용자 중단 요청"
            if not stop_reason:
                try:
                    raw, payload, frame_info = (frame_reader or read_frame)(frame_path)
                except TransferError as error:
                    stop_reason = str(error)
            if stop_reason:
                io.write_all(packet(STOP, session, acknowledged + 1), receive_deadline)
                metadata["stop_packets_sent"] = 1
                values, event = expect("USB_FRAME_DONE", time.monotonic() + deadlines.done)
                result = finish(values, event, stopped=True)
                result["stop_reason"] = stop_reason
                if stop_reason != "사용자 중단 요청":
                    raise TransferError(stop_reason)
                return result
            if first_prefix is not None and not acknowledged:
                with Path(str(first_prefix) + "-first.ppm").open("xb") as output:
                    output.write(raw)
                with Path(str(first_prefix) + "-first.pgm").open("xb") as output:
                    output.write(pgm(payload))
            sequence = acknowledged + 1
            wire = packet(FRAME, session, sequence, payload)
            crc = struct.unpack("<I", wire[-4:])[0]
            frame_info.update(sequence=sequence, crc=f"{crc:08x}")
            metadata["frames"].append(frame_info)
            frame_info["transmission"] = io.write_all(wire, receive_deadline)
            marker, values, event = response(time.monotonic() + deadlines.ack)
            if marker == "USB_FRAME_DONE":
                if values.get("reason") != "time_limit":
                    raise TransferError("FRAME ACK 없이 DONE 수신")
                return finish(values, event)
            if marker != "USB_FRAME_ACK":
                raise TransferError("FRAME 뒤 ACK가 아닌 응답: " + marker)
            exact(values, "session seq crc result rx_us display_us clear_us white_us black_us elapsed_us")
            match_session(values)
            if unsigned(values, "seq") != sequence or values["crc"] != f"{crc:08x}":
                raise TransferError("ACK sequence·CRC 불일치")
            if values["result"] not in ("applied", "unchanged"):
                raise TransferError("ACK result 불일치")
            timings = {key: unsigned(values, key) for key in ("rx_us", "display_us", "clear_us", "white_us", "black_us", "elapsed_us")}
            if timings["clear_us"] + timings["white_us"] + timings["black_us"] > timings["display_us"] or timings["elapsed_us"] < previous_elapsed:
                raise TransferError("ACK 시간 구간·누적 시간 불일치")
            previous_elapsed = timings["elapsed_us"]
            frame_info["ack"] = {**values, **event}
            acknowledged += 1
            applied += values["result"] == "applied"
            if acknowledged == frames:
                values, event = expect("USB_FRAME_DONE", time.monotonic() + deadlines.done)
                return finish(values, event)
            ready_event = event
    except BaseException as error:
        metadata.update(status="오류", error_type=type(error).__name__, error=str(error), finished_at=stamp())
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frame-path", type=Path, required=True, help="원자 교체되는 320×200 PPM")
    parser.add_argument("--frames", type=int, default=1, help="최대 프레임 수 1~12 (기본 1)")
    parser.add_argument("--port", help="별도 장치 식별을 완료한 현재 직렬 포트")
    parser.add_argument("--preview", type=Path, metavar="PGM", help="흑백 점무늬 PGM 변환만 저장하고 종료; 보드 접근 없음")
    parser.add_argument("--log-prefix", type=Path, help="원본·JSON·최초 이미지의 공통 경로")
    args = parser.parse_args(argv)
    if not 1 <= args.frames <= 12:
        parser.error("--frames는 1~12여야 합니다")
    if args.preview and args.port or not args.preview and not args.port:
        parser.error("변환은 --preview, 전송은 --port 중 하나를 지정하세요")
    try:
        _, payload, evidence = read_frame(args.frame_path)
        if args.preview:
            args.preview.parent.mkdir(parents=True, exist_ok=True)
            with args.preview.open("xb") as output:
                output.write(pgm(payload))
            print(json.dumps({"status": "변환 완료; 보드 접근 없음", "preview": str(args.preview), **evidence}, ensure_ascii=False))
            return 0
        session = secrets.randbelow(0xFFFFFFFF) + 1
        prefix = args.log_prefix or ROOT / "artifacts/logs" / ("usb-frame-" + datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{session:08x}")
        paths = {key: Path(str(prefix) + suffix) for key, suffix in
                 (("raw", "-runtime.log"), ("tx", "-tx.bin"), ("json", "-observation.json"),
                  ("ppm", "-first.ppm"), ("pgm", "-first.pgm"))}
        if any(path.exists() for path in paths.values()):
            raise TransferError("로그·이미지 경로가 이미 있습니다. 덮어쓰거나 재실행하지 않습니다")
        prefix.parent.mkdir(parents=True, exist_ok=True)
        metadata = {"frame_path": str(args.frame_path), "limitations": [
            "ACK는 드라이버·전원 끄기 함수 반환이며 광학 완료나 입력→화면 지연을 측정하지 않는다.",
            "포트의 동적 장치 식별은 실행 전에 별도로 확인해야 한다."]}
        stop = False

        def request_stop(_signum, _frame):
            nonlocal stop
            stop = True

        previous_handler = signal.signal(signal.SIGINT, request_stop)
        fd = None
        try:
            with paths["raw"].open("xb") as raw_log, paths["tx"].open("xb") as tx_log:
                fd = open_serial(args.port)
                print(f"제한 세션 시작: 최대 {args.frames}장. Ctrl-C는 진행 중 ACK 뒤 STOP을 보냅니다.", flush=True)
                run_session(fd, args.frame_path, args.frames, session, metadata, raw_log, tx_log,
                            stop_requested=lambda: stop, first_prefix=prefix)
            print(f"세션 종료: {metadata['status']} / {paths['json']}")
            return 0
        except BaseException as error:
            metadata.update(status="오류", error_type=type(error).__name__, error=str(error), finished_at=stamp())
            raise
        finally:
            if fd is not None:
                os.close(fd)
            signal.signal(signal.SIGINT, previous_handler)
            with paths["json"].open("x", encoding="utf-8") as output:
                json.dump(metadata, output, ensure_ascii=False, indent=2)
                output.write("\n")
    except (OSError, TransferError) as error:
        print(f"전송 중단: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
