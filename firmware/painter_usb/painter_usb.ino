// EPK1 v7 / 2bpp·무손실 행 패치·RLE. CRC 확인 전에는 패널을 초기화하지 않는다.
// 수신 상태 머신은 firmware/usb_frame의 검증된 EPK1 구조를 사용한다.
#include <Arduino.h>
#include "painter_lz.h"
#include <EPD_Painter.h>
#include <esp_heap_caps.h>
#include <esp_timer.h>
#include <esp32s3/spiram.h>
#if !CONFIG_IDF_TARGET_ESP32S3 || !BOARD_HAS_PSRAM || !ARDUINO_USB_MODE || !ARDUINO_USB_CDC_ON_BOOT
#error "ESP32-S3 / OPI PSRAM / HW CDC 설정이 필요합니다."
#endif
#ifndef EPD_PAINTER_NO_NVS
#error "모든 C++ 단위에 EPD_PAINTER_NO_NVS를 지정하세요."
#endif
#if defined(PAINTER_USB_DIAGNOSTICS) && !defined(EPD_PAINTER_CAPTURE_TIMING)
#error "진단 앱은 드라이버 시간 캡처 빌드가 필요합니다."
#endif
#if defined(EPD_PAINTER_FAST_WHITE_PAIR) && !defined(PAINTER_USB_FAST)
#error "흰색 복귀 시험은 FAST 빌드에만 허용합니다."
#endif
#if defined(EPD_PAINTER_FAST_BLACK_PAIR) && !defined(PAINTER_USB_FAST)
#error "검정 보정 시험은 FAST 빌드에만 허용합니다."
#endif
#if defined(EPD_PAINTER_FAST_BLACK_PAIR) && EPD_PAINTER_FAST_BLACK_PAIR != 2
#error "검정 보정 시험은2쌍으로 제한합니다."
#endif
#if defined(PAINTER_USB_NORMAL_GAP_2MS) && defined(PAINTER_USB_FAST)
#error "중간 속도 시험은 NORMAL 전용입니다."
#endif
#if defined(PAINTER_USB_NORMAL_DIRECT13) && !defined(PAINTER_USB_NORMAL_GAP_2MS)
#error "13주사 시험은 NORMAL 2ms 설정이 필요합니다."
#endif
#ifdef PAINTER_USB_NORMAL_DIRECT13
#include "normal_direct13.h"
#endif
#if defined(PAINTER_USB_PLAY120) && !defined(PAINTER_USB_NORMAL_DIRECT13)
#error "120장 시험은 NORMAL 2ms 13주사 설정이 필요합니다."
#endif
#ifdef PAINTER_USB_PLAY120
static constexpr uint32_t MAX_SESSION_FRAMES = 120;
#else
static constexpr uint32_t MAX_SESSION_FRAMES = 12;
#endif
static constexpr size_t SOURCE_BYTES = 16000, CANVAS_BYTES = 129600;
static constexpr int64_t RX_LIMIT_US = 5000000, SCREEN_LIMIT_US = 30000000;
static constexpr uint32_t WAIT_MS = 5000;
#ifdef PAINTER_USB_FAST
#define PAINTER_USB_QUALITY_NAME "FAST"
static constexpr auto DISPLAY_QUALITY = EPD_Painter::Quality::QUALITY_FAST;
#else
#define PAINTER_USB_QUALITY_NAME "NORMAL"
static constexpr auto DISPLAY_QUALITY = EPD_Painter::Quality::QUALITY_NORMAL;
#endif
#ifdef PAINTER_USB_NORMAL_GAP_2MS
// 제한 비교 전용: 원본 프리셋·파형은 보존하고 4명도 NORMAL 간격만 변경한다.
static EPD_Painter::Config trialConfig()
{
    auto config = EPD_LILYGO_EPD47_H716_PRESET;
    config.pass_gap_us_normal = 2000;
#ifdef PAINTER_USB_NORMAL_DIRECT13
    config.trains.dir_normal = NORMAL_DIRECT13_TRIAL;
#endif
    return config;
}
// 드라이버가 원본 Config 포인터도 보관하므로 정적 수명을 유지한다.
static const EPD_Painter::Config gapTrialConfig = trialConfig();
static EPD_Painter panel(gapTrialConfig);
#else
static EPD_Painter panel(EPD_LILYGO_EPD47_H716_PRESET);
#endif

