/**
 * RUN 명령으로 한 번만 실행하는 제한 차분 갱신 시험.
 * 출처: https://github.com/Xinyuan-LilyGO/LilyGo-EPD47
 * 제조사 기준: 50224221fb81a8fcf9068532374b775af84a38d4 + busy-delay fix.
 * 드라이버 저작권·GNU GPL v3 조건은 원본 저장소 LICENSE를 따른다.
 * 기존 15단계 파형을 유지하며, 시험량 제한이 패널의 무손상을 보장하지 않는다.
 */

#include <Arduino.h>
#include <esp_arduino_version.h>
#include <esp_heap_caps.h>
#include <esp_timer.h>
#include <xtensa/core-macros.h>
#include "epd_driver.h"

#if !defined(CONFIG_IDF_TARGET_ESP32S3)
#error "Select ESP32S3 Dev Module."
#endif
#ifndef BOARD_HAS_PSRAM
#error "Enable OPI PSRAM."
#endif

// 제조사 C 함수는 공개 헤더에 선언이 없어 동일한 시그니처로 연결한다.
extern "C" void busy_delay(uint32_t cycles);

static const int32_t MASK_Y = 238;
static const int32_t MASK_HEIGHT = 64;
static const int32_t SQUARE_SIZE = 64;
static const int32_t X_START = 184;
static const int32_t X_STEP = 48;
static const size_t ROW_BYTES = EPD_WIDTH / 2;
static const size_t BUFFER_BYTES = ROW_BYTES * MASK_HEIGHT;
static const uint32_t MAX_FRAMES = 12;
static const uint32_t FRAME_PAUSE_MS = 250;
static const int64_t SCREEN_LIMIT_US = 30000000;
static const uint32_t DELAY_CYCLES = 120000;
static const uint32_t EXPECTED_MHZ = 240;
static const int64_t DELAY_TEST_US = 20000000;
static const int64_t DELAY_LIMIT_US = 25000000;

static_assert(EPD_WIDTH == 960 && EPD_HEIGHT == 540, "Unexpected panel size.");
static_assert(BUFFER_BYTES == 30720 && ROW_BYTES % 4 == 0, "Unexpected mask layout.");
static_assert(MASK_Y >= 0 && MASK_Y + MASK_HEIGHT <= EPD_HEIGHT, "Mask exceeds panel.");
static_assert(X_START % 2 == 0 && X_STEP % 2 == 0 && SQUARE_SIZE % 2 == 0,
              "Use byte-aligned pixels.");
static_assert(X_STEP > 0 && X_STEP < SQUARE_SIZE &&
              X_START + X_STEP * (MAX_FRAMES - 1) + SQUARE_SIZE <= EPD_WIDTH,
              "Square exceeds panel.");

struct TimingStats {
    uint64_t minimum = UINT64_MAX;
    uint64_t maximum = 0;
    uint64_t total = 0;
    uint32_t count = 0;

    void add(uint64_t value)
    {
        if (value < minimum) minimum = value;
        if (value > maximum) maximum = value;
        total += value;
        ++count;
    }

    void print(const char *metric, const char *unit = "us") const
    {
        Serial.printf("DELTA_STATS metric=%s unit=%s count=%lu min=%llu mean=%llu max=%llu\r\n",
                      metric, unit, (unsigned long)count,
                      (unsigned long long)(count ? minimum : 0),
                      (unsigned long long)(count ? total / count : 0),
                      (unsigned long long)maximum);
    }
};

static bool runConsumed = false;
static char command[8];
static size_t commandLength = 0;
static bool commandOverflow = false;
static uint32_t lastReadyMs = 0;

static void ready()
{
    Serial.println("DELTA_READY firmware=1 command=RUN repeat=0 panel=untouched");
    lastReadyMs = millis();
}

