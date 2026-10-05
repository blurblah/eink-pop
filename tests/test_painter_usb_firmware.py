#!/usr/bin/env python3
"""실제 v5 수신 스케치의 파서·픽셀·패널 접근 시점·실패 종료 검증."""
from pathlib import Path
import struct
import subprocess
import tempfile
import zlib

ROOT=Path(__file__).resolve().parents[1]
ARDUINO=r'''
#pragma once
#include <cstdint>
#include <cstddef>
#include <cstring>
#include <string>
#include <vector>
#define CONFIG_IDF_TARGET_ESP32S3 1
#define BOARD_HAS_PSRAM 1
#define ARDUINO_USB_MODE 1
#define ARDUINO_USB_CDC_ON_BOOT 1
#define EPD_PAINTER_NO_NVS 1
struct MockSerial {
 std::vector<uint8_t> input; size_t pos=0; std::string output;
 size_t setRxBufferSize(size_t n);
 void begin(int) {} void setTxTimeoutMs(int) {}
 operator bool() const; void flush() {}
 int available(); size_t read(uint8_t*,size_t);
 void println(const char*); void printf(const char*,...);
};
extern MockSerial Serial;
uint32_t millis(); void delay(uint32_t); uint32_t getCpuFrequencyMhz(); bool psramFound();
'''
EPD=r'''
#pragma once
#include "Arduino.h"
class EPD_Painter {
public:
 enum class Quality {QUALITY_NORMAL,QUALITY_FAST};
 struct Config { int board=716, pass_gap_us_normal=4000; };
 int gap; const Config* preset;
 explicit EPD_Painter(const Config& c):gap(c.pass_gap_us_normal),preset(&c) { if(c.board!=716)__builtin_trap(); }
 void setAutoShutdown(bool); void setIdleTimeout(int); void setQuality(Quality); void setPaintProfile(bool);
 bool begin(); bool setDirectTransitions(bool); void clearBuffers(); void clear();
 bool waitForIdle(uint32_t); bool powerOffWhenIdle(uint32_t); uint32_t paintsCompleted();
 struct Rect {int16_t x,y,w,h;};
 void paintPacked(const uint8_t*,int);
 bool paintPackedRegion(const uint8_t*,const Rect&);
 struct PaintTiming { uint32_t cycle=0,passes=0; int64_t discovery_us=0,rowloop_us=0,settle_us=0,neutral_us=0,total_us=0; };
 bool readPaintTiming(PaintTiming&);
};
inline EPD_Painter::Config EPD_LILYGO_EPD47_H716_PRESET;
'''
HARNESS=r'''
#include "Arduino.h"
#include "EPD_Painter.h"
#include <algorithm>
#include <cassert>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <iterator>
static std::string scenario;
static int64_t clockUs=0;
static int begins=0,clears=0,paints=0,offs=0,completed=0,allocs=0,frees=0;
static bool busy=false,autoOff=true,profile=true,quality=false,direct=false,buffers=false;
static int idle=0;
static std::vector<uint8_t> expected;
MockSerial Serial;
#include SKETCH_PATH
uint32_t millis(){return clockUs/1000;}
void delay(uint32_t n){clockUs+=n*1000;}
uint32_t getCpuFrequencyMhz(){return scenario=="cpu"?160:240;}
bool psramFound(){return scenario!="no_psram";}
size_t esp_spiram_get_size(){return scenario=="small_psram"?4194304:8388608;}
int64_t esp_timer_get_time(){return clockUs;}
void* heap_caps_malloc(size_t n,int){++allocs;assert(n==16000||n==129600);return scenario=="alloc" && allocs==2?nullptr:malloc(n);}
void heap_caps_free(void*p){++frees;free(p);}
size_t MockSerial::setRxBufferSize(size_t n){assert(n==32768);return scenario=="rx_buffer"?0:n;}
MockSerial::operator bool()const{return scenario!="disconnect";}
int MockSerial::available(){return scenario=="rx_queue"?-1:int(input.size()-pos);}
size_t MockSerial::read(uint8_t*p,size_t n){n=std::min({n,size_t(11),input.size()-pos});memcpy(p,input.data()+pos,n);pos+=n;return scenario=="rx_read"?size_t(-1):n;}
void MockSerial::println(const char*s){output+=std::string(s)+"\n";}
void MockSerial::printf(const char*fmt,...){char text[768];va_list ap;va_start(ap,fmt);vsnprintf(text,sizeof(text),fmt,ap);va_end(ap);if(strstr(text,"PAINTER_USB_ACK")==text)assert(!busy);if(strstr(text,"PAINTER_USB_DONE")==text)assert(!busy);output+=text;}
void EPD_Painter::setAutoShutdown(bool x){autoOff=x;}
void EPD_Painter::setIdleTimeout(int x){idle=x;}
void EPD_Painter::setQuality(Quality q){assert(q==DISPLAY_QUALITY);quality=true;}
void EPD_Painter::setPaintProfile(bool x){profile=x;}
bool EPD_Painter::begin(){assert(!autoOff&&!profile&&quality&&idle==60);++begins;return scenario!="begin";}
bool EPD_Painter::setDirectTransitions(bool x){assert(x&&begins==1&&!clears);direct=true;return scenario!="direct";}
void EPD_Painter::clearBuffers(){assert(direct);buffers=true;}
void EPD_Painter::clear(){assert(buffers);++clears;busy=true;clockUs+=100000;}
bool EPD_Painter::waitForIdle(uint32_t ms){assert(ms==5000);if(scenario=="clear_timeout"&&!paints)return false;if(scenario=="paint_timeout"&&paints)return false;busy=false;clockUs+=1000;if(paints)completed+=scenario=="cycles"?2:1;return true;}
bool EPD_Painter::powerOffWhenIdle(uint32_t ms){assert(ms==5000);++offs;if(scenario=="off")return false;busy=false;return true;}
uint32_t EPD_Painter::paintsCompleted(){return completed;}
bool EPD_Painter::readPaintTiming(PaintTiming&t){
 assert(!busy);if(scenario=="timing_missing")return false;
 t.cycle=completed+(scenario=="timing_cycle");t.passes=19;t.discovery_us=10;
 t.rowloop_us=100000;t.settle_us=76000;t.neutral_us=1000;t.total_us=177020;return true;
}
bool EPD_Painter::paintPackedRegion(const uint8_t*p,const Rect&r){
 if(scenario=="paint_region")return false;
 static uint8_t desired[129600]={};
 assert(r.x>=0&&r.y>=0&&r.w>0&&r.h>0&&r.x+r.w<=960&&r.y+r.h<=540&&!(r.x%4)&&!(r.w%4));
 for(int y=r.y;y<r.y+r.h;++y)memcpy(desired+y*240+r.x/4,p+y*240+r.x/4,r.w/4);
 paintPacked(desired,1);return true;
}
void EPD_Painter::paintPacked(const uint8_t*p,int repeat){
 assert(!busy&&clears==1&&offs==0&&repeat==1&&expected.size()==16000);
 for(int y=0;y<540;++y)for(int x=0;x<960;++x){
  int want=0;if(x>=160&&x<800&&y>=70&&y<470){int i=((y-70)/2)*320+(x-160)/2;want=(expected[i/4]>>(6-2*(i%4)))&3;}
  int i=y*960+x;assert(((p[i/4]>>(6-2*(i%4)))&3)==want);
 }
 ++paints;busy=true;clockUs+=scenario=="slow_paint"?31000000:200000;
}
static std::vector<uint8_t> file(const std::string&p){std::ifstream f(p,std::ios::binary);assert(f);return {std::istreambuf_iterator<char>(f),{}};}
static void feed(std::vector<uint8_t> p){Serial.input=std::move(p);Serial.pos=0;for(int i=0;i<100000&&Serial.pos<Serial.input.size()&&state!=TERMINAL;++i)loop();}
static void crc(std::vector<uint8_t>&p){uint32_t c=updateCrc(0xffffffff,p.data(),p.size()-4)^0xffffffff;for(int i=0;i<4;++i)p[p.size()-4+i]=(c>>(8*i))&255;}
int main(int argc,char**argv){
 assert(argc==3);scenario=argv[1];std::string dir=argv[2];setup();
 assert(begins==0&&clears==0&&paints==0);
 if(scenario!="rx_buffer")assert(Serial.output.find("quality=" PAINTER_USB_QUALITY_NAME)!=std::string::npos);
 assert(EPD_LILYGO_EPD47_H716_PRESET.pass_gap_us_normal==4000);
 assert(panel.preset->pass_gap_us_normal==panel.gap);
#ifdef PAINTER_USB_NORMAL_GAP_2MS
 assert(panel.gap==2000);
 if(scenario!="rx_buffer")assert(Serial.output.find("normal_gap_us=2000")!=std::string::npos);
#else
 assert(panel.gap==4000);
 assert(Serial.output.find("normal_gap_us=")==std::string::npos);
#endif
#if defined(EPD_PAINTER_FAST_WHITE_PAIR) && EPD_PAINTER_FAST_WHITE_PAIR == 2
#ifdef EPD_PAINTER_FAST_BLACK_PAIR
 if(scenario!="rx_buffer")assert(Serial.output.find(" black=pair2")!=std::string::npos);
#else
 assert(Serial.output.find(" black=pair2")==std::string::npos);
#endif
 if(scenario!="rx_buffer")assert(Serial.output.find(" white=pair2")!=std::string::npos);
#elif defined(EPD_PAINTER_FAST_WHITE_PAIR)
 if(scenario!="rx_buffer")assert(Serial.output.find(" white=pair")!=std::string::npos);
#else
 assert(Serial.output.find(" white=pair")==std::string::npos);
#endif
 const uint8_t known[]="123456789";assert((updateCrc(0xffffffff,known,9)^0xffffffff)==0xcbf43926);
 // 전 바이트 값·임의 초기 상태·모든 분할점에서 독립 비트 구현과 비교.
 uint8_t data[256];for(int i=0;i<256;++i)data[i]=i;
 for(uint32_t seed: {0U,1U,0xffffffffU,0x12345678U}) {
   uint32_t ref=seed;
   for(uint8_t b:data){ref^=b;for(int k=0;k<8;++k)ref=(ref>>1)^((ref&1)?0xedb88320U:0U);}
   assert(updateCrc(seed,data,256)==ref);
   for(int n=0;n<=256;++n)assert(updateCrc(updateCrc(seed,data,n),data+n,256-n)==ref);
 }
 auto start=file(dir+"/start.bin");auto first=file(dir+"/first.bin");auto second=file(dir+"/second.bin");
 if(scenario=="start_crc")start.back()^=1;
if(scenario=="old_version"){start[4]=4;crc(start);}
 if(scenario=="old_version5"){start[4]=5;crc(start);}
 if(scenario=="old_version6"){start[4]=6;crc(start);}
 if(scenario=="limit"){start[20]=13;crc(start);}
 if(scenario=="early")start.insert(start.end(),first.begin(),first.end());
 if(scenario=="full"){start[20]=12;crc(start);}
 feed(start);
 if(scenario=="stop_before")feed(file(dir+"/stop1.bin"));
 else if(scenario=="partial_timeout"){feed({0x45});clockUs+=5000000;loop();}
 else if(state!=TERMINAL){
  assert(begins==0); // START·할당만으로는 패널 접근 없음
  if(scenario=="frame_crc")first.back()^=1;
  if(scenario=="session"){first[8]^=1;crc(first);}
  if(scenario=="sequence"){first[12]=2;crc(first);}
  if(scenario=="length"){first[16]=0;crc(first);}
  if(scenario=="white")first=file(dir+"/white.bin");
  if(scenario=="patch_first")first=file(dir+"/patch_first.bin");
  expected.assign(first.begin()+20,first.end()-4);
  if(scenario=="rle_first"||scenario=="rle_same"||scenario=="rle_white"||scenario=="rle_first_invalid"){
    first=file(dir+"/"+scenario+"_first.bin");expected=file(dir+"/"+scenario+".expected");
  }
  if(scenario=="lz_first"||scenario=="lz_same"||scenario=="lz_white"||scenario=="lz_first_invalid"){
    first=file(dir+"/"+scenario+"_first.bin");expected=file(dir+"/"+scenario+".expected");
  }
  feed(first);
  if(state!=TERMINAL){
   if(scenario=="stop")feed(file(dir+"/stop2.bin"));
   else if(scenario=="screen_timeout"){clockUs+=30000000;loop();}
   else {
    if(scenario=="same"||scenario=="white"){second=first;second[12]=2;crc(second);}
    expected.assign(second.begin()+20,second.end()-4);
    if(scenario.rfind("patch",0)==0){second=file(dir+"/"+scenario+".bin");expected=file(dir+"/"+scenario+".expected");}
    if(scenario.rfind("rle",0)==0&&scenario!="rle_first"){
      second=file(dir+"/"+scenario+".bin");expected=file(dir+"/"+scenario+".expected");
    }
    if(scenario.rfind("lz",0)==0&&scenario!="lz_first"){
      second=file(dir+"/"+scenario+".bin");expected=file(dir+"/"+scenario+".expected");
    }
    feed(second);
    if(scenario=="full")for(int n=3;n<=12;++n){second[12]=n;crc(second);feed(second);}
   }
  }
 }
 bool ok=scenario=="lz"||scenario=="lz_first"||scenario=="lz_same"||scenario=="lz_white"||scenario=="rle"||scenario=="rle_first"||scenario=="rle_same"||scenario=="rle_white"||scenario=="patch"||scenario=="patch_same"||scenario=="normal"||scenario=="same"||scenario=="white"||scenario=="full"||scenario=="stop"||scenario=="stop_before"||scenario=="screen_timeout"||scenario=="slow_paint";
 assert(state==TERMINAL);
 if(ok){assert(Serial.output.find("PAINTER_USB_ERROR")==std::string::npos);assert(Serial.output.find("PAINTER_USB_DONE")!=std::string::npos);assert(offs==(scenario=="stop_before"?0:1));assert(frees==4);}
 else {assert(Serial.output.find("PAINTER_USB_ERROR")!=std::string::npos);if(panelInitialized)assert(offs==1);if(scenario=="off")assert(Serial.output.find("PAINTER_USB_DONE")==std::string::npos);}
 if(scenario=="patch")assert(paints==2&&frames==2);
 if(scenario=="patch_same")assert(paints==1&&frames==2);
 if(scenario=="rle"||scenario=="rle_first")assert(paints==2&&frames==2);
 if(scenario=="lz"||scenario=="lz_first")assert(paints==2&&frames==2);
 if(scenario=="rle_same")assert(paints==1&&frames==2);
 if(scenario=="lz_same")assert(paints==1&&frames==2);
 if(scenario=="rle_white")assert(paints==0&&clears==1&&frames==2);
 if(scenario=="lz_white")assert(paints==0&&clears==1&&frames==2);
 if(scenario.rfind("rle",0)==0&&!ok)assert(paints==(scenario=="rle_first_invalid"?0:1));
 if(scenario.rfind("lz",0)==0&&!ok)assert(paints==(scenario=="lz_first_invalid"?0:1));
 if(scenario.rfind("patch_",0)==0 && scenario!="patch_same")assert(paints==(scenario=="patch_first"?0:1));
 if(scenario=="normal")assert(paints==2&&frames==2&&applied==2);
 if(scenario=="same")assert(paints==1&&frames==2&&applied==1);
 if(scenario=="white")assert(paints==0&&clears==1&&frames==2&&applied==1);
 if(scenario=="full")assert(paints==2&&frames==12);
 if(scenario=="frame_crc"||scenario=="session"||scenario=="sequence"||scenario=="length"||scenario=="early")assert(begins==0);
 if(scenario=="direct"||scenario=="begin")assert(clears==0&&paints==0);
 auto output=Serial.output;auto before=paints;feed(start);loop();assert(Serial.output==output&&paints==before);
 // OFF 실패 시 실제 스케치가 보존한 버퍼는 이 검사 프로세스에서만 정리한다.
 if(previous)free(previous);if(incoming)free(incoming);if(canvas)free(canvas);if(encoded)free(encoded);
 std::cout<<scenario<<": 통과\n";
}
'''

