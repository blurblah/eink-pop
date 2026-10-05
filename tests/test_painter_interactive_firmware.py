"""실제 스케치 v8의 긴 세션·주기 정리·STOP·시간/수신 상한 검증."""
import subprocess,tempfile
from pathlib import Path
import test_painter_usb_firmware as base

prefix=base.HARNESS.split('int main(int argc,char**argv){')[0]
prefix=prefix.replace('assert(!busy&&clears==1&&offs==0','assert(!busy&&clears>=1&&offs==0')
prefix=prefix.replace('static uint8_t desired[129600]={};','static uint8_t desired[129600]={}; static int priorClears=0; if(priorClears!=clears){memset(desired,0,sizeof desired);priorClears=clears;}')
MAIN=r'''
static std::vector<uint8_t> make(int type,int seq,std::vector<uint8_t> data,int version=8){
 std::vector<uint8_t> p(24+data.size());memcpy(p.data(),"EPK1",4);p[4]=version;p[5]=type;p[8]=123;
 for(int i=0;i<4;i++){p[12+i]=(seq>>(i*8))&255;p[16+i]=(data.size()>>(i*8))&255;}
 memcpy(p.data()+20,data.data(),data.size());crc(p);return p;
}
int main(int argc,char**argv){
 assert(argc==2);scenario=argv[1];setup();
 assert(Serial.output.find("version=8")!=std::string::npos);
 assert(Serial.output.find("play_max=6000 screen_limit_ms=600000 refresh_ms=30000")!=std::string::npos);
 int n=scenario=="zero"?0:scenario=="over"?6001:6000;
 auto start=make(1,0,{uint8_t(n&255),uint8_t(n>>8)},scenario=="version"?7:8);
 if(scenario=="short")start=make(1,0,{12});
 feed(start);
 if(scenario=="zero"||scenario=="over"||scenario=="version"||scenario=="short"){
  assert(state==TERMINAL&&begins==0&&Serial.output.find("PAINTER_USB_ERROR")!=std::string::npos);return 0;
 }
 if(scenario=="stop_before")feed(make(3,1,{}));
 else {
  expected.assign(16000,0x55);feed(make(2,1,expected));assert(frames==1&&state==WAIT_FRAME);
  if(scenario=="full"){
   std::vector<uint8_t> rle;for(int i=0;i<125;i++){rle.push_back(255);rle.push_back(0x55);}
   for(int i=2;i<=6000;i++)feed(make(5,i,rle));
   assert(frames==6000&&paints==1&&Serial.output.find("reason=limit")!=std::string::npos);
  }else if(scenario=="timeout"){
   clockUs=screenStart+SCREEN_LIMIT_US;loop();assert(Serial.output.find("reason=time_limit")!=std::string::npos);
  }else if(scenario=="rx_timeout"){
   clockUs+=5000000;loop();assert(Serial.output.find("reason=rx_timeout")!=std::string::npos);
  }else{
   if(scenario=="refresh"||scenario=="refresh_white"){
    clockUs+=30000000;receiveWindow=clockUs;
    if(scenario=="refresh_white")expected.assign(16000,0);
    feed(make(2,2,expected));
    assert(state==WAIT_FRAME&&frames==2&&clears==2&&applied==2);
    assert(paints==(scenario=="refresh_white"?1:2));
    assert(Serial.output.find("PAINTER_USB_REFRESH session=123 seq=2")!=std::string::npos);
   }
   feed(make(3,frames+1,{}));assert(Serial.output.find("reason=stop")!=std::string::npos);
  }
 }
 assert(state==TERMINAL&&offs==(scenario=="stop_before"?0:1)&&frees==4);
 auto old=Serial.output;feed(start);loop();assert(Serial.output==old);
}
'''
def main():
 with tempfile.TemporaryDirectory() as d:
  p=Path(d);(p/'Arduino.h').write_text(base.ARDUINO);(p/'EPD_Painter.h').write_text(base.EPD)
  (p/'esp_heap_caps.h').write_text('#include <cstddef>\n#define MALLOC_CAP_SPIRAM 1\n#define MALLOC_CAP_8BIT 2\nvoid* heap_caps_malloc(size_t,int);void heap_caps_free(void*);\n')
  (p/'esp_timer.h').write_text('#include <cstdint>\nint64_t esp_timer_get_time();\n')
  (p/'esp32s3').mkdir();(p/'esp32s3/spiram.h').write_text('#include <cstddef>\nsize_t esp_spiram_get_size();\n')
  (p/'check.cpp').write_text(prefix+MAIN)
  flags=['-DPAINTER_USB_DIAGNOSTICS','-DEPD_PAINTER_CAPTURE_TIMING','-DPAINTER_USB_NORMAL_GAP_2MS','-DPAINTER_USB_NORMAL_DIRECT13','-DPAINTER_USB_INTERACTIVE']
  subprocess.run(['clang++','-std=c++17','-fsanitize=address,undefined','-I',d,*flags,f'-DSKETCH_PATH="{base.ROOT/"firmware/painter_usb/painter_usb.ino"}"',str(p/'check.cpp'),'-o',str(p/'check')],check=True)
  cases=('zero','over','version','short','stop_before','stop','full','timeout','rx_timeout','refresh','refresh_white')
  for case in cases:subprocess.run([str(p/'check'),case],check=True,timeout=40)
  print('실제 v8 스케치 11사례·6000장/6001거부·600초/수신상한·정리 후 재표시/흰색·STOP/OFF 통과')
if __name__=='__main__':main()