static bool checkDelay()
{
    const int expectedCore = xPortGetCoreID();
    const int64_t started = esp_timer_get_time();
    uint32_t signs[2] = {0, 0};
    TimingStats cyclesStats;
    TimingStats timeStats;
    const char *failure = nullptr;

    if (expectedCore < 0 || expectedCore > 1) failure = "invalid_core";
    while (!failure && esp_timer_get_time() - started < DELAY_TEST_US) {
        if (esp_timer_get_time() - started >= DELAY_LIMIT_US) {
            failure = "delay_test_timeout";
            break;
        }
        const int coreBefore = xPortGetCoreID();
        const uint32_t mhzBefore = getCpuFrequencyMhz();
        if (mhzBefore != EXPECTED_MHZ || coreBefore != expectedCore) {
            failure = mhzBefore != EXPECTED_MHZ ? "cpu_frequency" : "core_changed";
            break;
        }

        // 측정 구간에는 로그·대기가 없고, signed CCOUNT를 먼저 unsigned로 바꾼다.
        const int64_t timeBefore = esp_timer_get_time();
        const uint32_t counterBefore = (uint32_t)XTHAL_GET_CCOUNT();
        busy_delay(DELAY_CYCLES);
        const uint32_t counterAfter = (uint32_t)XTHAL_GET_CCOUNT();
        const int64_t timeAfter = esp_timer_get_time();
        const uint32_t elapsedCycles = counterAfter - counterBefore;
        const int coreAfter = xPortGetCoreID();
        const uint32_t mhzAfter = getCpuFrequencyMhz();
        ++signs[counterBefore >> 31];
        cyclesStats.add(elapsedCycles);
        timeStats.add(timeAfter - timeBefore);
        Serial.printf("DELTA_STATS metric=delay_sample sample=%lu core=%d core_after=%d mhz=%lu mhz_after=%lu sign=%lu start_cycles=%lu elapsed_cycles=%lu elapsed_us=%lld\r\n",
                      (unsigned long)cyclesStats.count, coreBefore, coreAfter,
                      (unsigned long)mhzBefore, (unsigned long)mhzAfter,
                      (unsigned long)(counterBefore >> 31), (unsigned long)counterBefore,
                      (unsigned long)elapsedCycles, (long long)(timeAfter - timeBefore));

        if (timeAfter - started >= DELAY_LIMIT_US) failure = "delay_test_timeout";
        else if (coreAfter != expectedCore) failure = "core_changed";
        else if (mhzAfter != EXPECTED_MHZ) failure = "cpu_frequency";
        else if (elapsedCycles < DELAY_CYCLES) failure = "delay_too_short";
        if (failure) break;
        delay(100);
    }
    const int64_t elapsed = esp_timer_get_time() - started;
    if (!failure && elapsed >= DELAY_LIMIT_US) failure = "delay_test_timeout";
    if (!failure && (!signs[0] || !signs[1])) failure = "missing_sign_samples";
    Serial.printf("DELTA_DELAY_DONE status=%s reason=%s samples=%lu sign0=%lu sign1=%lu core=%d requested_cycles=%lu elapsed_us=%lld\r\n",
                  failure ? "fail" : "pass", failure ? failure : "ok",
                  (unsigned long)cyclesStats.count, (unsigned long)signs[0],
                  (unsigned long)signs[1], expectedCore, (unsigned long)DELAY_CYCLES,
                  (long long)elapsed);
    cyclesStats.print("delay", "cycles");
    timeStats.print("delay");
    if (failure) Serial.printf("DELTA_ERROR reason=%s panel=untouched\r\n", failure);
    return !failure;
}

static void makeMask(uint8_t *pixels, int32_t x, int32_t width, bool white)
{
    // 전체 폭 입력으로 제조사의 좌우 F 패딩 경로를 피한다.
    // 흰 복원은 0이 무동작, 검정 추가는 F가 무동작이다. 짝수 x는 하위 nibble.
    memset(pixels, white ? 0x00 : 0xFF, BUFFER_BYTES);
    for (int32_t row = 0; row < MASK_HEIGHT; ++row) {
        memset(pixels + row * ROW_BYTES + x / 2, white ? 0xFF : 0x00, width / 2);
    }
}

