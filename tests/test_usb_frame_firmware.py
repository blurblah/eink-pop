#!/usr/bin/env python3
"""실제 USB 수신 스케치를 포함해 파서·확대·차분·종료를 장치 없이 검증한다."""
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
    size_t position = 0;
    size_t setRxBufferSize(size_t);
    void begin(unsigned long);
    void setTxTimeoutMs(uint32_t);
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
void epd_draw_image(Rect_t, uint8_t *, DrawMode_t);
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
static int whiteCalls = 0, blackCalls = 0, busyCalls = 0, mallocCalls = 0, freeCalls = 0;
static int ackCount = 0, readyCount = 0, readCalls = 0, whiteInFrame = 0;
static size_t shortRead = 11;
static bool queuePrepared = false, corruptRead = false;
static bool scanExpiryArmed = false;
static int scanClockReads = 0;
struct Allocation { uint8_t *raw = nullptr, *pointer = nullptr; size_t size = 0; bool freed = false; };
static std::array<Allocation, 3> allocations;
static std::vector<uint8_t> expectedOld(8000, 0), expectedNew(8000, 0);
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
static bool pixel(const std::vector<uint8_t> &image, int x, int y) {
    return (image[y * 40 + x / 8] & (0x80U >> (x % 8))) != 0;
}
static void checkPanel() {
    for (int y = 0; y < 540; ++y) for (int x = 0; x < 960; ++x) {
        uint8_t wanted = 15;
        if (x >= 160 && x < 800 && y >= 70 && y < 470)
            wanted = pixel(expectedNew, (x - 160) / 2, (y - 70) / 2) ? 0 : 15;
        require(panel[y * 960 + x] == wanted, "final panel pixel");
    }
}
static void logLine(const char *buffer) {
    require(!powered, "serial output within screen processing");
    require(std::strlen(buffer) <= 512, "response exceeds protocol line limit");
    Serial.output += buffer;
    if (std::strstr(buffer, "USB_FRAME_ACK ") == buffer) {
        require(previous && std::memcmp(previous, expectedNew.data(), 8000) == 0,
                "ACK before previous frame committed");
        require(!powered && onCalls == offCalls, "ACK before poweroff");
        checkPanel(); guards();
        expectedOld = expectedNew;
        ++ackCount; whiteInFrame = 0;
    }
    if (std::strstr(buffer, "USB_FRAME_READY ") == buffer) ++readyCount;
    nowUs += 30;
}
size_t MockSerial::setRxBufferSize(size_t size) {
    require(!begun && size == 8192 && !queuePrepared, "RX queue initialization order");
    queuePrepared = true;
    return scenario == "rx_buffer" ? 0 : size;
}
void MockSerial::begin(unsigned long baud) { require(queuePrepared && baud == 115200, "begin order"); begun = true; }
void MockSerial::setTxTimeoutMs(uint32_t value) { require(value == 100, "TX timeout"); }
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
    require(size >= 0 && (size_t)size < sizeof(line), "response truncated"); logLine(line);
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
    require(size == (mallocCalls == 3 ? 192000U : 8000U), "allocation size");
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
void epd_poweroff() { require(powered, "poweroff state"); powered = false; ++offCalls; nowUs += 20; }
void epd_clear() { require(powered && clearCalls == 0 && drawCalls == 0, "full clear count/order"); ++clearCalls; std::fill(panel.begin(), panel.end(), 15); nowUs += 100; }
void epd_draw_image(Rect_t area, uint8_t *data, DrawMode_t mode) {
    require(powered && clearCalls == 1 && data == allocations[2].pointer, "draw buffer/power");
    guards();
    int minimum = 200, maximum = -1; bool anyWhite = false, anyBlack = false;
    for (int y = 0; y < 200; ++y) for (int x = 0; x < 320; ++x) {
        bool before = pixel(expectedOld, x, y), after = pixel(expectedNew, x, y);
        if (before != after) { minimum = std::min(minimum, y); maximum = y; }
        anyWhite |= before && !after; anyBlack |= !before && after;
    }
    require(area.x == 0 && area.width == 960 && area.y == 70 + minimum * 2 &&
            area.height == 2 * (maximum - minimum + 1), "changed-row bounding box");
    bool white = mode == WHITE_ON_BLACK;
    require(white ? anyWhite : (mode == BLACK_ON_WHITE && anyBlack), "empty or invalid polarity");
    if (!white && anyWhite) require(whiteInFrame == 1, "black before white");
    for (int y = 0; y < area.height; ++y) for (int x = 0; x < 960; ++x) {
        bool active = false;
        if (x >= 160 && x < 800) {
            int sx = (x - 160) / 2, sy = (area.y + y - 70) / 2;
            bool before = pixel(expectedOld, sx, sy), after = pixel(expectedNew, sx, sy);
            active = white ? before && !after : !before && after;
        }
        uint8_t value = (data[y * 480 + x / 2] >> ((x % 2) * 4)) & 15;
        require(value == (white ? (active ? 15 : 0) : (active ? 0 : 15)), "mask pixel/stride/expansion");
        if (active) panel[(area.y + y) * 960 + x] = white ? 15 : 0;
    }
    ++drawCalls;
    if (white) { ++whiteCalls; ++whiteInFrame; } else ++blackCalls;
    nowUs += scenario == "slow_frame" && ackCount == 1 && white ? 31000000 : 1000;
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
            if (scenario == "rx_read") { corruptRead = true; feed({69}); error("rx_read"); }
            else if (scenario == "start_timeout") { feed({69}); nowUs = packetStart + 5000000; loop(); error("rx_timeout"); }
            else if (scenario.rfind("start_", 0) == 0) {
                auto bytes = fixture("start1");
                if (scenario == "start_magic") bytes[0] ^= 1;
                if (scenario == "start_version") bytes[4] = 2;
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
                auto bytes = fixture("start1"), frame = fixture("zero1"); bytes.insert(bytes.end(), frame.begin(), frame.end());
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
                    if (scenario == "golden") { sendFrame("zero1"); require(frames == 1 && applied == 1 && clearCalls == 1 && drawCalls == 0, "first white frame"); }
                    else if (scenario == "limit") {
                        for (int i = 1; i <= 12; ++i) sendFrame("zero" + std::to_string(i));
                        require(frames == 12 && applied == 1 && initCalls == 1 && onCalls == 1 && offCalls == 1 && drawCalls == 0, "unchanged or frame limit");
                    } else if (scenario == "render") {
                        for (int i = 1; i <= 7; ++i) sendFrame("render" + std::to_string(i));
                        require(frames == 7 && applied == 5 && onCalls == 5 && offCalls == 5 && whiteCalls == 3 && blackCalls == 3,
                                "polarity/unchanged counts");
                        feed(fixture("stop8")); require(Serial.output.find("reason=stop frames=7 applied=5") != std::string::npos, "STOP after frames");
                    } else if (scenario == "stop") { feed(fixture("stop1"), 1); noPanel(); require(state == TERMINAL && frames == 0, "STOP before frame"); }
                    else if (scenario == "cropped") {
                        sendFrame("zero1"); sendFrame("crop2"); sendFrame("crop3");
                        require(frames == 3 && applied == 3 && whiteCalls == 1 && blackCalls == 2, "cropped passes");
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
                        require(frames == 2 && applied == 2 && whiteCalls == 1 && blackCalls == 2 && nowUs - panelStart > 30000000,
                                "in-progress frame cutoff");
                        require(Serial.output.find("reason=time_limit frames=2") != std::string::npos, "slow frame limit");
                    } else if (scenario == "screen_timeout") {
                        sendFrame("render1"); nowUs = panelStart + 30000000; loop();
                        require(frames == 1 && drawCalls == 1 && state == TERMINAL && Serial.output.find("reason=time_limit frames=1") != std::string::npos,
                                "screen timeout while receiving");
                    } else if (scenario == "frame_timeout") { nowUs = receiveWindow + 5000000; loop(); error("rx_timeout"); }
                    else if (scenario == "partial_timeout") {
                        auto bytes = fixture("zero1"); feed(std::vector<uint8_t>(bytes.begin(), bytes.end() - 1));
                        noPanel(); nowUs = receiveWindow + 5000000; feed({bytes.back()}); error("rx_timeout");
                    } else {
                        auto bytes = fixture("zero1");
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
        for (const char *name : {"start1", "zero1", "stop2"}) {
            auto bytes = fixture(name);
            uint32_t crc = updateCrc(0xFFFFFFFFU, bytes.data(), bytes.size() - 4) ^ 0xFFFFFFFFU;
            require(crc == readLe32(bytes.data() + bytes.size() - 4), "golden packet CRC");
        }
        require(occurrences("USB_FRAME_ACK ") == ackCount, "duplicate ACK");
        std::cout << "PASS " << scenario << " ack=" << ackCount << " applied=" << applied
                  << " init=" << initCalls << " clear=" << clearCalls << " white=" << whiteCalls
                  << " black=" << blackCalls << " on=" << onCalls << " off=" << offCalls
                  << " frames=" << frames << " terminal=1 guards=ok\n";
        return 0;
    } catch (const std::exception &error) {
        std::cerr << "FAIL " << scenario << ": " << error.what() << '\n' << Serial.output;
        return 1;
    }
}
"""


def packet(kind: int, sequence: int, payload: bytes) -> bytes:
    header = struct.pack("<4sBBHIII", b"EPK1", 1, kind, 0, SESSION, sequence, len(payload))
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
        (BUILD / f"zero{seq}.bin").write_bytes(packet(2, seq, bytes(8000)))
    for seq in (1, 2, 4, 8):
        (BUILD / f"stop{seq}.bin").write_bytes(packet(3, seq, b""))
    first = bytes((i * 37 + 131) & 255 for i in range(8000))
    # 명령처럼 보이는 바이트도 프레임 본문으로만 처리되는지 함께 확인한다.
    first = first[:64] + b"EPK1RUN\nSTART\n" + first[78:]
    assert len(first) == 8000
    second = bytes(value ^ 0xA5 for value in first)
    subset = bytes(value & 0x55 for value in second)
    for seq, data in enumerate((first, first, second, subset, bytes([255]) * 8000, bytes(8000), bytes(8000)), 1):
        (BUILD / f"render{seq}.bin").write_bytes(packet(2, seq, data))
    (BUILD / "render2_changed.bin").write_bytes(packet(2, 2, second))
    crop = bytearray(8000)
    for x in (0, 7, 8, 319):
        crop[57 * 40 + x // 8] |= 0x80 >> (x % 8)
    (BUILD / "crop2.bin").write_bytes(packet(2, 2, bytes(crop)))
    moved = bytearray(8000)
    for x, y in ((7, 57), (8, 59)):
        moved[y * 40 + x // 8] |= 0x80 >> (x % 8)
    (BUILD / "crop3.bin").write_bytes(packet(2, 3, bytes(moved)))
    sketch = ROOT / "firmware/usb_frame/usb_frame.ino"
    source = '#define SKETCH_PATH ' + json.dumps(str(sketch)) + "\n#define FIXTURE_PATH " + json.dumps(str(BUILD)) + "\n" + HARNESS
    (BUILD / "harness.cpp").write_text(source)
    compile_command = [compiler, "-std=c++11", "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
                       "-fno-omit-frame-pointer", "-I", str(BUILD), str(BUILD / "harness.cpp"), "-o", str(BUILD / "harness")]
    scenarios = ["golden", "render", "cropped", "limit", "stop", "slow_frame", "screen_timeout", "scan_timeout", "late_crc", "frame_timeout", "partial_timeout",
                 "rx_buffer", "rx_queue", "rx_read", "start_timeout", "start_magic", "start_version", "start_reserved",
                 "start_type", "start_sequence", "start_session", "start_length", "start_crc", "early_data", "limit0", "limit13",
                 "allocation1", "allocation2", "allocation3", "alignment", "frequency", "short_delay", "missing_sign",
                 "delay_timeout", "core_change", "frame_crc", "frame_session", "frame_sequence", "frame_length", "frame_type"]
    with LOG.open("w") as log:
        def record(text: str) -> None:
            print(text, end="")
            log.write(text)
            log.flush()

        record(f"실제 USB 수신 스케치 호스트 검증\nsketch_sha256={hashlib.sha256(sketch.read_bytes()).hexdigest()}\n")
        for name in ("start1", "zero1", "stop2"):
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