enum SessionState { WAIT_START, WAIT_FRAME, TERMINAL };
static SessionState state = WAIT_START;
static uint32_t session = 0, expectedSequence = 1;
static uint32_t frameLimit = 0, frames = 0, applied = 0;
static uint8_t *previous = nullptr, *incoming = nullptr, *canvas = nullptr, *encoded = nullptr;
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
    static constexpr uint32_t nibble[16] = {
        0x00000000U, 0x1DB71064U, 0x3B6E20C8U, 0x26D930ACU,
        0x76DC4190U, 0x6B6B51F4U, 0x4DB26158U, 0x5005713CU,
        0xEDB88320U, 0xF00F9344U, 0xD6D6A3E8U, 0xCB61B38CU,
        0x9B64C2B0U, 0x86D3D2D4U, 0xA00AE278U, 0xBDBDF21CU
    };
    for (size_t i = 0; i < count; ++i) {
        crc ^= bytes[i];
        crc = (crc >> 4) ^ nibble[crc & 15U];
        crc = (crc >> 4) ^ nibble[crc & 15U];
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
    if (panelInitialized && !panel.powerOffWhenIdle(WAIT_MS)) {
        Serial.println("PAINTER_USB_ERROR reason=poweroff_timeout");
        return; // 출력 중일 수 있으므로 버퍼를 해제하거나 DONE을 내보내지 않는다.
    }
    if (previous) heap_caps_free(previous);
    if (incoming) heap_caps_free(incoming);
    if (canvas) heap_caps_free(canvas);
    if (encoded) heap_caps_free(encoded);
    previous = incoming = canvas = encoded = nullptr;
    Serial.printf("PAINTER_USB_DONE session=%lu reason=%s frames=%lu applied=%lu poweroff_returned=%d\r\n",
                  (unsigned long)session, reason, (unsigned long)frames,
                  (unsigned long)applied, panelInitialized ? 1 : 0);
}

static void fail(const char *reason)
{
    Serial.printf("PAINTER_USB_ERROR reason=%s\r\n", reason);
    finish("error");
}

