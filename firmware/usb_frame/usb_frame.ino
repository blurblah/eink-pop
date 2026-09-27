/**
 * EPK1 v3 제한 USB 흑백 수신: 최초 전체 지우기 후 바뀐 픽셀만 두 패스로 출력.
 * 드라이버: https://github.com/Xinyuan-LilyGO/LilyGo-EPD47
 * 기준 커밋: 9b8dcdba04ed03ee70dba412667803b1dac1adfd (GNU GPL v3).
 * 출처·저작권은 드라이버 원본 LICENSE를 따른다.
 * ACK는 호출 반환 기준이며 광학 전환 완료 또는 패널 무손상을 보장하지 않는다.
 */
#include <Arduino.h>
#include <esp_arduino_version.h>
#include <esp_heap_caps.h>
#include <esp_timer.h>
#include <xtensa/core-macros.h>
#include "epd_driver.h"

#if !defined(CONFIG_IDF_TARGET_ESP32S3) || !defined(BOARD_HAS_PSRAM)
#error "Select ESP32-S3 with OPI PSRAM."
#endif
#if !ARDUINO_USB_MODE || !ARDUINO_USB_CDC_ON_BOOT
#error "Select hardware USB CDC on boot."
#endif
extern "C" void busy_delay(uint32_t cycles);

static const size_t SOURCE_BYTES = 8000;
static const size_t MASK_ROW_BYTES = EPD_WIDTH / 2;
static const size_t MASK_BYTES = MASK_ROW_BYTES * 400;
static const int64_t RX_LIMIT_US = 5000000;
static const int64_t SCREEN_LIMIT_US = 30000000;
static const uint32_t DELAY_CYCLES = 120000;
static const uint32_t EXPECTED_MHZ = 240;
static const int64_t DELAY_TEST_US = 20000000;
static const int64_t DELAY_LIMIT_US = 25000000;
static_assert(EPD_WIDTH == 960 && EPD_HEIGHT == 540, "Unexpected panel.");
static_assert(MASK_BYTES == 192000 && MASK_ROW_BYTES % 4 == 0, "Unexpected stride.");

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
        Serial.printf("USB_FRAME_STATS metric=%s unit=%s count=%lu min=%llu mean=%llu max=%llu\r\n",
                      metric, unit, (unsigned long)count,
                      (unsigned long long)(count ? minimum : 0),
                      (unsigned long long)(count ? total / count : 0),
                      (unsigned long long)maximum);
    }
};

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
        Serial.printf("USB_FRAME_STATS metric=delay_sample sample=%lu core=%d core_after=%d mhz=%lu mhz_after=%lu sign=%lu start_cycles=%lu elapsed_cycles=%lu elapsed_us=%lld\r\n",
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
    Serial.printf("USB_FRAME_DELAY_DONE status=%s reason=%s samples=%lu sign0=%lu sign1=%lu core=%d requested_cycles=%lu elapsed_us=%lld\r\n",
                  failure ? "fail" : "pass", failure ? failure : "ok",
                  (unsigned long)cyclesStats.count, (unsigned long)signs[0],
                  (unsigned long)signs[1], expectedCore, (unsigned long)DELAY_CYCLES,
                  (long long)elapsed);
    cyclesStats.print("delay", "cycles");
    timeStats.print("delay");
    return !failure;
}

enum SessionState { WAIT_START, WAIT_FRAME, TERMINAL };
static SessionState state = WAIT_START;
static uint32_t session = 0, expectedSequence = 1;
static uint32_t frameLimit = 0, frames = 0, applied = 0;
static uint8_t *previous = nullptr, *incoming = nullptr, *mask = nullptr;
static bool panelInitialized = false;
static int64_t screenStart = -1, receiveWindow = -1, packetStart = -1;
static uint32_t lastReadyMs = 0;
static uint8_t header[20], crcBytes[4], startPayload;
static size_t headerUsed = 0, payloadUsed = 0, crcUsed = 0;
static uint32_t payloadLength = 0, runningCrc = 0xFFFFFFFFU;

static uint32_t readLe32(const uint8_t *bytes)
{
    return (uint32_t)bytes[0] | ((uint32_t)bytes[1] << 8) |
           ((uint32_t)bytes[2] << 16) | ((uint32_t)bytes[3] << 24);
}

