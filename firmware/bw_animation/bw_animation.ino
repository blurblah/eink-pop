/**
 * 제한된 흑백 도형 애니메이션과 드라이버 호출 시간 측정.
 * 제조사 예제의 영역 지우기 → 그리기 → 패널 전원 끄기 순서를 따른다.
 * 출처: https://github.com/Xinyuan-LilyGO/LilyGo-EPD47
 * 드라이버 커밋: 50224221fb81a8fcf9068532374b775af84a38d4
 * 드라이버 라이선스: GNU GPL v3 (원본 저장소 LICENSE 참조).
 * 시험량 제한은 패널의 무손상 또는 장시간 사용을 보장하지 않는다.
 */

#include <Arduino.h>
#include <esp_arduino_version.h>
#include <esp_heap_caps.h>
#include <esp_timer.h>
#include "epd_driver.h"

#if !defined(CONFIG_IDF_TARGET_ESP32S3)
#error "Select ESP32S3 Dev Module."
#endif

#ifndef BOARD_HAS_PSRAM
#error "Enable OPI PSRAM."
#endif

static const int32_t AREA_X = 160;
static const int32_t AREA_Y = 70;
static const int32_t AREA_WIDTH = 640;
static const int32_t AREA_HEIGHT = 400;
static const size_t ROW_BYTES = AREA_WIDTH / 2;
static const size_t BUFFER_BYTES = ROW_BYTES * AREA_HEIGHT;
static const int32_t SQUARE_SIZE = 64;
static const int32_t SQUARE_X_START = 24;
static const int32_t SQUARE_X_STEP = 48;
static const int32_t SQUARE_Y = 168;
static const uint32_t MAX_FRAMES = 12;
static const uint32_t FRAME_PAUSE_MS = 250;
static const int64_t TEST_LIMIT_US = 30000000;

static_assert(EPD_WIDTH == 960 && EPD_HEIGHT == 540, "Unexpected panel size.");
static_assert(AREA_X >= 0 && AREA_X + AREA_WIDTH <= EPD_WIDTH, "Area exceeds panel width.");
static_assert(AREA_Y >= 0 && AREA_Y + AREA_HEIGHT <= EPD_HEIGHT, "Area exceeds panel height.");
static_assert(AREA_WIDTH % 2 == 0 && SQUARE_SIZE % 2 == 0 &&
              SQUARE_X_START % 2 == 0 && SQUARE_X_STEP % 2 == 0, "Use byte-aligned pixels.");
static_assert(SQUARE_X_START >= 0 &&
              SQUARE_X_START + SQUARE_X_STEP * (MAX_FRAMES - 1) + SQUARE_SIZE <= AREA_WIDTH,
              "Square exceeds area width.");
static_assert(SQUARE_Y >= 0 && SQUARE_Y + SQUARE_SIZE <= AREA_HEIGHT,
              "Square exceeds area height.");
static_assert(BUFFER_BYTES == 128000, "Unexpected framebuffer size.");

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

    void print(const char *name) const
    {
        Serial.printf("BW_STATS metric=%s count=%lu min_us=%llu mean_us=%llu max_us=%llu\r\n",
                      name, (unsigned long)count,
                      (unsigned long long)(count ? minimum : 0),
                      (unsigned long long)(count ? total / count : 0),
                      (unsigned long long)maximum);
    }
};

