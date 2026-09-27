// H716 4명도 제한 시험. 유효한 명령 전에는 begin()도 호출하지 않는다.
#include <Arduino.h>
#include <EPD_Painter.h>
#include <esp_heap_caps.h>
#include <esp_timer.h>
#include <esp32s3/spiram.h>
#if defined(EINK_PAINTER_DIRECT) && !defined(EINK_PAINTER_GAME)
#error "직접 전이 비교는 동일 GAME 입력 빌드에서만 활성화합니다."
#endif
#if defined(EINK_PAINTER_NORMAL) && !defined(EINK_PAINTER_DIRECT)
#error "NORMAL 비교는 동일 GAME 직접 전이 빌드에서만 활성화합니다."
#endif
#if defined(EINK_PAINTER_PACKED) && !defined(EINK_PAINTER_NORMAL)
#error "압축 입력 비교는 NORMAL 직접 전이 빌드에서만 활성화합니다."
#endif
#ifdef EINK_PAINTER_GAME
#include <painter_game_frames.h>
static_assert(GAME_FRAME_COUNT == 12 && sizeof(GAME_PACKED) == 12 * 16000,
              "게임 입력은 320x200 2bpp 연속 12장이어야 합니다.");
#endif

#if !CONFIG_IDF_TARGET_ESP32S3 || !BOARD_HAS_PSRAM || !ARDUINO_USB_MODE || !ARDUINO_USB_CDC_ON_BOOT
#error "ESP32-S3, OPI PSRAM, HW CDC 설정이 필요합니다."
#endif
#ifndef EPD_PAINTER_NO_NVS
#error "기존 NVS 조정값을 사용하지 않도록 EPD_PAINTER_NO_NVS를 전체 빌드에 지정하세요."
#endif

static constexpr uint32_t LIMIT_US = 30000000;
static constexpr uint32_t WAIT_MS = 5000;
static constexpr int WIDTH = 960, HEIGHT = 540;
#ifdef EINK_PAINTER_PACKED
static constexpr size_t CANVAS_BYTES = WIDTH * HEIGHT / 4;
#else
static constexpr size_t CANVAS_BYTES = WIDTH * HEIGHT;
#endif
static EPD_Painter panel(EPD_LILYGO_EPD47_H716_PRESET);
static uint8_t *canvas = nullptr;
static bool terminal = false, initialized = false;
static uint32_t frames = 0, lastReady = 0;
static int64_t started = 0;
static char command[8];
static size_t commandUsed = 0;

static uint32_t physicalPsramBytes() {
    return psramFound() ? esp_spiram_get_size() : 0;
}