static uint32_t updateCrc(uint32_t crc, const uint8_t *bytes, size_t count)
{
    // IEEE CRC-32: 초기 FFFFFFFF, reflected polynomial, 최종 XOR FFFFFFFF.
    for (size_t i = 0; i < count; ++i) {
        crc ^= bytes[i];
        for (uint8_t bit = 0; bit < 8; ++bit) {
            crc = (crc >> 1) ^ ((crc & 1U) ? 0xEDB88320U : 0U);
        }
    }
    return crc;
}

static void resetPacket()
{
    headerUsed = payloadUsed = crcUsed = 0;
    payloadLength = 0;
    runningCrc = 0xFFFFFFFFU;
    packetStart = -1;
}

static void finish(const char *reason)
{
    state = TERMINAL;
    if (previous) heap_caps_free(previous);
    if (incoming) heap_caps_free(incoming);
    if (mask) heap_caps_free(mask);
    previous = incoming = mask = nullptr;
    Serial.printf("USB_FRAME_DONE session=%lu reason=%s frames=%lu applied=%lu\r\n",
                  (unsigned long)session, reason, (unsigned long)frames, (unsigned long)applied);
}

static void fail(const char *reason)
{
    Serial.printf("USB_FRAME_ERROR reason=%s\r\n", reason);
    finish("error");
}

static void ready()
{
    Serial.println("USB_FRAME_READY version=3 panel=untouched");
    lastReadyMs = millis();
}

static bool checkLimits()
{
    const int64_t now = esp_timer_get_time();
    if (screenStart >= 0 && now - screenStart >= SCREEN_LIMIT_US) {
        finish("time_limit");
        return false;
    }
    const int64_t deadlineStart = state == WAIT_START ? packetStart : receiveWindow;
    if (deadlineStart >= 0 && now - deadlineStart >= RX_LIMIT_US) {
        fail("rx_timeout");
        return false;
    }
    return true;
}

static bool validateHeader()
{
    if (memcmp(header, "EPK1", 4) != 0 || header[4] != 3 || header[6] || header[7]) {
        fail("header");
        return false;
    }
    const uint8_t type = header[5];
    const uint32_t packetSession = readLe32(header + 8);
    const uint32_t sequence = readLe32(header + 12);
    payloadLength = readLe32(header + 16);
    if (state == WAIT_START) {
        if (type != 1 || sequence != 0 || packetSession == 0 || payloadLength != 1) {
            fail("start_header");
            return false;
        }
    } else {
        if (packetSession != session) {
            fail("session");
            return false;
        }
        if (sequence != expectedSequence) {
            fail("sequence");
            return false;
        }
        if ((type != 2 && type != 3) ||
            (type == 2 && payloadLength != SOURCE_BYTES) || (type == 3 && payloadLength != 0)) {
            fail("frame_header");
            return false;
        }
    }
    runningCrc = updateCrc(runningCrc, header, sizeof(header));
    return true;
}

static void beginSession(uint32_t id)
{
    session = id;
    if (startPayload < 1 || startPayload > 12) {
        fail("frame_limit");
        return;
    }
    state = WAIT_FRAME; // 이 부팅에서는 성공·실패와 관계없이 START를 다시 받지 않는다.
    frameLimit = startPayload;
    Serial.printf("USB_FRAME_ACCEPT session=%lu max_frames=%lu\r\n",
                  (unsigned long)session, (unsigned long)frameLimit);
    Serial.printf("USB_FRAME_STATS metric=config arduino=%d.%d.%d driver=9b8dcdba04ed03ee70dba412667803b1dac1adfd source=320x200 bpp=1 msb_first=1 black=1 white=0 scale=2 x=160 y=70 rx_bytes=32768 mask_bytes=192000 refresh=initial_clear_then_delta\r\n",
                  ESP_ARDUINO_VERSION_MAJOR, ESP_ARDUINO_VERSION_MINOR, ESP_ARDUINO_VERSION_PATCH);
    Serial.printf("USB_FRAME_STATS metric=memory flash_bytes=%lu psram_bytes=%lu free_psram_bytes=%lu free_heap_bytes=%lu\r\n",
                  (unsigned long)ESP.getFlashChipSize(), (unsigned long)ESP.getPsramSize(),
                  (unsigned long)ESP.getFreePsram(), (unsigned long)ESP.getFreeHeap());

    const unsigned caps = MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT;
    previous = (uint8_t *)heap_caps_malloc(SOURCE_BYTES, caps);
    incoming = (uint8_t *)heap_caps_malloc(SOURCE_BYTES, caps);
    mask = (uint8_t *)heap_caps_malloc(MASK_BYTES, caps);
    if (!previous || !incoming || !mask) {
        fail("allocation_failed");
        return;
    }
    if (((uintptr_t)previous | (uintptr_t)incoming | (uintptr_t)mask) & 3U) {
        fail("buffer_alignment");
        return;
    }
    if (getCpuFrequencyMhz() != EXPECTED_MHZ) {
        fail("cpu_frequency");
        return;
    }
    memset(previous, 0x00, SOURCE_BYTES);
    if (!checkDelay()) {
        fail("delay_test_failed");
        return;
    }
    // 선행 검사 도중 미리 도착한 FRAME은 허용하지 않는다.
    if (Serial.available() != 0) {
        fail("early_data");
        return;
    }
    Serial.printf("USB_FRAME_RX session=%lu\r\n", (unsigned long)session);
    receiveWindow = esp_timer_get_time();
}

