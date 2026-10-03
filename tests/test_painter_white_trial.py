#!/usr/bin/env python3
"""실제 시험 바인딩의 전이 선택·주사 상한·파형 순합·배열 경계를 검증한다."""
from pathlib import Path
import subprocess
import sys
import json
import tempfile
from test_bw_interleaved_driver import definition

ROOT = Path(__file__).resolve().parents[1]
CPP = ROOT / 'repos/EPD_Painter/src/EPD_Painter.cpp'
TRAINS = ROOT / 'repos/EPD_Painter/src/LilyGo_H716_Trains.h'
sys.path.insert(0, str(ROOT / 'host'))
from analyze_painter_budget import array


def main():
    # 실사용 테이블과 보정 함수를 함께 컴파일한다. 양/음 코드 수는 독립 집계.
    code = r'''
#include <cassert>
#include <cstdint>
#include <cstring>
#include <cstdio>
#include <algorithm>
'''
    for name in ('fast_lighter', 'fast_darker'):
        values = json.dumps(array(TRAINS.read_text(), name, (3, 7))).replace('[', '{').replace(']', '}')
        code += f'const uint8_t {name}[3][7] = {values};\n'
    code += definition(CPP.read_text(), 'bindFastWhiteTrial')
    code += r'''
int net(const uint8_t* p,int n){int v=0;for(int i=0;i<n;++i)v+=(p[i]==1)-(p[i]==2);return v;}
int main(){
 int cases=0;
 for(int mask=0;mask<8;++mask)for(int base:{7,8,9,11,12,19}){
   const uint8_t* trains[32];uint8_t lengths[32];uint8_t storage[3][9];
   memset(storage,0x5a,sizeof storage);
   uint32_t active=(1u<<2)|(1u<<6)|(1u<<27); // 어두워짐/회색 전이는 변경 금지
   for(int i=0;i<32;++i){trains[i]=&fast_darker[0][0];lengths[i]=7;}
   for(int g=1;g<=3;++g){trains[g*2+1]=fast_lighter[g-1];if(mask&(1<<(g-1)))active|=1u<<(g*2+1);}
   int result=bindFastWhiteTrial(&fast_lighter[0][0],active,trains,lengths,storage,base);
   assert(result==(mask?std::max(base,9):base));
   for(int i=0;i<32;++i){
     const int g=i/2;
     bool white=(i==3||i==5||i==7);
     bool changed=white && (mask&(1<<(g-1)));
     if(changed){
       assert(lengths[i]==9&&trains[i][0]==1&&trains[i][8]==2);
       assert(!memcmp(trains[i]+1,fast_lighter[g-1],7));
       assert(net(trains[i],9)==net(fast_lighter[g-1],7));
       assert(net(trains[i],9)+net(fast_darker[g-1],7)==0);
     }else{
       assert(lengths[i]==7);
       assert(trains[i]==(white?fast_lighter[g-1]:&fast_darker[0][0]));
     }
   }
   ++cases;
 }
 printf("흰색 시험 바인딩 %d사례 통과\n",cases);
}
'''
    with tempfile.TemporaryDirectory(prefix='painter-white-') as tmp:
        path = Path(tmp)
        (path / 'check.cpp').write_text(code)
        subprocess.run(['clang++', '-std=c++17', '-fsanitize=address,undefined', '-g',
                        str(path / 'check.cpp'), '-o', str(path / 'check')], check=True)
        subprocess.run([str(path / 'check')], check=True, timeout=10)


if __name__ == '__main__':
    main()
