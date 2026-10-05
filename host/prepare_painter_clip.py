#!/usr/bin/env python3
"""검증된 게임 수집에서 연속 12장을 점무늬 없는 H716 4명도 시험 입력으로 만든다."""
import argparse
import hashlib
import json
from pathlib import Path
from compare_captures import load_capture

COUNT = 12
WIDTH, HEIGHT = 320, 200
PACKED_BYTES = WIDTH * HEIGHT // 4


def convert(rgb):
    if len(rgb) != WIDTH * HEIGHT * 3:
        raise ValueError('RGB 길이는 320×200×3이어야 합니다')
    rgb = bytes(rgb)
    packed = bytearray(PACKED_BYTES)
    # 게임의 반복되는 가로 4픽셀을 한 번만 변환한다. 캐시는 프레임 내에서만
    # 유지되어 다른 장면으로 넘어가도 누적되지 않으며 최대16000개다.
    blocks = {}
    for i in range(PACKED_BYTES):
        block = rgb[i*12:i*12+12]
        value = blocks.get(block)
        if value is None:
            value = 0
            for offset in (0, 3, 6, 9):
                r, g, b = block[offset:offset+3]
                gray = (77*r + 150*g + 29*b) >> 12
                if 1 <= gray <= 13:
                    gray += 1
                value = (value << 2) | (3 - (gray + 2) // 5)
            blocks[block] = value
        packed[i] = value
    # 위치에 의존하는 생명력 보정은 일반 색 캐시와 분리한다.
    for y in range(194, 199):
        for x in range(69):
            i = y * WIDTH + x
            color = rgb[i*3:i*3+3]
            if color == b'\xe4\x00\x00':
                level = 0
            elif color == b'\xb8\x00\x00':
                level = 1
            else:
                continue
            shift = 6 - 2 * (i % 4)
            packed[i//4] = (packed[i//4] & ~(3 << shift)) | (level << shift)
    return bytes(packed)


SKIN_COLORS = frozenset(((220,128,108), (216,184,160), (176,112,96)))


def convert_skin(rgb):
    """피부만 어둡게 하되 같은 명도 배경에 닿는 경계는 기존 대비를 보존한다."""
    rgb = bytes(rgb)
    packed = bytearray(convert(rgb))
    skin = [tuple(rgb[i:i+3]) in SKIN_COLORS for i in range(0,len(rgb),3)]
    for i, selected in enumerate(skin):
        if not selected:
            continue
        y,x = divmod(i,WIDTH)
        neighbors = ([i-1] if x else []) + ([i+1] if x+1<WIDTH else [])
        neighbors += ([i-WIDTH] if y else []) + ([i+WIDTH] if y+1<HEIGHT else [])
        # 비피부는 변경하지 않으므로 앞에서 처리한 피부의 영향을 받지 않는다.
        if any(not skin[j] and ((packed[j//4] >> (6-2*(j%4))) & 3)==2 for j in neighbors):
            continue
        shift = 6-2*(i%4)
        packed[i//4] = (packed[i//4] & ~(3<<shift)) | (2<<shift)
    return bytes(packed)


convert_skin.tone = 'skin_edge_v1'


def unpack(packed):
    if len(packed) != PACKED_BYTES:
        raise ValueError('2bpp 길이 오류')
    return bytes((value >> shift) & 3 for value in packed for shift in (6,4,2,0))


def select_frames(frames, start):
    selected = [f for f in frames if start <= f['source_sequence'] < start + COUNT]
    if [f['source_sequence'] for f in selected] != list(range(start,start+COUNT)):
        raise ValueError('누락 없는 연속 12장이 필요합니다')
    return selected


def prepare(capture, output, start=40):
    selected = select_frames(load_capture(capture),start)
    buffers = [convert(f['rgb']) for f in selected]
    clip_hash = hashlib.sha256(b''.join(buffers)).hexdigest()
    manifest = {'schema':1,'source_capture':str(Path(capture).resolve()),'frame_count':COUNT,
                'source_width':WIDTH,'source_height':HEIGHT,'bpp':2,'white':0,'black':3,
                'conversion':'corrected_gray_nearest4_no_dither','scale':2,'x':160,'y':70,
                'clip_sha256':clip_hash,'source_span_seconds':selected[-1]['seconds']-selected[0]['seconds'],
                'frames':[]}
    output=Path(output)
    output.mkdir(parents=True,exist_ok=False)
    previous=None
    for i,(frame,packed) in enumerate(zip(selected,buffers)):
        pixels=unpack(packed)
        matrix=[0]*16
        if previous is not None:
            for old,new in zip(previous,pixels):matrix[old*4+new]+=1
        manifest['frames'].append({'source_sequence':frame['source_sequence'],'captured_seconds':frame['seconds'],
                                  'rgb_sha256':hashlib.sha256(frame['rgb']).hexdigest(),
                                  'packed_sha256':hashlib.sha256(packed).hexdigest(),
                                  'level_counts':[pixels.count(n) for n in range(4)],
                                  'transition_counts_4x4':matrix if previous is not None else None})
        (output/f'frame-{i+1:02d}.bin').write_bytes(packed)
        (output/f'frame-{i+1:02d}.pgm').write_bytes(b'P5\n320 200\n255\n'+bytes(255-v*85 for v in pixels))
        previous=pixels
    header=['// 자동 생성: prepare_painter_clip.py. 입력·해시는 manifest.json 참조.','#pragma once',
            '#include <stdint.h>',f'#define GAME_CLIP_ID "{clip_hash[:16]}"',
            f'static constexpr uint32_t GAME_FRAME_COUNT = {COUNT};',
            f'static const uint8_t GAME_PACKED[{COUNT}][{PACKED_BYTES}] = {{']
    for packed in buffers:
        header.append('{')
        header.extend(','.join(f'0x{x:02x}' for x in packed[i:i+32])+',' for i in range(0,len(packed),32))
        header.append('},')
    header.append('};\n')
    header_path=output/'painter_game_frames.h';header_path.write_text('\n'.join(header))
    manifest['header_sha256']=hashlib.sha256(header_path.read_bytes()).hexdigest()
    (output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    return manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--start-sequence',type=int,default=40)
    args=parser.parse_args()
    result=prepare(args.capture,args.output,args.start_sequence)
    print(f"연속 {result['frame_count']}장 생성, clip SHA-256 {result['clip_sha256']}")

if __name__=='__main__':main()
