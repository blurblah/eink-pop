#!/usr/bin/env python3
"""제조사 원본과 작업 파일의 실제 busy_delay 함수를 C로 검증한다.

프로젝트 루트에서 python3 tests/test_busy_delay.py로 실행한다.
Python 표준 라이브러리, Git, clang만 사용하며 장치에 접근하지 않는다.
생성 C·실행 파일은 .local/build/, 원본 결과는 artifacts/logs/에 저장한다.
"""

import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = ROOT / "repos/LilyGo-EPD47"
BASE_COMMIT = "50224221fb81a8fcf9068532374b775af84a38d4"
SOURCE_PATH = "src/ed047tc1.c"
BUILD = ROOT / ".local/build/busy-delay-host-qa"
LOG = ROOT / "artifacts/logs/bw-delta-busy-delay-host-qa.log"


HARNESS_PREFIX = r"""
#include <inttypes.h>
#include <setjmp.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

_Static_assert(sizeof(int) == 4, "SDK 카운터와 같은 32비트 int가 필요하다.");

static jmp_buf read_limit_exit;
static uint32_t counter_start;
static uint32_t requested_cycles;
static uint32_t last_elapsed;
static uint32_t reads;
static uint32_t read_limit;
static unsigned schedule;
static const char *schedule_names[] = {"unit", "exact", "overshoot", "stalled"};

/* SDK처럼 signed int를 반환하며, 각 호출의 카운터 비트는 보존한다. */
static int mock_ccount(void)
{
    if (reads >= read_limit) {
        longjmp(read_limit_exit, 1);
    }
    uint32_t read_index = reads++;
    if (schedule == 3 || read_index == 0 || requested_cycles == 0) {
        last_elapsed = 0;
    } else if (schedule == 0) {
        last_elapsed = read_index - 1;
    } else if (read_index <= 3) {
        last_elapsed = 0;
    } else if (read_index == 4) {
        last_elapsed = requested_cycles - 1;
    } else {
        last_elapsed = requested_cycles + (schedule == 2 ? 37U : 0U);
    }
    uint32_t bits = counter_start + last_elapsed;
    int value;
    memcpy(&value, &bits, sizeof(value));
    return value;
}

#define IRAM_ATTR
#define XTHAL_GET_CCOUNT() mock_ccount()
"""

HARNESS_SUFFIX = r"""
static unsigned run_case(uint32_t start, uint32_t cycles, unsigned mode)
{
    counter_start = start;
    requested_cycles = cycles;
    schedule = mode;
    last_elapsed = 0;
    reads = 0;
    read_limit = mode == 0 ? cycles + 8U : 12U;

    int exhausted = setjmp(read_limit_exit);
    if (!exhausted) {
        busy_delay(cycles);
    }
    uint32_t expected = cycles + ((mode == 2 && cycles != 0) ? 37U : 0U);
    const char *result = mode == 3 ? (exhausted ? "pass" : "missing_bound")
        : exhausted ? "read_limit"
        : last_elapsed < cycles ? "early"
        : last_elapsed != expected ? "unexpected_return"
        : "pass";
    printf("{\"start\":\"0x%08" PRIx32 "\",\"cycles\":%" PRIu32
           ",\"schedule\":\"%s\",\"elapsed\":%" PRIu32
           ",\"reads\":%" PRIu32 ",\"read_limit_hit\":%d,\"result\":\"%s\"}\n",
           start, cycles, schedule_names[mode], last_elapsed, reads, exhausted, result);
    return strcmp(result, "pass") != 0;
}

int main(void)
{
    const uint32_t delays[] = {0, 1, 240, 2400, 24000, 120000};
    const uint32_t starts[] = {
        0, 1, UINT32_C(0x7fffff00), UINT32_C(0x7fffffff),
        UINT32_C(0x80000000), UINT32_C(0x80000001),
        UINT32_C(0x90000000), UINT32_C(0xffffff00), UINT32_MAX
    };
    unsigned failures = 0;
    for (unsigned d = 0; d < sizeof(delays) / sizeof(delays[0]); ++d) {
        for (unsigned mode = 0; mode < 3; ++mode) {
            for (unsigned s = 0; s < sizeof(starts) / sizeof(starts[0]); ++s) {
                failures += run_case(starts[s], delays[d], mode);
            }
            /* 요청값에 정확히 도달할 때 부호 경계·순환 경계를 넘는 경우. */
            failures += run_case(UINT32_C(0x80000000) - delays[d], delays[d], mode);
            failures += run_case(UINT32_C(0) - delays[d], delays[d], mode);
        }
    }
    /* 카운터가 정지해도 하네스의 읽기 상한이 실행을 끝내는지 확인한다. */
    failures += run_case(0, 240, 3);
    return failures ? 1 : 0;
}
"""


