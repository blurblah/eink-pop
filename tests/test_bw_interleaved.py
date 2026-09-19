#!/usr/bin/env python3
"""실제 bw_interleaved 스케치의 마스크·종료·실행 제한을 호스트에서 검증한다. 장치는 사용하지 않는다."""

from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / ".local/build/bw_interleaved-host-qa"
LOG = ROOT / "artifacts/logs/bw-interleaved-host-qa.log"

# 스케치를 복제하지 않고 실제 .ino를 include한다. 시간·입출력·할당만 대체한다.
ARDUINO = r"""
#pragma once
#include <cstdint>
#include <cstddef>
#include <cstring>
#include <string>
#define CONFIG_IDF_TARGET_ESP32S3 1
#define BOARD_HAS_PSRAM 1
struct MockSerial {
    std::string input, output;
    size_t position = 0;
    void begin(unsigned long);
    int available();
    int read();
    void println(const char *);
    void printf(const char *, ...);
};
extern MockSerial Serial;
struct MockEsp {
    const char *getSdkVersion() { return "host-stub"; }
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
void epd_draw_image(Rect_t, const uint8_t *, DrawMode_t);
bool epd_draw_bw_interleaved(Rect_t, uint8_t *);
"""

HARNESS = r"""
#include "Arduino.h"
#include "epd_driver.h"
#include <algorithm>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <iostream>
#include <stdexcept>
#include <vector>

static void require(bool condition, const char *message) {
    if (!condition) throw std::runtime_error(message);
}
static std::string scenario;
static int64_t nowUs = 0;
static bool initialized = false, powered = false;
static int initCalls = 0, onCalls = 0, offCalls = 0, clearCalls = 0;
static int drawCalls = 0, frames = 0, legacyCalls = 0, interleavedCalls = 0;
static int busyCalls = 0, allocations = 0, frees = 0;
static int64_t screenStarted = -1;
static std::vector<int64_t> frameStarts;
static std::vector<uint8_t> storage;
static uint8_t *allocation = nullptr;
static size_t allocatedSize = 0;
static std::vector<uint8_t> panel(960 * 540, 15);
MockSerial Serial;
MockEsp ESP;

void MockSerial::begin(unsigned long baud) { require(baud == 115200, "serial baud"); }
int MockSerial::available() { return position < input.size(); }
int MockSerial::read() { return available() ? input[position++] : -1; }
void MockSerial::println(const char *value) {
    require(!powered, "Serial inside panel processing");
    output += value; output += '\n';
    nowUs += scenario == "exact_boundary" ? 0 : 30;
}
void MockSerial::printf(const char *format, ...) {
    require(!powered, "Serial inside panel processing");
    char buffer[2048];
    va_list args;
    va_start(args, format);
    int count = vsnprintf(buffer, sizeof(buffer), format, args);
    va_end(args);
    require(count >= 0 && (size_t)count < sizeof(buffer), "log truncation");
    output += buffer;
    nowUs += scenario == "exact_boundary" ? 0 : 30;
}
uint32_t millis() { return (uint32_t)(nowUs / 1000); }
int64_t esp_timer_get_time() { return nowUs; }
void delay(uint32_t ms) { require(!powered, "delay while powered"); nowUs += (int64_t)ms * 1000; }
uint32_t getCpuFrequencyMhz() {
    return scenario == "frequency" || (scenario == "frequency_change" && busyCalls >= 3) ? 160 : 240;
}
int xPortGetCoreID() { return scenario == "core_change" && busyCalls >= 3 ? 0 : 1; }
int32_t mock_ccount() {
    uint32_t value = (uint32_t)((uint64_t)nowUs * 240);
    if (scenario == "missing_sign") value &= 0x7FFFFFFF;
    return (int32_t)value;
}
extern "C" void busy_delay(uint32_t cycles) {
    require(!initialized && !powered, "delay check after panel init");
    require(cycles == 120000, "wrong diagnostic request");
    ++busyCalls;
    if (scenario == "delay_timeout") nowUs += 26000000;
    else nowUs += scenario == "short_delay" ? 1 : 500;
}
static void guards() {
    require(allocation != nullptr, "missing allocation");
    for (size_t i = 1; i <= 32; ++i) require(allocation[-(ptrdiff_t)i] == 0xA5, "leading guard");
    for (size_t i = 0; i < 32; ++i) require(allocation[allocatedSize + i] == 0xA5, "trailing guard");
}
void *heap_caps_malloc(size_t count, unsigned caps) {
    ++allocations;
    require(count == 30720 && caps == 3, "allocation size or PSRAM capabilities");
    if (scenario == "allocation") return nullptr;
    storage.assign(count + 68, 0xA5);
    allocation = storage.data() + 32 + (scenario == "alignment" ? 1 : 0);
    allocatedSize = count;
    return allocation;
}
void heap_caps_free(void *pointer) {
    require(pointer == allocation, "free wrong pointer");
    guards();
    ++frees;
}
void epd_init() {
    require(!initialized && !powered, "repeated init");
    require(Serial.output.find("INTERLEAVED_DELAY_DONE status=pass reason=ok") != std::string::npos,
            "panel init before successful self-test");
    require(busyCalls > 1 && nowUs >= 20000000, "diagnostic duration");
    initialized = true; ++initCalls;
}
void epd_poweron() {
    require(initialized && !powered, "invalid poweron");
    if (screenStarted < 0) screenStarted = nowUs;
    else {
        require(nowUs - screenStarted < 30000000, "new frame after time limit");
        frameStarts.push_back(nowUs - screenStarted);
    }
    powered = true; ++onCalls;
    nowUs += scenario == "exact_boundary" ? 0 : 10;
}
void epd_poweroff() {
    require(powered, "invalid poweroff");
    powered = false; ++offCalls;
    nowUs += scenario == "exact_boundary" ? 0 : 20;
}
void epd_clear() {
    require(powered && clearCalls == 0 && drawCalls == 0, "initial clear order");
    ++clearCalls;
    std::fill(panel.begin(), panel.end(), 15);
    nowUs += scenario == "exact_boundary" ? 30000000 : 100;
}
static void checkPanel() {
    // 누적 화면은 마지막 성공 사각형 하나와 주변 흰색만 남아야 한다.
    int squareX = 184 + 48 * (frames - 1);
    for (int y = 0; y < 540; ++y) {
        for (int x = 0; x < 960; ++x) {
            unsigned expected = frames && y >= 238 && y < 302 &&
                x >= squareX && x < squareX + 64 ? 0 : 15;
            require(panel[y * 960 + x] == expected, "accumulated panel mismatch");
        }
    }
}
static void checkMask(Rect_t area, const uint8_t *pixels, bool firstFrame) {
    require(powered && clearCalls == 1, "draw before initial clear");
    require(area.x == 0 && area.y == 238 && area.width == 960 && area.height == 64, "mask rect");
    require(pixels == allocation && (uintptr_t)pixels % 4 == 0, "mask alignment");
    guards();
    int squareX = 184 + 48 * frames;
    // 실제 전달 버퍼의 모든 픽셀을 검사한다. 행 경계·주변·겹침도 포함된다.
    for (int y = 0; y < 64; ++y) {
        for (int x = 0; x < 960; ++x) {
            unsigned pixel = (pixels[y * 480 + x / 2] >> ((x % 2) * 4)) & 15;
            unsigned expected = 15;
            if (firstFrame) {
                if (x >= squareX && x < squareX + 64) expected = 0;
            } else {
                if (x >= squareX - 48 && x < squareX) expected = 1;
                if (x >= squareX + 16 && x < squareX + 64) expected = 0;
            }
            require(pixel == expected, "mask pixel mismatch");
        }
    }
}
static void applyMask(const uint8_t *pixels) {
    // 호스트의 논리 화면이며 실제 전기적 파형·패널 반응을 모사하지 않는다.
    for (int y = 0; y < 64; ++y) {
        for (int x = 0; x < 960; ++x) {
            unsigned pixel = (pixels[y * 480 + x / 2] >> ((x % 2) * 4)) & 15;
            if (pixel == 0) panel[(y + 238) * 960 + x] = 0;
            else if (pixel == 1) panel[(y + 238) * 960 + x] = 15;
        }
    }
    ++frames;
    checkPanel();
}
void epd_draw_image(Rect_t area, const uint8_t *pixels, DrawMode_t mode) {
    require(frames == 0 && legacyCalls == 0 && interleavedCalls == 0,
            "legacy call only on first frame");
    require(mode == BLACK_ON_WHITE, "legacy mode");
    checkMask(area, pixels, true);
    ++legacyCalls; ++drawCalls;
    applyMask(pixels);
    nowUs += scenario == "after_first_limit" ? 30000000 :
        scenario == "slow_screen" ? 6000000 : 1000;
}
bool epd_draw_bw_interleaved(Rect_t area, uint8_t *pixels) {
    require(frames > 0 && legacyCalls == 1, "interleaved call before first frame");
    checkMask(area, pixels, false);
    ++interleavedCalls; ++drawCalls;
    if (scenario == "api_reject") {
        nowUs += 100;
        checkPanel();
        return false;
    }
    applyMask(pixels);
    nowUs += scenario == "slow_screen" ? 6000000 : 2000;
    return true;
}

#include SKETCH_PATH

static int occurrences(const std::string &text, const std::string &needle) {
    int count = 0;
    for (size_t at = 0; (at = text.find(needle, at)) != std::string::npos; at += needle.size()) ++count;
    return count;
}
static void send(const std::string &line) { Serial.input += line; loop(); }
int main(int argc, char **argv) {
    try {
        require(argc == 2, "scenario required"); scenario = argv[1];
        setup();
        for (int i = 0; i < 5; ++i) { delay(1000); loop(); }
        require(!initialized && allocations == 0 && busyCalls == 0, "panel access without RUN");
        require(occurrences(Serial.output, "INTERLEAVED_READY ") >= 2, "READY does not repeat");
        send("run\n RUN\nRUN \nRUNx\n0123456789RUN\n");
        require(!initialized && allocations == 0, "invalid command accepted");
        send("RU"); require(allocations == 0, "partial RUN accepted");
        send("N\r\nRUN\n");

        bool noScreen = scenario != "normal" && scenario != "slow_screen" &&
            scenario != "exact_boundary" && scenario != "after_first_limit" && scenario != "api_reject";
        require(allocations == 1, "allocation count");
        if (noScreen) {
            require(initCalls == 0 && onCalls == 0 && offCalls == 0 && clearCalls == 0 && drawCalls == 0,
                    "failure touched panel");
            require(Serial.output.find("INTERLEAVED_DONE status=fail completed=0") != std::string::npos, "missing failure log");
            const char *reason = scenario == "allocation" ? "allocation_failed" :
                scenario == "alignment" ? "buffer_alignment" :
                (scenario == "frequency" || scenario == "frequency_change") ? "cpu_frequency" :
                scenario == "core_change" ? "core_changed" :
                scenario == "short_delay" ? "delay_too_short" :
                scenario == "missing_sign" ? "missing_sign_samples" : "delay_test_timeout";
            require(Serial.output.find(std::string("INTERLEAVED_ERROR reason=") + reason) != std::string::npos,
                    "wrong failure reason");
            if (scenario == "allocation" || scenario == "alignment" || scenario == "frequency")
                require(busyCalls == 0, "precheck failure called delay test");
            if (scenario == "delay_timeout") require(busyCalls == 1, "call after diagnostic timeout");
        } else {
            require(initCalls == 1 && clearCalls == 1 && !powered, "panel lifecycle");
            require(onCalls == drawCalls + 1 && offCalls == onCalls, "missing frame poweroff");
            require(frames <= 12 && drawCalls <= 12, "frame or API call limit");
            require(legacyCalls == (frames ? 1 : 0) && interleavedCalls == drawCalls - legacyCalls,
                    "API call counts");
            require((int)frameStarts.size() == drawCalls, "frame starts count");
            checkPanel();
            if (scenario == "api_reject") {
                require(frames == 1 && drawCalls == 2 && interleavedCalls == 1, "API rejection counts");
                require(Serial.output.find("INTERLEAVED_ERROR reason=interleaved_rejected index=1 panel=off") !=
                        std::string::npos, "missing API rejection error");
                require(Serial.output.find("INTERLEAVED_DONE status=fail completed=1 reason=interleaved_rejected") !=
                        std::string::npos, "API rejection did not fail safely");
            } else {
                require(Serial.output.find("INTERLEAVED_DONE status=pass") != std::string::npos,
                        "missing success log");
                if (scenario == "normal") {
                    require(frames == 12 && drawCalls == 12 && legacyCalls == 1 && interleavedCalls == 11,
                            "normal frame and API counts");
                    require(busyCalls == 199, "normal 20 second diagnostic sample count");
                    require(Serial.output.find("reason=frame_limit") != std::string::npos, "normal stop reason");
                } else if (scenario == "slow_screen" || scenario == "after_first_limit") {
                    require(frames == (scenario == "slow_screen" ? 5 : 1) &&
                            nowUs - screenStarted > 30000000, "soft limit does not permit active frame");
                    require(frameStarts.back() < 30000000, "late final frame");
                } else require(frames == 0, "frame started at exact limit");
                if (scenario != "normal") require(Serial.output.find("reason=time_limit") != std::string::npos,
                                                  "time stop reason");
            }
        }
        require(frees == (scenario == "allocation" ? 0 : 1), "allocation not freed once");
        int previousDraws = drawCalls, previousBusy = busyCalls, previousInit = initCalls;
        size_t previousOutput = Serial.output.size();
        for (int i = 0; i < 20; ++i) send("RUN\n");
        require(drawCalls == previousDraws && busyCalls == previousBusy && initCalls == previousInit && allocations == 1,
                "loop or repeated RUN restarted experiment");
        require(Serial.output.size() == previousOutput, "output repeats after completion");
        require(occurrences(Serial.output, "INTERLEAVED_START ") == 1 && occurrences(Serial.output, "INTERLEAVED_DONE ") == 1,
                "start or done count");
        require(occurrences(Serial.output, "INTERLEAVED_FRAME ") == frames, "frame log count");
        // 개별 표본 200여 줄은 로그를 부풀리지 않고 완료·오류·프레임을 보존한다.
        size_t at = 0;
        while (at < Serial.output.size()) {
            size_t end = Serial.output.find('\n', at);
            if (end == std::string::npos) end = Serial.output.size();
            std::string line = Serial.output.substr(at, end - at);
            if (line.find("INTERLEAVED_DELAY_DONE ") == 0 || line.find("INTERLEAVED_DONE ") == 0 ||
                line.find("INTERLEAVED_ERROR ") == 0 || line.find("INTERLEAVED_FRAME ") == 0) std::cout << line << '\n';
            at = end + 1;
        }
        std::cout << "PASS " << scenario << " frames=" << frames << " draws=" << drawCalls
                  << " legacy=" << legacyCalls << " interleaved=" << interleavedCalls
                  << " clear=" << clearCalls << " on=" << onCalls << " off=" << offCalls
                  << " busy_calls=" << busyCalls << " guards=ok repeat=blocked\n";
        return 0;
    } catch (const std::exception &error) {
        std::cerr << "FAIL " << scenario << ": " << error.what() << '\n';
        return 1;
    }
}
"""



