"""표 파싱·명도 전이·실제 구동 수 판정의 경계를 검증한다."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'host'))
import analyze_painter_budget as b


class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source=(b.DRIVER/'src/LilyGo_H716_Trains.h').read_text()
        cls.normal,cls.direct=b.tables(cls.source)

    def test_all_level_pairs(self):
        for f in range(4):
            for t in range(4):
                with self.subTest(f=f,t=t):
                    c=b.transition_counts(bytes([f*85])*16000,bytes([t*85])*16000)
                    self.assertEqual(sum(c),0 if f==t else 64000)
                    if f!=t:self.assertEqual(c[f*4+t],64000)
                    expected=0 if f==t else 13
                    if f and t and f!=t:expected=max(13,max(i+1 for i,v in enumerate(self.direct[f][t]) if v))
                    self.assertEqual(b.required_passes(c,self.direct),expected)

    def test_single_pixel_all_positions(self):
        for offset in (0,79,80,15999):
            for shift in (6,4,2,0):
                old=bytearray(16000);new=bytearray(16000)
                old[offset]=3<<shift;new[offset]=2<<shift
                c=b.transition_counts(old,new)
                self.assertEqual(c[14],1);self.assertEqual(sum(c),1)
                self.assertEqual(b.required_passes(c,self.direct),19)

    def test_direct_dc_matches_endpoint_difference(self):
        def net(x):return sum(1 if v==1 else -1 if v==2 else 0 for v in x)
        levels=[0]+[net(x) for x in self.normal['darker']]
        self.assertEqual(levels,[0,2,4,13])
        for f in range(1,4):
            self.assertEqual(net(self.normal['lighter'][f-1]),-levels[f])
            for t in range(1,4):
                if f!=t:self.assertEqual(net(self.direct[f][t]),levels[t]-levels[f])

    def test_initializer_and_errors(self):
        self.assertEqual(b.array('a[2][3] = { /* } */ {1,2}, {3} };','a',(2,3)),[[1,2,0],[3,0,0]])
        for source in ('a={4};','a={-1};','a={1,2,3,0};','a={1;','missing={0};'):
            with self.subTest(source=source),self.assertRaises((ValueError,SyntaxError)):
                b.array(source,'a',(3,))
        with self.assertRaises(ValueError):b.transition_counts(b'',b'')
        with self.assertRaises(ValueError):b.required_passes([-1]*16,self.direct)
        broken=[[[0]*26 for _ in range(4)] for _ in range(4)]
        with self.assertRaises(ValueError):b.required_passes([0]*14+[1,0],broken)


if __name__=='__main__':unittest.main()
