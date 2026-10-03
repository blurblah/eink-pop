"""후방참조 C/Python 상호 복원과 잘못된 입력·C 메모리 경계 검증."""
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'host'))
import painter_lz as reference
import painter_native

class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory=tempfile.TemporaryDirectory();cls.path=Path(cls.directory.name)
        painter_native.build(cls.path/painter_native.DEFAULT.name)
        cls.codec=staticmethod(painter_native.load(cls.path/painter_native.DEFAULT.name))
    @classmethod
    def tearDownClass(cls):cls.directory.cleanup()

    def test_cross_decode(self):
        cases=[bytes([value])*16000 for value in range(256)]
        cases += [(random.Random(n).randbytes(n)*((16000+n-1)//n))[:16000] for n in (2,3,4,5,127,128,129,131,132,255,256,4095,8000,15996,16000)]
        for raw in cases:
            for encoder in (reference.encode,self.codec.encode_lz):
                packed=encoder(raw)
                if packed is not None:
                    self.assertLess(len(packed),16000)
                    self.assertEqual(reference.decode(packed),raw)
                    self.assertEqual(self.codec.decode_lz(packed),raw)
        self.assertIsNone(self.codec.encode_lz(random.Random(716).randbytes(16000)))

    def test_malformed_and_lengths(self):
        packed=self.codec.encode_lz(bytes(16000))
        bad=[packed[:i] for i in range(len(packed))]
        bad += [b'\x80\x00\x00',b'\x80\x01\x00',b'\x00\x00\xff\xff\xff',packed+b'\x00\x00',bytes(16000)]
        for value in bad:
            for decoder in (reference.decode,self.codec.decode_lz):
                with self.assertRaises(ValueError):decoder(value)
        for value in (b'',bytes(15999),bytes(16001)):
            with self.assertRaises(ValueError):self.codec.encode_lz(value)

    def test_c_asan_ubsan(self):
        code=r'''
#include <cassert>
#include <random>
#include <cstring>
#include "painter_lz.h"
int main(){
 std::mt19937 rng(716);uint8_t src[16002],encoded[16002],output[16002];
 for(int trial=0;trial<300;++trial){
   memset(src,0x5a,sizeof src);memset(encoded,0x5a,sizeof encoded);memset(output,0x5a,sizeof output);
   int period=trial%2?1+trial*17:16000;
   for(int i=0;i<16000;++i)src[i+1]=i<period?rng():src[1+i-period];
   int count=encode_painter_lz(src+1,16000,encoded+1,16000);
   assert(count>=0&&count<16000&&encoded[0]==0x5a&&encoded[16001]==0x5a);
   if(count){assert(decode_painter_lz(encoded+1,count,output+1,16000)==0);assert(!memcmp(src+1,output+1,16000));}
   assert(src[0]==0x5a&&src[16001]==0x5a&&output[0]==0x5a&&output[16001]==0x5a);
 }
 for(int trial=0;trial<2000;++trial){
   int count=1+rng()%15999;
   for(int i=0;i<count;++i)encoded[i+1]=rng();
   memset(output,0x5a,sizeof output);
   int result=decode_painter_lz(encoded+1,count,output+1,16000);
   assert(result==0||result==-1);assert(output[0]==0x5a&&output[16001]==0x5a);
 }
 for(size_t size:{0,1,15999,16001}){
   assert(encode_painter_lz(src+1,size,encoded+1,16000)==-1);
   assert(encode_painter_lz(src+1,16000,encoded+1,size)==-1);
   assert(decode_painter_lz(src+1,100,output+1,size)==-1);
 }
 assert(encode_painter_lz(nullptr,16000,encoded+1,16000)==-1);
 assert(encode_painter_lz(src+1,16000,nullptr,16000)==-1);
 assert(decode_painter_lz(nullptr,100,output+1,16000)==-1);
 assert(decode_painter_lz(src+1,100,nullptr,16000)==-1);
}
'''
        (self.path/'check.cpp').write_text(code)
        subprocess.run(['clang++','-std=c++17','-O2','-fsanitize=address,undefined','-g','-I',str(ROOT/'firmware/painter_usb'),str(self.path/'check.cpp'),'-o',str(self.path/'check')],check=True)
        subprocess.run([str(self.path/'check')],check=True,timeout=15)

if __name__=='__main__':unittest.main()
