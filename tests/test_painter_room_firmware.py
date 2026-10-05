"""v9 실제 스케치: CRC 검증 후 패치 복원→방 정리→동기화된 전체 재표시."""
import subprocess,tempfile
from pathlib import Path
import test_painter_usb_firmware as base
from test_painter_interactive_firmware import prefix
MAIN=r'''
static std::vector<uint8_t> make(int type,int seq,std::vector<uint8_t> data,int flags=0,int version=9){
 std::vector<uint8_t> p(24+data.size());memcpy(p.data(),"EPK1",4);p[4]=version;p[5]=type;p[6]=flags;p[8]=123;
 for(int i=0;i<4;i++){p[12+i]=(seq>>(i*8))&255;p[16+i]=(data.size()>>(i*8))&255;}
 memcpy(p.data()+20,data.data(),data.size());crc(p);return p;
}
int main(int argc,char**argv){
 assert(argc==2);scenario=argv[1];setup();
 assert(Serial.output.find("version=9")!=std::string::npos&&Serial.output.find("room_refresh=1")!=std::string::npos);
 auto start=make(1,0,{3,0},scenario=="start_flag"?1:0,scenario=="version"?8:9);feed(start);
 if(scenario=="start_flag"||scenario=="version"){assert(state==TERMINAL&&begins==0);return 0;}
 expected.assign(16000,0x55);auto first=make(2,1,expected,scenario=="first_flag"?1:0);feed(first);
 if(scenario=="first_flag"){assert(state==TERMINAL&&begins==0);return 0;}
 assert(frames==1&&clears==1);
 if(scenario=="periodic"||scenario=="both"){clockUs+=30000000;receiveWindow=clockUs;}
 int flag=scenario=="periodic"||scenario=="same"?0:1;
 std::vector<uint8_t> second;
 if(scenario=="patch"||scenario=="patch_bad"){
  expected[19]=0;second=make(4,2,{19,0,1,0},flag);
  if(scenario=="patch_bad"){second=make(4,2,{0,0,0},flag);}
 }else if(scenario=="rle_white"){
  expected.assign(16000,0);std::vector<uint8_t> rle;for(int i=0;i<125;i++){rle.push_back(255);rle.push_back(0);}second=make(5,2,rle,flag);
 }else if(scenario=="lz"){
  std::vector<uint8_t> lz(16000);int used=encode_painter_lz(expected.data(),16000,lz.data(),16000);assert(used>0);lz.resize(used);
  second=make(6,2,lz,flag);
 }else second=make(2,2,expected,scenario=="flags"?2:flag);
 if(scenario=="crc")second.back()^=1;
 if(scenario=="stop_flag")second=make(3,2,{},1);
 feed(second);
 if(scenario=="crc"||scenario=="flags"||scenario=="patch_bad"||scenario=="stop_flag"){
  assert(state==TERMINAL&&frames==1&&clears==1&&offs==1);return 0;
 }
 bool refreshed=scenario!="same";
 assert(state==WAIT_FRAME&&frames==2&&clears==(refreshed?2:1));
 assert(paints==(scenario=="same"||scenario=="rle_white"?1:2));
 if(refreshed){
  const char*reason=scenario=="both"?"both":scenario=="periodic"?"periodic":"room";
  assert(Serial.output.find(std::string("seq=2 reason=")+reason)!=std::string::npos);
  // 정리 직후 같은 입력은 다시 구동하지 않는다. PATCH 기준은 이미 표시한 입력이다.
  int before=paints;feed(make(4,3,{}));assert(paints==before&&clears==2);
 }else feed(make(3,3,{}));
 assert(state==TERMINAL&&offs==1&&frees==4);
}
'''
def main():
 with tempfile.TemporaryDirectory() as d:
  p=Path(d);(p/'Arduino.h').write_text(base.ARDUINO);(p/'EPD_Painter.h').write_text(base.EPD)
  (p/'esp_heap_caps.h').write_text('#include <cstddef>\n#define MALLOC_CAP_SPIRAM 1\n#define MALLOC_CAP_8BIT 2\nvoid* heap_caps_malloc(size_t,int);void heap_caps_free(void*);\n')
  (p/'esp_timer.h').write_text('#include <cstdint>\nint64_t esp_timer_get_time();\n');(p/'esp32s3').mkdir();(p/'esp32s3/spiram.h').write_text('#include <cstddef>\nsize_t esp_spiram_get_size();\n')
  (p/'check.cpp').write_text(prefix+MAIN)
  flags=['-DPAINTER_USB_DIAGNOSTICS','-DEPD_PAINTER_CAPTURE_TIMING','-DPAINTER_USB_NORMAL_GAP_2MS','-DPAINTER_USB_NORMAL_DIRECT13','-DPAINTER_USB_INTERACTIVE','-DPAINTER_USB_ROOM_REFRESH']
  subprocess.run(['clang++','-std=c++17','-fsanitize=address,undefined','-I',d,*flags,f'-DSKETCH_PATH="{base.ROOT/"firmware/painter_usb/painter_usb.ino"}"',str(p/'check.cpp'),'-o',str(p/'check')],check=True)
  cases=('version','start_flag','first_flag','same','room','periodic','both','patch','rle_white','lz','crc','flags','patch_bad','stop_flag')
  for case in cases:subprocess.run([str(p/'check'),case],check=True,timeout=40)
  print(f'실제 v9 스케치 {len(cases)}사례 ASan/UBSan 통과')
if __name__=='__main__':main()
