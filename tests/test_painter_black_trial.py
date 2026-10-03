#!/usr/bin/env python3
"""실제 검정 시험 바인딩의 전이 선택과 흰색 보정 동시 사용을 검증한다."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from test_bw_interleaved_driver import definition
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'host'))
from analyze_painter_budget import array

def main():
    source=(ROOT/'repos/EPD_Painter/src/EPD_Painter.cpp').read_text()
    tables=(ROOT/'repos/EPD_Painter/src/LilyGo_H716_Trains.h').read_text()
    code='#include <cassert>\n#include <cstdint>\n#include <cstring>\n#include <algorithm>\n#include <cstdio>\n'
    for name in ('fast_darker','fast_lighter'):
        initializer=json.dumps(array(tables,name,(3,7))).replace('[','{').replace(']','}')
        code+=f'const uint8_t {name}[3][7]={initializer};\n'
    code+=definition(source,'bindFastWhiteTrial')+definition(source,'bindFastBlackTrial')
    code+=r'''
int net(const uint8_t*p,int n){int s=0;for(int i=0;i<n;++i){assert(p[i]<=2);s+=(p[i]==1)-(p[i]==2);}return s;}
int main(){
 int cases=0;
 for(int mask=0;mask<16;++mask)for(int base:{7,8,9,11,12,19}){
   uint8_t whites[3][11],black[11],lengths[32];const uint8_t*trains[32];
   memset(whites,0x5a,sizeof whites);memset(black,0x5a,sizeof black);
   uint32_t active=0xffffffffU&~((1u<<3)|(1u<<5)|(1u<<7)|(1u<<6));
   for(int g=1;g<=3;++g)if(mask&(1<<(g-1)))active|=1u<<(g*2+1);
   if(mask&8)active|=1u<<6;
   for(int i=0;i<32;++i){trains[i]=fast_darker[0];lengths[i]=7;}
   trains[6]=fast_darker[2];
   for(int g=1;g<=3;++g)trains[g*2+1]=fast_lighter[g-1];
   int passes=bindFastWhiteTrial(&fast_lighter[0][0],active,trains,lengths,whites,base);
   const uint8_t*saved[32];uint8_t saved_len[32];memcpy(saved,trains,sizeof saved);memcpy(saved_len,lengths,32);
   int result=bindFastBlackTrial(&fast_darker[0][0],active,trains,lengths,black,passes);
   assert(result==((mask&8)?std::max(11,passes):passes));
   for(int i=0;i<32;++i){
     if(i==6&&(mask&8)){
       assert(trains[i]==black&&lengths[i]==11&&black[0]==2&&black[1]==2&&black[9]==1&&black[10]==1);
       assert(!memcmp(black+2,fast_darker[2],7)&&net(black,11)==7);
       assert(net(black,11)+net(trains[7],lengths[7])==0);
     }else assert(trains[i]==saved[i]&&lengths[i]==saved_len[i]);
   }
   if(!(mask&8))for(auto v:black)assert(v==0x5a);
   ++cases;
 }
 printf("검정/흰색 시험 바인딩 %d사례 통과\n",cases);
}
'''
    with tempfile.TemporaryDirectory(prefix='painter-black-') as tmp:
        p=Path(tmp);(p/'check.cpp').write_text(code)
        subprocess.run(['clang++','-std=c++17','-fsanitize=address,undefined','-g','-DEPD_PAINTER_FAST_WHITE_PAIR=2',str(p/'check.cpp'),'-o',str(p/'check')],check=True)
        subprocess.run([str(p/'check')],check=True,timeout=10)
if __name__=='__main__':main()
