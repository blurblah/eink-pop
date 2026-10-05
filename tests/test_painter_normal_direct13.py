#!/usr/bin/env python3
"""실제 H716 원본과13주사 후보의 전체 전이·선택 변경·구동 코드 제약 비교."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'host'))
from analyze_painter_budget import array

def main():
    table=array((ROOT/'repos/EPD_Painter/src/LilyGo_H716_Trains.h').read_text(),'DIRECT_H716_NORMAL',(4,4,26))
    initializer=json.dumps(table).replace('[','{').replace(']','}')
    code='#include <cassert>\n#include <cstring>\n#include <cstdio>\n#include "normal_direct13.h"\n'
    code+=f'const uint8_t original[4][4][26]={initializer};\n'
    code+=r'''
int main(){
 const int Q[4]={0,2,4,13};int changed=0;
 for(int from=0;from<4;++from)for(int to=0;to<4;++to){
  const auto* a=original[from][to];const auto* b=NORMAL_DIRECT13_TRIAL[from][to];
  int a1=0,a2=0,b1=0,b2=0,alen=0,blen=0;
  for(int p=0;p<26;++p){
   assert(b[p]<=2);if(a[p])alen=p+1;if(b[p])blen=p+1;
   a1+=a[p]==1;a2+=a[p]==2;b1+=b[p]==1;b2+=b[p]==2;
  }
  assert(blen<=13 && a1-a2==b1-b2);
  if(alen<=13){assert(!memcmp(a,b,26));continue;}
  ++changed;assert(from && to && from!=to && blen==13);
  assert(a1-b1==a2-b2 && a1>b1 && a2>b2);
  assert(b1-b2==Q[to]-Q[from]);
  for(int p=0;p<blen;++p)assert(b[p]==(p<b2?2:1));
  for(int p=blen;p<26;++p)assert(!b[p]);
 }
 assert(changed==4);
 puts("원본/후보16전이·26슬롯·네쌍한정·코드순합·길이·방향 ASan/UBSan 통과");
}
'''
    with tempfile.TemporaryDirectory() as d:
        p=Path(d);(p/'check.cpp').write_text(code)
        subprocess.run(['clang++','-std=c++17','-fsanitize=address,undefined','-g','-I',str(ROOT/'firmware/painter_usb'),str(p/'check.cpp'),'-o',str(p/'check')],check=True)
        subprocess.run([str(p/'check')],check=True,timeout=10)
if __name__=='__main__':main()
