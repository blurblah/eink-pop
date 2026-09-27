#!/usr/bin/env python3
"""보드 없이 수집한 PPM을 같은 보정의 세 흑백 방식으로 비교한다."""
import argparse
import base64
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
import struct
import zlib

from capture_frames import read_ppm, HEADER
from offline_display import PROFILE, timing_model, transitions, estimate
from usb_frame import BAYER

MODES = ('bayer4', 'bayer2', 'threshold')
LABELS = {'bayer4': '기존 4×4 점무늬', 'bayer2': '2×2 점무늬', 'threshold': '단순 흑백'}
BAYER2 = ((0, 2), (3, 1))


def convert(rgb):
    """USB v3와 같은 16단계 밝기·생명력 보정, 양자화만 비교한다."""
    if len(rgb) != 192000:
        raise ValueError('RGB 데이터 길이 오류')
    buffers = {mode: bytearray(8000) for mode in MODES}
    for i in range(64000):
        r, g, b = rgb[i*3:i*3+3]
        gray = ((77*r + 150*g + 29*b) >> 8) >> 4
        y, x = divmod(i, 320)
        if x < 69 and 194 <= y < 199 and (r,g,b) == (228,0,0):
            gray = 15
        elif x < 69 and 194 <= y < 199 and (r,g,b) == (184,0,0):
            gray = 12
        elif 1 <= gray <= 13:
            gray += 1
        decisions = (gray*32 < (2*BAYER[y%4][x%4]+1)*15,
                     gray*8 < (2*BAYER2[y%2][x%2]+1)*15, gray < 8)
        for mode, black in zip(MODES, decisions):
            if black:
                buffers[mode][i//8] |= 128 >> (i%8)
    return {mode: bytes(buf) for mode,buf in buffers.items()}


def png(rgb):
    def chunk(kind, data):
        return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
    scan = b''.join(b'\x00'+rgb[y*960:(y+1)*960] for y in range(200))
    raw = b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',320,200,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(scan))+chunk(b'IEND',b'')
    return 'data:image/png;base64,'+base64.b64encode(raw).decode()


def load_capture(directory):
    directory = Path(directory)
    manifest = json.loads((directory/'capture.json').read_text())
    entries = manifest.get('frames', [])
    if manifest.get('schema') != 1 or manifest.get('status') != '완료' or not 1 <= len(entries) <= 360:
        raise ValueError('정상 완료된 1~360장 수집만 지원합니다')
    frames=[];last_seq=0;last_time=-1;names=set()
    for entry in entries:
        name=entry['file'];seconds=entry['captured_seconds']
        if not re.fullmatch(r'frame-[0-9]{4}\.ppm',name) or name in names:
            raise ValueError('수집 파일 이름·중복 오류')
        path=directory/name
        if path.is_symlink():
            raise ValueError('수집 파일의 심볼릭 링크는 지원하지 않습니다')
        if isinstance(seconds,bool) or not isinstance(seconds,(int,float)) or not math.isfinite(seconds) or not 0 <= seconds <= 31 or seconds < last_time:
            raise ValueError('수집 시간 순서·범위 오류')
        raw,seq=read_ppm(path)
        if seq != entry['source_sequence'] or seq <= last_seq or entry['bytes'] != len(raw) or entry['sha256'] != hashlib.sha256(raw).hexdigest():
            raise ValueError('수집 번호·길이·해시 불일치')
        skipped=max(0,seq-last_seq-1) if last_seq else 0
        if entry['skipped_before'] != skipped:
            raise ValueError('누락 번호 기록 불일치')
        frames.append({'source_sequence':seq,'seconds':seconds,'skipped_before':skipped,'rgb':raw[HEADER.match(raw).end():]})
        names.add(name);last_seq=seq;last_time=seconds
    return frames


def summarize(frames, mode):
    records=[f['modes'][mode] for f in frames]
    deltas=records[1:]
    supported=[d['prediction']['display_us'] for d in deltas if d['prediction']['display_us'] is not None]
    return {'label':LABELS[mode], 'frames':len(records), 'transitions':len(deltas),
            'mean_black_percent':statistics.mean(r['total_black_pixels']/640 for r in records),
            'mean_changed_pixels':statistics.mean(d['changed_pixels'] for d in deltas) if deltas else None,
            'max_physical_rows':max((d['physical_rows'] for d in deltas),default=0),
            'unchanged':sum(d['changed_pixels']==0 for d in deltas),
            'supported':len(supported),
            'interpolated':sum(d['prediction']['status'].startswith('보간') for d in deltas),
            'supported_mean_us':statistics.mean(supported) if supported else None}


def build_report(directory, start_sequence=1, end_sequence=None):
    source=load_capture(directory)
    if start_sequence < 1 or end_sequence is not None and end_sequence < start_sequence:
        raise ValueError('선택할 게임 번호 범위 오류')
    source=[f for f in source if f['source_sequence'] >= start_sequence and (end_sequence is None or f['source_sequence'] <= end_sequence)]
    if not source:
        raise ValueError('선택 범위에 수집 화면이 없습니다')
    source[0]['skipped_before']=0
    model=timing_model(json.loads(PROFILE.read_text()))
    frames=[];previous=None
    for original in source:
        bitmaps=convert(original['rgb'])
        frame={k:v for k,v in original.items() if k!='rgb'}
        frame['source_png']=png(original['rgb']);frame['modes']={}
        for mode,payload in bitmaps.items():
            delta=transitions(previous[mode],payload) if previous else transitions(payload,payload)
            prediction=estimate(model,delta['physical_rows'],bool(delta['white_pixels']),bool(delta['black_pixels']))
            if previous is None:prediction={'status':'최초 화면: 모델 대상 아님','display_us':None}
            frame['modes'][mode]={'payload':base64.b64encode(payload).decode(),
                                  'total_black_pixels':sum(b.bit_count() for b in payload), **delta,'prediction':prediction}
        frames.append(frame);previous=bitmaps
    return {'schema':1,'frames':frames,'summary':{mode:summarize(frames,mode) for mode in MODES},
            'model':model,'skipped':sum(f['skipped_before'] for f in frames),
            'limitations':['재생은 수집 시각 기준의 원본 게임 움직임입니다. 패널에서 이 속도로 표시된다는 예측이 아닙니다.',
                           '인접한 수집 화면의 변화량입니다. 실제 ACK 대기 후 최신 화면 선택에서는 비교 쌍과 부하가 달라집니다.',
                           '시간 모델은 기존 드라이버·두 극성·30~102행만 지원합니다. 표본이 없는 중간 구간은 미검증 보간입니다.',
                           '예측 지원 표본의 평균을 전체 장면 속도로 해석할 수 없습니다. 변화 픽셀 감소가 같은 비율의 속도 향상을 뜻하지 않습니다.',
                           '잔상·깜빡임·장시간 구동 안전성은 계산하지 않습니다. 점무늬 제거는 중간톤 손실을 동반합니다.']}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--start-sequence',type=int,default=1)
    parser.add_argument('--end-sequence',type=int)
    args=parser.parse_args(argv)
    try:
        report=build_report(args.capture,args.start_sequence,args.end_sequence)
        encoded=json.dumps(report,ensure_ascii=False,allow_nan=False).replace('<','\\u003c')
        html=Path(__file__).with_suffix('.html').read_text().replace('__REPORT_DATA__',encoded)
        args.output.mkdir(parents=True,exist_ok=False)
        (args.output/'index.html').write_text(html)
        (args.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
        print(f"{len(report['frames'])}장·세 방식 비교: {args.output/'index.html'}")
        return 0
    except (OSError,ValueError,KeyError,TypeError) as error:
        print(f'비교 중단: {error}')
        return 1


if __name__=='__main__':
    raise SystemExit(main())
