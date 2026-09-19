#!/usr/bin/env python3
"""실제 USB 수신 스케치를 포함해 v2 파서·회색 확대·전체 정리·종료를 장치 없이 검증한다."""
from pathlib import Path
import hashlib
import json
import shutil
import struct
import subprocess
import sys
import zlib

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / ".local/build/usb_frame-host-qa"
LOG = ROOT / "artifacts/logs/usb-frame-firmware-host-qa.log"
SESSION = 0x10203040

ARDUINO = r"""
#pragma once
#include <cstdint>
#include <cstddef>
#include <cstring>
#include <string>
#include <vector>
#define CONFIG_IDF_TARGET_ESP32S3 1
#define BOARD_HAS_PSRAM 1
#define ARDUINO_USB_MODE 1
#define ARDUINO_USB_CDC_ON_BOOT 1
struct MockSerial {
    std::vector<uint8_t> input;
    std::string output;
    std::string queuedAck;
    size_t position = 0;
    size_t setRxBufferSize(size_t);
    void begin(unsigned long);
    void setTxTimeoutMs(uint32_t);
    operator bool() const;
    void flush();
    int available();
    size_t read(uint8_t *, size_t);
    void println(const char *);
    void printf(const char *, ...);
};
extern MockSerial Serial;
struct MockEsp {
    uint32_t getFlashChipSize() { return 16777216; }
    uint32_t getPsramSize() { return 8388608; }
    uint32_t getFreePsram() { return 8000000; }
    uint32_t getFreeHeap() { return 200000; }
};
extern MockEsp ESP;
uint32_t millis();
void delay(uint32_t);
uint32_t getCpuFrequencyMhz();
int xPortGetCoreID();
"""

EPD = r"""
#pragma once
#include "Arduino.h"
#define EPD_WIDTH 960
#define EPD_HEIGHT 540
struct Rect_t { int32_t x, y, width, height; };
enum DrawMode_t { BLACK_ON_WHITE = 1, WHITE_ON_WHITE = 2, WHITE_ON_BLACK = 4 };
void epd_init();
void epd_poweron();
void epd_poweroff();
void epd_clear();
void epd_draw_grayscale_image(Rect_t, uint8_t *);
"""