static void runExperiment()
{
    Serial.printf("DELTA_START firmware=1 arduino=%d.%d.%d idf=%s driver=50224221fb81a8fcf9068532374b775af84a38d4+busy-delay-fix\r\n",
                  ESP_ARDUINO_VERSION_MAJOR, ESP_ARDUINO_VERSION_MINOR,
                  ESP_ARDUINO_VERSION_PATCH, ESP.getSdkVersion());
    Serial.printf("DELTA_STATS metric=config panel=960x540 mask_x=0 mask_y=%ld mask_width=960 mask_height=64 bpp=4 row_bytes=%lu buffer_bytes=%lu x_start=184 x_step=48 square=64 max_frames=12 soft_limit_us=30000000 pause_ms=250 waveform_steps=15\r\n",
                  (long)MASK_Y, (unsigned long)ROW_BYTES, (unsigned long)BUFFER_BYTES);
    Serial.printf("DELTA_STATS metric=memory flash_bytes=%lu psram_bytes=%lu free_psram_bytes=%lu free_heap_bytes=%lu\r\n",
                  (unsigned long)ESP.getFlashChipSize(), (unsigned long)ESP.getPsramSize(),
                  (unsigned long)ESP.getFreePsram(), (unsigned long)ESP.getFreeHeap());

    // 검사가 모두 끝날 때까지 패널 초기화·전원·갱신 함수를 부르지 않는다.
    uint8_t *pixels = (uint8_t *)heap_caps_malloc(BUFFER_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    const char *failure = nullptr;
    if (!pixels) failure = "allocation_failed";
    else if ((uintptr_t)pixels % 4 != 0) failure = "buffer_alignment";
    else if (getCpuFrequencyMhz() != EXPECTED_MHZ) failure = "cpu_frequency";
    if (failure) Serial.printf("DELTA_ERROR reason=%s panel=untouched\r\n", failure);
    if (failure || !checkDelay()) {
        if (pixels) heap_caps_free(pixels);
        Serial.printf("DELTA_DONE status=fail completed=0 reason=%s screen_elapsed_us=0 panel=untouched updates=stopped\r\n",
                      failure ? failure : "delay_test_failed");
        return;
    }

    Rect_t area = {0, MASK_Y, EPD_WIDTH, MASK_HEIGHT};
    TimingStats generateStats, onStats, whiteStats, blackStats, offStats, processStats, intervalStats;
    uint32_t completed = 0;
    int64_t previousFrameStart = 0;
    const char *stopReason = "frame_limit";

    epd_init();
    // 최초 전원 켜기부터 화면 예산을 센다. 실행 중인 호출을 강제로 중단하지 않는다.
    const int64_t screenStart = esp_timer_get_time();
    epd_poweron();
    const int64_t initialOnEnd = esp_timer_get_time();
    epd_clear();
    const int64_t initialClearEnd = esp_timer_get_time();
    epd_poweroff();
    const int64_t initialOffEnd = esp_timer_get_time();
    Serial.printf("DELTA_STATS metric=initial_clear power_on_us=%lld clear_us=%lld power_off_us=%lld screen_elapsed_us=%lld\r\n",
                  (long long)(initialOnEnd - screenStart),
                  (long long)(initialClearEnd - initialOnEnd),
                  (long long)(initialOffEnd - initialClearEnd),
                  (long long)(initialOffEnd - screenStart));

    for (uint32_t index = 0; index < MAX_FRAMES; ++index) {
        const int64_t frameStart = esp_timer_get_time();
        if (frameStart - screenStart >= SCREEN_LIMIT_US) {
            stopReason = "time_limit";
            break;
        }
        const int32_t squareX = X_START + X_STEP * (int32_t)index;
        if (index == 0) makeMask(pixels, squareX, SQUARE_SIZE, false);
        else makeMask(pixels, squareX - X_STEP, X_STEP, true);
        const int64_t generateEnd = esp_timer_get_time();
        epd_poweron();
        const int64_t onEnd = esp_timer_get_time();
        int64_t whiteUs = 0;
        int64_t generateUs = generateEnd - frameStart;
        int64_t blackStart = onEnd;
        if (index > 0) {
            epd_draw_image(area, pixels, WHITE_ON_BLACK);
            const int64_t whiteEnd = esp_timer_get_time();
            whiteUs = whiteEnd - onEnd;
            // 앞쪽 48열을 복원한 뒤 새 사각형 오른쪽 48열만 검게 한다.
            makeMask(pixels, squareX + SQUARE_SIZE - X_STEP, X_STEP, false);
            blackStart = esp_timer_get_time();
            generateUs += blackStart - whiteEnd;
        }
        epd_draw_image(area, pixels, BLACK_ON_WHITE);
        const int64_t blackEnd = esp_timer_get_time();
        epd_poweroff();
        const int64_t frameEnd = esp_timer_get_time();

        // 생성·전원·그리기·처리에는 Serial과 250ms 대기를 넣지 않는다.
        // 시작 간격·누적 경과에는 앞 프레임의 로그와 대기가 포함된다.
        const uint64_t interval = completed ? frameStart - previousFrameStart : 0;
        generateStats.add(generateUs);
        onStats.add(onEnd - generateEnd);
        if (index > 0) whiteStats.add(whiteUs);
        blackStats.add(blackEnd - blackStart);
        offStats.add(frameEnd - blackEnd);
        processStats.add(frameEnd - frameStart);
        if (completed) intervalStats.add(interval);
        ++completed;
        previousFrameStart = frameStart;
        Serial.printf("DELTA_FRAME index=%lu completed=%lu x=%ld y=%ld generate_us=%lld power_on_us=%lld white_us=%lld black_us=%lld power_off_us=%lld process_us=%lld start_interval_valid=%u start_interval_us=%llu screen_elapsed_us=%lld panel=off\r\n",
                      (unsigned long)index, (unsigned long)completed, (long)squareX, (long)MASK_Y,
                      (long long)generateUs, (long long)(onEnd - generateEnd),
                      (long long)whiteUs, (long long)(blackEnd - blackStart),
                      (long long)(frameEnd - blackEnd), (long long)(frameEnd - frameStart),
                      index ? 1U : 0U, (unsigned long long)interval,
                      (long long)(frameEnd - screenStart));
        delay(FRAME_PAUSE_MS);
    }

    const int64_t screenEnd = esp_timer_get_time();
    heap_caps_free(pixels);
    Serial.printf("DELTA_DONE status=pass completed=%lu reason=%s screen_elapsed_us=%lld panel=off updates=stopped\r\n",
                  (unsigned long)completed, stopReason, (long long)(screenEnd - screenStart));
    generateStats.print("generate");
    onStats.print("power_on");
    whiteStats.print("white");
    blackStats.print("black");
    offStats.print("power_off");
    processStats.print("process");
    intervalStats.print("start_interval");
}

void setup()
{
    Serial.begin(115200);
    ready();
}

void loop()
{
    if (runConsumed) {
        // 성공·실패와 관계없이 부팅당 한 번만 실행하고 최종 화면을 유지한다.
        delay(1000);
        return;
    }
    while (Serial.available()) {
        const char next = (char)Serial.read();
        if (next == '\n') {
            // CRLF도 받되 공백·접두어·긴 줄은 RUN으로 인정하지 않는다.
            if (commandLength && command[commandLength - 1] == '\r') --commandLength;
            if (!commandOverflow && commandLength == 3 && memcmp(command, "RUN", 3) == 0) {
                runConsumed = true;
                runExperiment();
                return;
            }
            commandLength = 0;
            commandOverflow = false;
        } else if (commandLength < sizeof(command)) {
            command[commandLength++] = next;
        } else {
            commandOverflow = true;
        }
    }
    if ((uint32_t)(millis() - lastReadyMs) >= 1000) ready();
    delay(10);
}
