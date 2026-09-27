#!/usr/bin/env python3
"""검증된 v3 기록의 동일 픽셀을 제한 재생한다. --port 없이는 파일 준비만 한다."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import statistics
import sys

import usb_frame as usb
from offline_display import load_session


def recording(prefix):
    observation=json.loads(Path(str(prefix)+'-observation.json').read_text())
    configs=[r['line'] for r in observation.get('responses',[]) if r.get('line','').startswith('USB_FRAME_STATS metric=config ')]
    if len(configs)!=1:raise ValueError('원본 드라이버 설정이 유일하지 않습니다')
    _,values=usb.fields_of(configs[0]);driver=values.get('driver','')
    if not re.fullmatch('[0-9a-f]{40}',driver):raise ValueError('원본 드라이버 커밋 오류')
    return load_session(prefix,driver),driver


def prepare(prefix,output):
    frames,driver=recording(prefix)
    if len(frames)<2:raise ValueError('처음 화면을 제외한 비교에는 두 장 이상이 필요합니다')
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    manifest={'baseline_driver':driver,'baseline_prefix':str(Path(prefix).resolve()),'frames':[]}
    for frame in frames:
        payload=base64.b64decode(frame['payload'],validate=True)
        gray=usb.pgm(payload).split(b'\n',3)[3]
        rgb=bytes(channel for value in gray for channel in (value,value,value))
        raw=f"P6\n# eink sequence {frame['source_sequence']}\n320 200\n255\n".encode()+rgb
        name=f"frame-{frame['sequence']:02d}.ppm";path=output/name;path.write_bytes(raw)
        _,actual,info=usb.read_frame(path)
        if actual!=payload:raise ValueError('복원한 PPM의 픽셀이 원본과 다릅니다')
        manifest['frames'].append({'file':name,'source_sequence':frame['source_sequence'],
                                   'ppm_sha256':hashlib.sha256(raw).hexdigest(),
                                   'bitmap_sha256':info['bitmap_sha256']})
    (output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    return frames,manifest


class ReplayReader:
    def __init__(self,directory,manifest,metadata,expected_driver):
        if not re.fullmatch('[0-9a-f]{40}',expected_driver):raise ValueError('대상 드라이버 커밋 오류')
        self.directory=Path(directory);self.frames=manifest['frames'];self.metadata=metadata
        self.expected_driver=expected_driver;self.position=0

    def __call__(self,_unused):
        configs=[r['line'] for r in self.metadata.get('responses',[]) if r.get('line','').startswith('USB_FRAME_STATS metric=config ')]
        if len(configs)!=1:raise usb.TransferError('수신한 드라이버 설정이 유일하지 않습니다')
        _,actual=usb.fields_of(configs[0])
        expected={'driver':self.expected_driver,'source':'320x200','bpp':'1','msb_first':'1',
                  'black':'1','white':'0','scale':'2','x':'160','y':'70','refresh':'initial_clear_then_delta'}
        if any(actual.get(k)!=v for k,v in expected.items()):
            raise usb.TransferError('대상 드라이버·표시 설정 불일치; FRAME 송신 거부')
        if self.position>=len(self.frames):raise usb.TransferError('저장된 프레임 수 초과')
        frame=self.frames[self.position]
        if frame['file']!=f'frame-{self.position+1:02d}.ppm':raise usb.TransferError('재생 파일 순서 오류')
        raw,payload,info=usb.read_frame(self.directory/frame['file'])
        if info['ppm_sha256']!=frame['ppm_sha256'] or info['bitmap_sha256']!=frame['bitmap_sha256'] or info['source_sequence']!=frame['source_sequence']:
            raise usb.TransferError('준비 후 재생 입력이 바뀌었습니다')
        self.position+=1;info['recorded_replay']=True
        return raw,payload,info


def comparison(before,after):
    if len(before)!=len(after) or len(before)<2:raise ValueError('완전한 두 장 이상 대응 기록이 필요합니다')
    for a,b in zip(before,after):
        if a['payload']!=b['payload'] or a['source_sequence']!=b['source_sequence']:
            raise ValueError('비교 화면·순서가 다릅니다')
    rows=[]
    for a,b in zip(before[1:],after[1:]):
        rows.append({'sequence':a['sequence'],'rows':a['physical_rows'],
                     **{key:{'before':a[key],'after':b[key],'delta':b[key]-a[key]} for key in ('display_us','white_us','black_us')}})
    mean=lambda key,side:statistics.mean(r[key][side] for r in rows)
    return {'first_frame_excluded':True,'samples':len(rows),'frames':rows,
            'mean':{key:{'before':mean(key,'before'),'after':mean(key,'after'),
                         'change_percent':100*(mean(key,'after')/mean(key,'before')-1) if mean(key,'before') else None}
                    for key in ('display_us','white_us','black_us')},
            'limitations':['단일 실행의 대응 비교이며 통계적 성능 보장이 아닙니다.',
                           '호스트 입력은 비트맵을 RGB로 복원한 것으로 원래 RGB 변환 비용과 다릅니다. ACK 간격 전체를 콜백 효과로 해석하지 않습니다.',
                           '표시 호출 반환 시간이며 광학 완료·잔상·깜빡임은 별도 관찰이 필요합니다.']}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline-prefix',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True,help='새 준비·결과 디렉터리')
    p.add_argument('--port',help='실행 직전에 동일 장치로 확인한 포트; 생략하면 준비만')
    p.add_argument('--driver-commit',help='--port 사용 시 필수인 대상 드라이버40자리 커밋')
    args=p.parse_args(argv)
    if args.port and (not args.driver_commit or not re.fullmatch('[0-9a-f]{40}',args.driver_commit)):
        p.error('--port에는 유효한 --driver-commit이 필요합니다')
    try:
        before,manifest=prepare(args.baseline_prefix,args.output)
        print(f'{len(before)}장 원본과 비트 일치 확인. 준비 위치: {args.output}',flush=True)
        if not args.port:return 0
        prefix=args.output/'replay';metadata={'replay_source':manifest,'limitations':['복원 PPM을 사용한 동일 비트맵 재생; 원래 RGB 비용과 다름']}
        reader=ReplayReader(args.output,manifest,metadata,args.driver_commit)
        session=secrets.randbelow(0xffffffff)+1;fd=None;stop=False
        def request_stop(_sig,_frame):
            nonlocal stop
            stop=True
        previous=signal.signal(signal.SIGINT,request_stop)
        try:
            with Path(str(prefix)+'-runtime.log').open('xb') as raw,Path(str(prefix)+'-tx.bin').open('xb') as tx:
                fd=usb.open_serial(args.port)
                usb.run_session(fd,None,len(before),session,metadata,raw,tx,
                                stop_requested=lambda:stop,frame_reader=reader)
        finally:
            if fd is not None:os.close(fd)
            signal.signal(signal.SIGINT,previous)
            Path(str(prefix)+'-observation.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+'\n')
        if metadata.get('status')!='통과' or metadata.get('done',{}).get('reason')!='limit' or reader.position!=len(before):
            raise usb.TransferError('재생이 중단되어 완전한 비교 자료가 아닙니다')
        after=load_session(prefix,args.driver_commit)
        result=comparison(before,after)
        (args.output/'comparison.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps(result['mean'],ensure_ascii=False),flush=True)
        return 0
    except (OSError,ValueError,KeyError,usb.TransferError) as error:
        print(f'재생 중단: {error}',file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