static void ready() {
#ifdef EINK_PAINTER_GAME
#ifdef EINK_PAINTER_PACKED
    Serial.printf("PAINTER_READY version=7 panel=untouched driver=7544e2d board=H716 quality=NORMAL gray=4 nvs=0 direct=1 profile=0 packed=1 clip=%s cpu_mhz=%lu psram_bytes=%lu psram_heap_bytes=%lu\r\n",
#elif defined(EINK_PAINTER_NORMAL)
    Serial.printf("PAINTER_READY version=6 panel=untouched driver=7544e2d board=H716 quality=NORMAL gray=4 nvs=0 direct=1 profile=0 clip=%s cpu_mhz=%lu psram_bytes=%lu psram_heap_bytes=%lu\r\n",
#elif defined(EINK_PAINTER_DIRECT)
    Serial.printf("PAINTER_READY version=4 panel=untouched driver=7544e2d board=H716 quality=FAST gray=4 nvs=0 direct=1 clip=%s cpu_mhz=%lu psram_bytes=%lu psram_heap_bytes=%lu\r\n",
#else
    Serial.printf("PAINTER_READY version=3 panel=untouched driver=7544e2d board=H716 quality=FAST gray=4 nvs=0 clip=%s cpu_mhz=%lu psram_bytes=%lu psram_heap_bytes=%lu\r\n",
#endif
                  GAME_CLIP_ID, (unsigned long)getCpuFrequencyMhz(),
                  (unsigned long)physicalPsramBytes(), (unsigned long)ESP.getPsramSize());
#else
    Serial.printf("PAINTER_READY version=2 panel=untouched driver=7544e2d board=H716 quality=FAST gray=4 nvs=0 cpu_mhz=%lu psram_bytes=%lu psram_heap_bytes=%lu\r\n",
                  (unsigned long)getCpuFrequencyMhz(), (unsigned long)physicalPsramBytes(),
                  (unsigned long)ESP.getPsramSize());
#endif
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
#ifdef EINK_PAINTER_PACKED
    for (int row = y; row < y + h; ++row)
        for (int col = x; col < x + w; ++col) {
            const int pixel = row * WIDTH + col;
            const int shift = 6 - 2 * (pixel % 4);
            canvas[pixel / 4] = (canvas[pixel / 4] & ~(3u << shift)) | (value << shift);
        }
#else
    for (int row = y; row < y + h; ++row)
        memset(canvas + row * WIDTH + x, value, w);
#endif
}

static void pattern(uint32_t index, uint8_t mode) {
    memset(canvas, 0, CANVAS_BYTES); // 흰색=0, 검정=3. 점무늬 변환 없음.
#ifdef EINK_PAINTER_GAME
    if (mode == 2) {
#ifdef EINK_PAINTER_PACKED
        for (int y = 0; y < 200; ++y) {
            uint8_t *row = canvas + (70 + y * 2) * (WIDTH / 4) + 160 / 4;
            for (int x = 0; x < 80; ++x) {
                const uint8_t b = GAME_PACKED[index][y * 80 + x];
                // [a,b,c,d] 네2비트 픽셀을 [a,a,b,b,c,c,d,d]로 확대한다.
                row[x * 2] = ((b >> 6) * 0x50) | (((b >> 4) & 3) * 0x05);
                row[x * 2 + 1] = (((b >> 2) & 3) * 0x50) | ((b & 3) * 0x05);
            }
            memcpy(row + WIDTH / 4, row, 640 / 4);
        }
#else
        for (int y = 0; y < 200; ++y) {
            uint8_t *row = canvas + (70 + y * 2) * WIDTH + 160;
            for (int x = 0; x < 320; ++x) {
                const int pixel = y * 320 + x;
                const uint8_t value = (GAME_PACKED[index][pixel / 4] >> (6 - 2 * (pixel % 4))) & 3;
                row[x * 2] = row[x * 2 + 1] = value;
            }
            memcpy(row + WIDTH, row, 640);
        }
#endif
        return;
    }
#endif
    if (!mode) {
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

static void run(uint8_t mode) {
    terminal = true; // 부팅당 한 번. 오류·STOP 뒤에도 다시 시작하지 않는다.
    started = esp_timer_get_time();
    if (Serial.available()) { fail("queued_command"); return; }
    if (getCpuFrequencyMhz() != 240 || physicalPsramBytes() < 8388608) {
        fail("board_config"); return;
    }
    canvas = (uint8_t *)heap_caps_malloc(CANVAS_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!canvas) { fail("canvas_allocation"); return; }
    const uint32_t limit = mode ? 12 : 1;
    Serial.printf("PAINTER_START mode=%s max_frames=%lu limit_us=%lu\r\n",
                  mode == 2 ? "game" : mode ? "move" : "still", (unsigned long)limit, (unsigned long)LIMIT_US);
    // 클래스 기본 AUTO가 있어도 명시한 H716 설정 때문에 탐색 전에 반환한다.
    panel.setAutoShutdown(false);
    panel.setIdleTimeout(60); // 30초 시험 중 기본 5초 타이머가 레일을 끄지 않도록 한다.
#ifdef EINK_PAINTER_NORMAL
    panel.setQuality(EPD_Painter::Quality::QUALITY_NORMAL);
    // 드라이버 printf와 HWCDC Serial이 같은 USB에서 섞인 v5 관측에 따른 제한.
    panel.setPaintProfile(false);
#else
    panel.setQuality(EPD_Painter::Quality::QUALITY_FAST);
    panel.setPaintProfile(true);
#endif
    if (!panel.begin()) { fail("begin_failed"); return; }
    initialized = true;
#ifdef EINK_PAINTER_DIRECT
    if (!panel.setDirectTransitions(true)) { fail("direct_init"); return; }
#endif
    panel.clearBuffers(); // 초기화되지 않은 화면 상태를 사용하지 않는다.
    if (expired()) { fail("time_limit"); return; }
    const int64_t clearStart = esp_timer_get_time();
    panel.clear();
    if (!panel.waitForIdle(WAIT_MS)) { fail("clear_timeout"); return; }
    Serial.printf("PAINTER_CLEAR elapsed_us=%lld\r\n", (long long)(esp_timer_get_time() - clearStart));
    for (uint32_t index = 0; index < limit; ++index) {
        if (expired()) { fail("time_limit"); return; }
        if (Serial.available()) { finish("host_stop"); return; } // 시작 후 입력은 중단으로만 처리한다.
        pattern(index, mode);
        const uint32_t before = panel.paintsCompleted();
        const int64_t drawStart = esp_timer_get_time();
#ifdef EINK_PAINTER_PACKED
        panel.paintPacked(canvas, 1);
#else
        panel.paint(canvas);
#endif
        if (!panel.waitForIdle(WAIT_MS)) { fail("paint_timeout"); return; }
        const uint32_t cycles = panel.paintsCompleted() - before;
        if (!cycles || cycles > 2) { fail("drive_count"); return; }
        ++frames;
        Serial.printf("PAINTER_FRAME seq=%lu cycles=%lu display_us=%lld elapsed_us=%lld\r\n",
                      (unsigned long)frames, (unsigned long)cycles,
                      (long long)(esp_timer_get_time() - drawStart),
                      (long long)(esp_timer_get_time() - started));
        if (expired()) { fail("time_limit"); return; }
        if (mode != 2 && index + 1 < limit) delay(100);
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
#ifdef EINK_PAINTER_GAME
            else if (!strcmp(command, "GAME")) run(2);
#endif
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
