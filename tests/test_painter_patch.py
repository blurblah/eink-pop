"""행 패치의 무손실 복원, 최대크기 대체, 손상 거부 검증."""
import random
import struct
import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'host'))
from painter_patch import encode, decode


class Tests(unittest.TestCase):
    def test_all_row_boundaries(self):
        old=bytes(16000);new=bytearray(old)
        for y in range(200):new[y*80]=y;new[y*80+79]=255-y
        self.assertIsNone(encode(old,new))
        # 각 행 양끝 변경을 따로 전송하면 모두 경계 안에서 복원된다.
        for x in (0,79):
            new=bytearray(old)
            for y in range(200):new[y*80+x]=1+y%255
            self.assertEqual(decode(old,encode(old,new)),new)
    def test_random_sparse_changes_and_empty(self):
        rng=random.Random(716)
        for _ in range(30):
            old=rng.randbytes(16000);new=bytearray(old)
            for i in rng.sample(range(16000),80):new[i]^=255
            encoded=encode(old,new)
            self.assertIsNotNone(encoded);self.assertEqual(decode(old,encoded),new)
            self.assertEqual(encode(old,old),b'');self.assertEqual(decode(old,b''),old)
    def test_bad_lengths_and_rows(self):
        record=lambda offset,count,data:struct.pack('<HB',offset,count)+data
        malformed=[b'x',record(0,0,b''),record(0,81,bytes(81)),record(79,2,b'ab'),
                   record(15999,2,b'ab'),record(0,2,b'a'),record(0,1,b'a')*2,
                   record(80,1,b'a')+record(0,1,b'b'),bytes(16000)]
        for data in malformed:
            with self.subTest(data=data[:8]),self.assertRaises(ValueError):decode(bytes(16000),data)
        with self.assertRaises(ValueError):encode(b'',bytes(16000))
        with self.assertRaises(ValueError):decode(b'',b'')


if __name__=='__main__':unittest.main()