static void ready()
{
    Serial.println("PAINTER_USB_READY version=7 panel=untouched driver=ffeea2a board=H716 quality=" PAINTER_USB_QUALITY_NAME " gray=4 direct=1 profile=0 packed=1"
#ifdef PAINTER_USB_DIAGNOSTICS
                   " timing=1"
#else
                   " timing=0"
#endif
#if defined(EPD_PAINTER_FAST_WHITE_PAIR) && EPD_PAINTER_FAST_WHITE_PAIR == 2
                   " white=pair2"
#elif defined(EPD_PAINTER_FAST_WHITE_PAIR)
                   " white=pair"
#endif
#ifdef EPD_PAINTER_FAST_BLACK_PAIR
                   " black=pair2"
#endif
#ifdef PAINTER_USB_NORMAL_GAP_2MS
                   " normal_gap_us=2000"
#endif
#ifdef PAINTER_USB_NORMAL_DIRECT13
                   " normal_direct=13"
#endif
#ifdef PAINTER_USB_PLAY120
                   " play_max=120"
#endif
                   " rle=1 lz=1");
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
    if (memcmp(header, "EPK1", 4) != 0 || header[4] != 7 || header[6] || header[7]) {
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
        if ((type != 2 && type != 3 && type != 4 && type != 5 && type != 6) ||
            (type == 2 && payloadLength != SOURCE_BYTES) || (type == 3 && payloadLength != 0) ||
            (type == 4 && (frames == 0 || payloadLength >= SOURCE_BYTES)) ||
            ((type == 5 || type == 6) && (payloadLength == 0 || payloadLength >= SOURCE_BYTES))) {
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
    if (startPayload < 1 || startPayload > MAX_SESSION_FRAMES) { fail("frame_limit"); return; }
    state = WAIT_FRAME;
    frameLimit = startPayload;
    Serial.printf("PAINTER_USB_ACCEPT session=%lu max_frames=%lu\r\n",
                  (unsigned long)session, (unsigned long)frameLimit);
    if (getCpuFrequencyMhz() != 240 || !psramFound() || esp_spiram_get_size() < 8388608) {
        fail("board_config"); return;
    }
    const unsigned caps = MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT;
    previous = (uint8_t *)heap_caps_malloc(SOURCE_BYTES, caps);
    incoming = (uint8_t *)heap_caps_malloc(SOURCE_BYTES, caps);
    canvas = (uint8_t *)heap_caps_malloc(CANVAS_BYTES, caps);
    encoded = (uint8_t *)heap_caps_malloc(SOURCE_BYTES, caps);
    if (!previous || !incoming || !canvas || !encoded) { fail("allocation_failed"); return; }
    memset(previous, 0, SOURCE_BYTES);
    // 여백은 부팅당 한 번만 초기화한다. paintPacked는 이 입력을 복사한다.
    memset(canvas, 0, CANVAS_BYTES);
    if (Serial.available() != 0) { fail("early_data"); return; }
    Serial.printf("PAINTER_USB_RX session=%lu\r\n", (unsigned long)session);
    receiveWindow = esp_timer_get_time();
}

static EPD_Painter::Rect expandFrame()
{
    uint8_t expanded[160];
    int left = 80, right = -1, top = 200, bottom = -1;
    for (int y = 0; y < 200; ++y) {
        const uint8_t *next = incoming + y * 80;
        const uint8_t *prior = previous + y * 80;
        if (memcmp(next, prior, 80) == 0) continue;
        int first = 0, last = 79;
        while (next[first] == prior[first]) ++first;
        while (next[last] == prior[last]) --last;
        if (first < left) left = first;
        if (last > right) right = last;
        if (y < top) top = y;
        bottom = y;
        uint8_t *row = canvas + (70 + y * 2) * 240 + 40;
        for (int x = first; x <= last; ++x) {
            const uint8_t b = next[x];
            expanded[x * 2] = ((b >> 6) * 0x50) | (((b >> 4) & 3) * 0x05);
            expanded[x * 2 + 1] = (((b >> 2) & 3) * 0x50) | ((b & 3) * 0x05);
        }
        const size_t count = (last - first + 1) * 2;
        memcpy(row + first * 2, expanded + first * 2, count);
        memcpy(row + 240 + first * 2, expanded + first * 2, count);
    }
    if (right < left) return {0, 0, 0, 0};
    return {(int16_t)(160 + left * 8), (int16_t)(70 + top * 2),
            (int16_t)((right - left + 1) * 8), (int16_t)((bottom - top + 1) * 2)};
}

static void displayFrame(uint32_t sequence, uint32_t packetCrc, int64_t rxUs, int64_t decodeUs)
{
    if (!checkLimits()) return;
    const int64_t displayStart = esp_timer_get_time();
    const bool firstFrame = !panelInitialized;
    const bool changed = memcmp(previous, incoming, SOURCE_BYTES) != 0;
    int64_t clearUs = 0, prepareUs = 0, paintUs = 0;
    uint32_t cycles = 0;
#ifdef PAINTER_USB_DIAGNOSTICS
    EPD_Painter::PaintTiming timing;
#endif
    if (firstFrame) {
        screenStart = esp_timer_get_time();
        panel.setAutoShutdown(false);
        panel.setIdleTimeout(60);
        panel.setQuality(DISPLAY_QUALITY);
        panel.setPaintProfile(false);
        if (!panel.begin()) { fail("begin_failed"); return; }
        panelInitialized = true;
        if (!panel.setDirectTransitions(true)) { fail("direct_init"); return; }
        panel.clearBuffers();
        if (!checkLimits()) return;
        const int64_t started = esp_timer_get_time();
        panel.clear();
        if (!panel.waitForIdle(WAIT_MS)) { fail("clear_timeout"); return; }
        clearUs = esp_timer_get_time() - started;
    }
    if (changed) {
        if (!checkLimits()) return;
        const int64_t prepared = esp_timer_get_time();
        const EPD_Painter::Rect dirty = expandFrame();
        prepareUs = esp_timer_get_time() - prepared;
        const uint32_t before = panel.paintsCompleted();
        const int64_t painted = esp_timer_get_time();
        if (!panel.paintPackedRegion(canvas, dirty)) { fail("paint_region"); return; }
        if (!panel.waitForIdle(WAIT_MS)) { fail("paint_timeout"); return; }
        paintUs = esp_timer_get_time() - painted;
        cycles = panel.paintsCompleted() - before;
        if (cycles != 1) { fail("drive_count"); return; }
#ifdef PAINTER_USB_DIAGNOSTICS
        if (!panel.readPaintTiming(timing) || timing.cycle != before + 1) { fail("timing"); return; }
#endif
    }
    if (firstFrame || changed) ++applied;
    uint8_t *old = previous; previous = incoming; incoming = old;
    ++frames; ++expectedSequence;
    const int64_t ended = esp_timer_get_time();
    const int64_t ackWait = esp_timer_get_time();
    while (!Serial) {
        if (esp_timer_get_time() - ackWait >= 100000) { fail("ack_connection"); return; }
        delay(1);
    }
#ifdef PAINTER_USB_DIAGNOSTICS
    if (changed) Serial.printf("PAINTER_USB_TIMING session=%lu seq=%lu cycle=%lu passes=%lu discovery_us=%lld rowloop_us=%lld settle_us=%lld neutral_us=%lld total_us=%lld\r\n",
            (unsigned long)session, (unsigned long)sequence, (unsigned long)timing.cycle,
            (unsigned long)timing.passes, (long long)timing.discovery_us, (long long)timing.rowloop_us,
            (long long)timing.settle_us, (long long)timing.neutral_us, (long long)timing.total_us);
#endif
    Serial.printf("PAINTER_USB_ACK session=%lu seq=%lu crc=%08lx result=%s rx_us=%lld decode_us=%lld display_us=%lld clear_us=%lld prepare_us=%lld paint_us=%lld cycles=%lu elapsed_us=%lld\r\n",
                  (unsigned long)session, (unsigned long)sequence, (unsigned long)packetCrc,
                  firstFrame || changed ? "applied" : "unchanged", (long long)rxUs, (long long)decodeUs,
                  (long long)(ended - displayStart), (long long)clearUs,
                  (long long)prepareUs, (long long)paintUs, (unsigned long)cycles,
                  (long long)(ended - screenStart));
    Serial.flush();
    if (!Serial) { fail("ack_connection"); return; }
    if (frames >= frameLimit) finish("limit");
    else if (ended - screenStart >= SCREEN_LIMIT_US) finish("time_limit");
    else receiveWindow = esp_timer_get_time();
}

static bool decodePatch()
{
    memcpy(incoming, previous, SOURCE_BYTES);
    size_t at = 0;
    int lastRow = -1;
    while (at < payloadLength) {
        if (payloadLength - at < 3) return false;
        const size_t offset = (size_t)encoded[at] | ((size_t)encoded[at + 1] << 8);
        const size_t count = encoded[at + 2];
        at += 3;
        const int row = offset / 80;
        if (count < 1 || count > 80 || offset + count > SOURCE_BYTES ||
            offset % 80 + count > 80 || row <= lastRow || count > payloadLength - at) return false;
        memcpy(incoming + offset, encoded + at, count);
        at += count;
        lastRow = row;
    }
    return true;
}

static bool decodeRle()
{
    size_t at = 0, output = 0;
    while (at < payloadLength) {
        const uint8_t control = encoded[at++];
        const size_t count = (control & 127U) + 1;
        if (count > SOURCE_BYTES - output) return false;
        const size_t needed = (control & 128U) ? 1 : count;
        if (needed > payloadLength - at) return false;
        if (control & 128U) memset(incoming + output, encoded[at], count);
        else memcpy(incoming + output, encoded + at, count);
        at += needed;
        output += count;
    }
    return output == SOURCE_BYTES;
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
    const int64_t decodeStart = esp_timer_get_time();
    if (type == 4 && !decodePatch()) { fail("patch"); return; }
    if (type == 5 && !decodeRle()) { fail("rle"); return; }
    if (type == 6 && decode_painter_lz(encoded, payloadLength, incoming, SOURCE_BYTES) != 0) { fail("lz"); return; }
    const int64_t decodeUs = (type == 4 || type == 5 || type == 6) ? esp_timer_get_time() - decodeStart : 0;
    resetPacket();
    if (type == 1) beginSession(id);
    else if (type == 3) finish("stop");
    else displayFrame(sequence, packetCrc, rxUs, decodeUs);
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
            uint8_t *destination = state == WAIT_START ? &startPayload : ((header[5] == 4 || header[5] == 5 || header[5] == 6) ? encoded : incoming);
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
