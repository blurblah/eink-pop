#!/usr/bin/env python3
"""해시 검증된 실제 캡처에서 이전/현재 변환의 바이트와 시간을 비교한다."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import platform
import random
import statistics
import sys
import time
import tracemalloc

from test_painter_clip import reference_convert, clip
from compare_captures import load_capture


def summary(values):
    return dict(count=len(values), mean_ms=statistics.mean(values),
                p95_ms=sorted(values)[math.ceil(len(values)*.95)-1], max_ms=max(values))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists(): parser.error('기존 결과를 덮어쓰지 않습니다')
    frames=load_capture(args.capture)
    times={name:[] for name in ('reference','candidate')}
    records=[]
    for frame in frames:
        rgb=frame['rgb']; expected=reference_convert(rgb)
        row=dict(source_sequence=frame['source_sequence'],
                 packed_sha256=hashlib.sha256(expected).hexdigest())
        for repeat in range(3):
            order=[('reference',reference_convert),('candidate',clip.convert)]
            if (frame['source_sequence']+repeat)%2:order.reverse()
            for name,fn in order:
                start=time.perf_counter_ns(); result=fn(rgb)
                times[name].append((time.perf_counter_ns()-start)/1e6)
                if result != expected:raise RuntimeError(f'픽셀 불일치: {row}')
        records.append(row)
    # 게임 팔레트 밖 고유색이 많은 입력에서 시간·캐시 메모리 상한도 관측한다.
    random_rgb=random.Random(716).randbytes(192000)
    stress={}
    for name,fn in [('reference',reference_convert),('candidate',clip.convert)]:
        elapsed=[]
        for _ in range(5):
            start=time.perf_counter_ns(); result=fn(random_rgb)
            elapsed.append((time.perf_counter_ns()-start)/1e6)
            assert result==reference_convert(random_rgb)
        tracemalloc.start(); fn(random_rgb); _,peak=tracemalloc.get_traced_memory();tracemalloc.stop()
        stress[name]={**summary(elapsed),'peak_traced_bytes':peak}
    report=dict(python=sys.version,platform=platform.platform(),rounds_per_frame=3,
                capture=str(args.capture.resolve()),frames=records,
                timing={name:summary(v) for name,v in times.items()},random_rgb=stress,
                pixel_mismatches=0,limitation='CPU 변환 벤치마크. 패널/USB 완료 속도는 측정하지 않음.')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k not in ('frames','python','platform','capture')},ensure_ascii=False,indent=2))


if __name__=='__main__': main()
