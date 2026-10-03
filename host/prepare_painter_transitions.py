#!/usr/bin/env python3
"""FAST/NORMAL 비교용 합성 전이표를 만든다. 장치 실행·파형 변경은 하지 않는다."""
import argparse
import base64
import hashlib
import json
from pathlib import Path

from compare_captures import png

WIDTH, HEIGHT = 320, 200
# 기존 게임 변환을 통과했을 때 정확히 명도0,1,2,3이 되는 RGB 회색.
INPUT_GRAY = (255, 160, 80, 0)
DIGITS = ('111101101101111', '010110010010111',
          '111001111100111', '111001111001111')


def regions():
    """각 칸의 왼쪽은 목표 명도 참조, 오른쪽은 이전 명도에서 전이한다."""
    return [dict(source=source, target=target,
                 reference=[36 + target * 64, 32 + source * 36, 24, 24],
                 probe=[64 + target * 64, 32 + source * 36, 24, 24])
            for source in range(4) for target in range(4)]


def card(comparison):
    levels = bytearray(WIDTH * HEIGHT)

    def rect(box, level):
        x, y, width, height = box
        for row in range(y, y + height):
            levels[row * WIDTH + x:row * WIDTH + x + width] = bytes([level]) * width

    def digit(value, x, y):
        for i, bit in enumerate(DIGITS[value]):
            if bit == '1':
                rect((x + i % 3 * 2, y + i // 3 * 2, 2, 2), 3)

    for level in range(4):
        digit(level, 58 + level * 64, 12)
        digit(level, 14, 39 + level * 36)
    for entry in regions():
        rect(entry['reference'], entry['target'])
        rect(entry['probe'], entry['target'] if comparison else entry['source'])
    return bytes(levels)


def prepare(output, pairs=1):
    if type(pairs) is not int or not 1 <= pairs <= 6:
        raise ValueError('비교 쌍은1~6개(2~12장)여야 합니다')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    manifest = dict(schema=1, status='완료', kind='synthetic_transition_diagnostic',
                    frames=[], pairs=pairs, regions=regions(),
                    limitations=['합성 입력의 명목 시각이며 실제 게임 캡처나 실행 시간이 아니다.',
                                 '미리보기는 목표 픽셀만 표현한다. 잔상·실물 회색 밝기를 예측하지 않는다.',
                                 '게임 성능 시험이 아니다. 모든 전이가 함께 있어 전체 주사 시간이 영향을 받는다.'])
    for index in range(pairs * 2):
        comparison = bool(index % 2)
        levels = card(comparison)
        rgb = b''.join(bytes([INPUT_GRAY[level]]) * 3 for level in levels)
        sequence = index + 1
        raw = f'P6\n# eink sequence {sequence}\n320 200\n255\n'.encode() + rgb
        name = f'frame-{sequence:04d}.ppm'
        (output / name).write_bytes(raw)
        manifest['frames'].append(dict(file=name, source_sequence=sequence,
                                       captured_seconds=index / 10, bytes=len(raw),
                                       sha256=hashlib.sha256(raw).hexdigest(), skipped_before=0,
                                       phase='비교' if comparison else '준비'))
        if index < 2:
            # 미리보기 밝기도 명목4명도로 표현하며 실제 패널 응답과 구분한다.
            preview = b''.join(bytes([255 - level * 85]) * 3 for level in levels)
            image = base64.b64decode(png(preview).split(',', 1)[1])
            (output / ('comparison.png' if comparison else 'preparation.png')).write_bytes(image)
    (output / 'capture.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--pairs', type=int, default=1)
    args = parser.parse_args()
    result = prepare(args.output, args.pairs)
    print(f"진단 입력{len(result['frames'])}장 생성: {args.output}")


if __name__ == '__main__':
    main()
