#!/usr/bin/env python3
"""4명도 변환의 값·비트 순서·생명력·연속 입력 검증."""
import hashlib
import json
from pathlib import Path
import re
import random
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'host'))
import prepare_painter_clip as clip

def reference_convert(rgb):
    """최적화 이전의 픽셀별 기준 구현. 캐시/후처리를 사용하지 않는다."""
    packed = bytearray(16000)
    for i in range(64000):
        r,g,b = rgb[i*3:i*3+3]
        gray = ((77*r + 150*g + 29*b) >> 8) >> 4
        y,x = divmod(i,320)
        if x < 69 and 194 <= y < 199 and (r,g,b) == (228,0,0):
            gray = 15
        elif x < 69 and 194 <= y < 199 and (r,g,b) == (184,0,0):
            gray = 12
        elif 1 <= gray <= 13:
            gray += 1
        packed[i//4] |= (3-(gray+2)//5) << (6-2*(i%4))
    return bytes(packed)

class ClipTests(unittest.TestCase):
    def test_reference_colors_and_spatial_overrides(self):
        rgb = bytearray(random.Random(716).randbytes(192000))
        # 같은 색이 보정 영역 안팎·같은 패킹 바이트에 섞여도 일치해야 한다.
        for y in (0,193,194,195,198,199):
            for x in range(73):
                i = y*320+x
                rgb[i*3:i*3+3] = bytes((228 if x%2 else 184,0,0))
        self.assertEqual(clip.convert(rgb), reference_convert(rgb))
        self.assertEqual(clip.convert(memoryview(rgb)), reference_convert(rgb))

    def test_rgb_axes_and_frame_cache_isolation(self):
        colors = [(v,0,0) for v in range(256)] + [(0,v,0) for v in range(256)]
        colors += [(0,0,v) for v in range(256)] + [(v,v,v) for v in range(256)]
        rgb = (bytes(c for color in colors for c in color)*63)[:192000]
        for source in (rgb, bytes(reversed(rgb)), rgb):
            self.assertEqual(clip.convert(source), reference_convert(source))

    def test_endpoints(self):
        self.assertEqual(clip.convert(bytes(192000)),b'\xff'*16000)
        self.assertEqual(clip.convert(b'\xff'*192000),bytes(16000))
    def test_packing(self):
        rgb=bytes(c for v in (255,170,85,0) for c in (v,v,v))*16000
        self.assertEqual(clip.convert(rgb),b'\x1b'*16000)
        self.assertEqual(clip.unpack(b'\x1b'*16000),bytes([0,1,2,3])*16000)
    def test_uniform_midtones(self):
        for gray in range(16):
            level=3-((gray+1 if 1<=gray<=13 else gray)+2)//5
            values=clip.unpack(clip.convert(bytes([gray*16])*192000))
            self.assertEqual(set(values),{level})
    def test_life_region(self):
        for color,inside in [((228,0,0),0),((184,0,0),1)]:
            values=clip.unpack(clip.convert(bytes(color)*64000))
            for x,y in [(0,194),(68,198)]:self.assertEqual(values[y*320+x],inside)
            for x,y in [(69,194),(68,193),(0,199)]:self.assertEqual(values[y*320+x],2)
    def test_lengths(self):
        for value in [b'',bytes(192001)]:
            with self.assertRaises(ValueError):clip.convert(value)
        with self.assertRaises(ValueError):clip.unpack(bytes(15999))
    def test_missing_or_out_of_order(self):
        entries=[{'source_sequence':i} for i in range(40,52)]
        self.assertEqual(clip.select_frames(entries,40),entries)
        for wrong in [entries[:-1],entries[::-1],entries[:4]+entries[5:]]:
            with self.assertRaises(ValueError):clip.select_frames(wrong,40)
    def test_generated_header_and_source_integrity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cap=root/'capture';cap.mkdir();frames=[]
            for i in range(12):
                raw=f'P6\n# eink sequence {i+1}\n320 200\n255\n'.encode()+bytes([i*20])*192000
                name=f'frame-{i+1:04d}.ppm';(cap/name).write_bytes(raw)
                frames.append({'file':name,'source_sequence':i+1,'captured_seconds':i/12,
                               'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'skipped_before':0})
            (cap/'capture.json').write_text(json.dumps({'schema':1,'status':'완료','frames':frames}))
            result=clip.prepare(cap,root/'out',1)
            packed=b''.join((root/'out'/f'frame-{i+1:02d}.bin').read_bytes() for i in range(12))
            header=(root/'out/painter_game_frames.h').read_text()
            self.assertEqual(bytes(int(x,16) for x in re.findall(r'0x([0-9a-f]{2})',header)),packed)
            self.assertEqual(result['clip_sha256'],hashlib.sha256(packed).hexdigest())
            self.assertEqual(result['frames'][0]['transition_counts_4x4'],None)
            self.assertEqual(sum(result['frames'][1]['transition_counts_4x4']),64000)
            (cap/'frame-0002.ppm').write_bytes(b'corrupted')
            with self.assertRaises(ValueError):clip.prepare(cap,root/'bad',1)
            self.assertFalse((root/'bad').exists())

if __name__=='__main__':unittest.main()