def extract_function(source):
    matches = list(re.finditer(
        r"\bvoid\s+IRAM_ATTR\s+busy_delay\s*\(\s*uint32_t\s+cycles\s*\)\s*\{",
        source,
    ))
    if len(matches) != 1:
        raise AssertionError("busy_delay 정의를 정확히 한 개 찾지 못했습니다.")
    match = matches[0]
    depth = 1
    end = match.end()
    while end < len(source) and depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    if depth:
        raise AssertionError("busy_delay 함수의 닫는 중괄호가 없습니다.")
    return source[match.start():end]


def run(command, timeout=30):
    return subprocess.run(
        command, cwd=ROOT, text=True, capture_output=True, timeout=timeout,
    )


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    BUILD.mkdir(parents=True, exist_ok=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    evidence = []
    try:
        clang = shutil.which("clang")
        check(clang is not None, "clang을 찾을 수 없습니다.")
        version = run([clang, "--version"])
        check(version.returncode == 0, "clang 버전을 확인하지 못했습니다.")
        evidence.append(version.stdout)

        original = run([
            "git", "-C", str(REPOSITORY), "show", f"{BASE_COMMIT}:{SOURCE_PATH}",
        ])
        check(original.returncode == 0, f"기준 소스를 읽지 못했습니다: {original.stderr}")
        sources = {
            "original": original.stdout,
            "fixed": (REPOSITORY / SOURCE_PATH).read_text(),
        }
        results = {}
        for name, source in sources.items():
            function = extract_function(source)
            evidence.append(
                f"\n[{name}] 기준 커밋={BASE_COMMIT}\n"
                f"함수 SHA-256={hashlib.sha256(function.encode()).hexdigest()}\n"
                f"{function}\n"
            )
            c_path = BUILD / f"{name}.c"
            binary = BUILD / name
            c_path.write_text(HARNESS_PREFIX + "\n" + function + "\n" + HARNESS_SUFFIX)
            command = [
                clang, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                "-Wno-sign-compare", str(c_path), "-o", str(binary),
            ]
            compiled = run(command)
            evidence.append(f"컴파일 명령={json.dumps(command, ensure_ascii=False)}\n")
            evidence.extend((compiled.stdout, compiled.stderr))
            check(compiled.returncode == 0, f"{name} C 하네스 컴파일 실패")
            executed = run([str(binary)], timeout=10)
            evidence.extend((executed.stdout, executed.stderr))
            check(executed.returncode in (0, 1), f"{name} 하네스 비정상 종료")
            cases = [json.loads(line) for line in executed.stdout.splitlines()]
            check(len(cases) == 199, f"{name} 검증 사례 수 불일치: {len(cases)}")
            stalled = next(case for case in cases if case["schedule"] == "stalled")
            check(stalled["result"] == "pass" and stalled["read_limit_hit"] == 1,
                  f"{name} 정지 카운터의 읽기 상한 검증 실패")
            results[name] = cases
            if name == "fixed":
                check(executed.returncode == 0, "수정 함수가 경계 검증에 실패했습니다.")
                check(all(case["result"] == "pass" for case in cases), "수정 함수 실패 사례 발견")

        # 기준 함수의 알려진 결함이 실제 함수 실행에서도 재현돼야 한다.
        for start in ("0x80000000", "0x90000000", "0xffffffff"):
            case = next(case for case in results["original"] if
                        case["start"] == start and case["cycles"] == 120000 and
                        case["schedule"] == "unit")
            check(case["result"] == "early" and case["elapsed"] == 0,
                  f"기준 함수의 조기 종료를 재현하지 못했습니다: {start}")
        zero_cases = [case for case in results["fixed"] if case["cycles"] == 0]
        check(all(case["reads"] == 2 for case in zero_cases), "0cycle 요청이 즉시 반환하지 않았습니다.")

        original_early = sum(case["result"] == "early" for case in results["original"])
        summary = (
            f"통과: 실제 원본 함수 {len(results['original'])}개 사례 중 "
            f"조기 종료 {original_early}개 재현.\n"
            f"통과: 실제 수정 함수 {len(results['fixed'])}개 경계·반환 검증.\n"
            "검증 범위: signed 32비트 반환, 부호 경계, 32비트 순환, "
            "0/1/240/2400/24000/120000cycle, 요청 직전·도달·초과, 읽기 상한.\n"
            "이 결과는 호스트 정수·제어 흐름 검증이며 실제 장치 지연·전압 계측이 아닙니다."
        )
        evidence.append("\n" + summary + "\n")
        print(summary)
    except (AssertionError, OSError, subprocess.SubprocessError, ValueError) as error:
        evidence.append(f"\n검증 실패: {error}\n")
        print(f"검증 실패: {error}", file=sys.stderr)
        return 1
    finally:
        LOG.write_text("".join(evidence))
        print(f"원본 결과: {LOG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
