#!/usr/bin/env python3
"""확인된 S3 드라이버의 호출 수·명목 신호 시간. 실제 실행 시간 예측은 아니다."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import statistics

ROOT=Path(__file__).resolve().parents[1]
DRIVER=ROOT/'repos/LilyGo-EPD47/src'
# 계산과 대조한 소스만 허용한다. 변경되면 실행 순서를 다시 검토해야 한다.
HASHES={
 'epd_driver.c':'a8ba823883e11b9a908085aa68298c77a0b25cf70c102bca080a9fe5656aa68d',
 'ed047tc1.c':'b3fb9c97377f29d498c5d2c62c0421fe5d8519c19953777f74e9904c47877e64',
 'i2s_data_bus.c':'c36fa93d785784590eea3cc813e3aa8a70237e16ded589be49449e1a028c8979',
 'rmt_pulse.c':'6edf74c017c4964104c86efe298fd73597a79bfb20afc78ae2a2a8409e3435fb'}


def checked_source(directory=DRIVER):
    sources={}
    for name,digest in HASHES.items():
        raw=(directory/name).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=digest:
            raise ValueError(f'검토한 소스와 다릅니다: {name}')
        sources[name]=raw.decode()
    return sources


def waveform(source,name):
    match=re.search(r'\b'+name+r'\[15\]\s*=\s*\{([^}]+)\}',source)
    if not match:raise ValueError('15단계 파형 배열 누락')
    values=[int(s.strip()) for s in match[1].split(',')]
    if len(values)!=15 or any(v<=0 for v in values):raise ValueError('파형 배열 오류')
    return values


def scan(y,height,contrast,skipping):
    """feed_display→skip_row의 순서. 이전 단계의 skipping 상태도 이어진다."""
    if not 0<=y<540 or not 1<=height<=540-y or contrast<=0 or skipping<0:
        raise ValueError('행 영역·펄스·상태 오류')
    result={'active_rows':0,'dma_rows':0,'fast_skips':0,'rmt_row_calls':0,'row_pulse_ticks':0}
    for row in range(540):
        if y<=row<y+height:
            result['active_rows']+=1;skipping=0;pulse=contrast+50;dma=True
        else:
            if skipping==0:pulse=contrast+50;dma=True
            elif skipping==1:pulse=10+50;dma=True
            else:pulse=45+5;dma=False;result['fast_skips']+=1
            skipping+=1
        result['dma_rows']+=int(dma);result['row_pulse_ticks']+=pulse;result['rmt_row_calls']+=1
    if skipping==0: # 패널 마지막 행까지 그렸으면 파이프라인 한 행 추가
        result['dma_rows']+=1;result['row_pulse_ticks']+=contrast+50;result['rmt_row_calls']+=1
    return result,skipping


def budget(y,height,sources,initial_skipping=2):
    phases=waveform(sources['epd_driver.c'],'contrast_cycles_4_white')+waveform(sources['epd_driver.c'],'contrast_cycles_4')
    totals={key:0 for key in scan(y,height,phases[0],initial_skipping)[0]}
    state=initial_skipping
    for contrast in phases:
        counters,state=scan(y,height,contrast,state)
        for key,value in counters.items():totals[key]+=value
    # RMT10MHz: 한 tick=0.1us. 단계마다 start4회/end2회의38us를 더한다.
    rmt_us=totals['row_pulse_ticks']/10+30*38
    return {**totals,'physical_y':y,'physical_height':height,'initial_skipping':initial_skipping,
            'phases':30,'task_creations':60,'task_deletions':60,
            'queue_send_calls':30*height,'queue_receive_calls':30*height,
            'rmt_total_calls':totals['rmt_row_calls']+30*6,
            'lcd_data_bytes':totals['dma_rows']*248,
            'lcd_data_only_us':totals['dma_rows']*248/10,
            'rmt_programmed_pulse_us':rmt_us,
            'caller_delay_requested_us':170000,'producer_delay_requested_us':150000,
            'producer_delay_note':'소비자와 겹친다. 150ms를 전체 시간에 그대로 더하지 않는다.',
            'callback_yield_requests_removed':totals['dma_rows'],
            'time_prediction_us':None,
            'note':'RMT 펄스는 같은 채널에서 직렬이다. LCD 전송과 RMT는 겹칠 수 있어 두 시간은 합산하지 않는다. 태스크 지연은 tick 기준 요청량이며 측정 시간이 아니다.'}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--y',type=int,default=70);parser.add_argument('--rows',type=int,default=64)
    args=parser.parse_args(argv)
    try:
        sources=checked_source()
        profile=json.loads((ROOT/'host/data/panel_timing_v3.json').read_text())
        measured=statistics.mean(s['display_us'] for s in profile['samples'])
        output={'source_sha256':HASHES,'conditions':'S3·IDF4.4.7·10MHz RMT/I80·tick1ms·양극성15단계씩. callback만 수정된 소스.',
                'steady':budget(args.y,args.rows,sources),'after_clear':budget(args.y,args.rows,sources,0),
                'baseline_mean_display_us':measured,
                'delay_only_arithmetic':{'minus_caller_requested_us':measured-170000,'minus_all_requested_us':measured-320000,
                    'note':'소스 요청량을 뺀 산술 예시이며 속도 예측·보장 하한이 아니다. 병렬 대기·스케줄링 효과가 미측정이다.'}}
        print(json.dumps(output,ensure_ascii=False,indent=2));return 0
    except (OSError,ValueError,KeyError) as error:print(f'분석 중단: {error}');return 1


if __name__=='__main__':raise SystemExit(main())
