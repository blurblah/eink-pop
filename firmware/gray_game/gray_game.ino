/**
 * 사용자 확인16계조 게임 화면을 RUN 명령 뒤 한 번 표시한다.
 * 드라이버: LilyGo-EPD47 f1607dab2df95295e6b50fc0e44c4c092a13938f.
 * 드라이버 출처·GPL v3 조건은 repos/LilyGo-EPD47/LICENSE를 따른다.
 * 기존 USB v1 전송 시험과 구분되는 정지 화면 시험이다.
 */
#include <Arduino.h>
#include <esp_arduino_version.h>
#include <esp_heap_caps.h>
#include <esp_timer.h>
#include <xtensa/core-macros.h>
#include "epd_driver.h"
#include "src/game_image.h"

#if !defined(CONFIG_IDF_TARGET_ESP32S3) || !defined(BOARD_HAS_PSRAM)
#error "Select ESP32-S3 with OPI PSRAM."
#endif
#if !ARDUINO_USB_MODE || !ARDUINO_USB_CDC_ON_BOOT
#error "Select hardware USB CDC on boot."
#endif
extern "C" void busy_delay(uint32_t cycles);

static const size_t ROW_BYTES = EPD_WIDTH / 2;
static const size_t BUFFER_BYTES = ROW_BYTES * 400;
static const uint32_t DELAY_CYCLES = 120000;
static const uint32_t EXPECTED_MHZ = 240;
static const int64_t DELAY_TEST_US = 20000000;
static const int64_t DELAY_LIMIT_US = 25000000;
static_assert(EPD_WIDTH == 960 && EPD_HEIGHT == 540, "Unexpected panel.");
static_assert(BUFFER_BYTES == 192000 && ROW_BYTES % 4 == 0, "Unexpected stride.");
static_assert(sizeof(GAME_PIXELS) == 32000, "Unexpected image size.");

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
        Serial.printf("GRAY_GAME_STATS metric=%s unit=%s count=%lu min=%llu mean=%llu max=%llu\r\n",
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
    Serial.println("GRAY_GAME_READY firmware=1 command=RUN repeat=0 panel=untouched");
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
        Serial.printf("GRAY_GAME_STATS metric=delay_sample sample=%lu core=%d core_after=%d mhz=%lu mhz_after=%lu sign=%lu start_cycles=%lu elapsed_cycles=%lu elapsed_us=%lld\r\n",
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
    Serial.printf("GRAY_GAME_DELAY_DONE status=%s reason=%s samples=%lu sign0=%lu sign1=%lu core=%d requested_cycles=%lu elapsed_us=%lld\r\n",
                  failure ? "fail" : "pass", failure ? failure : "ok",
                  (unsigned long)cyclesStats.count, (unsigned long)signs[0],
                  (unsigned long)signs[1], expectedCore, (unsigned long)DELAY_CYCLES,
                  (long long)elapsed);
    cyclesStats.print("delay", "cycles");
    timeStats.print("delay");
    if (failure) Serial.printf("GRAY_GAME_ERROR reason=%s panel=untouched\r\n", failure);
    return !failure;
}

static void makeImage(uint8_t *pixels)
{
    memset(pixels, 0xFF, BUFFER_BYTES);
    for (int y = 0; y < 200; ++y) {
        uint8_t *row = pixels + y * 2 * ROW_BYTES;
        for (int x = 0; x < 320; ++x) {
            const uint8_t source = GAME_PIXELS[y * 160 + x / 2];
            const uint8_t gray = (source >> ((x % 2) * 4)) & 0x0F;
            // 가로2픽셀을 한 바이트에, 세로2행에 같은 값을 복제한다.
            row[80 + x] = gray | (gray << 4);
            row[ROW_BYTES + 80 + x] = gray | (gray << 4);
        }
    }
}

static void runExperiment()
{
    Serial.printf("GRAY_GAME_START firmware=1 arduino=%d.%d.%d driver=f1607dab2df95295e6b50fc0e44c4c092a13938f\r\n",
                  ESP_ARDUINO_VERSION_MAJOR, ESP_ARDUINO_VERSION_MINOR, ESP_ARDUINO_VERSION_PATCH);
    Serial.printf("GRAY_GAME_STATS metric=image source=320x200 bpp=4 source_sequence=46 packed_sha256=%s scale=2 x=160 y=70 width=640 height=400 draw_x=0 draw_y=70 draw_width=960 draw_height=400 buffer_bytes=192000 frames=1\r\n", GAME_PIXELS_SHA256);
    Serial.printf("GRAY_GAME_STATS metric=memory flash_bytes=%lu psram_bytes=%lu free_psram_bytes=%lu free_heap_bytes=%lu\r\n",
                  (unsigned long)ESP.getFlashChipSize(), (unsigned long)ESP.getPsramSize(),
                  (unsigned long)ESP.getFreePsram(), (unsigned long)ESP.getFreeHeap());
    uint8_t *pixels = (uint8_t *)heap_caps_malloc(BUFFER_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    const char *failure = nullptr;
    if (!pixels) failure = "allocation_failed";
    else if ((uintptr_t)pixels % 4 != 0) failure = "buffer_alignment";
    else if (getCpuFrequencyMhz() != EXPECTED_MHZ) failure = "cpu_frequency";
    if (failure) Serial.printf("GRAY_GAME_ERROR reason=%s panel=untouched\r\n", failure);
    if (failure || !checkDelay()) {
        if (pixels) heap_caps_free(pixels);
        Serial.printf("GRAY_GAME_DONE status=fail completed=0 reason=%s panel=untouched updates=stopped\r\n",
                      failure ? failure : "delay_test_failed");
        return;
    }
    const int64_t generateStart = esp_timer_get_time();
    makeImage(pixels);
    const int64_t generateEnd = esp_timer_get_time();
    epd_init();
    const int64_t screenStart = esp_timer_get_time();
    epd_poweron();
    const int64_t onEnd = esp_timer_get_time();
    epd_clear();
    const int64_t clearEnd = esp_timer_get_time();
    Rect_t area = {0, 70, EPD_WIDTH, 400};
    epd_draw_grayscale_image(area, pixels);
    const int64_t drawEnd = esp_timer_get_time();
    epd_poweroff();
    const int64_t screenEnd = esp_timer_get_time();
    heap_caps_free(pixels);
    Serial.printf("GRAY_GAME_DONE status=pass completed=1 reason=single_image generate_us=%lld power_on_us=%lld clear_us=%lld draw_us=%lld power_off_us=%lld screen_elapsed_us=%lld panel=off updates=stopped\r\n",
                  (long long)(generateEnd - generateStart), (long long)(onEnd - screenStart),
                  (long long)(clearEnd - onEnd), (long long)(drawEnd - clearEnd),
                  (long long)(screenEnd - drawEnd), (long long)(screenEnd - screenStart));
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