def main() -> int:
    compiler = shutil.which("clang++")
    if not compiler:
        print("clang++가 없어 검증하지 못했습니다. 설치는 하지 않습니다.", file=sys.stderr)
        return 1
    BUILD.mkdir(parents=True, exist_ok=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    headers = {
        "Arduino.h": ARDUINO,
        "epd_driver.h": EPD,
        "esp_arduino_version.h": "#define ESP_ARDUINO_VERSION_MAJOR 2\n#define ESP_ARDUINO_VERSION_MINOR 0\n#define ESP_ARDUINO_VERSION_PATCH 17\n",
        "esp_heap_caps.h": '#pragma once\n#include "Arduino.h"\n#define MALLOC_CAP_SPIRAM 1\n#define MALLOC_CAP_8BIT 2\nvoid *heap_caps_malloc(size_t, unsigned);\nvoid heap_caps_free(void *);\n',
        "esp_timer.h": '#pragma once\n#include "Arduino.h"\nint64_t esp_timer_get_time();\n',
        "xtensa/core-macros.h": '#pragma once\n#include "Arduino.h"\nint32_t mock_ccount();\n#define XTHAL_GET_CCOUNT() mock_ccount()\n',
    }
    for name, content in headers.items():
        path = BUILD / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    sketch = ROOT / "firmware/bw_interleaved/bw_interleaved.ino"
    harness = '#define SKETCH_PATH ' + json.dumps(str(sketch)) + "\n" + HARNESS
    (BUILD / "harness.cpp").write_text(harness)
    command = [compiler, "-std=c++11", "-Wall", "-Wextra", "-Werror", "-I", str(BUILD),
               str(BUILD / "harness.cpp"), "-o", str(BUILD / "harness")]
    scenarios = ("normal", "slow_screen", "exact_boundary", "after_first_limit", "api_reject",
                 "allocation", "alignment", "frequency", "frequency_change", "core_change",
                 "short_delay", "missing_sign", "delay_timeout")
    with LOG.open("w") as log:
        def record(message: str) -> None:
            print(message, end="")
            log.write(message)
            log.flush()

        record("실제 교차 갱신 스케치 호스트 검증; 전기적 파형·실물 반응 제외\n")
        record(f"sketch_sha256={hashlib.sha256(sketch.read_bytes()).hexdigest()}\n")
        result = subprocess.run(command, capture_output=True, text=True)
        record(result.stdout + result.stderr)
        if result.returncode:
            return result.returncode
        record("PASS compile clang++ -std=c++11 -Wall -Wextra -Werror\n")
        for scenario in scenarios:
            result = subprocess.run([str(BUILD / "harness"), scenario], capture_output=True, text=True, timeout=30)
            record(result.stdout + result.stderr)
            if result.returncode:
                return result.returncode
            for line in result.stdout.splitlines():
                if not line.startswith("INTERLEAVED_FRAME "):
                    continue
                fields = dict(item.split("=", 1) for item in line.split()[1:])
                parts = ("generate_us", "power_on_us", "draw_us", "power_off_us")
                if sum(int(fields[name]) for name in parts) != int(fields["process_us"]):
                    record(f"FAIL processing_time_partition scenario={scenario}\n")
                    return 1
        record("PASS processing_time_partition excludes_serial_and_pause=1\n")
        record(f"PASS all scenarios={len(scenarios)} device_access=none\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
