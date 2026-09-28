#!/usr/bin/env python3
"""실제 paintPacked의 등록 조합·복사·제출 대기를 검증한다."""
from pathlib import Path
import subprocess
import tempfile
from test_bw_interleaved_driver import definition

ROOT = Path(__file__).resolve().parents[1]
PREFIX = r'''
#include <atomic>
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <functional>
#include <vector>
constexpr int portMAX_DELAY=100;
int takes=0,gives=0,ticks=0,submitted=0;
bool locked=false;
std::function<void()> tick,release;
void xSemaphoreTake(int*,int){assert(!locked);locked=true;++takes;}
void xSemaphoreGive(int*){assert(locked);locked=false;++gives;release();}
void vTaskDelay(int){++ticks;tick();}
struct EPD_Painter {
  bool _grey16=false,_decision_direct=false;
  uint16_t _dir_loaded=0;
  struct { int width=64,height=4; } _config;
  int packed_row_bytes=16,sem=0;int* _paint_buffer_sem=&sem;
  uint8_t storage[64]={};uint8_t* packed_paintbuffer=storage;
  std::atomic<int> paintStage{0};
  struct Rect {int16_t x,y,w,h;};
  bool idle=true;
  bool waitForIdle(uint32_t){return idle && paintStage==0;}
  void paintPacked(const uint8_t*,int);
  bool paintPackedRegion(const uint8_t*,const Rect&);
  void _submitPacked();
};
'''
MAIN = r'''
int main(){
  int cases=0;
  for(int enabled=0;enabled<2;++enabled)for(unsigned mask=0;mask<64;++mask)for(int repeat:{1,2}){
    EPD_Painter d;d._decision_direct=enabled;int bit=0;
    for(int from=1;from<4;++from)for(int to=1;to<4;++to)if(from!=to){
      if(mask&(1u<<bit))d._dir_loaded|=1u<<((from<<2)|to);++bit;
    }
    // 다른 등록 비트로 부족한 직접 쌍을 채운 것으로 판정하지 않는다.
    d._dir_loaded|=0x953f;
    uint8_t input[64];for(int i=0;i<64;++i)input[i]=i+1;
    takes=gives=ticks=0;submitted=0;
    release=[&]{submitted=d.paintStage;};
    tick=[&]{assert(!locked);--d.paintStage;};
    d.paintPacked(input,repeat);
    assert(submitted==(enabled&&mask==63?1:2));
    assert(takes==1&&gives==1&&ticks==1&&d.paintStage==submitted-1);
    for(int row=0;row<4;++row)assert(!memcmp(d.storage+row*16,input+(row/repeat)*16,16));
    ++cases;
  }
  EPD_Painter d;d._grey16=true;takes=gives=ticks=0;
  d.paintPacked(nullptr,1);assert(!takes&&!gives&&!ticks&&d.paintStage==0);++cases;
  d._grey16=false;d._decision_direct=true;d._dir_loaded=0xffff;
  release=[&]{submitted=d.paintStage;};tick=[&]{--d.paintStage;};
  uint8_t input[64];for(int i=0;i<64;++i)input[i]=i+1;
  for(int y=0;y<4;++y)for(int h=1;h<=4-y;++h)for(int x=0;x<64;x+=4)for(int w=4;w<=64-x;w+=4){
    uint8_t expected[64];memcpy(expected,d.storage,64);
    for(int r=y;r<y+h;++r)memcpy(expected+r*16+x/4,input+r*16+x/4,w/4);
    takes=gives=ticks=0;d.paintStage=0;
    assert(d.paintPackedRegion(input,{(int16_t)x,(int16_t)y,(int16_t)w,(int16_t)h}));
    assert(submitted==1&&takes==1&&gives==1&&ticks==1&&!memcmp(expected,d.storage,64));
    for(uint8_t &b:input)b^=0xff;++cases;
  }
  for(auto rect:std::vector<EPD_Painter::Rect>{{-4,0,4,1},{0,-1,4,1},{0,0,0,1},{0,0,4,0},
      {0,0,-4,1},{0,0,4,-1},{1,0,4,1},{0,0,3,1},{64,0,4,1},{0,4,4,1},
      {60,0,8,1},{0,3,4,2},{32764,0,32764,1},{0,32767,4,32767}}){
    uint8_t before[64];memcpy(before,d.storage,64);takes=gives=ticks=0;d.paintStage=0;
    assert(!d.paintPackedRegion(input,rect)&&!takes&&!gives&&!ticks&&!memcmp(before,d.storage,64));++cases;
  }
  for(int fault=0;fault<5;++fault){
    uint8_t before[64];memcpy(before,d.storage,64);takes=gives=ticks=0;
    d._grey16=fault==0;d.idle=fault!=1;d.packed_paintbuffer=fault==2?nullptr:d.storage;
    d._paint_buffer_sem=fault==3?nullptr:&d.sem;
    assert(!d.paintPackedRegion(fault==4?nullptr:input,{0,0,64,4})&&!takes&&!gives&&!ticks&&!memcmp(before,d.storage,64));++cases;
  }
  printf("압축 제출/부분복사 %d사례 통과\n",cases);
}
'''

def main():
    source=(ROOT/'repos/EPD_Painter/src/EPD_Painter.cpp').read_text()
    with tempfile.TemporaryDirectory(prefix='painter-packed-') as d:
        p=Path(d);(p/'check.cpp').write_text(PREFIX+''.join(definition(source,n) for n in ['paintPacked','paintPackedRegion','_submitPacked'])+MAIN)
        subprocess.run(['clang++','-std=c++17','-O2','-fsanitize=address,undefined',str(p/'check.cpp'),'-o',str(p/'check')],check=True)
        subprocess.run([str(p/'check')],check=True,timeout=30)

if __name__=='__main__':main()