static void makeMask(int firstRow, int lastRow, bool white)
{
    const size_t count = MASK_ROW_BYTES * (lastRow - firstRow + 1) * 2;
    memset(mask, white ? 0x00 : 0xFF, count);
    for (int y = firstRow; y <= lastRow; ++y) {
        uint8_t *row = mask + (y - firstRow) * 2 * MASK_ROW_BYTES;
        for (int x = 0; x < 320; ++x) {
            const uint8_t bit = 0x80U >> (x % 8);
            const size_t offset = y * 40 + x / 8;
            const bool oldBlack = (previous[offset] & bit) != 0;
            const bool newBlack = (incoming[offset] & bit) != 0;
            if (white ? (oldBlack && !newBlack) : (!oldBlack && newBlack)) {
                // 두 가로 픽셀은 한 바이트이며 같은 바이트를 다음 행에도 복제한다.
                row[80 + x] = white ? 0xFF : 0x00;
                row[MASK_ROW_BYTES + 80 + x] = white ? 0xFF : 0x00;
            }
        }
    }
}

static void displayFrame(uint32_t sequence, uint32_t packetCrc, int64_t rxUs)
{
    if (!checkLimits()) return;
    const int64_t displayStart = esp_timer_get_time();
    int firstRow = 200, lastRow = -1;
    bool hasWhite = false, hasBlack = false;
    for (size_t i = 0; i < SOURCE_BYTES; ++i) {
        if (previous[i] != incoming[i]) {
            const int row = i / 40;
            if (row < firstRow) firstRow = row;
            lastRow = row;
            hasWhite |= (previous[i] & (uint8_t)~incoming[i]) != 0;
            hasBlack |= ((uint8_t)~previous[i] & incoming[i]) != 0;
        }
    }
    const bool firstFrame = !panelInitialized;
    const bool changed = firstFrame || hasWhite || hasBlack;
    int64_t clearUs = 0, whiteUs = 0, blackUs = 0;
    if (changed) {
        // 차분 검사 중 예산이 만료되면 패널 접근 전에 중단한다.
        if (!checkLimits()) return;
        if (firstFrame) {
            epd_init();
            panelInitialized = true;
            screenStart = esp_timer_get_time();
        }
        epd_poweron();
        if (firstFrame) {
            const int64_t started = esp_timer_get_time();
            epd_clear();
            clearUs = esp_timer_get_time() - started;
        }
        // 시작한 흰 복원→검정 추가는 시간 예산을 넘겨도 끝내고 전원을 끈다.
        if (hasWhite || hasBlack) {
            Rect_t area = {0, 70 + firstRow * 2, EPD_WIDTH, (lastRow - firstRow + 1) * 2};
            if (hasWhite) {
                makeMask(firstRow, lastRow, true);
                const int64_t started = esp_timer_get_time();
                epd_draw_image(area, mask, WHITE_ON_BLACK);
                whiteUs = esp_timer_get_time() - started;
            }
            if (hasBlack) {
                makeMask(firstRow, lastRow, false);
                const int64_t started = esp_timer_get_time();
                epd_draw_image(area, mask, BLACK_ON_WHITE);
                blackUs = esp_timer_get_time() - started;
            }
        }
        epd_poweroff();
        ++applied;
    }
    // 전체 프레임 처리가 반환한 뒤에만 이전 화면을 확정한다. 무변경도 한도에 센다.
    uint8_t *old = previous;
    previous = incoming;
    incoming = old;
    ++frames;
    ++expectedSequence;
    const int64_t displayEnd = esp_timer_get_time();
    // HWCDC는 연결 미확인 상태의 write를 큐에 남길 수 있다.
    // 연결 확인 없이 flush하면 그 큐를 버리므로, 패널을 끈 뒤 짧게 확인한다.
    const int64_t ackWait = esp_timer_get_time();
    while (!Serial) {
        if (esp_timer_get_time() - ackWait >= 100000) {
            fail("ack_connection");
            return;
        }
        delay(1);
    }
    Serial.printf("USB_FRAME_ACK session=%lu seq=%lu crc=%08lx result=%s rx_us=%lld display_us=%lld clear_us=%lld white_us=%lld black_us=%lld elapsed_us=%lld\r\n",
                  (unsigned long)session, (unsigned long)sequence, (unsigned long)packetCrc,
                  changed ? "applied" : "unchanged", (long long)rxUs,
                  (long long)(displayEnd - displayStart), (long long)clearUs, (long long)whiteUs, (long long)blackUs,
                  (long long)(displayEnd - screenStart));
    Serial.flush();
    if (!Serial) {
        fail("ack_connection");
        return;
    }
    if (frames >= frameLimit) finish("limit");
    else if (displayEnd - screenStart >= SCREEN_LIMIT_US) finish("time_limit");
    else receiveWindow = esp_timer_get_time();
}

