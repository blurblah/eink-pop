#!/usr/bin/env python3
"""Painter 로그와 실제 전송 바이트를 대조하고 수치 목표를 판정한다."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import statistics
import struct

from painter_usb import HEADER, START, FRAME, PATCH, RLE, packet
from painter_patch import decode as decode_patch
from painter_rle import decode as decode_rle


def require(condition, message):
    if not condition: raise ValueError(message)


def distribution(values):
    require(bool(values) and all(math.isfinite(v) and v >= 0 for v in values), '시간 표본 오류')
    return dict(count=len(values),mean=statistics.mean(values),
                p95=sorted(values)[math.ceil(len(values)*.95)-1],maximum=max(values))


def analyze(prefix):
    observation=json.loads(Path(str(prefix)+'-observation.json').read_text())
    wire=Path(str(prefix)+'-tx.bin').read_bytes()
    frames=observation['frames'];done=observation['done']
    require(observation['status']=='통과' and observation['serial_closed'] and
            observation['after_done_seconds']>=10 and done['reason']=='limit' and
            done['poweroff_returned']=='1' and int(done['frames'])==len(frames) and
            2<=len(frames)<=12, '정상 제한 종료/관측 표본 오류')
    offset=0; packets=[]; protocol=None
    while offset<len(wire):
        require(offset+24<=len(wire),'잘린 헤더')
        magic,version,kind,flags,session,seq,size=HEADER.unpack(wire[offset:offset+20])
        require(size<=16000 and offset+size+24<=len(wire),'잘린 본문')
        blob=wire[offset:offset+size+24];payload=blob[20:-4]
        if protocol is None: protocol=version
        require(version==protocol and blob==packet(kind,session,seq,payload,version=version) and session==observation['session'],'패킷/CRC/세션 오류')
        packets.append((kind,seq,payload,struct.unpack('<I',blob[-4:])[0]))
        offset+=len(blob)
    require(len(packets)==len(frames)+1 and packets[0][:2]==(START,0) and
            packets[0][2]==bytes([len(frames)]),'START/패킷 수 오류')
    previous=bytes(16000);changed=[];applied=0;last_source=0
    for i,(f,p) in enumerate(zip(frames,packets[1:])):
        kind,seq,payload,crc=p;ack=f['ack']
        if kind==PATCH:payload=decode_patch(previous,payload)
        elif kind==RLE:payload=decode_rle(payload)
        change=payload!=previous
        require(kind in (FRAME,PATCH,RLE) and seq==i+1==f['sequence']==int(ack['seq']) and
                int(ack['session'])==observation['session'] and
                ack['crc']==f'{crc:08x}' and hashlib.sha256(payload).hexdigest()==f['bitmap_sha256'],
                'FRAME/ACK/본문 해시 불일치')
        require(int(ack['cycles'])==int(change) and
                ack['result']==('applied' if i==0 or change else 'unchanged'),'변경과 실제 구동 불일치')
        require((int(ack['clear_us'])>0 if i==0 else int(ack['clear_us'])==0),'정리 횟수 오류')
        require(f['source_sequence']>last_source,'원본 순서 오류')
        changed.append(change);applied+=ack['result']=='applied'
        previous=payload;last_source=f['source_sequence']
    require(applied==int(done['applied']),'적용 수 오류')
    acks=[f['ack']['received_monotonic'] for f in frames]
    gaps=[(b-a)*1000 for a,b in zip(acks,acks[1:])]
    require(all(g>0 for g in gaps),'ACK 시간 순서 오류')
    gap=distribution(gaps)
    sources=[f['source_sequence'] for f in frames]
    source_steps=[b-a for a,b in zip(sources,sources[1:])]
    host_times={}
    timing_keys=('read_started_monotonic','read_finished_monotonic','transmission')
    if any(any(k in f for k in timing_keys) for f in frames):
        for f in frames:
            require(all(k in f for k in timing_keys),'호스트 시간 기록 일부 누락')
            stamps=[f['read_started_monotonic'],f['read_finished_monotonic'],
                    f['transmission']['send_started_monotonic'],
                    f['transmission']['send_finished_monotonic'],f['ack']['received_monotonic']]
            require(all(math.isfinite(t) for t in stamps) and
                    all(a<=b for a,b in zip(stamps,stamps[1:])),'호스트 시간 순서 오류')
        host_times={
            'read_convert_ms':distribution([(f['read_finished_monotonic']-f['read_started_monotonic'])*1000 for f in frames[1:]]),
            'read_to_ack_ms':distribution([(f['ack']['received_monotonic']-f['read_started_monotonic'])*1000 for f in frames[1:]]),
            'first_read_to_ack_ms':(frames[0]['ack']['received_monotonic']-frames[0]['read_started_monotonic'])*1000,
        }
    times={key:distribution([int(f['ack'][key])/1000 for f in frames[1:]])
           for key in ('rx_us','prepare_us','paint_us','display_us')}
    if protocol>=5:
        times['decode_us']=distribution([int(f['ack']['decode_us'])/1000 for f in frames[1:]])
    driver={}
    if observation.get('timing_diagnostics'):
        captured=[];last_cycle=0
        for f,change in zip(frames,changed):
            if not change:
                require('driver_timing' not in f,'무구동 진단')
                continue
            t=f['driver_timing']
            require(t['seq']==f['sequence'] and t['cycle']>last_cycle and 1<=t['passes']<=26 and
                    sum(t[k] for k in ('discovery_us','rowloop_us','settle_us','neutral_us'))<=t['total_us']<=int(f['ack']['paint_us']),
                    '내부 시간/주사 진단 오류')
            last_cycle=t['cycle']
            if f is not frames[0]:captured.append(t)
        if captured:
            driver=dict(passes=[t['passes'] for t in captured],
                        subsequent_ms={k:distribution([t[k]/1000 for t in captured]) for k in
                                       ('discovery_us','rowloop_us','settle_us','neutral_us','total_us')})
    quality=observation.get('quality','NORMAL') # 옵션 도입 전 v4/v5 호스트는 NORMAL 전용이었다.
    require(quality in ('NORMAL','FAST'),'알 수 없는 화질 모드')
    numeric=len(frames)==12 and all(changed) and gap['mean']<=100 and gap['p95']<=125
    return dict(input_mode=observation.get('input_mode','latest_file'),quality=quality,white_pair=int(observation.get('white_pair',0)),black_pair=int(observation.get('black_pair',0)),frames=len(frames),
                source_sequences=[f['source_sequence'] for f in frames],wire_bytes=len(wire),
                first_display_ms=int(frames[0]['ack']['display_us'])/1000,
                ack_interval_ms=gap,software_completion_hz=1000/gap['mean'],
                source_steps=source_steps,skipped_source_frames=sum(s-1 for s in source_steps),host_timing=host_times,
                subsequent_ms=times,driver_timing=driver,all_frames_changed=all(changed),
                verified_packet_crc=True,quiet_seconds=observation['after_done_seconds'],
                meets_numeric_timing=numeric,meets_single_run_timing=quality=='NORMAL' and numeric,
                limitation='단일 제한 실행. 캡처 재생은 실시간 목표 달성이 아니며 ACK는 광학fps·입력 지연이 아니다.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--log-prefix',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    report=analyze(args.log_prefix)
    with args.output.open('x') as f:json.dump(report,f,ensure_ascii=False,indent=2);f.write('\n')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__': main()
