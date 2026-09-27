#!/usr/bin/env python3
"""실제 제한 스케치의 시작·픽셀·횟수·오류 종료를 ASan/UBSan으로 검증한다."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
ARDUINO = r'''
#pragma once
#include <cstdint>
#include <cstddef>
#include <cstring>
#include <string>
#define CONFIG_IDF_TARGET_ESP32S3 1
#define BOARD_HAS_PSRAM 1
#define ARDUINO_USB_MODE 1
#define ARDUINO_USB_CDC_ON_BOOT 1
#define EPD_PAINTER_NO_NVS 1
struct MockSerial {
  std::string input, output;
  void setRxBufferSize(int) {}
  void begin(int) {}
  void setTxTimeoutMs(int) {}
  int available() { return input.size(); }
  int read() { int v=(uint8_t)input[0]; input.erase(0,1); return v; }
  void println(const char*);
  void printf(const char*, ...);
};
extern MockSerial Serial;
struct MockEsp { uint32_t getPsramSize(); };
extern MockEsp ESP;
uint32_t getCpuFrequencyMhz();
bool psramFound();
uint32_t millis();
void delay(uint32_t);
'''
EPD = r'''
#pragma once
#include "Arduino.h"
#define EPD_LILYGO_EPD47_H716_PRESET 716
class EPD_Painter {
public:
  enum class Quality { QUALITY_FAST, QUALITY_NORMAL };
  explicit EPD_Painter(int preset);
  void setAutoShutdown(bool);
  void setIdleTimeout(int);
  void setQuality(Quality);
  void setPaintProfile(bool);
  bool begin();
  bool setDirectTransitions(bool);
  void clearBuffers();
  void clear();
  bool waitForIdle(uint32_t);
  bool powerOffWhenIdle(uint32_t);
  uint32_t paintsCompleted() const;
  void paint(uint8_t*);
  void paintPacked(const uint8_t*, int);
};
'''
HARNESS = r'''
#include "Arduino.h"
#include "EPD_Painter.h"
#include <cassert>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <iostream>
static std::string scenario;
static int64_t clockUs=0;
static int begins=0, clears=0, paints=0, offs=0, freed=0, completed=0;
static bool autoOff=true, qualitySet=false, buffers=false, busy=false;
static int idleSeconds=0, directCalls=0;
static bool profileSet=false;
MockSerial Serial;
MockEsp ESP;
#include SKETCH_PATH
EPD_Painter::EPD_Painter(int p) { assert(p==716); }
void EPD_Painter::setAutoShutdown(bool b) { autoOff=b; }
void EPD_Painter::setIdleTimeout(int s) { idleSeconds=s; }
void EPD_Painter::setQuality(Quality q) {
  assert(begins==0 && !qualitySet);
#ifdef EINK_PAINTER_NORMAL
  assert(q==Quality::QUALITY_NORMAL);
#else
  assert(q==Quality::QUALITY_FAST);
#endif
  qualitySet=true;
}
void EPD_Painter::setPaintProfile(bool on) {
  assert(begins==0);
#ifdef EINK_PAINTER_NORMAL
  assert(!on);
#else
  assert(on);
#endif
  profileSet=true;
}
bool EPD_Painter::begin() {
  assert(!autoOff && qualitySet && profileSet && idleSeconds==60); ++begins;
  if(scenario=="slow_begin") clockUs+=31000000;
  return scenario!="begin_fail";
}
bool EPD_Painter::setDirectTransitions(bool on) {
  assert(on && begins==1 && initialized && !buffers && !clears && !paints);
  ++directCalls; return scenario!="direct_fail";
}
void EPD_Painter::clearBuffers() {
#ifdef EINK_PAINTER_DIRECT
  assert(directCalls==1);
#else
  assert(directCalls==0);
#endif
  buffers=true;
}
void EPD_Painter::clear() {
  assert(buffers); ++clears; busy=true;
  if(scenario=="host_stop") Serial.input="STOP\n";
  clockUs+=100000;
}
bool EPD_Painter::waitForIdle(uint32_t ms) {
  assert(ms==5000);
  if((scenario=="clear_timeout" && !paints) || (scenario=="paint_timeout" && paints)) return false;
  clockUs+=paints?20000:1000; busy=false;
  if(paints) completed += scenario=="zero_cycles"?0:scenario=="extra_cycles"?3:scenario=="two_cycles"?2:1;
  return true;
}
bool EPD_Painter::powerOffWhenIdle(uint32_t ms) {
  assert(ms==5000); ++offs;
  if(scenario=="off_fail") return false;
  busy=false; return true;
}
uint32_t EPD_Painter::paintsCompleted() const { return completed; }
static void checkPixels(const uint8_t* pixels, bool packed) {
  assert(!busy && clears==1 && offs==0);
  const bool moving=scenario=="move";
  for(int y=0;y<540;++y) for(int x=0;x<960;++x) {
    int expected=0;
    if(scenario=="game") {
      if(x>=160 && x<800 && y>=70 && y<470) {
        int sx=(x-160)/2, sy=(y-70)/2;
        int byte=(sx/4+sy*80+paints*16000)%256;
        expected=(byte >> (6-2*(sx%4)))&3;
      }
    } else if(moving) {
      if(x>=200+paints*16 && x<264+paints*16 && y>=240 && y<304) expected=3;
    } else {
      if(y>=190 && y<290 && x>=200 && x<720) {
        int column=(x-200)/140, local=(x-200)%140;
        if(local<100) expected=(local<4 || local>=96 || y<194 || y>=286)?3:column;
      }
      if(y>=330 && y<334 && x>=200 && x<720) expected=3;
    }
    const int pixel=y*960+x;
    const int actual=packed?((pixels[pixel/4] >> (6-2*(pixel%4)))&3):pixels[pixel];
    assert(actual==expected);
  }
  ++paints; busy=true; clockUs+=1000;
  if(scenario=="slow_paint") clockUs+=31000000;
}
void EPD_Painter::paint(uint8_t* pixels) {
#ifdef EINK_PAINTER_PACKED
  assert(false && "압축 빌드가 비압축 API를 호출함");
#endif
  checkPixels(pixels, false);
}
void EPD_Painter::paintPacked(const uint8_t* pixels, int repeat) {
#ifndef EINK_PAINTER_PACKED
  assert(false && "기존 빌드가 압축 API를 호출함");
#endif
  assert(repeat==1); checkPixels(pixels, true);
}
void MockSerial::println(const char* s) { output+=std::string(s)+"\n"; }
void MockSerial::printf(const char* fmt, ...) {
  char out[512]; va_list ap; va_start(ap,fmt); vsnprintf(out,sizeof(out),fmt,ap); va_end(ap);
  if(strstr(out,"PAINTER_FRAME")==out) assert(!busy && completed>0);
  if(strstr(out,"PAINTER_DONE")==out) assert(!busy && offs==1 && freed==1);
  output+=out;
}
// 물리 용량이 아닌 실기에서 관측한 힙 크기를 반환한다.
uint32_t MockEsp::getPsramSize() { return scenario=="psram"?4192000:8386279; }
bool psramFound() { return scenario!="psram_missing"; }
size_t esp_spiram_get_size() { assert(psramFound()); return scenario=="psram"?4194304:8388608; }
uint32_t getCpuFrequencyMhz() { return scenario=="cpu"?160:240; }
uint32_t millis() { return clockUs/1000; }
void delay(uint32_t ms) { if(scenario=="game") assert(ms!=100); clockUs+=ms*1000; }
int64_t esp_timer_get_time() { return clockUs; }
void* heap_caps_malloc(size_t n, int) {
#ifdef EINK_PAINTER_PACKED
  assert(n==129600);
#else
  assert(n==518400);
#endif
  return scenario=="alloc_fail"?nullptr:malloc(n);
}
void heap_caps_free(void* p) { ++freed; free(p); }
int main(int argc, char** argv) {
  assert(argc==2); scenario=argv[1]; setup();
  for(int i=0;i<2200;++i) loop();
  assert(begins==0 && clears==0 && paints==0 && offs==0);
#ifdef EINK_PAINTER_PACKED
  assert(Serial.output.find("PAINTER_READY version=7 panel=untouched")!=std::string::npos);
  assert(Serial.output.find("quality=NORMAL gray=4 nvs=0 direct=1 profile=0 packed=1 clip=fixture")!=std::string::npos);
#elif defined(EINK_PAINTER_NORMAL)
  assert(Serial.output.find("PAINTER_READY version=6 panel=untouched")!=std::string::npos);
  assert(Serial.output.find("quality=NORMAL gray=4 nvs=0 direct=1 profile=0 clip=fixture")!=std::string::npos);
#elif defined(EINK_PAINTER_DIRECT)
  assert(Serial.output.find("PAINTER_READY version=4 panel=untouched")!=std::string::npos);
#elif defined(EINK_PAINTER_GAME)
  assert(Serial.output.find("PAINTER_READY version=3 panel=untouched")!=std::string::npos);
#else
  assert(Serial.output.find("PAINTER_READY version=2 panel=untouched")!=std::string::npos);
#endif
  Serial.input=scenario=="move"?"MOVE\n":"STILL\n";
  if(scenario=="partial") {
    Serial.input="STI"; loop(); assert(begins==0); Serial.input="LL\r\n";
  }
  if(scenario=="invalid") Serial.input="RUN\n";
  if(scenario=="game" || scenario=="unsupported_game") Serial.input="GAME\n";
  if(scenario=="nul") Serial.input=std::string("STILL\0\n",7);
  if(scenario=="length") Serial.input="STILLLLL\n";
  if(scenario=="queued") Serial.input="STILL\nMOVE\n";
  loop();
  bool config=scenario=="cpu"||scenario=="psram"||scenario=="psram_missing"||scenario=="alloc_fail"||scenario=="invalid"||scenario=="nul"||scenario=="length"||scenario=="queued"||scenario=="unsupported_game";
  bool success=scenario=="still"||scenario=="move"||scenario=="partial"||scenario=="two_cycles"||scenario=="game";
  assert(terminal);
  if(config) assert(begins==0 && offs==0 && paints==0);
  else assert(begins==1);
  if(success) {
    assert(paints==((scenario=="move"||scenario=="game")?12:1)); assert(frames==(unsigned)paints);
    assert(Serial.output.find("PAINTER_DONE reason=limit")!=std::string::npos);
    assert(Serial.output.find("PAINTER_ERROR")==std::string::npos);
    assert(clears==1 && offs==1 && freed==1);
  } else if(scenario=="host_stop") {
    assert(paints==0 && offs==1 && freed==1);
    assert(Serial.output.find("PAINTER_DONE reason=host_stop")!=std::string::npos);
  } else {
    assert(Serial.output.find("PAINTER_ERROR")!=std::string::npos);
    if(config||scenario=="begin_fail"||scenario=="off_fail")
      assert(Serial.output.find("PAINTER_DONE")==std::string::npos);
    else assert(offs==1 && freed==1 && Serial.output.find("PAINTER_DONE reason=error")!=std::string::npos);
  }
  if(scenario=="slow_begin"||scenario=="clear_timeout"||scenario=="direct_fail") assert(paints==0);
  if(scenario=="direct_fail") assert(clears==0 && directCalls==1);
  if(scenario=="slow_paint") assert(paints==1 && Serial.output.find("reason=time_limit")!=std::string::npos);
  int before=paints; std::string output=Serial.output; Serial.input="MOVE\n";
  for(int i=0;i<1000;++i) loop();
  assert(paints==before && Serial.output==output);
  // OFF 실패 시 스케치가 보존한 버퍼만 테스트 프로세스 종료 전에 해제한다.
  if(canvas) { free(canvas); canvas=nullptr; }
  std::cout<<scenario<<": 통과\n";
}
'''
SCENARIOS = ['still','move','partial','two_cycles','invalid','nul','length','queued',
             'cpu','psram','psram_missing','alloc_fail','begin_fail','slow_begin','clear_timeout',
             'paint_timeout','off_fail','zero_cycles','extra_cycles','slow_paint','host_stop']

def main():
    with tempfile.TemporaryDirectory(prefix='eink-painter-sketch-') as tmp:
        p=Path(tmp)
        (p/'Arduino.h').write_text(ARDUINO)
        (p/'esp32s3').mkdir()
        (p/'esp32s3/spiram.h').write_text('#pragma once\n#include <cstddef>\nsize_t esp_spiram_get_size();\n')
        (p/'EPD_Painter.h').write_text(EPD)
        (p/'esp_timer.h').write_text('#pragma once\n#include <cstdint>\nint64_t esp_timer_get_time();\n')
        (p/'esp_heap_caps.h').write_text('#pragma once\n#include <cstddef>\n#define MALLOC_CAP_SPIRAM 1\n#define MALLOC_CAP_8BIT 2\nvoid* heap_caps_malloc(size_t,int);\nvoid heap_caps_free(void*);\n')
        (p/'check.cpp').write_text(HARNESS)
        # 게임 빌드에서는 위치·프레임별로 다른 4명도 값을 전체 픽셀에서 대조한다.
        rows=[]
        for frame in range(12):
            packed=[(i+frame*16000)%256 for i in range(16000)]
            assert set(packed)==set(range(256)) # 전체2bpp 바이트 조합
            rows.append('{'+','.join(map(str,packed))+'}')
        (p/'painter_game_frames.h').write_text('#pragma once\n#define GAME_CLIP_ID "fixture"\nstatic constexpr uint32_t GAME_FRAME_COUNT=12;\nstatic const uint8_t GAME_PACKED[12][16000]={'+','.join(rows)+'};\n')
        cases=0
        for game_build in (False,True,"direct","normal","packed"):
            subprocess.run(['clang++','-std=c++17','-fsanitize=address,undefined','-g','-I',tmp,
                            *(['-DEINK_PAINTER_GAME'] if game_build else []),
                            *(['-DEINK_PAINTER_DIRECT'] if game_build in ('direct','normal','packed') else []),
                            *(['-DEINK_PAINTER_NORMAL'] if game_build in ('normal','packed') else []),
                            *(['-DEINK_PAINTER_PACKED'] if game_build=='packed' else []),
                            f'-DSKETCH_PATH="{ROOT / "firmware/painter_bounded/painter_bounded.ino"}"',
                            str(p/'check.cpp'),'-o',str(p/'check')],check=True)
            for scenario in SCENARIOS+(['game'] if game_build else ['unsupported_game'])+(['direct_fail'] if game_build in ('direct','normal','packed') else []):
                subprocess.run([str(p/'check'),scenario],check=True,timeout=10)
                cases+=1
        print(f'실제 스케치 {cases}개 사례 통과 (기본/게임/direct/NORMAL/packed 빌드, 256바이트 조합·확대 픽셀 전체 비교 포함)')

if __name__=='__main__':
    main()