static void completePacket()
{
    if (!checkLimits()) return;
    const uint32_t packetCrc = readLe32(crcBytes);
    const int64_t rxUs = esp_timer_get_time() - packetStart;
    if ((runningCrc ^ 0xFFFFFFFFU) != packetCrc) {
        fail("crc");
        return;
    }
    const uint8_t type = header[5];
    const uint32_t id = readLe32(header + 8), sequence = readLe32(header + 12);
    resetPacket();
    if (type == 1) beginSession(id);
    else if (type == 3) finish("stop");
    else displayFrame(sequence, packetCrc, rxUs);
}

static size_t readChunk(uint8_t *destination, size_t remaining, int available)
{
    const size_t requested = remaining < (size_t)available ? remaining : (size_t)available;
    const size_t received = Serial.read(destination, requested);
    // 코어 2.0.17 HWCDC는 RX 큐가 없으면 size_t(-1)을 반환한다.
    if (received > requested) {
        fail("rx_read");
        return 0;
    }
    return received;
}

void setup()
{
    // HWCDC begin 전에 큐 크기를 고정한다. 동작 중 큐를 재생성하지 않는다.
    const size_t rxSize = Serial.setRxBufferSize(32768);
    Serial.begin(115200);
    Serial.setTxTimeoutMs(100);
    if (rxSize != 32768) fail("rx_buffer");
    else ready();
}

void loop()
{
    if (state == TERMINAL) {
        delay(1000);
        return;
    }
    if (!checkLimits()) return;
    while (state != TERMINAL) {
        if (!checkLimits()) return;
        const int available = Serial.available();
        if (available < 0) {
            fail("rx_queue");
            return;
        }
        if (available == 0) break;
        size_t received;
        if (headerUsed < sizeof(header)) {
            if (packetStart < 0) packetStart = esp_timer_get_time();
            received = readChunk(header + headerUsed, sizeof(header) - headerUsed, available);
            headerUsed += received;
            if (state == TERMINAL) return;
            if (headerUsed == sizeof(header) && !validateHeader()) return;
        } else if (payloadUsed < payloadLength) {
            uint8_t *destination = state == WAIT_START ? &startPayload : incoming;
            received = readChunk(destination + payloadUsed, payloadLength - payloadUsed, available);
            if (state == TERMINAL) return;
            runningCrc = updateCrc(runningCrc, destination + payloadUsed, received);
            payloadUsed += received;
        } else {
            received = readChunk(crcBytes + crcUsed, sizeof(crcBytes) - crcUsed, available);
            crcUsed += received;
            if (state == TERMINAL) return;
            if (crcUsed == sizeof(crcBytes)) {
                completePacket();
                return;
            }
        }
        if (!received) break;
    }
    if (state == WAIT_START && !headerUsed && (uint32_t)(millis() - lastReadyMs) >= 1000) ready();
    delay(1);
}