HARNESS = r"""
#include "Arduino.h"
#include "epd_driver.h"
#include <algorithm>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <iterator>
#include <stdexcept>
#include <array>
#include <limits>
static void require(bool value, const char *reason) {
    if (!value) throw std::runtime_error(reason);
}
static std::string scenario;
static int64_t nowUs = 0, panelStart = -1;
static bool begun = false, powered = false, initialized = false;
static int initCalls = 0, clearCalls = 0, onCalls = 0, offCalls = 0, drawCalls = 0;
static int busyCalls = 0, mallocCalls = 0, freeCalls = 0;
static int ackCount = 0, readyCount = 0, readCalls = 0, connectionChecks = 0, flushCalls = 0;
static int64_t lastOffUs = -1, lastFlushUs = -1;
static size_t shortRead = 11;
static bool queuePrepared = false, corruptRead = false;
static bool scanExpiryArmed = false;
static int scanClockReads = 0;
struct Allocation { uint8_t *raw = nullptr, *pointer = nullptr; size_t size = 0; bool freed = false; };
static std::array<Allocation, 3> allocations;
static std::vector<uint8_t> expectedOld(32000, 255), expectedNew(32000, 255);
static std::vector<uint8_t> panel(960 * 540, 15);
MockSerial Serial;
MockEsp ESP;

#include SKETCH_PATH

static void guards() {
    for (const auto &item : allocations) {
        if (!item.pointer || item.freed) continue;
        for (int i = 1; i <= 32; ++i) require(item.pointer[-i] == 0xA5, "left allocation guard");
        for (int i = 0; i < 32; ++i) require(item.pointer[item.size + i] == 0xA5, "right allocation guard");
    }
}
static uint8_t pixel(const std::vector<uint8_t> &image, int x, int y) {
    return (image[y * 160 + x / 2] >> ((x % 2) * 4)) & 15;
}
static void checkPanel() {
    for (int y = 0; y < 540; ++y) for (int x = 0; x < 960; ++x) {
        uint8_t wanted = 15;
        if (x >= 160 && x < 800 && y >= 70 && y < 470)
            wanted = pixel(expectedNew, (x - 160) / 2, (y - 70) / 2);
        require(panel[y * 960 + x] == wanted, "final panel pixel");
    }
}
static void logLine(const char *buffer) {
    require(!powered, "serial output within screen processing");
    require(std::strlen(buffer) <= 512, "response exceeds protocol line limit");
    Serial.output += buffer;
    if (std::strstr(buffer, "USB_FRAME_ACK ") == buffer) {
        require(previous && std::memcmp(previous, expectedNew.data(), 32000) == 0,
                "ACK before previous frame committed");
        require(!powered && onCalls == offCalls, "ACK before poweroff");
        bool changed = ackCount == 0 || expectedOld != expectedNew;
        require(std::strstr(buffer, changed ? "result=applied" : "result=unchanged"), "ACK result");
        require(clearCalls == onCalls && drawCalls == clearCalls, "each changed frame must clear and draw once");
        const char *clearField = std::strstr(buffer, " clear_us=");
        const char *drawField = std::strstr(buffer, " draw_us=");
        require(clearField && drawField && !std::strstr(buffer, "white_us=") && !std::strstr(buffer, "black_us="),
                "v2 ACK timing fields");
        long long clearUs = -1, drawUs = -1;
        require(std::sscanf(clearField, " clear_us=%lld", &clearUs) == 1 &&
                std::sscanf(drawField, " draw_us=%lld", &drawUs) == 1, "ACK timing values");
        require(clearUs == (changed ? (scenario == "slow_frame" && ackCount == 1 ? 31000000 : 100) : 0) &&
                drawUs == (changed ? 1000 : 0), "ACK clear/draw measurement boundary");
        checkPanel(); guards();
        expectedOld = expectedNew;
        ++ackCount;
    }
    if (std::strstr(buffer, "USB_FRAME_READY ") == buffer) ++readyCount;
    nowUs += 30;
}
size_t MockSerial::setRxBufferSize(size_t size) {
    require(!begun && size == 32768 && !queuePrepared, "RX queue initialization order");
    queuePrepared = true;
    return scenario == "rx_buffer" ? 0 : size;
}
void MockSerial::begin(unsigned long baud) { require(queuePrepared && baud == 115200, "begin order"); begun = true; }
void MockSerial::setTxTimeoutMs(uint32_t value) { require(value == 100, "TX timeout"); }
MockSerial::operator bool() const {
    require(!powered && lastOffUs >= 0, "ACK connection check before panel off");
    ++connectionChecks;
    if (scenario == "ack_connection" || (scenario == "ack_flush_loss" && flushCalls)) return false;
    if (scenario == "ack_recovery") return nowUs - lastOffUs >= 4000;
    return true;
}
void MockSerial::flush() {
    require(!powered && (bool)*this, "flush while disconnected or panel powered");
    require(!queuedAck.empty(), "flush without exactly one queued ACK");
    ++flushCalls;
    // ACK는 printf 반환만으로 전달되지 않는다. flush에서만 수신 측으로 넘긴다.
    nowUs += 2000;
    if (scenario == "ack_flush_loss") {
        // 코어 flush가 연결 상실 시 큐를 버리는 실패를 모의한다. 실제 원인 재현은 아니다.
        queuedAck.clear();
        lastFlushUs = nowUs;
        return;
    }
    logLine(queuedAck.c_str());
    queuedAck.clear();
    lastFlushUs = nowUs;
}
int MockSerial::available() { return scenario == "rx_queue" ? -1 : (int)(input.size() - position); }
size_t MockSerial::read(uint8_t *destination, size_t size) {
    ++readCalls;
    if (corruptRead) return std::numeric_limits<size_t>::max();
    size = std::min(size, std::min(shortRead, input.size() - position));
    std::memcpy(destination, input.data() + position, size);
    position += size;
    return size;
}
void MockSerial::println(const char *line) { std::string value(line); value += '\n'; logLine(value.c_str()); }
void MockSerial::printf(const char *format, ...) {
    char line[1024]; va_list args; va_start(args, format);
    int size = vsnprintf(line, sizeof(line), format, args); va_end(args);
    require(size >= 0 && (size_t)size < sizeof(line), "response truncated");
    if (std::strstr(line, "USB_FRAME_ACK ") == line) {
        require(!powered && (bool)*this && queuedAck.empty(), "ACK queued before connection/off or duplicated");
        queuedAck = line;
    } else logLine(line);
}
uint32_t millis() { return (uint32_t)(nowUs / 1000); }
int64_t esp_timer_get_time() {
    // 두 번째 유효 프레임의 검사·스캔 사이에 예산이 끝나는 상황을 모의한다.
    if (scanExpiryArmed && frames == 1 && headerUsed == 0 && scanClockReads-- == 0) {
        nowUs = panelStart + 30000000;
        scanExpiryArmed = false;
    }
    return nowUs;
}
void delay(uint32_t ms) { require(!powered, "delay while powered"); nowUs += (int64_t)ms * 1000; }
uint32_t getCpuFrequencyMhz() { return scenario == "frequency" ? 160 : 240; }
int xPortGetCoreID() { return scenario == "core_change" && busyCalls >= 3 ? 0 : 1; }
int32_t mock_ccount() {
    uint32_t count = (uint32_t)((uint64_t)nowUs * 240);
    if (scenario == "missing_sign") count &= 0x7FFFFFFF;
    return (int32_t)count;
}
extern "C" void busy_delay(uint32_t cycles) {
    require(!initialized && !powered && cycles == 120000, "unsafe delay test");
    ++busyCalls;
    nowUs += scenario == "short_delay" ? 1 : scenario == "delay_timeout" ? 26000000 : 500;
}
void *heap_caps_malloc(size_t size, unsigned caps) {
    ++mallocCalls;
    require(mallocCalls <= 3 && caps == 3, "PSRAM allocation contract");
    require(size == (mallocCalls == 3 ? 192000U : 32000U), "allocation size");
    if (scenario == "allocation" + std::to_string(mallocCalls)) return nullptr;
    Allocation &item = allocations[mallocCalls - 1];
    item.raw = (uint8_t *)std::malloc(size + 68); require(item.raw != nullptr, "host malloc");
    std::memset(item.raw, 0xA5, size + 68);
    item.pointer = item.raw + 32 + (scenario == "alignment" && mallocCalls == 3 ? 1 : 0);
    item.size = size; return item.pointer;
}
void heap_caps_free(void *pointer) {
    guards();
    for (auto &item : allocations) if (item.pointer == pointer && !item.freed) {
        item.freed = true; std::free(item.raw); item.raw = nullptr; ++freeCalls; return;
    }
    require(false, "invalid or repeated free");
}
void epd_init() {
    require(!initialized && state == WAIT_FRAME && frames == 0, "panel init state");
    require(Serial.output.find("USB_FRAME_RX session=") != std::string::npos, "panel before RX");
    require(Serial.output.find("USB_FRAME_DELAY_DONE status=pass") != std::string::npos, "panel before delay pass");
    initialized = true; ++initCalls;
}
void epd_poweron() {
    require(initialized && !powered, "poweron state");
    if (panelStart < 0) panelStart = nowUs;
    else require(nowUs - panelStart < 30000000, "new update after screen deadline");
    powered = true; ++onCalls; nowUs += 10;
}
void epd_poweroff() {
    require(powered && clearCalls == onCalls && drawCalls == clearCalls, "poweroff before complete clear/draw pair");
    powered = false; ++offCalls; nowUs += 20; lastOffUs = nowUs;
}
void epd_clear() {
    require(powered && clearCalls == drawCalls && onCalls == clearCalls + 1, "full clear count/order");
    ++clearCalls; std::fill(panel.begin(), panel.end(), 15);
    // 시작한 전체 지우기가 시간 예산을 넘겨도 그림·전원 끄기는 완료해야 한다.
    nowUs += scenario == "slow_frame" && ackCount == 1 ? 31000000 : 100;
}
void epd_draw_grayscale_image(Rect_t area, uint8_t *data) {
    require(powered && clearCalls == drawCalls + 1 && data == allocations[2].pointer, "draw buffer/power/clear");
    require(area.x == 0 && area.y == 70 && area.width == 960 && area.height == 400, "full-width grayscale rectangle");
    guards();
    for (int y = 0; y < 400; ++y) for (int x = 0; x < 960; ++x) {
        uint8_t wanted = 15;
        if (x >= 160 && x < 800) wanted = pixel(expectedNew, (x - 160) / 2, y / 2);
        uint8_t value = (data[y * 480 + x / 2] >> ((x % 2) * 4)) & 15;
        require(value == wanted, "grayscale pixel/nibble/stride/expansion/padding");
        require(panel[(70 + y) * 960 + x] == 15, "grayscale target was not cleared");
        panel[(70 + y) * 960 + x] = value;
    }
    ++drawCalls;
    nowUs += 1000;
}
static std::vector<uint8_t> fixture(const std::string &name) {
    std::ifstream file(std::string(FIXTURE_PATH) + "/" + name + ".bin", std::ios::binary);
    require((bool)file, "missing fixture");
    return std::vector<uint8_t>(std::istreambuf_iterator<char>(file), {});
}
static void feed(const std::vector<uint8_t> &bytes, size_t chunk = 97) {
    for (size_t start = 0; start < bytes.size(); start += chunk) {
        size_t count = std::min(chunk, bytes.size() - start);
        Serial.input.insert(Serial.input.end(), bytes.begin() + start, bytes.begin() + start + count);
        loop();
    }
}
static void sendFrame(const std::string &name) {
    auto packet = fixture(name);
    expectedNew.assign(packet.begin() + 20, packet.end() - 4);
    feed(packet);
    if (state == WAIT_FRAME) require(receiveWindow >= lastFlushUs && lastFlushUs >= 0, "RX deadline starts before ACK flush");
}
static int occurrences(const std::string &needle) {
    int count = 0;
    for (size_t at = 0; (at = Serial.output.find(needle, at)) != std::string::npos; at += needle.size()) ++count;
    return count;
}
static void noPanel() { require(initCalls == 0 && onCalls == 0 && offCalls == 0 && clearCalls == 0 && drawCalls == 0, "failure touched panel"); }
static void error(const char *reason) {
    require(state == TERMINAL, "not terminal after error");
    require(Serial.output.find(std::string("USB_FRAME_ERROR reason=") + reason) != std::string::npos, "wrong error");
    require(occurrences("USB_FRAME_DONE ") == 1 && Serial.output.find("reason=error frames=0") != std::string::npos, "error completion");
    noPanel();
}
static void checkTerminal() {
    require(state == TERMINAL && !powered, "session not terminal/off");
    require(Serial.queuedAck.empty() && flushCalls == ackCount + (scenario == "ack_flush_loss" ? 1 : 0),
            "ACK was not flushed exactly once");
    int oldReads = readCalls, oldBusy = busyCalls, oldDraws = drawCalls, oldInit = initCalls;
    size_t oldOutput = Serial.output.size();
    for (int i = 0; i < 3; ++i) feed(fixture("start1"));
    require(readCalls == oldReads && busyCalls == oldBusy && drawCalls == oldDraws && initCalls == oldInit &&
            Serial.output.size() == oldOutput, "terminal restarted/parses/prints");
    for (auto &item : allocations) require(!item.pointer || item.freed, "allocation not released");
}
int main(int argc, char **argv) {
    try {
        require(argc == 2, "scenario needed"); scenario = argv[1];
        require((updateCrc(0xFFFFFFFFU, (const uint8_t *)"123456789", 9) ^ 0xFFFFFFFFU) == 0xCBF43926U, "IEEE CRC vector");
        setup();
        if (scenario == "rx_buffer") error("rx_buffer");
        else if (scenario == "rx_queue") { loop(); error("rx_queue"); }
        else {
            for (int i = 0; i < 3; ++i) { delay(1000); loop(); }
            noPanel(); require(mallocCalls == 0 && busyCalls == 0 && readyCount >= 2, "boot not idle");
            require(Serial.output.find("USB_FRAME_READY version=2 ") != std::string::npos, "READY protocol version");
            if (scenario == "rx_read") { corruptRead = true; feed({69}); error("rx_read"); }
            else if (scenario == "start_timeout") { feed({69}); nowUs = packetStart + 5000000; loop(); error("rx_timeout"); }
            else if (scenario.rfind("start_", 0) == 0) {
                auto bytes = fixture("start1");
                if (scenario == "start_magic") bytes[0] ^= 1;
                if (scenario == "start_version") bytes[4] = 1;
                if (scenario == "start_reserved") bytes[6] = 1;
                if (scenario == "start_type") bytes[5] = 2;
                if (scenario == "start_sequence") bytes[12] = 1;
                if (scenario == "start_session") std::fill(bytes.begin() + 8, bytes.begin() + 12, 0);
                if (scenario == "start_length") std::fill(bytes.begin() + 16, bytes.begin() + 20, 255);
                if (scenario == "start_crc") bytes.back() ^= 1;
                feed(bytes);
                error(scenario == "start_crc" ? "crc" :
                      (scenario == "start_magic" || scenario == "start_version" || scenario == "start_reserved") ? "header" : "start_header");
            } else if (scenario == "early_data") {
                auto bytes = fixture("start1"), frame = fixture("white1"); bytes.insert(bytes.end(), frame.begin(), frame.end());
                feed(bytes, bytes.size()); error("early_data");
            } else {
                std::string start = scenario == "golden" ? "start1" : scenario == "limit0" ? "start0" : scenario == "limit13" ? "start13" : "start12";
                feed(fixture(start), 1);
                noPanel();
                if (scenario == "limit0" || scenario == "limit13") error("frame_limit");
                else if (scenario.rfind("allocation", 0) == 0) error("allocation_failed");
                else if (scenario == "alignment") error("buffer_alignment");
                else if (scenario == "frequency") error("cpu_frequency");
                else if (scenario == "short_delay" || scenario == "missing_sign" || scenario == "delay_timeout" || scenario == "core_change") error("delay_test_failed");
                else {
                    require(state == WAIT_FRAME && busyCalls > 1 && occurrences("USB_FRAME_RX ") == 1, "START did not finish checks");
                    if (scenario == "golden") { sendFrame("white1"); require(frames == 1 && applied == 1 && clearCalls == 1 && drawCalls == 1, "first same white frame still clears and draws"); }
                    else if (scenario == "ack_recovery") {
                        sendFrame("render1");
                        require(state == WAIT_FRAME && frames == 1 && applied == 1 && ackCount == 1 && flushCalls == 1,
                                "recovered connection must send one ACK");
                        require(connectionChecks >= 5 && lastFlushUs - lastOffUs >= 6000 && lastFlushUs - lastOffUs < 100000,
                                "temporary disconnect did not wait then flush");
                        feed(fixture("stop2"));
                    } else if (scenario == "ack_flush_loss") {
                        sendFrame("render1");
                        require(state == TERMINAL && frames == 1 && applied == 1 && ackCount == 0 && flushCalls == 1 &&
                                onCalls == 1 && offCalls == 1 && clearCalls == 1 && drawCalls == 1 && Serial.queuedAck.empty(),
                                "flush connection loss was not stopped after completed display");
                        require(occurrences("USB_FRAME_ERROR reason=ack_connection") == 1 && occurrences("USB_FRAME_DONE ") == 1 &&
                                Serial.output.find("reason=error frames=1 applied=1") != std::string::npos,
                                "flush failure completion must preserve displayed frame count");
                        checkPanel(); guards();
                    } else if (scenario == "ack_connection") {
                        sendFrame("render1");
                        require(state == TERMINAL && frames == 1 && applied == 1 && ackCount == 0 && flushCalls == 0 &&
                                onCalls == 1 && offCalls == 1 && clearCalls == 1 && drawCalls == 1 && Serial.queuedAck.empty(),
                                "failed ACK connection changed completed display or queued ACK");
                        require(nowUs - lastOffUs >= 100000 && nowUs - lastOffUs < 101000 && connectionChecks >= 100,
                                "ACK connection wait is not bounded at 100ms");
                        require(occurrences("USB_FRAME_ERROR reason=ack_connection") == 1 && occurrences("USB_FRAME_DONE ") == 1 &&
                                Serial.output.find("reason=error frames=1 applied=1") != std::string::npos,
                                "ACK failure completion must preserve displayed frame count");
                        checkPanel(); guards();
                    }
                    else if (scenario == "limit") {
                        for (int i = 1; i <= 12; ++i) sendFrame("white" + std::to_string(i));
                        require(frames == 12 && applied == 1 && initCalls == 1 && onCalls == 1 && offCalls == 1 && clearCalls == 1 && drawCalls == 1, "unchanged or frame limit");
                    } else if (scenario == "render") {
                        for (int i = 1; i <= 7; ++i) sendFrame("render" + std::to_string(i));
                        require(frames == 7 && applied == 5 && onCalls == 5 && offCalls == 5 && clearCalls == 5 && drawCalls == 5,
                                "gray transitions/unchanged counts");
                        feed(fixture("stop8")); require(Serial.output.find("reason=stop frames=7 applied=5") != std::string::npos, "STOP after frames");
                    } else if (scenario == "stop") { feed(fixture("stop1"), 1); noPanel(); require(state == TERMINAL && frames == 0, "STOP before frame"); }
                    else if (scenario == "cropped") {
                        sendFrame("white1"); sendFrame("crop2"); sendFrame("crop3");
                        require(frames == 3 && applied == 3 && clearCalls == 3 && drawCalls == 3, "nibble and edge changes still redraw full image");
                        feed(fixture("stop4"));
                    } else if (scenario == "scan_timeout") {
                        sendFrame("render1");
                        nowUs = panelStart + 29999000;
                        auto bytes = fixture("render2_changed");
                        expectedNew.assign(bytes.begin() + 20, bytes.end() - 4);
                        scanExpiryArmed = true; scanClockReads = 2;
                        feed(bytes, bytes.size());
                        require(frames == 1 && applied == 1 && onCalls == 1 && drawCalls == 1 && state == TERMINAL,
                                "new panel access after scan crossed deadline");
                    } else if (scenario == "late_crc") {
                        sendFrame("render1");
                        auto bytes = fixture("render2_changed"); bytes.back() ^= 1; feed(bytes);
                        require(frames == 1 && applied == 1 && ackCount == 1 && onCalls == 1 && drawCalls == 1 && state == TERMINAL,
                                "bad later CRC changed panel/committed frame");
                        require(Serial.output.find("USB_FRAME_ERROR reason=crc") != std::string::npos, "late CRC reason");
                    }
                    else if (scenario == "slow_frame") {
                        sendFrame("render1"); sendFrame("render2_changed");
                        require(frames == 2 && applied == 2 && clearCalls == 2 && drawCalls == 2 && onCalls == 2 && offCalls == 2 && nowUs - panelStart > 30000000,
                                "in-progress frame cutoff");
                        require(Serial.output.find("reason=time_limit frames=2") != std::string::npos, "slow frame limit");
                    } else if (scenario == "screen_timeout") {
                        sendFrame("render1"); nowUs = panelStart + 30000000; loop();
                        require(frames == 1 && drawCalls == 1 && state == TERMINAL && Serial.output.find("reason=time_limit frames=1") != std::string::npos,
                                "screen timeout while receiving");
                    } else if (scenario == "frame_timeout") { nowUs = receiveWindow + 5000000; loop(); error("rx_timeout"); }
                    else if (scenario == "partial_timeout") {
                        auto bytes = fixture("white1"); feed(std::vector<uint8_t>(bytes.begin(), bytes.end() - 1));
                        noPanel(); nowUs = receiveWindow + 5000000; feed({bytes.back()}); error("rx_timeout");
                    } else {
                        auto bytes = fixture("white1");
                        if (scenario == "frame_crc") bytes[800] ^= 1;
                        if (scenario == "frame_session") bytes[8] ^= 1;
                        if (scenario == "frame_sequence") bytes[12] = 2;
                        if (scenario == "frame_length") std::fill(bytes.begin() + 16, bytes.begin() + 20, 255);
                        if (scenario == "frame_type") bytes[5] = 1;
                        feed(bytes);
                        error(scenario == "frame_crc" ? "crc" : scenario == "frame_session" ? "session" :
                              scenario == "frame_sequence" ? "sequence" : "frame_header");
                    }
                }
            }
        }
        checkTerminal();
        for (const char *name : {"start1", "white1", "stop2"}) {
            auto bytes = fixture(name);
            uint32_t crc = updateCrc(0xFFFFFFFFU, bytes.data(), bytes.size() - 4) ^ 0xFFFFFFFFU;
            require(crc == readLe32(bytes.data() + bytes.size() - 4), "golden packet CRC");
        }
        require(occurrences("USB_FRAME_ACK ") == ackCount, "duplicate ACK");
        std::cout << "PASS " << scenario << " ack=" << ackCount << " applied=" << applied
                  << " init=" << initCalls << " clear=" << clearCalls << " gray=" << drawCalls
                  << " on=" << onCalls << " off=" << offCalls
                  << " frames=" << frames << " flush=" << flushCalls << " connection_checks=" << connectionChecks
                  << " terminal=1 guards=ok\n";
        return 0;
    } catch (const std::exception &error) {
        std::cerr << "FAIL " << scenario << ": " << error.what() << '\n' << Serial.output;
        return 1;
    }
}
"""


