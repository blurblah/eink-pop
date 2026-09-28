#!/usr/bin/env python3
"""H716 NORMAL에 최신 게임 화면을 보내는 EPK1 v5 / 무손실 행 패치 전송."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import struct
import sys
import time
import zlib

from prepare_painter_clip import convert, unpack
from compare_captures import load_capture
from painter_patch import encode as encode_patch
from usb_frame import (HEADER, START, FRAME, STOP, SerialIO, TransferError,
                       ResponseTimeout, open_serial, fields_of, unsigned, exact, stamp)

VERSION = 5
PATCH = 4
PIXEL_BYTES = 16000
READY = dict(version='5', panel='untouched', driver='afa69d8', board='H716',
             quality='NORMAL', gray='4', direct='1', profile='0', packed='1', timing='0')


def packet(kind, session, sequence, payload=b'', version=VERSION):
    valid = (kind == START and sequence == 0 and len(payload) == 1 and 1 <= payload[0] <= 12
             or kind == FRAME and sequence > 0 and len(payload) == PIXEL_BYTES
             or version == 5 and kind == PATCH and sequence > 1 and len(payload) < PIXEL_BYTES
             or kind == STOP and sequence > 0 and not payload)
    if version not in (4,5) or not valid or not 1 <= session <= 0xffffffff or not 0 <= sequence <= 0xffffffff:
        raise TransferError('패킷 종류·세션·순서·길이 오류')
    body = HEADER.pack(b'EPK1', version, kind, 0, session, sequence, len(payload)) + payload
    return body + struct.pack('<I', zlib.crc32(body) & 0xffffffff)


def read_frame(path):
    started = time.monotonic()
    try:
        with Path(path).open('rb') as source:
            raw = source.read(192129)
            stat = os.fstat(source.fileno())
    except OSError as error:
        raise TransferError(f'PPM 읽기 실패: {type(error).__name__}') from error
    match = re.match(rb'P6\n# eink sequence ([1-9][0-9]{0,19})\n320 200\n255\n', raw)
    if not match or match.end() > 128 or len(raw) != match.end() + 192000:
        raise TransferError('PPM 헤더·해상도·길이 오류')
    payload = convert(raw[match.end():])
    return raw, payload, dict(source_sequence=int(match.group(1)),
        read_started_monotonic=started, read_finished_monotonic=time.monotonic(),
        file_mtime_ns=stat.st_mtime_ns, file_bytes=stat.st_size,
        ppm_sha256=hashlib.sha256(raw).hexdigest(), bitmap_sha256=hashlib.sha256(payload).hexdigest(),
        conversion='corrected_gray_nearest4_no_dither', bpp=2, payload_bytes=16000,
        white=0, black=3, pixel_order='msb_first')


def capture_reader(directory, start, count):
    """고정 입력 비교용. 검증된 캡처의 연속 번호를 ACK마다 한 장씩 읽는다."""
    frames = load_capture(directory)
    manifest = json.loads((Path(directory)/'capture.json').read_text())
    selected = [(f,entry) for f,entry in zip(frames,manifest['frames'])
                if start <= f['source_sequence'] < start+count]
    if [f['source_sequence'] for f,_ in selected] != list(range(start,start+count)):
        raise TransferError('재생 입력은 누락 없는 연속 원본 번호가 필요합니다')
    source = iter(selected)
    def read(_):
        try: expected,entry = next(source)
        except StopIteration as error: raise TransferError('재생 입력 소진') from error
        raw,payload,info = read_frame(Path(directory)/entry['file'])
        if info['source_sequence'] != expected['source_sequence'] or info['ppm_sha256'] != entry['sha256']:
            raise TransferError('재생 입력이 사전 검증 후 변경됨')
        info['captured_seconds'] = expected['seconds']
        return raw,payload,info
    return read


def run_session(fd, frame_path, frames, session, metadata, raw_log=None, tx_log=None,
                stop_requested=lambda: False, frame_reader=read_frame, after_done=10.0,
                first_prefix=None, timing_diagnostics=False, quality='NORMAL'):
    if not 1 <= frames <= 12 or not 1 <= session <= 0xffffffff:
        raise TransferError('세션·프레임 범위 오류')
    if quality not in ('NORMAL','FAST'):raise TransferError('지원하지 않는 화질 모드')
    io = SerialIO(fd, raw_log, tx_log, metadata)
    expected_ready = {**READY,'timing':str(int(timing_diagnostics)),'quality':quality}
    metadata.update(status='진행 중', started_at=stamp(), session=session, max_frames=frames,
                    protocol_version=VERSION, bpp=2, timing_diagnostics=timing_diagnostics, quality=quality,
                    frames=[], start_packets_sent=0, stop_packets_sent=0)
    started = False
    deadline = time.monotonic() + 15
    count = applied = 0
    last_elapsed = last_source = 0
    last_timing_cycle = 0
    previous_payload = bytes(PIXEL_BYTES)

    def response(until, boot=False):
        while True:
            line, event = io.line(min(until, deadline))
            if any(t in line.lower() for t in ('panic','guru meditation','abort()','assert failed','watchdog','brownout','corruption')):
                raise TransferError('보드 오류 로그')
            if started and ('ESP-ROM:' in line or 'rst:0x' in line):
                raise TransferError('START 후 재부팅')
            if boot and not line.startswith('PAINTER_USB_'):
                continue
            if not line:  # CDC 연결 경계의 CRLF만 허용, 마감은 갱신하지 않는다.
                continue
            marker, fields = fields_of(line)
            if marker == 'PAINTER_USB_ERROR':
                raise TransferError('펌웨어 오류: ' + fields.get('reason', '누락'))
            return marker, fields, event

    def expect(marker, seconds=10):
        actual, v, event = response(time.monotonic()+seconds)
        if actual != marker:
            raise TransferError(f'응답 순서 오류: {actual}, 예상 {marker}')
        return v, event

    def same_session(v):
        if unsigned(v,'session') != session:
            raise TransferError('응답 세션 불일치')

    def finish(v, event, stopped=False):
        exact(v, 'session reason frames applied poweroff_returned')
        same_session(v)
        if unsigned(v,'frames') != count or unsigned(v,'applied') != applied:
            raise TransferError('종료 프레임 수 불일치')
        if unsigned(v,'poweroff_returned') != (1 if metadata['frames'] else 0):
            raise TransferError('종료 전원OFF 확인 실패')
        if v['reason'] == 'limit':
            if count != frames or stopped: raise TransferError('종료 한도 불일치')
        elif v['reason'] == 'stop':
            if not stopped: raise TransferError('요청하지 않은 STOP')
        elif v['reason'] == 'time_limit':
            if stopped or (last_elapsed < 30000000 and time.monotonic()-sent_at < 30):
                raise TransferError('시간 한도 근거 없음')
        else:
            raise TransferError('정상 종료가 아님')
        metadata['done'] = {**v, **event}
        until = time.monotonic() + after_done
        try:
            io.line(until)
        except ResponseTimeout:
            if io.pending: raise TransferError('종료 뒤 미완성 로그')
        else:
            raise TransferError('종료 뒤 추가 로그')
        metadata.update(status='통과', after_done_seconds=time.monotonic()-event['received_monotonic'], finished_at=stamp())
        return metadata

    marker, v, _ = response(deadline, boot=True)
    if marker != 'PAINTER_USB_READY' or v != expected_ready:
        raise TransferError('READY 버전·설정 불일치')
    if stop_requested():
        metadata.update(status='시작 전 취소', finished_at=stamp())
        return metadata
    sent_at = time.monotonic()
    deadline = sent_at + 45
    io.write_all(packet(START,session,0,bytes([frames])), min(deadline,time.monotonic()+5))
    metadata['start_packets_sent'] = 1
    started = True
    accept_until = time.monotonic()+5
    while True:
        marker,v,_ = response(accept_until)
        if marker == 'PAINTER_USB_READY' and v == expected_ready: continue
        if marker != 'PAINTER_USB_ACCEPT': raise TransferError('ACCEPT 순서 오류')
        break
    exact(v,'session max_frames'); same_session(v)
    if unsigned(v,'max_frames') != frames: raise TransferError('ACCEPT 한도 오류')
    v, event = expect('PAINTER_USB_RX',5)
    exact(v,'session'); same_session(v)
    while True:
        receive_deadline = min(deadline,event['received_monotonic']+4.5)
        if stop_requested():
            io.write_all(packet(STOP,session,count+1),receive_deadline)
            metadata['stop_packets_sent'] = 1
            v,event = expect('PAINTER_USB_DONE',5)
            return finish(v,event,True)
        # ACK 이후에 열어 읽는다. 큐의 과거 화면을 차례로 보내지 않는다.
        fresh_until = min(receive_deadline,time.monotonic()+1)
        while True:
            raw,payload,info = frame_reader(frame_path)
            source_seq = info['source_sequence']
            if source_seq < last_source: raise TransferError('원본 게임 번호 역행')
            if source_seq > last_source: break
            if time.monotonic() >= fresh_until: raise TransferError('게임 화면 갱신 정지')
            time.sleep(.005)
        last_source = source_seq
        if first_prefix is not None and not count:
            with Path(str(first_prefix)+'-first.ppm').open('xb') as f: f.write(raw)
            with Path(str(first_prefix)+'-first.pgm').open('xb') as f:
                f.write(b'P5\n320 200\n255\n'+bytes(255-v*85 for v in unpack(payload)))
        expected_changed = payload != previous_payload
        encoded = encode_patch(previous_payload,payload) if count else None
        kind = FRAME if encoded is None else PATCH
        wire = packet(kind,session,count+1,payload if encoded is None else encoded)
        crc = struct.unpack('<I',wire[-4:])[0]
        info.update(sequence=count+1,crc=f'{crc:08x}',packet_type=kind,wire_payload_bytes=len(wire)-24)
        metadata['frames'].append(info)
        info['transmission'] = io.write_all(wire,receive_deadline)
        marker,v,event = response(time.monotonic()+10)
        if marker == 'PAINTER_USB_DONE':
            if v.get('reason') != 'time_limit': raise TransferError('ACK 없는 종료')
            return finish(v,event)
        if timing_diagnostics and expected_changed:
            if marker != 'PAINTER_USB_TIMING':raise TransferError('완료 진단 누락')
            exact(v,'session seq cycle passes discovery_us rowloop_us settle_us neutral_us total_us')
            same_session(v)
            timing={key:unsigned(v,key) for key in ('seq','cycle','passes','discovery_us','rowloop_us','settle_us','neutral_us','total_us')}
            if (timing['seq']!=count+1 or timing['cycle']<=last_timing_cycle or not 1<=timing['passes']<=26 or
                    sum(timing[k] for k in ('discovery_us','rowloop_us','settle_us','neutral_us'))>timing['total_us']):
                raise TransferError('완료 진단 회차·시간 오류')
            last_timing_cycle=timing['cycle']
            info['driver_timing']={**timing,**event}
            marker,v,event=response(time.monotonic()+10)
        if marker != 'PAINTER_USB_ACK': raise TransferError('ACK 순서 오류')
        exact(v,'session seq crc result rx_us decode_us display_us clear_us prepare_us paint_us cycles elapsed_us')
        same_session(v)
        if unsigned(v,'seq') != count+1 or v['crc'] != f'{crc:08x}': raise TransferError('ACK 순서·CRC 오류')
        if v['result'] not in ('applied','unchanged'): raise TransferError('ACK 적용 결과 오류')
        times = {k:unsigned(v,k) for k in ('rx_us','decode_us','display_us','clear_us','prepare_us','paint_us','cycles','elapsed_us')}
        if kind==FRAME and times['decode_us']:
            raise TransferError('전체 FRAME에 패치 복원 시간')
        if 'driver_timing' in info and info['driver_timing']['total_us']>times['paint_us']:
            raise TransferError('드라이버 내부 시간이 paint 시간 초과')
        if times['cycles'] > 1 or times['clear_us']+times['prepare_us']+times['paint_us'] > times['display_us'] or times['elapsed_us'] < last_elapsed:
            raise TransferError('ACK 시간·주기 오류')
        if (count == 0 and times['clear_us'] <= 0) or (count and times['clear_us']):
            raise TransferError('최초/후속 전체 정리 불일치')
        if times['cycles'] != int(expected_changed) or v['result'] != ('applied' if not count or expected_changed else 'unchanged'):
            raise TransferError('실제 입력과 구동 결과 불일치')
        if v['result']=='unchanged' and any(times[k] for k in ('clear_us','prepare_us','paint_us','cycles')):
            raise TransferError('무변경 화면의 구동 감지')
        info['ack'] = {**v,**event}
        previous_payload = payload
        last_elapsed = times['elapsed_us']
        count += 1
        applied += v['result']=='applied'
        if count == frames or last_elapsed >= 30000000:
            v,event = expect('PAINTER_USB_DONE',5)
            return finish(v,event)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    source=parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--frame-path',type=Path)
    source.add_argument('--replay-capture',type=Path,help='동일 입력 성능 비교용 캡처 디렉터리')
    parser.add_argument('--start-sequence',type=int,default=40)
    parser.add_argument('--port',required=True,help='실행 전 동일 장치로 확인한 포트')
    parser.add_argument('--frames',type=int,default=12)
    parser.add_argument('--timing-diagnostics',action='store_true',help='시간 캡처 진단 앱에만 사용')
    parser.add_argument('--quality',choices=('NORMAL','FAST'),default='NORMAL',help='FAST는 별도 속도 진단 앱에만 사용')
    parser.add_argument('--log-prefix',type=Path,required=True)
    args=parser.parse_args(argv)
    if not 1 <= args.frames <= 12: parser.error('--frames는1~12')
    paths={k:Path(str(args.log_prefix)+suffix) for k,suffix in
           [('raw','-runtime.log'),('tx','-tx.bin'),('json','-observation.json'),('ppm','-first.ppm'),('pgm','-first.pgm')]}
    if any(p.exists() for p in paths.values()): parser.error('기존 로그·이미지를 덮어쓰거나 반복 실행하지 않습니다')
    args.log_prefix.parent.mkdir(parents=True,exist_ok=True)
    metadata={'input_mode':'captured_replay' if args.replay_capture else 'latest_file'};fd=None;stop=False
    def request_stop(*_):
        nonlocal stop
        stop=True
    previous_handler=signal.signal(signal.SIGINT,request_stop)
    try:
        reader=capture_reader(args.replay_capture,args.start_sequence,args.frames) if args.replay_capture else read_frame
        with paths['raw'].open('xb') as raw,paths['tx'].open('xb') as tx:
            fd=open_serial(args.port)
            run_session(fd,args.frame_path,args.frames,secrets.randbelow(0xffffffff)+1,metadata,raw,tx,
                        stop_requested=lambda:stop,frame_reader=reader,first_prefix=args.log_prefix,
                        timing_diagnostics=args.timing_diagnostics,quality=args.quality)
        print('제한 전송 종료: '+metadata['status'])
        return 0
    except (OSError,ValueError,TransferError) as error:
        metadata.update(status='오류',error=str(error),finished_at=stamp())
        print('전송 중단: '+str(error),file=sys.stderr)
        return 1
    finally:
        if fd is not None: os.close(fd)
        metadata['serial_closed']=True
        signal.signal(signal.SIGINT,previous_handler)
        paths['json'].write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+'\n')

if __name__=='__main__': raise SystemExit(main())
