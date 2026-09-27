// H716 FAST 4명도 제한 시험. STILL/MOVE 명령 전에는 begin()도 호출하지 않는다.
#include <Arduino.h>
#include <EPD_Painter.h>
#include <esp_heap_caps.h>
#include <esp_timer.h>

#if !CONFIG_IDF_TARGET_ESP32S3 || !BOARD_HAS_PSRAM || !ARDUINO_USB_MODE || !ARDUINO_USB_CDC_ON_BOOT
#error "ESP32-S3, OPI PSRAM, HW CDC 설정이 필요합니다."
#endif
#ifndef EPD_PAINTER_NO_NVS
#error "기존 NVS 조정값을 사용하지 않도록 EPD_PAINTER_NO_NVS를 전체 빌드에 지정하세요."
#endif

static constexpr uint32_t LIMIT_US = 30000000;
static constexpr uint32_t WAIT_MS = 5000;
static constexpr int WIDTH = 960, HEIGHT = 540;
static EPD_Painter panel(EPD_LILYGO_EPD47_H716_PRESET);
static uint8_t *canvas = nullptr;
static bool terminal = false, initialized = false;
static uint32_t frames = 0, lastReady = 0;
static int64_t started = 0;
static char command[8];
static size_t commandUsed = 0;

static void ready() {
    Serial.println("PAINTER_READY version=1 panel=untouched driver=7544e2d board=H716 quality=FAST gray=4 nvs=0");
    lastReady = millis();
}

static bool finish(const char *reason) {
    if (initialized && !panel.powerOffWhenIdle(WAIT_MS)) {
        Serial.println("PAINTER_ERROR reason=poweroff_timeout");
        return false; // 실제 출력이 계속될 수 있어 DONE을 내보내지 않는다.
    }
    if (canvas) { heap_caps_free(canvas); canvas = nullptr; }
    Serial.printf("PAINTER_DONE reason=%s frames=%lu poweroff_returned=%d\r\n",
                  reason, (unsigned long)frames, initialized ? 1 : 0);
    return true;
}

static void fail(const char *reason) {
    Serial.printf("PAINTER_ERROR reason=%s\r\n", reason);
    // 초기화 실패는 드라이버 내부의 부분 초기화 상태이므로 end()를 호출하지 않는다.
    if (initialized) finish("error");
    else if (canvas) { heap_caps_free(canvas); canvas = nullptr; }
}

static bool expired() { return esp_timer_get_time() - started >= LIMIT_US; }

static void rectangle(int x, int y, int w, int h, uint8_t value) {
    for (int row = y; row < y + h; ++row)
        memset(canvas + row * WIDTH + x, value, w);
}

static void pattern(uint32_t index, bool moving) {
    memset(canvas, 0, WIDTH * HEIGHT); // 흰색=0, 검정=3. 점무늬 변환 없음.
    if (!moving) {
        // 왼쪽부터 흰색·밝은 회색·어두운 회색·검정. 흰색도 테두리로 구별한다.
        for (int shade = 0; shade < 4; ++shade) {
            const int x = 200 + shade * 140;
            rectangle(x, 190, 100, 100, 3);
            rectangle(x + 4, 194, 92, 92, shade);
        }
        rectangle(200, 330, 520, 4, 3);
    } else {
        rectangle(200 + index * 16, 240, 64, 64, 3);
    }
}

static void run(bool moving) {
    terminal = true; // 부팅당 한 번. 오류·STOP 뒤에도 다시 시작하지 않는다.
    started = esp_timer_get_time();
    if (Serial.available()) { fail("queued_command"); return; }
    if (getCpuFrequencyMhz() != 240 || ESP.getPsramSize() < 8388608) {
        fail("board_config"); return;
    }
    canvas = (uint8_t *)heap_caps_malloc(WIDTH * HEIGHT, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!canvas) { fail("canvas_allocation"); return; }
    const uint32_t limit = moving ? 12 : 1;
    Serial.printf("PAINTER_START mode=%s max_frames=%lu limit_us=%lu\r\n",
                  moving ? "move" : "still", (unsigned long)limit, (unsigned long)LIMIT_US);
    // 클래스 기본 AUTO가 있어도 명시한 H716 설정 때문에 탐색 전에 반환한다.
    panel.setAutoShutdown(false);
    panel.setIdleTimeout(60); // 30초 시험 중 기본 5초 타이머가 레일을 끄지 않도록 한다.
    panel.setQuality(EPD_Painter::Quality::QUALITY_FAST);
    panel.setPaintProfile(true);
    if (!panel.begin()) { fail("begin_failed"); return; }
    initialized = true;
    panel.clearBuffers(); // 초기화되지 않은 화면 상태를 사용하지 않는다.
    if (expired()) { fail("time_limit"); return; }
    const int64_t clearStart = esp_timer_get_time();
    panel.clear();
    if (!panel.waitForIdle(WAIT_MS)) { fail("clear_timeout"); return; }
    Serial.printf("PAINTER_CLEAR elapsed_us=%lld\r\n", (long long)(esp_timer_get_time() - clearStart));
    for (uint32_t index = 0; index < limit; ++index) {
        if (expired()) { fail("time_limit"); return; }
        if (Serial.available()) { finish("host_stop"); return; } // 시작 후 입력은 중단으로만 처리한다.
        pattern(index, moving);
        const uint32_t before = panel.paintsCompleted();
        const int64_t drawStart = esp_timer_get_time();
        panel.paint(canvas);
        if (!panel.waitForIdle(WAIT_MS)) { fail("paint_timeout"); return; }
        const uint32_t cycles = panel.paintsCompleted() - before;
        if (!cycles || cycles > 2) { fail("drive_count"); return; }
        ++frames;
        Serial.printf("PAINTER_FRAME seq=%lu cycles=%lu display_us=%lld elapsed_us=%lld\r\n",
                      (unsigned long)frames, (unsigned long)cycles,
                      (long long)(esp_timer_get_time() - drawStart),
                      (long long)(esp_timer_get_time() - started));
        if (expired()) { fail("time_limit"); return; }
        if (index + 1 < limit) delay(100);
    }
    finish("limit"); // 마지막 화면은 유지하되 패널 레일 제어는 OFF.
}

void setup() {
    Serial.setRxBufferSize(1024);
    Serial.begin(115200);
    Serial.setTxTimeoutMs(100);
    ready();
}

void loop() {
    if (terminal) { delay(10); return; }
    while (Serial.available()) {
        const int value = Serial.read();
        if (value < 0) break;
        if (value == '\r') continue;
        if (value == '\n') {
            command[commandUsed] = 0;
            if (!strcmp(command, "STILL")) run(false);
            else if (!strcmp(command, "MOVE")) run(true);
            else { terminal = true; fail("command"); }
            return;
        }
        if (value < 'A' || value > 'Z') {
            terminal = true; fail("command_byte"); return;
        }
        if (commandUsed + 1 >= sizeof(command)) {
            terminal = true; fail("command_length"); return;
        }
        command[commandUsed++] = (char)value;
    }
    if (!commandUsed && (uint32_t)(millis() - lastReady) >= 1000) ready();
    delay(1);
}
