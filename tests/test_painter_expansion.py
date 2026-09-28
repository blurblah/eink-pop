#!/usr/bin/env python3
"""실제 증분 확대의 누적 canvas를 독립 전체 픽셀 기준과 비교한다."""
from pathlib import Path
import subprocess
import tempfile
from test_bw_interleaved_driver import definition

ROOT=Path(__file__).resolve().parents[1]
PREFIX=r'''
#include <algorithm>
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <random>
#include <vector>
struct EPD_Painter {struct Rect {int16_t x,y,w,h;};};
std::vector<uint8_t> old_frame(16000),new_frame(16000),panel(129600);
uint8_t *previous=old_frame.data(),*incoming=new_frame.data(),*canvas=panel.data();
'''
MAIN=r'''
int cases=0;
void verify(){
 auto rect=expandFrame();
 int left=320,top=200,right=-1,bottom=-1;
 for(int i=0;i<16000;++i)if(previous[i]!=incoming[i]){
   left=std::min(left,(i%80)*4);right=std::max(right,(i%80)*4+3);
   top=std::min(top,i/80);bottom=std::max(bottom,i/80);
 }
 if(right<left)assert(rect.x==0&&rect.y==0&&rect.w==0&&rect.h==0);
 else assert(rect.x==160+left*2&&rect.y==70+top*2&&rect.w==(right-left+1)*2&&rect.h==(bottom-top+1)*2);
 for(int y=0;y<540;++y)for(int x=0;x<960;++x){
   int level=0;
   if(x>=160&&x<800&&y>=70&&y<470){
     int sx=(x-160)/2,sy=(y-70)/2;
     level=(incoming[sy*80+sx/4]>>(6-2*(sx%4)))&3;
   }
   assert(((canvas[y*240+x/4]>>(6-2*(x%4)))&3)==level);
 }
 memcpy(previous,incoming,16000);++cases;
}
int main(){
 verify();
 for(int b=0;b<256;++b){std::fill(new_frame.begin(),new_frame.end(),b);verify();}
 for(int y:{0,1,99,199})for(int x:{0,1,78,79}){
   incoming[y*80+x]^=0xff;verify();incoming[y*80+x]^=0xff;verify();
 }
 std::mt19937 rng(716);
 for(int n=0;n<32;++n){
   for(int i=0;i<(n%2?12000:17);++i)incoming[rng()%16000]=rng();
   verify();verify();
 }
 std::fill(new_frame.begin(),new_frame.end(),0);verify();
 printf("증분 확대 %d사례 전체패널 일치\n",cases);
}
'''

def main():
    source=(ROOT/'firmware/painter_usb/painter_usb.ino').read_text()
    with tempfile.TemporaryDirectory(prefix='painter-expand-') as d:
        p=Path(d);(p/'check.cpp').write_text(PREFIX+definition(source,'expandFrame')+MAIN)
        subprocess.run(['clang++','-std=c++17','-O2','-fsanitize=address,undefined',str(p/'check.cpp'),'-o',str(p/'check')],check=True)
        subprocess.run([str(p/'check')],check=True,timeout=30)

if __name__=='__main__':main()
