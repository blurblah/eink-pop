#!/usr/bin/env python3
"""실제 전송 로그를 검증하고 같은 입력/모드끼리 Markdown 비교표를 만든다."""
import argparse
from collections import OrderedDict
import json
from pathlib import Path

from analyze_painter_run import analyze

ROOT=Path(__file__).resolve().parents[1]


def compare(prefixes):
    groups=OrderedDict()
    for prefix in prefixes:
        prefix=Path(prefix).resolve()
        result=analyze(prefix)
        observation=json.loads(Path(str(prefix)+'-observation.json').read_text())
        identity=tuple((f['source_sequence'],f['bitmap_sha256']) for f in observation['frames'])
        key=(result['quality'],result['input_mode'],bool(observation.get('timing_diagnostics')),identity)
        try:label=str(prefix.parent.relative_to(ROOT/'artifacts/performance-loop-20260929'))
        except ValueError:label=str(prefix.parent.relative_to(ROOT))
        groups.setdefault(key,[]).append((label,prefix,result))
    return groups


def markdown(groups):
    lines=['# Painter 실측 비교','',
           '각 전송 원본의 CRC·복원 해시·구동 횟수·정상 종료·OFF·10초 정지를 재검증한 결과다. '
           '원본 번호와 전체 화면 해시, 화질, 입력 방식, 내부 계측 여부가 모두 같은 실행만 묶었다.',
           '', '현재 목표·중단 조건·채택/기각·다음 작업은 [반복 기록](성능-개선-반복.md)을 따른다. '
           '이 표는 단일 실행들의 비교이며 광학fps·입력 지연·통계적 유의성을 검증한 결과가 아니다. '
           '동일 픽셀·모드 그룹 자체가 동일 광학 품질을 보증하지는 않는다.','']
    for number,(key,runs) in enumerate(groups.items(),1):
        quality,mode,diagnostic,identity=key
        lines += [f'## 그룹 {number}: {quality} / {mode} / 내부 계측 {"켜짐" if diagnostic else "꺼짐"}',
                  '',f'원본 번호: {", ".join(str(seq) for seq,_ in identity)}. 초기화 이후11개 간격, p95는 nearest-rank다.','',
                  '| 실행 | 완료 평균 ms | p95 ms | 소프트웨어 장/초 | prepare ms | paint ms | 전송 바이트 | NORMAL 수치 기준 |',
                  '| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |']
        for label,prefix,r in runs:
            a=r['ack_interval_ms'];s=r['subsequent_ms']
            status='충족' if r['meets_single_run_timing'] else '미달' if quality=='NORMAL' else '별도 모드'
            lines.append(f"| {label} | {a['mean']:.3f} | {a['p95']:.3f} | {r['software_completion_hz']:.2f} | "
                         f"{s['prepare_us']['mean']:.3f} | {s['paint_us']['mean']:.3f} | {r['wire_bytes']} | {status} |")
        if len(runs)>1:
            best=min(runs,key=lambda x:x[2]['ack_interval_ms']['mean'])
            first=runs[0][2]['ack_interval_ms']['mean'];saving=(first-best[2]['ack_interval_ms']['mean'])/first*100
            lines+=['',f'가장 짧은 단일 실행은 `{best[0]}`이며 첫 실행 대비 완료 간격이 {saving:.2f}% 줄었다. 채택 여부는 회차 기록을 따른다.']
        if quality=='FAST':lines+=['','FAST는 이전 잔상·흐릿함 관찰이 있어 NORMAL 화질 목표의 달성으로 처리하지 않는다.']
        lines+=['','원본 로그:', '']+[f'- `{prefix.relative_to(ROOT)}`' for _,prefix,_ in runs]+['']
    return '\n'.join(lines)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('prefixes',type=Path,nargs='+')
    args=parser.parse_args();groups=compare(args.prefixes)
    with args.output.open('x') as output:output.write(markdown(groups))
    print(f'{sum(map(len,groups.values()))}개 실행 원본 재검증, {len(groups)}개 비교 그룹: {args.output}')

if __name__=='__main__':main()
