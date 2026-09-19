#!/usr/bin/env python3
"""실제 gray_game 스케치의 한 장 출력·전체 확대 픽셀·실패 경로를 ASan/UBSan으로 검증한다."""

from pathlib import Path
import hashlib
import json
import re
import shutil
import subprocess
import sys

from test_bw_interleaved import ARDUINO


ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / ".local/build/gray_game-host-qa"
LOG = ROOT / "artifacts/logs/gray-game-host-qa.log"
SKETCH = ROOT / "firmware/gray_game/gray_game.ino"
IMAGE = ROOT / "firmware/gray_game/src/game_image.h"

EPD = r"""
#pragma once
#include "Arduino.h"
#define EPD_WIDTH 960
#define EPD_HEIGHT 540
struct Rect_t { int32_t x, y, width, height; };
void epd_init();
void epd_poweron();
void epd_poweroff();
void epd_clear();
void epd_draw_grayscale_image(Rect_t, uint8_t *);
"""

HARNESS = r"""
#include "Arduino.h"
#include "epd_driver.h"
#include <cstdarg>
#include <cstdio>
#include <iostream>
#include <stdexcept>
#include <vector>

static void require(bool condition, const char *message) {
    if (!condition) throw std::runtime_error(message);
}
static std::string scenario, panelEvents;
static int64_t nowUs = 0;
static bool initialized = false, powered = false;
static unsigned initCalls = 0, onCalls = 0, offCalls = 0, clearCalls = 0, drawCalls = 0;
static unsigned busyCalls = 0, allocations = 0, frees = 0, checkedSourcePixels = 0, checkedMargins = 0;
static size_t allocatedSize = 0;
static std::vector<uint8_t> storage;
static uint8_t *allocation = nullptr;
static void checkImage(const uint8_t *pixels);
MockSerial Serial;
MockEsp ESP;

void MockSerial::begin(unsigned long baud) { require(baud == 115200, "serial baud"); }
int MockSerial::available() { return position < input.size(); }
int MockSerial::read() { return available() ? (unsigned char)input[position++] : -1; }
void MockSerial::println(const char *value) {
    require(!powered, "Serial inside powered panel processing");
    output += value; output += '\n'; nowUs += 30;
}
void MockSerial::printf(const char *format, ...) {
    require(!powered, "Serial inside powered panel processing");
    char buffer[2048];
    va_list arguments;
    va_start(arguments, format);
    const int count = vsnprintf(buffer, sizeof(buffer), format, arguments);
    va_end(arguments);
    require(count >= 0 && (size_t)count < sizeof(buffer), "log truncation");
    output += buffer; nowUs += 30;
}
uint32_t millis() { return (uint32_t)(nowUs / 1000); }
int64_t esp_timer_get_time() { return nowUs; }
void delay(uint32_t ms) {
    require(!powered, "delay inside powered panel processing");
    nowUs += (int64_t)ms * 1000;
}
uint32_t getCpuFrequencyMhz() {
    return scenario == "frequency" || (scenario == "frequency_change" && busyCalls >= 3) ? 160 : 240;
}
int xPortGetCoreID() {
    if (scenario == "invalid_core") return -1;
    return scenario == "core_change" && busyCalls >= 3 ? 0 : 1;
}
int32_t mock_ccount() {
    uint32_t value = (uint32_t)((uint64_t)nowUs * 240);
    if (scenario == "missing_sign") value &= 0x7FFFFFFF;
    return (int32_t)value;
}
extern "C" void busy_delay(uint32_t cycles) {
    require(!initialized && !powered, "delay check after panel init");
    require(cycles == 120000, "diagnostic request cycles");
    ++busyCalls;
    if (scenario == "delay_timeout") nowUs += 26000000;
    else nowUs += scenario == "short_delay" ? 1 : 500;
}
static void guards() {
    require(allocation != nullptr, "missing allocation");
    for (size_t i = 1; i <= 32; ++i)
        require(allocation[-(ptrdiff_t)i] == 0xA5, "leading guard damaged");
    for (size_t i = 0; i < 32; ++i)
        require(allocation[allocatedSize + i] == 0xA5, "trailing guard damaged");
}
void *heap_caps_malloc(size_t count, unsigned caps) {
    require(allocations++ == 0, "more than one allocation");
    require(count == 192000 && caps == 3, "buffer size or PSRAM capabilities");
    if (scenario == "allocation") return nullptr;
    storage.assign(count + 68, 0xA5);
    allocation = storage.data() + 32 + (scenario == "alignment" ? 1 : 0);
    allocatedSize = count;
    return allocation;
}
void heap_caps_free(void *pointer) {
    require(!powered && pointer == allocation && frees == 0, "free order or count");
    guards(); ++frees;
}
void epd_init() {
    require(!initialized && !powered && initCalls == 0, "repeated panel initialization");
    require(Serial.output.find("GRAY_GAME_DELAY_DONE status=pass reason=ok") != std::string::npos,
            "panel initialization before successful diagnostic");
    require(busyCalls == 199 && nowUs >= 20000000, "diagnostic duration or sample count");
    initialized = true; ++initCalls; panelEvents += 'I';
}
void epd_poweron() {
    require(initialized && !powered && onCalls == 0, "poweron order or count");
    powered = true; ++onCalls; panelEvents += 'N'; nowUs += 10;
}
void epd_clear() {
    require(powered && clearCalls == 0 && drawCalls == 0, "initial clear order or count");
    ++clearCalls; panelEvents += 'C'; nowUs += 100;
}
void epd_draw_grayscale_image(Rect_t area, uint8_t *pixels) {
    require(powered && clearCalls == 1 && drawCalls == 0, "grayscale draw order or count");
    require(area.x == 0 && area.y == 70 && area.width == 960 && area.height == 400,
            "full-width area or centered panel placement");
    require(pixels == allocation && (uintptr_t)pixels % 4 == 0, "wrong or unaligned image pointer");
    guards(); checkImage(pixels);
    ++drawCalls; panelEvents += 'D'; nowUs += 1000;
}
void epd_poweroff() {
    require(powered && drawCalls == 1 && offCalls == 0, "poweroff before draw or repeated");
    powered = false; ++offCalls; panelEvents += 'O'; nowUs += 20;
}

// 이미지와 변환 함수는 복제하지 않고 실제 스케치·src/game_image.h를 포함한다.
#include SKETCH_PATH

static unsigned outputPixel(const uint8_t *pixels, unsigned x, unsigned y) {
    return (pixels[y * 480 + x / 2] >> ((x & 1) * 4)) & 15;
}
static void checkImage(const uint8_t *pixels) {
    static_assert(sizeof(GAME_PIXELS) == 32000, "source byte count");
    // 원본 64,000개 각각의 2×2 출력 네 곳을 독립 좌표식으로 확인한다.
    for (unsigned sy = 0; sy < 200; ++sy) {
        for (unsigned sx = 0; sx < 320; ++sx) {
            const unsigned source = GAME_PIXELS[sy * 160 + sx / 2];
            const unsigned expected = (sx & 1) ? source / 16 : source % 16;
            for (unsigned dy = 0; dy < 2; ++dy)
                for (unsigned dx = 0; dx < 2; ++dx)
                    require(outputPixel(pixels, 160 + sx * 2 + dx, sy * 2 + dy) == expected,
                            "source pixel, nibble order, stride or 2x2 expansion mismatch");
            ++checkedSourcePixels;
        }
    }
    // 출력 영역의 좌우 각160열 전체를 검사한다. 위아래70행은 draw 영역 밖이다.
    for (unsigned y = 0; y < 400; ++y)
        for (unsigned x = 0; x < 960; ++x)
            if (x < 160 || x >= 800) {
                require(outputPixel(pixels, x, y) == 15, "non-white side margin");
                ++checkedMargins;
            }
}
static unsigned occurrences(const std::string &text, const std::string &needle) {
    unsigned count = 0;
    for (size_t at = 0; (at = text.find(needle, at)) != std::string::npos; at += needle.size()) ++count;
    return count;
}
static void send(const std::string &data) { Serial.input += data; loop(); }
int main(int argc, char **argv) {
    try {
        require(argc == 2, "scenario required"); scenario = argv[1];
        const bool success = scenario == "normal_lf" || scenario == "normal_crlf";
        const std::vector<uint8_t> originalImage(GAME_PIXELS, GAME_PIXELS + sizeof(GAME_PIXELS));
        setup();
        for (unsigned i = 0; i < 5; ++i) { delay(1000); loop(); }
        require(!initialized && allocations == 0 && busyCalls == 0 && !powered,
                "automatic panel access or allocation without RUN");
        require(occurrences(Serial.output, "GRAY_GAME_READY ") >= 2, "READY repetition missing");
        send("run\n RUN\nRUN \nRUNx\nRUN\r\r\n0123456789RUN\n");
        send(std::string("RUN\0\n", 5));
        send(std::string(4096, 'X') + "RUN\n");
        require(allocations == 0 && !initialized && busyCalls == 0 &&
                occurrences(Serial.output, "GRAY_GAME_START ") == 0, "invalid or overflow command accepted");
        send("R"); require(allocations == 0, "one-byte RUN fragment accepted");
        send("U"); require(allocations == 0, "two-byte RUN fragment accepted");
        if (scenario == "normal_lf") send("N\nRUN\n");
        else {
            send("N\r"); require(allocations == 0, "CR without LF accepted");
            send("\nRUN\n");
        }
        require(allocations == 1, "allocation count");
        if (success) {
            require(panelEvents == "INCDO", "one init/on/clear/grayscale/off lifecycle");
            require(initCalls == 1 && onCalls == 1 && clearCalls == 1 && drawCalls == 1 && offCalls == 1 && !powered,
                    "single image lifecycle count");
            require(checkedSourcePixels == 64000 && checkedMargins == 128000, "pixel coverage");
            require(Serial.output.find("GRAY_GAME_DONE status=pass completed=1 reason=single_image") != std::string::npos,
                    "missing successful terminal log");
            require(occurrences(Serial.output, "GRAY_GAME_ERROR ") == 0, "success error log");
        } else {
            require(panelEvents.empty() && !initialized && !powered &&
                    initCalls == 0 && onCalls == 0 && clearCalls == 0 && drawCalls == 0 && offCalls == 0,
                    "failure accessed panel");
            const char *reason = scenario == "allocation" ? "allocation_failed" :
                scenario == "alignment" ? "buffer_alignment" :
                (scenario == "frequency" || scenario == "frequency_change") ? "cpu_frequency" :
                scenario == "core_change" ? "core_changed" :
                scenario == "invalid_core" ? "invalid_core" :
                scenario == "short_delay" ? "delay_too_short" :
                scenario == "missing_sign" ? "missing_sign_samples" : "delay_test_timeout";
            require(Serial.output.find(std::string("GRAY_GAME_ERROR reason=") + reason) != std::string::npos,
                    "wrong failure reason");
            require(Serial.output.find("GRAY_GAME_DONE status=fail completed=0") != std::string::npos,
                    "missing failure terminal log");
            if (scenario == "allocation" || scenario == "alignment" || scenario == "frequency" || scenario == "invalid_core")
                require(busyCalls == 0, "diagnostic called after failed prerequisite");
            if (scenario == "delay_timeout") require(busyCalls == 1, "diagnostic continues after timeout");
        }
        require(frees == (scenario == "allocation" ? 0U : 1U), "allocation not freed exactly once");
        require(memcmp(GAME_PIXELS, originalImage.data(), sizeof(GAME_PIXELS)) == 0, "source image changed");
        unsigned previousBusy = busyCalls;
        const std::string previousEvents = panelEvents;
        const size_t previousOutput = Serial.output.size();
        for (unsigned i = 0; i < 20; ++i) send("RUN\nRUN\r\n");
        require(panelEvents == previousEvents && busyCalls == previousBusy && allocations == 1 &&
                Serial.output.size() == previousOutput, "success or failure retriggered by RUN/loop");
        require(occurrences(Serial.output, "GRAY_GAME_START ") == 1 &&
                occurrences(Serial.output, "GRAY_GAME_DONE ") == 1, "START/DONE repeated");
        for (size_t at = 0; at < Serial.output.size();) {
            size_t end = Serial.output.find('\n', at);
            if (end == std::string::npos) end = Serial.output.size();
            const std::string line = Serial.output.substr(at, end - at);
            if (line.find("GRAY_GAME_DELAY_DONE ") == 0 || line.find("GRAY_GAME_DONE ") == 0 ||
                line.find("GRAY_GAME_ERROR ") == 0) std::cout << line << '\n';
            at = end + 1;
        }
        std::cout << "PASS " << scenario << " draws=" << drawCalls << " clear=" << clearCalls
                  << " on=" << onCalls << " off=" << offCalls << " samples=" << busyCalls
                  << " source_pixels=" << checkedSourcePixels << " margins=" << checkedMargins
                  << " guards=ok repeat=blocked\n";
        return 0;
    } catch (const std::exception &error) {
        std::cerr << "FAIL " << scenario << ": " << error.what() << '\n';
        return 1;
    }
}
"""


