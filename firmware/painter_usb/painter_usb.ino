// EPK1 v4 / 2bpp 제한 수신. CRC 확인 전에는 패널을 초기화하지 않는다.
// 수신 상태 머신은 firmware/usb_frame의 검증된 EPK1 구조를 사용한다.
#include <Arduino.h>
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
static constexpr size_t SOURCE_BYTES = 16000, CANVAS_BYTES = 129600;
static constexpr int64_t RX_LIMIT_US = 5000000, SCREEN_LIMIT_US = 30000000;
static constexpr uint32_t WAIT_MS = 5000;
static EPD_Painter panel(EPD_LILYGO_EPD47_H716_PRESET);

enum SessionState { WAIT_START, WAIT_FRAME, TERMINAL };
static SessionState state = WAIT_START;
static uint32_t session = 0, expectedSequence = 1;
static uint32_t frameLimit = 0, frames = 0, applied = 0;
static uint8_t *previous = nullptr, *incoming = nullptr, *canvas = nullptr;
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
    if (panelInitialized && !panel.powerOffWhenIdle(WAIT_MS)) {
        Serial.println("PAINTER_USB_ERROR reason=poweroff_timeout");
        return; // 출력 중일 수 있으므로 버퍼를 해제하거나 DONE을 내보내지 않는다.
    }
    if (previous) heap_caps_free(previous);
    if (incoming) heap_caps_free(incoming);
    if (canvas) heap_caps_free(canvas);
    previous = incoming = canvas = nullptr;
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
    Serial.println("PAINTER_USB_READY version=4 panel=untouched driver=7544e2d board=H716 quality=NORMAL gray=4 direct=1 profile=0 packed=1");
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
    if (memcmp(header, "EPK1", 4) != 0 || header[4] != 4 || header[6] || header[7]) {
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
    if (startPayload < 1 || startPayload > 12) { fail("frame_limit"); return; }
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
    if (!previous || !incoming || !canvas) { fail("allocation_failed"); return; }
    memset(previous, 0, SOURCE_BYTES);
    if (Serial.available() != 0) { fail("early_data"); return; }
    Serial.printf("PAINTER_USB_RX session=%lu\r\n", (unsigned long)session);
    receiveWindow = esp_timer_get_time();
}

static void expandFrame()
{
    memset(canvas, 0, CANVAS_BYTES);
    for (int y = 0; y < 200; ++y) {
        uint8_t *row = canvas + (70 + y * 2) * 240 + 40;
        for (int x = 0; x < 80; ++x) {
            const uint8_t b = incoming[y * 80 + x];
            row[x * 2] = ((b >> 6) * 0x50) | (((b >> 4) & 3) * 0x05);
            row[x * 2 + 1] = (((b >> 2) & 3) * 0x50) | ((b & 3) * 0x05);
        }
        memcpy(row + 240, row, 160);
    }
}

static void displayFrame(uint32_t sequence, uint32_t packetCrc, int64_t rxUs)
{
    if (!checkLimits()) return;
    const int64_t displayStart = esp_timer_get_time();
    const bool firstFrame = !panelInitialized;
    const bool changed = memcmp(previous, incoming, SOURCE_BYTES) != 0;
    int64_t clearUs = 0, prepareUs = 0, paintUs = 0;
    uint32_t cycles = 0;
    if (firstFrame) {
        screenStart = esp_timer_get_time();
        panel.setAutoShutdown(false);
        panel.setIdleTimeout(60);
        panel.setQuality(EPD_Painter::Quality::QUALITY_NORMAL);
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
        expandFrame();
        prepareUs = esp_timer_get_time() - prepared;
        const uint32_t before = panel.paintsCompleted();
        const int64_t painted = esp_timer_get_time();
        panel.paintPacked(canvas, 1);
        if (!panel.waitForIdle(WAIT_MS)) { fail("paint_timeout"); return; }
        paintUs = esp_timer_get_time() - painted;
        cycles = panel.paintsCompleted() - before;
        if (cycles != 1) { fail("drive_count"); return; }
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
    Serial.printf("PAINTER_USB_ACK session=%lu seq=%lu crc=%08lx result=%s rx_us=%lld display_us=%lld clear_us=%lld prepare_us=%lld paint_us=%lld cycles=%lu elapsed_us=%lld\r\n",
                  (unsigned long)session, (unsigned long)sequence, (unsigned long)packetCrc,
                  firstFrame || changed ? "applied" : "unchanged", (long long)rxUs,
                  (long long)(ended - displayStart), (long long)clearUs,
                  (long long)prepareUs, (long long)paintUs, (unsigned long)cycles,
                  (long long)(ended - screenStart));
    Serial.flush();
    if (!Serial) { fail("ack_connection"); return; }
    if (frames >= frameLimit) finish("limit");
    else if (ended - screenStart >= SCREEN_LIMIT_US) finish("time_limit");
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
