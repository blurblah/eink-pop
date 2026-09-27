#!/usr/bin/env python3
"""실제 LCD 콜백의 SDK 반환 계약을 C로 검사한다. 보드는 사용하지 않는다."""
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'repos/LilyGo-EPD47/src/i2s_data_bus.c'


def callback(source):
    match=re.search(r'static bool notify_trans_done\([^)]*\)\s*\{[^{}]*\}',source)
    if not match:raise ValueError('실제 콜백 추출 실패')
    return match[0]


class CallbackTests(unittest.TestCase):
    def test_actual_callback_contract(self):
        harness='''#include <stdbool.h>
#include <assert.h>
typedef void *esp_lcd_panel_io_handle_t;
typedef struct { int unused; } esp_lcd_panel_io_event_data_t;
static volatile bool output_done;
'''+callback(SOURCE.read_text())+'''
int main(void) {
    output_done=false;
    bool yield=notify_trans_done(0,0,0);
    assert(output_done); /* 완료 플래그는 계속 설정해야 한다. */
    assert(!yield);      /* 콜백 자체는 RTOS 태스크를 깨우지 않는다. */
    assert(!notify_trans_done(0,0,0)); /* 반복 호출도 같은 계약 */
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory);(p/'callback.c').write_text(harness)
            subprocess.run(['clang','-std=c11','-Wall','-Wextra','-Werror','-Wno-unused-parameter',str(p/'callback.c'),'-o',str(p/'callback')],check=True,capture_output=True)
            result=subprocess.run([str(p/'callback')],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)


if __name__=='__main__':unittest.main(verbosity=2)
