#!/usr/bin/env python3
"""실제 NORMAL 파형의 주사 수와, 기존 주사 시간을 유지하는 예산을 분석한다."""
import argparse
import ast
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import subprocess

from analyze_painter_run import analyze, distribution, require
from compare_captures import load_capture
from prepare_painter_clip import convert

ROOT=Path(__file__).resolve().parents[1]
DRIVER=ROOT/'repos/EPD_Painter'


def array(source,name,shape):
    clean=re.sub(r'/\*.*?\*/|//[^\n]*','',source,flags=re.S)
    found=re.search(r'\b'+re.escape(name)+r'(?:\s*\[[^\]]+\])*\s*=\s*\{',clean)
    require(found is not None,'파형 배열 누락: '+name)
    start=found.end()-1;depth=0;end=None
    for i in range(start,len(clean)):
        depth+=(clean[i]=='{')-(clean[i]=='}')
        if depth==0:end=i+1;break
    require(end is not None,'파형 배열 종료 누락')
    value=ast.literal_eval(clean[start:end].replace('{','[').replace('}',']'))
    def fill(v,dims):
        if not dims:
            require(type(v) is int and 0<=v<=3,'파형 코드 범위 오류')
            return v
        require(isinstance(v,list) and len(v)<=dims[0],'파형 배열 크기 오류')
        zero=0 if len(dims)==1 else []
        return [fill(x,dims[1:]) for x in v+[zero]*(dims[0]-len(v))]
    return fill(value,shape)


def tables(source):
    normal={k:array(source,'normal_'+k,(3,13)) for k in ('lighter','darker')}
    direct=array(source,'DIRECT_H716_NORMAL',(4,4,26))
    for f in range(1,4):
        for t in range(1,4):
            if f!=t:require(any(direct[f][t]),'미등록 직접 전이')
    return normal,direct


def transition_counts(previous,current):
    require(len(previous)==len(current)==16000,'2bpp 화면 크기 오류')
    counts=[0]*16
    for a,b in zip(previous,current):
        if a==b:continue
        for shift in (6,4,2,0):
            f=(a>>shift)&3;t=(b>>shift)&3
            if f!=t:counts[f*4+t]+=1
    return counts


def required_passes(counts,direct):
    require(len(counts)==16 and all(type(n) is int and n>=0 for n in counts),'전이 수 오류')
    active=[i for i,n in enumerate(counts) if n and i//4!=i%4]
    if not active:return 0
    passes=13 # 현재 NORMAL 4명도 엔진의 기본 주사 수.
    for i in active:
        f,t=divmod(i,4)
        if f and t:
            train=direct[f][t]
            require(any(train),'미등록 직접 전이')
            passes=max(passes,max(p+1 for p,code in enumerate(train) if code))
    return passes


def section(source,start,end):
    at=source.index(start)
    return source[at:source.index(end,at)]


def build_report(capture,calibration):
    measured=analyze(calibration)
    obs=json.loads(Path(str(calibration)+'-observation.json').read_text())
    require(obs.get('timing_diagnostics') and measured['all_frames_changed'],'완료 진단 계측 필요')
    ready=[r['line'] for r in obs['responses'] if r['line'].startswith('PAINTER_USB_READY ')]
    require(bool(ready),'계측 드라이버 식별자 누락')
    match=re.search(r'\bdriver=([a-f0-9]{7,40})\b',ready[0])
    require(match is not None,'계측 드라이버 식별자 오류')
    ref=match[1]
    def old(name):return subprocess.check_output(['git','-C',str(DRIVER),'show',ref+':src/'+name],text=True)
    train=(DRIVER/'src/LilyGo_H716_Trains.h').read_text()
    require(train==old('LilyGo_H716_Trains.h'),'계측 이후 파형 변경')
    require((DRIVER/'src/EPD_Painter_presets.h').read_text()==old('EPD_Painter_presets.h'),'계측 이후 보드 설정 변경')
    current=(DRIVER/'src/EPD_Painter.cpp').read_text();before=old('EPD_Painter.cpp')
    for start,end in [('void EPD_Painter::sendRow(', '// =============================================================================\n// begin()'),
                      ('void EPD_Painter::_paint_task_body()', '// GPIO set/clear via direct register write')]:
        require(section(current,start,end)==section(before,start,end),'계측 이후 주사 구현 변경')
    config=(DRIVER/'src/EPD_Painter.h').read_text()
    gap=int(re.search(r'int pass_gap_us_normal\s*=\s*(\d+)',config)[1])
    require(gap==int(re.search(r'int pass_gap_us_normal\s*=\s*(\d+)',old('EPD_Painter.h'))[1]),'계측 이후 대기 변경')
    normal,direct=tables(train)
    captures=load_capture(capture)
    packed={f['source_sequence']:convert(f['rgb']) for f in captures}
    previous=bytes(16000);validation=[]
    for f in obs['frames']:
        current=packed[f['source_sequence']]
        require(hashlib.sha256(current).hexdigest()==f['bitmap_sha256'],'계측과 원본 픽셀 불일치')
        predicted=required_passes(transition_counts(previous,current),direct)
        require(predicted==f['driver_timing']['passes'],'실제 주사 수와 예측 불일치')
        validation.append(predicted);previous=current
    rows=[];previous=bytes(16000)
    for f in captures:
        current=packed[f['source_sequence']];counts=transition_counts(previous,current)
        rows.append(dict(source_sequence=f['source_sequence'],required_passes=required_passes(counts,direct),
                         changed_source_pixels=sum(counts),deep_lightening_pixels=counts[14]))
        previous=current
    intervals=[f['driver_timing'] for f in obs['frames'][1:]]
    scan_wait=distribution([(t['rowloop_us']+t['settle_us'])/1000 for t in intervals])
    periods=distribution([(t['rowloop_us']+t['settle_us'])/t['passes']/1000 for t in intervals])
    lengths={f'{f}->{t}':max(i+1 for i,c in enumerate(direct[f][t]) if c)
             for f in range(1,4) for t in range(1,4) if f!=t}
    return dict(driver_reference=ref,waveform_sha256=hashlib.sha256(train.encode()).hexdigest(),
                quality='NORMAL',pass_gap_us=gap,direct_lengths=lengths,
                calibrated_frame_count=len(validation),predicted_and_measured_passes=validation,
                captured_frames=len(rows),subsequent_pass_histogram=dict(Counter(r['required_passes'] for r in rows[1:])),
                measured_scan_and_wait_ms=scan_wait,measured_pass_period_ms=periods,
                software_only_ceiling_hz=1000/scan_wait['mean'],
                scan_wait_exceeds_100ms=scan_wait['mean']>100,frames=rows,
                limitations=['주사 수 계산은 현재 전체 직접 전이·NORMAL·같은 픽셀 조건이다.',
                             '시간 예산은 계측한12장에만 해당한다. 다른 장면의 시간은 예측하지 않는다.',
                             '주사+대기 시간을 유지하고 나머지 비용을0으로 놓은 한정 상한이다. 패널의 물리적 최고 속도가 아니다.',
                             '명암·잔상·온도·입력 지연·실제 광학fps를 예측하지 않는다.'])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture',type=Path,required=True)
    parser.add_argument('--calibration-prefix',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();report=build_report(args.capture,args.calibration_prefix)
    with args.output.open('x') as f:json.dump(report,f,ensure_ascii=False,indent=2);f.write('\n')
    print(json.dumps({k:v for k,v in report.items() if k!='frames'},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