def packet(kind,seq,payload):
    body=struct.pack('<4sBBHIII',b'EPK1',7,kind,0,123,seq,len(payload))+payload
    return body+struct.pack('<I',zlib.crc32(body)&0xffffffff)

def main():
    cases=['normal','same','white','full','stop','stop_before','screen_timeout','slow_paint',
           'start_crc','frame_crc','old_version','old_version5','old_version6','limit','early','partial_timeout','session','sequence','length',
           'cpu','no_psram','small_psram','alloc','rx_buffer','rx_queue','rx_read','begin','direct',
           'clear_timeout','paint_timeout','paint_region','cycles','off','disconnect',
           'patch','patch_same','patch_first','patch_crc','patch_header','patch_zero','patch_long',
           'patch_row','patch_end','patch_truncated','patch_duplicate','patch_reverse','patch_size',
           'rle','rle_first','rle_same','rle_white','rle_first_invalid','rle_crc','rle_empty',
           'rle_run','rle_literal','rle_short','rle_long','rle_trailing','rle_size',
           'lz','lz_first','lz_same','lz_white','lz_first_invalid','lz_crc','lz_empty','lz_ref',
           'lz_literal','lz_short','lz_long','lz_trailing','lz_size','lz_zero','lz_future']
    with tempfile.TemporaryDirectory(prefix='painter-usb-qa-') as d:
        p=Path(d);(p/'esp32s3').mkdir()
        for name,data in [('Arduino.h',ARDUINO),('EPD_Painter.h',EPD),('check.cpp',HARNESS),
                          ('esp32s3/spiram.h','#pragma once\n#include <cstddef>\nsize_t esp_spiram_get_size();\n'),
                          ('esp_timer.h','#pragma once\n#include <cstdint>\nint64_t esp_timer_get_time();\n'),
                          ('esp_heap_caps.h','#pragma once\n#include <cstddef>\n#define MALLOC_CAP_SPIRAM 1\n#define MALLOC_CAP_8BIT 2\nvoid* heap_caps_malloc(size_t,int);\nvoid heap_caps_free(void*);\n')]:
            (p/name).write_text(data)
        a=bytes(range(256))*62+bytes(range(128));assert len(a)==16000
        for name,data in [('start',packet(1,0,b'\x02')),('first',packet(2,1,a)),('second',packet(2,2,bytes(255-x for x in a))),('white',packet(2,1,bytes(16000))),('stop1',packet(3,1,b'')),('stop2',packet(3,2,b''))]:
            (p/(name+'.bin')).write_bytes(data)
        target=bytearray(a)
        encoded_patch=bytearray()
        for offset,data in [(0,b'\x01'*80),(80,b'\x1b'),(15999,b'\xe4')]:
            target[offset:offset+len(data)]=data
            encoded_patch.extend(struct.pack('<HB',offset,len(data))+data)
        patches={'patch':bytes(encoded_patch),'patch_same':b'', 'patch_first':b'',
                 'patch_crc':b'', 'patch_header':b'\x00', 'patch_zero':b'\x00\x00\x00',
                 'patch_long':struct.pack('<HB',0,81)+bytes(81),
                 'patch_row':struct.pack('<HB',79,2)+bytes(2),
                 'patch_end':struct.pack('<HB',65535,1)+b'x',
                 'patch_truncated':struct.pack('<HB',0,2)+b'x',
                 'patch_duplicate':(struct.pack('<HB',0,1)+b'x')*2,
                 'patch_reverse':struct.pack('<HB',80,1)+b'x'+struct.pack('<HB',0,1)+b'x',
                 'patch_size':bytes(16000)}
        for name,data in patches.items():
            wire=packet(4,1 if name=='patch_first' else 2,data)
            if name=='patch_crc':wire=wire[:-1]+bytes([wire[-1]^1])
            (p/(name+'.bin')).write_bytes(wire)
            (p/(name+'.expected')).write_bytes(target if name=='patch' else a)
        # 실제 호스트 인코더와 독립된 원본 기대값으로 스케치 복원을 대조한다.
        import sys
        sys.path.insert(0,str(ROOT/'host'))
        from painter_rle import encode
        rle_target=bytes(range(128))+b'\x55'*(16000-128)
        valid=encode(rle_target)
        rles={'rle':valid,'rle_same':valid,'rle_white':encode(bytes(16000)),
              'rle_crc':valid,'rle_empty':b'','rle_run':b'\xff','rle_literal':b'\x01x',
              'rle_short':b'\x80x','rle_long':b'\xffx'*126,
              'rle_trailing':valid+b'\x80x','rle_size':bytes(16000)}
        for name,data in rles.items():
            wire=packet(5,2,data)
            if name=='rle_crc':wire=wire[:-1]+bytes([wire[-1]^1])
            (p/(name+'.bin')).write_bytes(wire)
            (p/(name+'.expected')).write_bytes(bytes(16000) if name=='rle_white' else rle_target)
        for name in ('rle_first','rle_same','rle_white','rle_first_invalid'):
            raw=bytes(16000) if name=='rle_white' else rle_target
            data=b'\xff' if name=='rle_first_invalid' else encode(raw)
            (p/(name+'_first.bin')).write_bytes(packet(5,1,data))
            (p/(name+'.expected')).write_bytes(raw)
        from painter_lz import encode as lz_encode
        lz_target=rle_target
        valid=lz_encode(lz_target)
        lzs={'lz':valid,'lz_same':valid,'lz_white':lz_encode(bytes(16000)),
             'lz_crc':valid,'lz_empty':b'','lz_ref':b'\xff','lz_literal':b'\x01x',
             'lz_short':b'\x00x','lz_long':valid+b'\xff\x01\x00',
             'lz_trailing':valid+b'\x00x','lz_size':bytes(16000),
             'lz_zero':b'\x00x\x80\x00\x00','lz_future':b'\x00x\x80\x02\x00'}
        for name,data in lzs.items():
            wire=packet(6,2,data)
            if name=='lz_crc':wire=wire[:-1]+bytes([wire[-1]^1])
            (p/(name+'.bin')).write_bytes(wire)
            (p/(name+'.expected')).write_bytes(bytes(16000) if name=='lz_white' else lz_target)
        for name in ('lz_first','lz_same','lz_white','lz_first_invalid'):
            raw=bytes(16000) if name=='lz_white' else lz_target
            data=b'\x80\x01\x00' if name=='lz_first_invalid' else lz_encode(raw)
            (p/(name+'_first.bin')).write_bytes(packet(6,1,data))
            (p/(name+'.expected')).write_bytes(raw)
        total=0
        for fast,diagnostics,white_pair,black_pair,gap2 in ((False,False,0,0,False),(False,True,0,0,False),(True,False,0,0,False),(True,True,0,0,False),(True,False,1,0,False),(True,True,1,0,False),(True,False,2,0,False),(True,True,2,0,False),(True,False,2,2,False),(True,True,2,2,False),(False,False,0,0,True),(False,True,0,0,True)):
            flags=['-DPAINTER_USB_DIAGNOSTICS','-DEPD_PAINTER_CAPTURE_TIMING'] if diagnostics else []
            if gap2:flags.append('-DPAINTER_USB_NORMAL_GAP_2MS')
            if fast:flags.append('-DPAINTER_USB_FAST')
            if black_pair:flags.append('-DEPD_PAINTER_FAST_BLACK_PAIR=2')
            if white_pair:flags.append(f'-DEPD_PAINTER_FAST_WHITE_PAIR={int(white_pair)}')
            subprocess.run(['clang++','-std=c++17','-fsanitize=address,undefined','-g','-I',d,*flags,
                            f'-DSKETCH_PATH="{ROOT/"firmware/painter_usb/painter_usb.ino"}"',str(p/'check.cpp'),'-o',str(p/'check')],check=True)
            selected=cases+(['timing_missing','timing_cycle'] if diagnostics else [])
            for case in selected:subprocess.run([str(p/'check'),case,d],check=True,timeout=10,stdout=subprocess.DEVNULL)
            total+=len(selected)
        invalid=subprocess.run(['clang++','-std=c++17','-I',d,'-DPAINTER_USB_FAST','-DPAINTER_USB_NORMAL_GAP_2MS',
                                f'-DSKETCH_PATH="{ROOT/"firmware/painter_usb/painter_usb.ino"}"',str(p/'check.cpp'),'-o',str(p/'invalid')],capture_output=True,text=True)
        assert invalid.returncode != 0 and '중간 속도 시험은 NORMAL 전용' in invalid.stderr
        print(f'실제 USB 스케치 NORMAL/FAST·일반/진단 {total}사례 통과')
if __name__=='__main__':main()