def packet(kind: int, sequence: int, payload: bytes) -> bytes:
    header = struct.pack("<4sBBHIII", b"EPK1", 2, kind, 0, SESSION, sequence, len(payload))
    return header + payload + struct.pack("<I", zlib.crc32(header + payload) & 0xFFFFFFFF)


def main() -> int:
    compiler = shutil.which("clang++")
    if not compiler:
        print("clang++가 없어 검증하지 못했습니다. 설치하지 않습니다.", file=sys.stderr)
        return 1
    BUILD.mkdir(parents=True, exist_ok=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    headers = {
        "Arduino.h": ARDUINO,
        "epd_driver.h": EPD,
        "esp_arduino_version.h": "#define ESP_ARDUINO_VERSION_MAJOR 2\n#define ESP_ARDUINO_VERSION_MINOR 0\n#define ESP_ARDUINO_VERSION_PATCH 17\n",
        "esp_heap_caps.h": '#pragma once\n#include "Arduino.h"\n#define MALLOC_CAP_SPIRAM 1\n#define MALLOC_CAP_8BIT 2\nvoid *heap_caps_malloc(size_t,unsigned);\nvoid heap_caps_free(void *);\n',
        "esp_timer.h": '#pragma once\n#include "Arduino.h"\nint64_t esp_timer_get_time();\n',
        "xtensa/core-macros.h": '#pragma once\n#include "Arduino.h"\nint32_t mock_ccount();\n#define XTHAL_GET_CCOUNT() mock_ccount()\n',
    }
    for name, content in headers.items():
        path = BUILD / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    for limit in (0, 1, 12, 13):
        (BUILD / f"start{limit}.bin").write_bytes(packet(1, 0, bytes([limit])))
    for seq in range(1, 13):
        (BUILD / f"white{seq}.bin").write_bytes(packet(2, seq, bytes([255]) * 32000))
    for seq in (1, 2, 4, 8):
        (BUILD / f"stop{seq}.bin").write_bytes(packet(3, seq, b""))
    first = bytes((i * 37 + 131) & 255 for i in range(32000))
    # 명령처럼 보이는 바이트도 프레임 본문으로만 처리되는지 함께 확인한다.
    first = first[:64] + b"EPK1RUN\nSTART\n" + first[78:]
    assert len(first) == 32000
    second = bytes(value ^ 0xA5 for value in first)
    subset = bytes(value & 0x55 for value in second)
    for seq, data in enumerate((first, first, second, subset, bytes([255]) * 32000, bytes(32000), bytes(32000)), 1):
        (BUILD / f"render{seq}.bin").write_bytes(packet(2, seq, data))
    (BUILD / "render2_changed.bin").write_bytes(packet(2, 2, second))
    # 하위·상위 nibble과 첫/끝 행·열, 모든 16계조를 고립된 변경으로 검사한다.
    def set_gray(image: bytearray, x: int, y: int, value: int) -> None:
        index, shift = y * 160 + x // 2, (x % 2) * 4
        image[index] = (image[index] & ~(15 << shift)) | (value << shift)

    crop = bytearray([255]) * 32000
    for x, y, gray in ((0, 0, 0), (1, 0, 1), (318, 199, 13), (319, 199, 14)):
        set_gray(crop, x, y, gray)
    for gray in range(16):
        set_gray(crop, gray, 57, gray)
    (BUILD / "crop2.bin").write_bytes(packet(2, 2, bytes(crop)))
    moved = bytearray([255]) * 32000
    for x, y, gray in ((0, 0, 15), (1, 0, 0), (318, 199, 1), (319, 199, 15), (159, 58, 7), (160, 59, 8)):
        set_gray(moved, x, y, gray)
    (BUILD / "crop3.bin").write_bytes(packet(2, 3, bytes(moved)))
    sketch = ROOT / "firmware/usb_frame/usb_frame.ino"
    source = '#define SKETCH_PATH ' + json.dumps(str(sketch)) + "\n#define FIXTURE_PATH " + json.dumps(str(BUILD)) + "\n" + HARNESS
    (BUILD / "harness.cpp").write_text(source)
    compile_command = [compiler, "-std=c++11", "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
                       "-fno-omit-frame-pointer", "-I", str(BUILD), str(BUILD / "harness.cpp"), "-o", str(BUILD / "harness")]
    scenarios = ["golden", "ack_recovery", "ack_connection", "ack_flush_loss", "render", "cropped", "limit", "stop", "slow_frame", "screen_timeout", "scan_timeout", "late_crc", "frame_timeout", "partial_timeout",
                 "rx_buffer", "rx_queue", "rx_read", "start_timeout", "start_magic", "start_version", "start_reserved",
                 "start_type", "start_sequence", "start_session", "start_length", "start_crc", "early_data", "limit0", "limit13",
                 "allocation1", "allocation2", "allocation3", "alignment", "frequency", "short_delay", "missing_sign",
                 "delay_timeout", "core_change", "frame_crc", "frame_session", "frame_sequence", "frame_length", "frame_type"]
    with LOG.open("w") as log:
        def record(text: str) -> None:
            print(text, end="")
            log.write(text)
            log.flush()

        record(f"실제 USB v2 4bpp 수신 스케치 호스트 검증\nsketch_sha256={hashlib.sha256(sketch.read_bytes()).hexdigest()}\n")
        for name in ("start1", "white1", "stop2"):
            value = (BUILD / f"{name}.bin").read_bytes()
            record(f"GOLDEN {name} bytes={len(value)} header={value[:20].hex()} crc={int.from_bytes(value[-4:], 'little'):08x} trailer={value[-4:].hex()}\n")
        result = subprocess.run(compile_command, capture_output=True, text=True)
        record(result.stdout + result.stderr)
        if result.returncode:
            return result.returncode
        record("PASS compile C++11 -Wall -Wextra -Werror ASan UBSan\n")
        for scenario in scenarios:
            result = subprocess.run([str(BUILD / "harness"), scenario], capture_output=True, text=True, timeout=30)
            record(result.stdout + result.stderr)
            if result.returncode:
                return result.returncode
        record(f"PASS all scenarios={len(scenarios)} golden_crc=production_matches_zlib device_access=none\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
