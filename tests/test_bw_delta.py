#!/usr/bin/env python3
"""실제 bw_delta 스케치와 드라이버 LUT를 호스트에서 검증한다. 장치는 사용하지 않는다."""

from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / ".local/build/bw_delta-host-qa"
LOG = ROOT / "artifacts/logs/bw-delta-host-qa.log"

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
static int drawCalls = 0, frames = 0, whiteCalls = 0, blackCalls = 0;
static int busyCalls = 0, allocations = 0, frees = 0;
static int64_t screenStarted = -1;
static std::vector<int64_t> frameStarts;
static std::vector<uint8_t> storage;
static uint8_t *allocation = nullptr;
static size_t allocatedSize = 0;
static bool whiteSeen = false;
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
    require(Serial.output.find("DELTA_DELAY_DONE status=pass reason=ok") != std::string::npos,
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
    require(!whiteSeen, "frame missing black pass");
    powered = false; ++offCalls;
    nowUs += scenario == "exact_boundary" ? 0 : 20;
}
void epd_clear() {
    require(powered && clearCalls == 0 && drawCalls == 0, "initial clear order");
    ++clearCalls;
    nowUs += scenario == "exact_boundary" ? 30000000 : 100;
}
void epd_draw_image(Rect_t area, const uint8_t *pixels, DrawMode_t mode) {
    require(powered && clearCalls == 1, "draw before initial clear");
    require(area.x == 0 && area.y == 238 && area.width == 960 && area.height == 64, "mask rect");
    require(pixels == allocation && (uintptr_t)pixels % 4 == 0, "mask alignment");
    guards();
    int changedX = 0, changedWidth = 0;
    bool white = mode == WHITE_ON_BLACK;
    if (white) {
        require(frames > 0 && !whiteSeen, "white pass order");
        changedX = 184 + 48 * (frames - 1); changedWidth = 48;
        whiteSeen = true; ++whiteCalls;
    } else {
        require(mode == BLACK_ON_WHITE && (frames == 0 || whiteSeen), "black pass order");
        changedX = frames == 0 ? 184 : 184 + 48 * frames + 16;
        changedWidth = frames == 0 ? 64 : 48;
        whiteSeen = false; ++blackCalls;
    }
    // 실제 전달 버퍼의 모든 픽셀을 검사한다. 행 경계·주변·겹침도 포함된다.
    for (int y = 0; y < 64; ++y) {
        for (int x = 0; x < 960; ++x) {
            unsigned pixel = (pixels[y * 480 + x / 2] >> ((x % 2) * 4)) & 15;
            bool changed = x >= changedX && x < changedX + changedWidth;
            unsigned expected = white ? (changed ? 15 : 0) : (changed ? 0 : 15);
            require(pixel == expected, "mask pixel mismatch");
        }
    }
    ++drawCalls;
    if (!white) ++frames;
    nowUs += scenario == "slow_screen" ? 6000000 : 1000;
}

#define IRAM_ATTR
#define ESP_LOGW(...) ((void)0)
// 제조사 원문에 있는 signed/unsigned 비교 경고만 추출 본문 안에서 허용한다.
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wsign-compare"
#include "actual_lut.h"
#pragma clang diagnostic pop
#include SKETCH_PATH

