"""승인된 전체 톤 A의 독립 명세·명암 경계·Python/C/실제 전송 일치."""
import random,sys,tempfile,unittest,subprocess
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'host'))
import prepare_painter_clip as clip
import painter_native as native
import painter_usb as host

def reference(rgb):
    levels=[]
    for i in range(64000):
        r,g,b=rgb[i*3:i*3+3];y,x=divmod(i,320)
        luma=(77*r+150*g+29*b)//256
        level=3 if luma<43 else 2 if luma<128 else 1 if luma<213 else 0
        if 194<=y<199 and x<69 and g==b==0:
            if r==228:level=0
            elif r==184:level=1
        levels.append(level)
    return bytes(sum(levels[i+q]<<(6-2*q) for q in range(4)) for i in range(0,64000,4))

class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory();cls.binary=Path(cls.tmp.name)/native.DEFAULT.name
        native.build(cls.binary);cls.convert=staticmethod(native.load(cls.binary,global_tone=True))
    @classmethod
    def tearDownClass(cls):cls.tmp.cleanup()
    def test_full_ramp_boundaries_and_random(self):
        ramp=bytes(v for i in range(64000) for v in (i%256,)*3)
        random_rgb=random.Random(36).randbytes(192000)
        for rgb in (ramp,random_rgb):
            expected=reference(rgb)
            self.assertEqual(clip.convert_global(rgb),expected)
            self.assertEqual(self.convert(rgb),expected)
        levels=clip.unpack(self.convert(ramp))[:256]
        self.assertTrue(all(a>=b for a,b in zip(levels,levels[1:])))
        self.assertEqual([levels[x] for x in (0,42,43,127,128,212,213,255)],[3,3,2,2,1,1,0,0])
    def test_uniform_position_independent_and_hud(self):
        for color in ((220,128,108),(216,184,160),(176,112,96),(252,252,216),(228,0,0),(184,0,0)):
            rgb=bytes(color)*64000;expected=reference(rgb)
            self.assertEqual(self.convert(rgb),expected);self.assertEqual(clip.convert_global(rgb),expected)
            levels=clip.unpack(expected)
            if color[1]:self.assertEqual(len(set(levels)),1)
            else:
                old=clip.unpack(clip.convert(rgb))
                for y in range(194,199):self.assertEqual(levels[y*320:y*320+69],old[y*320:y*320+69])
    def test_host_identity_invalid_input_and_conflict(self):
        path=Path(self.tmp.name)/'frame.ppm';path.write_bytes(b'P6\n# eink sequence 1\n320 200\n255\n'+b'\xff'*192000)
        _,data,info=host.read_frame(path,self.convert)
        self.assertEqual(info['tone'],'global_a_v1');self.assertEqual(data,bytes(16000))
        with self.assertRaises(ValueError):native.load(self.binary,skin_contrast=True,global_tone=True)
        for f in (clip.convert_global,self.convert):
            with self.assertRaises(ValueError):f(b'bad')
    def test_c_buffer_bounds(self):
        root=Path(self.tmp.name)
        (root/'check.c').write_text('''
#include <assert.h>
#include <string.h>
#include "painter_convert.c"
int main(void){uint8_t rgb[192002],out[16002];memset(rgb,255,sizeof rgb);memset(out,123,sizeof out);
assert(painter_convert_global(rgb+1,192000,out+1,16000)==0);
assert(out[0]==123&&out[16001]==123);for(int i=1;i<16001;i++)assert(out[i]==0);
assert(painter_convert_global(0,192000,out,16000)==-1);
assert(painter_convert_global(rgb,191999,out,16000)==-1);
assert(painter_convert_global(rgb,192000,out,15999)==-1);
assert(painter_convert_global(rgb,192000,0,16000)==-1);}
''')
        subprocess.run(['cc','-std=c11','-fsanitize=address,undefined','-I',str(native.SOURCE.parent),str(root/'check.c'),'-o',str(root/'check')],check=True)
        subprocess.run([str(root/'check')],check=True)

if __name__=='__main__':unittest.main()