void setup()
{
    Serial.begin(115200);
    uint32_t serialStart = millis();
    while (!Serial && millis() - serialStart < 5000) {
        delay(10);
    }

    Serial.printf("BW_START firmware=1 arduino=%d.%d.%d idf=%s driver=50224221fb81a8fcf9068532374b775af84a38d4\r\n",
                  ESP_ARDUINO_VERSION_MAJOR, ESP_ARDUINO_VERSION_MINOR,
                  ESP_ARDUINO_VERSION_PATCH, ESP.getSdkVersion());
    Serial.printf("BW_CONFIG panel=960x540 area_x=%ld area_y=%ld area_width=%ld area_height=%ld bpp=4 colors=2 buffer_bytes=%lu row_bytes=%lu\r\n",
                  (long)AREA_X, (long)AREA_Y, (long)AREA_WIDTH, (long)AREA_HEIGHT,
                  (unsigned long)BUFFER_BYTES, (unsigned long)ROW_BYTES);
    Serial.printf("BW_LIMITS max_frames=%lu soft_limit_us=%lld pause_ms=%lu repeat=0 initial_full_clears=1\r\n",
                  (unsigned long)MAX_FRAMES, (long long)TEST_LIMIT_US,
                  (unsigned long)FRAME_PAUSE_MS);
    Serial.printf("BW_MEMORY flash_bytes=%lu psram_bytes=%lu free_psram_bytes=%lu free_heap_bytes=%lu\r\n",
                  (unsigned long)ESP.getFlashChipSize(),
                  (unsigned long)ESP.getPsramSize(),
                  (unsigned long)ESP.getFreePsram(),
                  (unsigned long)ESP.getFreeHeap());

    // 실패하면 패널 초기화·전원 조작·갱신을 수행하지 않는다.
    uint8_t *pixels = (uint8_t *)heap_caps_malloc(BUFFER_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!pixels) {
        Serial.println("BW_ERROR reason=allocation_failed updates=stopped");
        Serial.println("BW_DONE completed=0 reason=allocation_failed elapsed_us=0 panel_power=unchanged");
        return;
    }

    Rect_t area = {AREA_X, AREA_Y, AREA_WIDTH, AREA_HEIGHT};
    TimingStats clearStats;
    TimingStats drawStats;
    TimingStats processStats;
    uint32_t completed = 0;
    int64_t previousFrameStart = 0;
    const char *stopReason = "frame_limit";

    epd_init();

    // 시간 예산은 첫 화면 갱신의 전원 켜기 직전부터 센다.
    int64_t testStart = esp_timer_get_time();
    epd_poweron();
    int64_t initialOnEnd = esp_timer_get_time();
    epd_clear();
    int64_t initialClearEnd = esp_timer_get_time();
    epd_poweroff();
    int64_t initialOffEnd = esp_timer_get_time();
    Serial.printf("BW_INITIAL_CLEAR power_on_us=%lld clear_us=%lld power_off_us=%lld elapsed_us=%lld\r\n",
                  (long long)(initialOnEnd - testStart),
                  (long long)(initialClearEnd - initialOnEnd),
                  (long long)(initialOffEnd - initialClearEnd),
                  (long long)(initialOffEnd - testStart));

    for (uint32_t index = 0; index < MAX_FRAMES; ++index) {
        int32_t squareX = SQUARE_X_START + SQUARE_X_STEP * (int32_t)index;
        int64_t frameStart = esp_timer_get_time();
        // 실행 중인 드라이버를 중단하지 않고, 기한 이후 새 프레임만 막는다.
        if (frameStart - testStart >= TEST_LIMIT_US) {
            stopReason = "time_limit";
            break;
        }

        // 640폭 전용 버퍼: 짝수 x가 하위 nibble, 흰색은 F, 검정은 0.
        // 사각형 좌표·폭이 짝수이므로 한 바이트에 두 픽셀을 채운다.
        memset(pixels, 0xFF, BUFFER_BYTES);
        for (int32_t row = SQUARE_Y; row < SQUARE_Y + SQUARE_SIZE; ++row) {
            memset(pixels + row * ROW_BYTES + squareX / 2, 0x00, SQUARE_SIZE / 2);
        }
        int64_t generateEnd = esp_timer_get_time();
        epd_poweron();
        int64_t powerOnEnd = esp_timer_get_time();
        epd_clear_area(area);
        int64_t clearEnd = esp_timer_get_time();
        epd_draw_image(area, pixels, BLACK_ON_WHITE);
        int64_t drawEnd = esp_timer_get_time();
        epd_poweroff();
        int64_t frameEnd = esp_timer_get_time();

        // 처리 시간은 생성 시작부터 전원 끄기 완료까지이며 Serial·대기는 제외한다.
        // 시작 간격·누적 경과에는 이전 로그 출력과 대기가 포함된다.
        uint64_t startInterval = completed ? frameStart - previousFrameStart : 0;
        clearStats.add(clearEnd - powerOnEnd);
        drawStats.add(drawEnd - clearEnd);
        processStats.add(frameEnd - frameStart);
        ++completed;
        previousFrameStart = frameStart;
        Serial.printf("BW_FRAME index=%lu completed=%lu x=%ld y=%ld width=%ld height=%ld generate_us=%lld power_on_us=%lld clear_us=%lld draw_us=%lld power_off_us=%lld process_us=%lld start_interval_valid=%u start_interval_us=%llu elapsed_us=%lld panel_power=off\r\n",
                      (unsigned long)index, (unsigned long)completed,
                      (long)squareX, (long)SQUARE_Y, (long)SQUARE_SIZE, (long)SQUARE_SIZE,
                      (long long)(generateEnd - frameStart),
                      (long long)(powerOnEnd - generateEnd),
                      (long long)(clearEnd - powerOnEnd),
                      (long long)(drawEnd - clearEnd),
                      (long long)(frameEnd - drawEnd),
                      (long long)(frameEnd - frameStart),
                      index > 0 ? 1U : 0U, (unsigned long long)startInterval,
                      (long long)(frameEnd - testStart));
        delay(FRAME_PAUSE_MS);
    }

    int64_t testEnd = esp_timer_get_time();
    heap_caps_free(pixels);
    Serial.printf("BW_DONE completed=%lu reason=%s elapsed_us=%lld panel_power=off updates=stopped\r\n",
                  (unsigned long)completed, stopReason, (long long)(testEnd - testStart));
    clearStats.print("clear");
    drawStats.print("draw");
    processStats.print("process");
}

void loop()
{
    // 마지막 그림을 유지하며 재갱신·딥슬립·자동 재시작을 하지 않는다.
    delay(1000);
}