static int occurrences(const std::string &text, const std::string &needle) {
    int count = 0;
    for (size_t at = 0; (at = text.find(needle, at)) != std::string::npos; at += needle.size()) ++count;
    return count;
}
static void testLut() {
    std::vector<uint8_t> lut(1 << 16);
    for (DrawMode_t mode : {BLACK_ON_WHITE, WHITE_ON_BLACK}) {
        reset_lut(lut.data(), mode);
        for (uint8_t stage = 0; stage < 15; ++stage) {
            update_LUT(lut.data(), stage, mode);
            // 전체 16비트 조합에서 네 픽셀 위치의 무동작·활성 명령을 확인한다.
            for (unsigned word = 0; word < 65536; ++word) {
                for (unsigned position = 0; position < 4; ++position) {
                    unsigned pixel = (word >> (position * 4)) & 15;
                    unsigned command = (lut[word] >> (position * 2)) & 3;
                    if (pixel == (mode == BLACK_ON_WHITE ? 15U : 0U))
                        require(command == 0, "LUT no-op mismatch");
                    if (pixel == (mode == BLACK_ON_WHITE ? 0U : 15U))
                        require(command == (mode == BLACK_ON_WHITE ? 1U : 2U), "LUT active mismatch");
                }
            }
        }
    }
    std::cout << "PASS actual_lut modes=2 stages=15 words=65536 positions=4\n";
}
static void send(const std::string &line) { Serial.input += line; loop(); }
int main(int argc, char **argv) {
    try {
        require(argc == 2, "scenario required"); scenario = argv[1];
        if (scenario == "lut") { testLut(); return 0; }
        setup();
        for (int i = 0; i < 5; ++i) { delay(1000); loop(); }
        require(!initialized && allocations == 0 && busyCalls == 0, "panel access without RUN");
        require(occurrences(Serial.output, "DELTA_READY ") >= 2, "READY does not repeat");
        send("run\n RUN\nRUN \nRUNx\n0123456789RUN\n");
        require(!initialized && allocations == 0, "invalid command accepted");
        send("RU"); require(allocations == 0, "partial RUN accepted");
        send("N\r\nRUN\n");

        bool noScreen = scenario != "normal" && scenario != "slow_screen" && scenario != "exact_boundary";
        require(allocations == 1, "allocation count");
        if (noScreen) {
            require(initCalls == 0 && onCalls == 0 && offCalls == 0 && clearCalls == 0 && drawCalls == 0,
                    "failure touched panel");
            require(Serial.output.find("DELTA_DONE status=fail completed=0") != std::string::npos, "missing failure log");
            const char *reason = scenario == "allocation" ? "allocation_failed" :
                scenario == "alignment" ? "buffer_alignment" :
                (scenario == "frequency" || scenario == "frequency_change") ? "cpu_frequency" :
                scenario == "core_change" ? "core_changed" :
                scenario == "short_delay" ? "delay_too_short" :
                scenario == "missing_sign" ? "missing_sign_samples" : "delay_test_timeout";
            require(Serial.output.find(std::string("DELTA_ERROR reason=") + reason) != std::string::npos,
                    "wrong failure reason");
            if (scenario == "allocation" || scenario == "alignment" || scenario == "frequency")
                require(busyCalls == 0, "precheck failure called delay test");
            if (scenario == "delay_timeout") require(busyCalls == 1, "call after diagnostic timeout");
        } else {
            require(initCalls == 1 && clearCalls == 1 && !powered, "panel lifecycle");
            require(onCalls == frames + 1 && offCalls == onCalls, "missing frame poweroff");
            require(frames <= 12 && drawCalls <= 23, "frame or draw limit");
            require(blackCalls == frames && whiteCalls == std::max(0, frames - 1), "pass counts");
            require((int)frameStarts.size() == frames, "frame starts count");
            require(Serial.output.find("DELTA_DONE status=pass") != std::string::npos, "missing success log");
            if (scenario == "normal") {
                require(frames == 12 && drawCalls == 23, "normal frame count");
                require(Serial.output.find("reason=frame_limit") != std::string::npos, "normal stop reason");
            } else if (scenario == "slow_screen") {
                require(frames == 3 && nowUs - screenStarted > 30000000, "soft limit does not permit active frame");
                require(frameStarts.back() < 30000000, "late final frame");
            } else require(frames == 0, "frame started at exact limit");
            if (scenario != "normal") require(Serial.output.find("reason=time_limit") != std::string::npos, "time stop reason");
        }
        require(frees == (scenario == "allocation" ? 0 : 1), "allocation not freed once");
        int previousDraws = drawCalls, previousBusy = busyCalls, previousInit = initCalls;
        size_t previousOutput = Serial.output.size();
        for (int i = 0; i < 20; ++i) send("RUN\n");
        require(drawCalls == previousDraws && busyCalls == previousBusy && initCalls == previousInit && allocations == 1,
                "loop or repeated RUN restarted experiment");
        require(Serial.output.size() == previousOutput, "output repeats after completion");
        require(occurrences(Serial.output, "DELTA_START ") == 1 && occurrences(Serial.output, "DELTA_DONE ") == 1,
                "start or done count");
        require(occurrences(Serial.output, "DELTA_FRAME ") == frames, "frame log count");
        // 개별 표본 200여 줄은 로그를 부풀리지 않고 완료·오류·프레임을 보존한다.
        size_t at = 0;
        while (at < Serial.output.size()) {
            size_t end = Serial.output.find('\n', at);
            if (end == std::string::npos) end = Serial.output.size();
            std::string line = Serial.output.substr(at, end - at);
            if (line.find("DELTA_DELAY_DONE ") == 0 || line.find("DELTA_DONE ") == 0 ||
                line.find("DELTA_ERROR ") == 0 || line.find("DELTA_FRAME ") == 0) std::cout << line << '\n';
            at = end + 1;
        }
        std::cout << "PASS " << scenario << " frames=" << frames << " draws=" << drawCalls
                  << " clear=" << clearCalls << " on=" << onCalls << " off=" << offCalls
                  << " busy_calls=" << busyCalls << " guards=ok repeat=blocked\n";
        return 0;
    } catch (const std::exception &error) {
        std::cerr << "FAIL " << scenario << ": " << error.what() << '\n';
        return 1;
    }
}
"""


def function_definition(source: str, name: str) -> str:
    """제조사 정의를 그대로 추출한다. 앞쪽 전방 선언은 건너뛴다."""
    marker = f"static void IRAM_ATTR {name}("
    start = 0
    while True:
        start = source.index(marker, start)
        brace = source.index("{", start)
        semicolon = source.index(";", start)
        if brace < semicolon:
            break
        start += len(marker)
    depth = 0
    for end in range(brace, len(source)):
        if source[end] == "{":
            depth += 1
        elif source[end] == "}":
            depth -= 1
            if depth == 0:
                return source[start : end + 1]
    raise ValueError(f"함수 본문을 찾지 못했습니다: {name}")


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
    source = (ROOT / "repos/LilyGo-EPD47/src/epd_driver.c").read_text()
    lut = "\n\n".join(function_definition(source, name) for name in ("reset_lut", "update_LUT"))
    headers["actual_lut.h"] = lut
    for name, content in headers.items():
        path = BUILD / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    sketch = ROOT / "firmware/bw_delta/bw_delta.ino"
    harness = '#define SKETCH_PATH ' + json.dumps(str(sketch)) + "\n" + HARNESS
    (BUILD / "harness.cpp").write_text(harness)
    command = [compiler, "-std=c++11", "-Wall", "-Wextra", "-Werror", "-I", str(BUILD),
               str(BUILD / "harness.cpp"), "-o", str(BUILD / "harness")]
    scenarios = ("lut", "normal", "slow_screen", "exact_boundary", "allocation", "alignment",
                 "frequency", "frequency_change", "core_change", "short_delay", "missing_sign", "delay_timeout")
    with LOG.open("w") as log:
        def record(message: str) -> None:
            print(message, end="")
            log.write(message)
            log.flush()

        record("실제 스케치 및 제조사 LUT 호스트 검증\n")
        record(f"sketch_sha256={hashlib.sha256(sketch.read_bytes()).hexdigest()}\n")
        record(f"extracted_lut_sha256={hashlib.sha256(lut.encode()).hexdigest()}\n")
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
                if not line.startswith("DELTA_FRAME "):
                    continue
                fields = dict(item.split("=", 1) for item in line.split()[1:])
                parts = ("generate_us", "power_on_us", "white_us", "black_us", "power_off_us")
                if sum(int(fields[name]) for name in parts) != int(fields["process_us"]):
                    record(f"FAIL processing_time_partition scenario={scenario}\n")
                    return 1
        record("PASS processing_time_partition excludes_serial_and_pause=1\n")
        record(f"PASS all scenarios={len(scenarios)} device_access=none\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