def main():
    BUILD.mkdir(parents=True, exist_ok=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("w") as log:
        def record(text):
            print(text, end="")
            log.write(text)
            log.flush()

        try:
            compiler = shutil.which("clang++")
            if not compiler:
                raise RuntimeError("clang++가 없어 검증할 수 없습니다. 설치하지 않습니다.")
            image_text = IMAGE.read_text()
            image_body = re.search(r"GAME_PIXELS\[32000\]\s*=\s*\{([^}]*)\}", image_text, re.S).group(1)
            packed = bytes(int(value, 16) for value in re.findall(r"0x([0-9a-fA-F]{2})", image_body))
            recorded_hash = re.search(r'GAME_PIXELS_SHA256\[\]\s*=\s*"([a-f0-9]{64})"', image_text).group(1)
            if len(packed) != 32000 or hashlib.sha256(packed).hexdigest() != recorded_hash:
                raise RuntimeError("실제 이미지 바이트 수·기록 SHA-256 불일치")
            headers = {
                "Arduino.h": ARDUINO + "\n#define ARDUINO_USB_MODE 1\n#define ARDUINO_USB_CDC_ON_BOOT 1\n",
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
            harness = '#define SKETCH_PATH ' + json.dumps(str(SKETCH)) + "\n" + HARNESS
            (BUILD / "harness.cpp").write_text(harness)
            command = [compiler, "-std=c++11", "-O1", "-g", "-Wall", "-Wextra", "-Werror",
                       "-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-fno-omit-frame-pointer",
                       "-I", str(BUILD), str(BUILD / "harness.cpp"), "-o", str(BUILD / "harness")]
            record("실제 16계조 한 장 스케치 검증; 패널 파형·전압·광학 효과는 미검증\n")
            for path in (SKETCH, IMAGE):
                record(f"source={path.relative_to(ROOT)} sha256={hashlib.sha256(path.read_bytes()).hexdigest()}\n")
            record(f"PASS source_image bytes={len(packed)} packed_sha256={recorded_hash}\n")
            record("compile=" + json.dumps(command, ensure_ascii=False) + "\n")
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            record(result.stdout + result.stderr)
            if result.returncode:
                raise RuntimeError("네이티브 ASan/UBSan 빌드 실패")
            scenarios = ("normal_lf", "normal_crlf", "allocation", "alignment", "frequency", "frequency_change",
                         "core_change", "invalid_core", "short_delay", "missing_sign", "delay_timeout")
            for scenario in scenarios:
                result = subprocess.run([str(BUILD / "harness"), scenario], capture_output=True, text=True, timeout=20)
                record(result.stdout + result.stderr)
                if result.returncode:
                    raise RuntimeError("하네스 실패: " + scenario)
                for line in result.stdout.splitlines():
                    if not line.startswith("GRAY_GAME_DONE status=pass"):
                        continue
                    fields = dict(item.split("=", 1) for item in line.split()[1:])
                    parts = ("power_on_us", "clear_us", "draw_us", "power_off_us")
                    if sum(int(fields[name]) for name in parts) != int(fields["screen_elapsed_us"]):
                        raise RuntimeError("화면 처리 시간 구간 합 불일치")
            record(f"PASS all scenarios={len(scenarios)} source_pixels_per_success=64000 expanded_pixels=256000 margins=128000 ASan=pass UBSan=pass device_access=none\n")
            return 0
        except (OSError, AttributeError, RuntimeError, subprocess.SubprocessError) as error:
            record(f"FAIL {error}\n")
            return 1


if __name__ == "__main__":
    sys.exit(main())
