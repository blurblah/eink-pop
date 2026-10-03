#!/usr/bin/env python3
"""실제 직접 전이 탐색을 고정 원본과 전체 버퍼 단위로 비교한다."""
from pathlib import Path
import subprocess
import tempfile
from test_bw_interleaved_driver import definition

ROOT=Path(__file__).resolve().parents[1]
REPO=ROOT/'repos/EPD_Painter'
BASE='7544e2da178fb7be99620ba94d32bb921155ad3a'
PREFIX=r'''
#include <algorithm>
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <iterator>
#include <random>
#include <vector>
struct Buffers {
  static constexpr int DEC_IDS=32,DEC_MAX_SWEEPS_DIR=4;
  struct {int width,height;} _config;
  struct LineSweep {const uint8_t *plane_row=nullptr;uint32_t mask=0;uint8_t dec[3]={};};
  int packed_row_bytes;uint16_t _dir_loaded;
  std::vector<uint8_t> pb,sb,fast,light,spill,counts;
  std::vector<uint32_t> todo;
  std::vector<LineSweep> sweeps;
  uint8_t *packed_paintbuffer,*packed_screenbuffer,*packed_fastbuffer,*packed_lightbuffer,*dec_spill_dir,*dec_nsweeps;
  uint32_t *dec_todo;LineSweep *dec_sweeps_dir;
  Buffers(int width,int height,uint16_t loaded):_config{width,height},packed_row_bytes(width/4),_dir_loaded(loaded),
    pb(width*height/4),sb(pb.size()),fast(pb.size(),0x6b),light(pb.size(),0x6b),spill(pb.size()*2,0x6b),
    counts(height),todo(height),sweeps(height*4) {
    packed_paintbuffer=pb.data();packed_screenbuffer=sb.data();packed_fastbuffer=fast.data();
    packed_lightbuffer=light.data();dec_spill_dir=spill.data();dec_nsweeps=counts.data();dec_todo=todo.data();dec_sweeps_dir=sweeps.data();
  }
  static uint8_t directId(uint8_t from,uint8_t to){return 16|(from<<2)|to;}
};
'''
CLASS=r'''
struct EPD_Painter:Buffers {
 using Buffers::Buffers;
 uint8_t *_dec_plane_row_dir(int,int);
 void _decision_discover_direct_row(int);
 uint32_t _decision_discover_direct();
};
'''
MAIN=r'''
void compare(const Buffers&a,const Buffers&b){
 assert(a.pb==b.pb&&a.sb==b.sb&&a.fast==b.fast&&a.light==b.light&&a.spill==b.spill&&a.counts==b.counts&&a.todo==b.todo);
 for(int y=0;y<a._config.height;++y)for(int s=0;s<a.counts[y];++s){
   const auto &x=a.sweeps[y*4+s],&z=b.sweeps[y*4+s];
   assert(x.mask==z.mask&&!memcmp(x.dec,z.dec,3)&&!memcmp(x.plane_row,z.plane_row,a.packed_row_bytes));
 }
}
int main(int argc,char**argv){
 std::mt19937 rng(716);int cases=0;
 // 6종 직접전이의 모든 등록/미등록 조합. 입력은 모든명도쌍·혼합·같은묶음.
 for(unsigned mask=0;mask<64;++mask){
   uint16_t loaded=0;int bit=0;
   for(int from=1;from<4;++from)for(int to=1;to<4;++to)if(from!=to){if(mask&(1U<<bit))loaded|=1U<<((from<<2)|to);++bit;}
   for(int width:{64,960})for(bool repeated:{false,true}) {
     EPD_Painter a(width,8,loaded);ReferencePainter b(width,8,loaded);
     for(size_t i=0;i<a.pb.size();++i){a.pb[i]=rng();a.sb[i]=rng();if(i%32<16)a.sb[i]=a.pb[i];}
     if(repeated){
       const int bytes=width/4;
       // 3행/2행 반복, 목표만 같음, 이전 상태만 같음을 함께 검증한다.
       for(int row:{1,2,4}){
         const int source=row==4?3:0;
         memcpy(a.pb.data()+row*bytes,a.pb.data()+source*bytes,bytes);
         memcpy(a.sb.data()+row*bytes,a.sb.data()+source*bytes,bytes);
       }
       memcpy(a.pb.data()+6*bytes,a.pb.data()+5*bytes,bytes);
       memcpy(a.sb.data()+7*bytes,a.sb.data()+6*bytes,bytes);
     }
     std::copy(a.pb.begin(),a.pb.end(),b.pb.begin());std::copy(a.sb.begin(),a.sb.end(),b.sb.begin());
     for(int cycle=0;cycle<3;++cycle){assert(a._decision_discover_direct()==b._decision_discover_direct());compare(a,b);++cases; if(mask==63){assert(a.pb==a.sb);if(cycle>0)assert(std::all_of(a.counts.begin(),a.counts.end(),[](uint8_t n){return n==0;}));}}
   }
 }
 for(int from=0;from<4;++from)for(int to=0;to<4;++to)for(int height:{1,2,3}){
   EPD_Painter a(960,height,0xffff);ReferencePainter b(960,height,0xffff);
   std::fill(a.pb.begin(),a.pb.end(),to*0x55);std::fill(a.sb.begin(),a.sb.end(),from*0x55);
   std::copy(a.pb.begin(),a.pb.end(),b.pb.begin());std::copy(a.sb.begin(),a.sb.end(),b.sb.begin());
   assert(a._decision_discover_direct()==b._decision_discover_direct());compare(a,b);++cases;
 }
 // 인접 픽셀 묶음 최적화를 모든 이전/목표 바이트로 대조한다.
 {
   EPD_Painter a(64,3,0xffff);ReferencePainter b(64,3,0xffff);
   for(int old=0;old<256;++old)for(int next=0;next<256;++next){
     std::fill(a.pb.begin(),a.pb.end(),next);std::fill(a.sb.begin(),a.sb.end(),old);
     std::copy(a.pb.begin(),a.pb.end(),b.pb.begin());std::copy(a.sb.begin(),a.sb.end(),b.sb.begin());
     assert(a._decision_discover_direct()==b._decision_discover_direct());compare(a,b);++cases;
   }
 }
 // 반복 구간 경계와 이전/목표 중 한쪽만 같은 바이트를 모든 등록 조합에서 확인.
 for(unsigned mask=0;mask<64;++mask){
   uint16_t loaded=0;int bit=0;
   for(int from=1;from<4;++from)for(int to=1;to<4;++to)if(from!=to){if(mask&(1U<<bit))loaded|=1U<<((from<<2)|to);++bit;}
   for(int start=0;start<32;++start)for(int length:{1,2,15,16,17,32}){
     EPD_Painter a(128,3,loaded);ReferencePainter b(128,3,loaded);
     for(size_t i=0;i<a.pb.size();++i){a.pb[i]=rng();a.sb[i]=rng();}
     for(int row=0;row<3;++row){
       const uint8_t old=rng(),next=rng();
       for(int x=start;x<std::min(32,start+length);++x){
         a.pb[row*32+x]=next;a.sb[row*32+x]=old;
         if(row==1&&x%3==0)a.pb[row*32+x]^=0x41;
         if(row==2&&x%3==0)a.sb[row*32+x]^=0x14;
       }
     }
     std::copy(a.pb.begin(),a.pb.end(),b.pb.begin());std::copy(a.sb.begin(),a.sb.end(),b.sb.begin());
     for(int cycle=0;cycle<3;++cycle){assert(a._decision_discover_direct()==b._decision_discover_direct());compare(a,b);}
     ++cases;
   }
 }
 // 기존 게임12장(2배 확대·흰 여백), 이전 화면 상태를 보존하여 연속 비교.
 EPD_Painter a(960,540,0xffff);ReferencePainter b(960,540,0xffff);
 for(int arg=1;arg<argc;++arg){
   std::ifstream in(argv[arg],std::ios::binary);assert(in);
   std::vector<uint8_t> packed{std::istreambuf_iterator<char>(in),{}};assert(packed.size()==16000);
   std::fill(a.pb.begin(),a.pb.end(),0);
   for(int y=0;y<200;++y)for(int x=0;x<320;++x){
     int level=(packed[y*80+x/4]>>(6-2*(x%4)))&3;
     for(int dy=0;dy<2;++dy)for(int dx=0;dx<2;++dx){int px=160+x*2+dx,py=70+y*2+dy;a.pb[py*240+px/4]|=level<<(6-2*(px%4));}
   }
   std::copy(a.pb.begin(),a.pb.end(),b.pb.begin());
   assert(a._decision_discover_direct()==b._decision_discover_direct());compare(a,b);++cases;
   assert(a.sb==a.pb);assert(a._decision_discover_direct()==0);
 }
 printf("직접 전이 탐색 %d사례 전체 버퍼/결정/마스크 일치\n",cases);
}
'''


def main():
    old=subprocess.check_output(['git','-C',str(REPO),'show',BASE+':src/epd_painter_decision.cpp'],text=True)
    new=(REPO/'src/epd_painter_decision.cpp').read_text()
    names=['_dec_plane_row_dir','_decision_discover_direct_row','_decision_discover_direct']
    code=PREFIX+CLASS+CLASS.replace('EPD_Painter','ReferencePainter')
    code+='\n'.join(definition(new,n) for n in names)
    code+='\n'.join(definition(old,n).replace('EPD_Painter::','ReferencePainter::') for n in names)+MAIN
    clips=sorted((ROOT/'artifacts/painter-game-20260927/clip').glob('frame-*.bin'))
    if len(clips)!=12:raise RuntimeError('기준 게임12장이 필요합니다')
    with tempfile.TemporaryDirectory(prefix='painter-discovery-') as d:
        p=Path(d);(p/'check.cpp').write_text(code)
        subprocess.run(['clang++','-std=c++17','-O2','-fsanitize=address,undefined','-g',str(p/'check.cpp'),'-o',str(p/'check')],check=True)
        subprocess.run([str(p/'check'),*map(str,clips)],check=True,timeout=60)


if __name__=='__main__':main()
