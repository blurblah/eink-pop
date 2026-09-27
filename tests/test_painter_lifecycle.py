#!/usr/bin/env python3
"""실제 EPD_Painter 완료/전원 함수의 경계 동작을 native C++로 검증한다."""
from pathlib import Path
import subprocess
import tempfile
from test_bw_interleaved_driver import definition

ROOT = Path(__file__).resolve().parents[1]
CPP = ROOT / 'repos/EPD_Painter/src/EPD_Painter.cpp'
HDR = ROOT / 'repos/EPD_Painter/src/EPD_Painter.h'
PREFIX = r'''
#include <atomic>
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <functional>
using TickType_t = uint32_t;
#define pdTRUE 1
#define pdMS_TO_TICKS(ms) ((TickType_t)(ms))
struct Sem { bool available = true; } active, power;
struct { struct { bool lcd_start = false; } lcd_user; } LCD_CAM;
static int64_t now = 0;
static int off = 0, gives = 0;
static std::function<void()> tick, acquired;
int64_t esp_timer_get_time() { return now; }
void vTaskDelay(int n) { now += n * 1000; if (tick) tick(); }
int xSemaphoreTake(Sem* s, TickType_t ticks) {
  for (TickType_t i = 0; !s->available && i < ticks; ++i) vTaskDelay(1);
  if (!s->available) return 0;
  s->available = false;
  if (s == &active && acquired) acquired();
  return 1;
}
void xSemaphoreGive(Sem* s) { assert(!s->available); s->available = true; ++gives; }
class EPD_Painter {
public:
  void* _paint_task_h = this;
  Sem* _paint_active_sem = &active;
  std::atomic<int> paintStage{0};
  void powerOff() { assert(paintStage == 0); assert(!LCD_CAM.lcd_user.lcd_start); ++off; }
  bool waitForIdle(uint32_t);
  bool powerOffWhenIdle(uint32_t);
  class PanelPowerGuard {
  public:
    static inline Sem* power_mtx = &power;
    static inline uint8_t state = 5;
    static bool powerOffNow(EPD_Painter&, TickType_t);
  };
};
'''
MAIN = r'''
static void reset() {
  now=off=gives=0; active.available=power.available=true;
  LCD_CAM.lcd_user.lcd_start=false; tick=nullptr; acquired=nullptr;
  EPD_Painter::PanelPowerGuard::power_mtx=&power;
  EPD_Painter::PanelPowerGuard::state=5;
}
int main() {
  reset(); EPD_Painter d;
  assert(d.waitForIdle(0)); assert(now==0 && active.available);
  d._paint_task_h=nullptr; assert(!d.waitForIdle(1)); d._paint_task_h=&d;
  d._paint_active_sem=nullptr; assert(!d.waitForIdle(1)); d._paint_active_sem=&active;

  // 두 번째 전이에서는 stage가0이어도 출력 잠금은 아직 잡혀 있다.
  reset(); active.available=false; LCD_CAM.lcd_user.lcd_start=true;
  tick=[] { if(now==5000) active.available=true; if(now==8000) LCD_CAM.lcd_user.lcd_start=false; };
  assert(d.powerOffWhenIdle(10)); assert(now==8000 && off==1 && active.available && power.available);
  assert(EPD_Painter::PanelPowerGuard::state==0);

  // 초기 stage2, 실제 전이 진행, 마지막 LCD 완료의 세 경계를 각각 기다린다.
  reset(); d.paintStage=2; active.available=false; LCD_CAM.lcd_user.lcd_start=true;
  tick=[&] { if(now==2000) d.paintStage=0; if(now==4000) active.available=true; if(now==6000) LCD_CAM.lcd_user.lcd_start=false; };
  assert(d.waitForIdle(10)); assert(now==6000 && off==0);

  reset(); d.paintStage=1; assert(!d.powerOffWhenIdle(3)); assert(now==3000 && off==0);
  reset(); d.paintStage=0; active.available=false;
  assert(!d.powerOffWhenIdle(3)); assert(now==3000 && off==0);
  reset(); LCD_CAM.lcd_user.lcd_start=true;
  assert(!d.powerOffWhenIdle(3)); assert(now==3000 && off==0 && active.available);

  // 출력 대기에쓴시간을다시전원잠금대기에더하지않는다.
  reset(); active.available=false; power.available=false;
  tick=[] { if(now==4000) active.available=true; };
  assert(!d.powerOffWhenIdle(7)); assert(now==7000 && off==0);

  reset(); power.available=false;
  tick=[] { if(now==2000) power.available=true; };
  assert(d.powerOffWhenIdle(4)); assert(now==2000 && off==1);

  reset(); EPD_Painter::PanelPowerGuard::power_mtx=nullptr;
  assert(d.powerOffWhenIdle(0)); assert(off==1);

  // 잠금을 얻는 사이 상태가바뀌면완료로오인하지않는다.
  reset(); acquired=[&] { d.paintStage=2; };
  assert(!d.waitForIdle(2)); assert(now==2000 && active.available);
  puts("라이프사이클 12개 경계 사례 통과");
}
'''

def main():
    source=CPP.read_text();header=HDR.read_text()
    body=definition(header,'powerOffNow').replace('static bool powerOffNow','bool EPD_Painter::PanelPowerGuard::powerOffNow',1)
    code=PREFIX+'\n'+body+'\n'+definition(source,'waitForIdle')+'\n'+definition(source,'powerOffWhenIdle')+'\n'+MAIN
    with tempfile.TemporaryDirectory(prefix='eink-painter-life-') as tmp:
        p=Path(tmp);(p/'check.cpp').write_text(code)
        subprocess.run(['clang++','-std=c++17','-fsanitize=address,undefined','-g',str(p/'check.cpp'),'-o',str(p/'check')],check=True)
        subprocess.run([str(p/'check')],check=True,timeout=10)
if __name__=='__main__':main()
