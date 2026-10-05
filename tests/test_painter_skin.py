"""명세 기반 피부 변환, 경계 가독성, C/Python 일치와 실제 호스트 식별 검증."""
import json
from pathlib import Path
import random
import subprocess
import tempfile
import unittest
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'host'))
import prepare_painter_clip as clip
import painter_native as native
import painter_usb as host

COLORS=((220,128,108),(216,184,160),(176,112,96))

def reference(rgb):
    base=clip.unpack(clip.convert(rgb));out=list(base)
    colors=[tuple(rgb[i:i+3]) for i in range(0,len(rgb),3)]
    for y in range(200):
        for x in range(320):
            i=y*320+x
            if colors[i] not in COLORS:continue
            preserve=False
            for dy,dx in ((0,-1),(0,1),(-1,0),(1,0)):
                yy,xx=y+dy,x+dx
                if 0<=yy<200 and 0<=xx<320:
                    j=yy*320+xx
                    if colors[j] not in COLORS and base[j]==2:preserve=True
            if not preserve:out[i]=2
    return bytes(sum(out[i+q]<<(6-2*q) for q in range(4)) for i in range(0,64000,4))

class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory();cls.binary=Path(cls.tmp.name)/native.DEFAULT.name
        native.build(cls.binary);cls.convert=staticmethod(native.load(cls.binary,skin_contrast=True))
    @classmethod
    def tearDownClass(cls):cls.tmp.cleanup()
    def test_colors_edges_and_unchanged_pixels(self):
        rng=random.Random(35);rgb=bytearray(rng.randbytes(192000))
        for i in [0,319,63680,63999]+[rng.randrange(64000) for _ in range(3000)]:
            rgb[i*3:i*3+3]=bytes(rng.choice(COLORS))
        expected=reference(rgb)
        self.assertEqual(clip.convert_skin(rgb),expected);self.assertEqual(self.convert(rgb),expected)
        old=clip.unpack(clip.convert(rgb));new=clip.unpack(expected)
        for i,(a,b) in enumerate(zip(old,new)):
            if tuple(rgb[i*3:i*3+3]) not in COLORS:self.assertEqual(a,b)
    def test_skin_against_background(self):
        for background,expected in [((0,0,0),2),((255,255,255),2),((85,85,85),1)]:
            for at in (0,319,10000,63999):
                rgb=bytearray(bytes(background)*64000);rgb[at*3:at*3+3]=bytes(COLORS[0])
                self.assertEqual(clip.unpack(self.convert(rgb))[at],expected)
                self.assertEqual(clip.convert_skin(rgb),self.convert(rgb))
    def test_uniform_and_life(self):
        for color in COLORS:self.assertEqual(self.convert(bytes(color)*64000),b'\xaa'*16000)
        for color in ((228,0,0),(184,0,0),(252,252,216)):
            rgb=bytes(color)*64000;self.assertEqual(self.convert(rgb),clip.convert(rgb))
    def test_host_tone_record_and_invalid_size(self):
        path=Path(self.tmp.name)/'tone.ppm';path.write_bytes(b'P6\n# eink sequence 1\n320 200\n255\n'+bytes(COLORS[0])*64000)
        _,packed,info=host.read_frame(path,converter=self.convert)
        self.assertEqual(info['tone'],'skin_edge_v1');self.assertEqual(packed,b'\xaa'*16000)
        for f in (clip.convert_skin,self.convert):
            with self.assertRaises(ValueError):f(b'bad')
    def test_c_bounds(self):
        root=Path(self.tmp.name);code='''
#include <assert.h>
#include <string.h>
#include "painter_convert.c"
int main(void){
 uint8_t rgb[192002],out[16002];memset(rgb,0,sizeof rgb);memset(out,0x7c,sizeof out);
 for(int i=1;i<192001;i+=3){rgb[i]=220;rgb[i+1]=128;rgb[i+2]=108;}
 assert(painter_convert_skin(rgb+1,192000,out+1,16000)==0);
 assert(out[0]==0x7c&&out[16001]==0x7c);
 for(int i=1;i<16001;++i)assert(out[i]==0xaa);
 assert(painter_convert_skin(0,192000,out+1,16000)==-1);
 assert(painter_convert_skin(rgb+1,191999,out+1,16000)==-1);
 assert(painter_convert_skin(rgb+1,192000,out+1,15999)==-1);
 assert(painter_convert_skin(rgb+1,192000,0,16000)==-1);
}
''';(root/'skin.c').write_text(code)
        subprocess.run(['cc','-std=c11','-fsanitize=address,undefined','-g','-I',str(native.SOURCE.parent),str(root/'skin.c'),'-o',str(root/'skin')],check=True)
        subprocess.run([str(root/'skin')],check=True,timeout=10)

if __name__=='__main__':unittest.main()
