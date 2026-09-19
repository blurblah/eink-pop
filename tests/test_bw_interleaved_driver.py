#!/usr/bin/env python3
"""실제 C 드라이버의 교차 출력·행 파이프라인·DMA 대기를 호스트에서 검증한다.

Python 표준 라이브러리와 clang을 사용한다. 장치·Git을 변경하지 않는다.
태스크는 생산자 다음 소비자 순서로 실행하므로 실제 RTOS 동시성·큐 역압은
검증하지 않는다. 생성물과 로그는 기존 제외 디렉터리에만 기록한다.
"""

import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT / "repos/LilyGo-EPD47"
BASE = "86be1ba"
BUILD = ROOT / ".local/build/bw_interleaved-driver-host-qa"
LOG = ROOT / "artifacts/logs/bw-interleaved-driver-host-qa.log"


PREFIX = r"""
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <limits.h>
#include <setjmp.h>

#define IRAM_ATTR
#define USER_I2S_REG 0
#define EPD_WIDTH 960
#define EPD_HEIGHT 540
#define EPD_LINE_BYTES 240
#define ESP_LOGW(...) ((void)0)
#define portMAX_DELAY UINT32_MAX
#define pdPASS 1
#define pdTRUE 1
#define min(a,b) ((a) < (b) ? (a) : (b))
typedef uint32_t TickType_t;
typedef struct { int live, given; } Semaphore;
typedef Semaphore *SemaphoreHandle_t;
typedef struct { int live, notified; unsigned slot; void (*entry)(void *); void *argument; } Task;
typedef Task *TaskHandle_t;
typedef void *QueueHandle_t;

static const char *case_name = "초기화";
static void require(bool condition, const char *message) {
    if (!condition) { fprintf(stderr, "FAIL %s: %s\n", case_name, message); exit(1); }
}
"""

GLOBALS = r"""
static uint8_t lut_storage[65536], baseline_lut[65536];
static uint8_t *conversion_lut = lut_storage;
static int queue_identity;
static QueueHandle_t output_queue = &queue_identity;
static uint32_t skipping;
static _Alignas(4) uint8_t buffer[248], pending_bytes[248], delivered_bytes[248];
static _Alignas(4) uint8_t input_storage[540 * 480 + 64];
static uint8_t saved_input[540 * 480];
static uint8_t *pixels = input_storage + 32;
static uint8_t queued_rows[540][480];
static unsigned queue_head, queue_tail, queue_count, sent_rows, received_rows, queue_resets;
static Semaphore semaphores[2];
static Task tasks[64];
static unsigned sem_calls, sem_live, task_calls, task_live, task_deletes;
static unsigned task_starts, notifications, notify_takes;
static Task *running_task;
static jmp_buf gate_probe;
static bool probing_gate;
static unsigned fail_sem, fail_task, bw_lut_resets;
static unsigned api_wait10, api_wait5, producer_wait5;
static int task_kind = -1;
static unsigned dma_remaining, dma_polls, premature_end_ops;
static bool allow_premature_end, phase_active;
static unsigned cfg_calls, pulse_calls, scans, ends, active_rows;
static int row_cursor;
static uint32_t last_output_time;
static Rect_t expected_area;
static DrawMode_t current_mode, legacy_mode;
static unsigned current_stage;
static bool interleaved_case = true;
static epd_config_register_t config_reg;

static bool i2s_is_busy(void);
static bool mock_output_done(void);
static uint8_t *epd_get_current_buffer(void);
static void epd_switch_buffer(void);
static void epd_output_row(uint32_t time);
static void epd_skip(void);
static void epd_start_frame(void);
static void push_cfg(epd_config_register_t *config);
static void pulse_ckv_us(uint16_t high, uint16_t low, bool wait);
static SemaphoreHandle_t xSemaphoreCreateBinary(void);
static int xSemaphoreTake(SemaphoreHandle_t semaphore, uint32_t ticks);
static int xSemaphoreGive(SemaphoreHandle_t semaphore);
static void vSemaphoreDelete(SemaphoreHandle_t semaphore);
static int xTaskCreatePinnedToCore(void (*entry)(void *), const char *name,
                                  uint32_t stack, void *argument, unsigned priority,
                                  TaskHandle_t *handle, int core);
static void vTaskDelete(TaskHandle_t task);
static void xTaskNotifyGive(TaskHandle_t task);
static uint32_t ulTaskNotifyTake(int clear, uint32_t ticks);
static void vTaskDelay(TickType_t ticks);
static int xQueueSendToBack(QueueHandle_t queue, const void *row, uint32_t ticks);
static int xQueueReceive(QueueHandle_t queue, void *row, uint32_t ticks);
static void reset_bw_lut(uint8_t *lut);
"""

MOCKS = r"""
static void reset_bw_lut(uint8_t *lut) {
    ++bw_lut_resets;
    actual_reset_bw_lut(lut);
}
static bool i2s_is_busy(void) {
    ++dma_polls;
    if (!dma_remaining) return false;
    require(memcmp(buffer, pending_bytes, sizeof(buffer)) == 0,
            "DMA 완료 전에 단일 버퍼가 변경됨");
    if (--dma_remaining == 0) {
        memcpy(delivered_bytes, pending_bytes, sizeof(buffer));
        return false;
    }
    return true;
}
static bool mock_output_done(void) { return !i2s_is_busy(); }
static uint8_t *epd_get_current_buffer(void) { return (uint8_t *)i2s_get_current_buffer(); }
static void epd_switch_buffer(void) {} /* 실제 S3 단일 버퍼 분기도 무동작이다. */

static unsigned expected_command(unsigned pixel) {
    if (interleaved_case) {
        return current_mode == WHITE_ON_BLACK ? (pixel == 1 ? 2 : 0)
                                             : (pixel == 0 ? 1 : 0);
    }
    if (current_mode == WHITE_ON_BLACK) return pixel > current_stage ? 2 : 0;
    unsigned active = pixel < 15U - current_stage;
    return active ? (current_mode == BLACK_ON_WHITE ? 1 : 2) : 0;
}
static void check_latched_row(int row, uint32_t time) {
    if (row < 0 || row >= EPD_HEIGHT) return;
    bool inside = row >= expected_area.y && row < expected_area.y + expected_area.height;
    if (inside) {
        uint32_t expected_time = current_mode == WHITE_ON_BLACK
            ? baseline_contrast_cycles_4_white[current_stage]
            : baseline_contrast_cycles_4[current_stage];
        require(time == expected_time, "유효 행·마지막 행의 주사 시간 불일치");
        ++active_rows;
    }
    for (unsigned x = 0; x < EPD_WIDTH; ++x) {
        unsigned command = (delivered_bytes[x / 4] >> ((x % 4) * 2)) & 3;
        unsigned expected = 0;
        if (inside) {
            unsigned offset = (unsigned)(row - expected_area.y) * 480 + x / 2;
            unsigned pixel = (pixels[offset] >> ((x % 2) * 4)) & 15;
            expected = expected_command(pixel);
        }
        require(command == expected, "행 데이터·극성·NOP·S3 픽셀 순서 불일치");
    }
}
static void epd_output_row(uint32_t time) {
    while (i2s_is_busy()) ;
    last_output_time = time;
    if (phase_active) check_latched_row(row_cursor - 1, time);
    ++row_cursor;
    require(memcmp(buffer + EPD_LINE_BYTES, "\0\0\0\0\0\0\0\0", 8) == 0,
            "DMA 더미 바이트 변경");
    memcpy(pending_bytes, buffer, sizeof(buffer));
    dma_remaining = 3;
}
static void epd_skip(void) {
    if (phase_active) {
        int row = row_cursor - 1;
        require(row < expected_area.y || row >= expected_area.y + expected_area.height,
                "유효 행을 건너뜀");
    }
    ++row_cursor;
}
static void epd_start_frame(void) {
    while (i2s_is_busy()) ;
    require(!phase_active, "이전 프레임이 종료되지 않음");
    phase_active = true;
    row_cursor = 0;
    active_rows = cfg_calls = pulse_calls = 0;
    ++scans;
}
static void control_change(void) {
    if (dma_remaining) {
        ++premature_end_ops;
        require(allow_premature_end, "DMA 완료 전에 프레임 종료 제어 변경");
    }
}
static void push_cfg(epd_config_register_t *config) {
    require(config == &config_reg, "설정 레지스터 주소");
    control_change();
    if (phase_active) ++cfg_calls;
}
static void pulse_ckv_us(uint16_t high, uint16_t low, bool wait) {
    control_change();
    require(high == 1 && low == 1 && wait, "프레임 종료 펄스 변경");
    if (phase_active && ++pulse_calls == 2) {
        require(cfg_calls == 2 && active_rows == (unsigned)expected_area.height,
                "마지막 행 latch·프레임 종료 누락");
        require(row_cursor == EPD_HEIGHT +
                (expected_area.y + expected_area.height == EPD_HEIGHT ? 1 : 0),
                "영역 끝의 파이프라인 flush 횟수");
        phase_active = false;
        ++ends;
    }
}

static SemaphoreHandle_t xSemaphoreCreateBinary(void) {
    unsigned slot = sem_calls++;
    require(slot < 2, "세마포어를 주사마다 재생성함");
    if (fail_sem == slot + 1) return NULL;
    semaphores[slot].live = 1;
    ++sem_live;
    return &semaphores[slot];
}
static int xSemaphoreTake(SemaphoreHandle_t semaphore, uint32_t ticks) {
    require(ticks == portMAX_DELAY && semaphore->live && semaphore->given,
            "완료되지 않은 태스크의 세마포어");
    semaphore->given = 0;
    return pdTRUE;
}
static int xSemaphoreGive(SemaphoreHandle_t semaphore) {
    require(semaphore->live && !semaphore->given, "중복 완료 신호");
    if (task_kind == 1) require(!phase_active && !dma_remaining, "소비자 조기 완료");
    semaphore->given = 1;
    return pdTRUE;
}
static void vSemaphoreDelete(SemaphoreHandle_t semaphore) {
    require(semaphore != NULL && semaphore->live, "세마포어 중복 삭제");
    semaphore->live = 0;
    --sem_live;
}
static int xQueueSendToBack(QueueHandle_t queue, const void *row, uint32_t ticks) {
    require(queue == output_queue && ticks == portMAX_DELAY && task_kind == 0,
            "생산자 큐 사용 오류");
    require(queue_tail < 540, "모의 큐의 읽기 상한 초과");
    /* FreeRTOS처럼 포인터가 아니라 480바이트 데이터를 복사한다. */
    memcpy(queued_rows[queue_tail++], row, 480);
    ++queue_count; ++sent_rows;
    return pdTRUE;
}
static int xQueueReceive(QueueHandle_t queue, void *row, uint32_t ticks) {
    require(queue == output_queue && ticks == portMAX_DELAY && task_kind == 1 && queue_count,
            "큐 부족·잘못된 소비 순서");
    require(memcmp(queued_rows[queue_head], pixels + queue_head * 480, 480) == 0,
            "생산자 stride·행 순서·복사 데이터 불일치");
    memcpy(row, queued_rows[queue_head++], 480);
    --queue_count; ++received_rows;
    if (!queue_count) queue_head = queue_tail = 0;
    return pdTRUE;
}
static void run_task(Task *task) {
    unsigned slot = task->slot;
    OutputParams *params = task->argument;
    unsigned scan = slot / 2;
    unsigned expected_stage = interleaved_case ? scan / 2 : scan;
    DrawMode_t expected_mode = interleaved_case
        ? (scan % 2 == 0 ? WHITE_ON_BLACK : BLACK_ON_WHITE) : legacy_mode;
    require(params->frame == (int32_t)expected_stage && params->mode == expected_mode &&
            params->bw_interleaved == interleaved_case, "stage·극성 교차 순서·모드 전달");
    require(params->data_ptr == pixels && memcmp(&params->area, &expected_area, sizeof(Rect_t)) == 0,
            "태스크에 전달된 입력·영역");
    require(slot % 2 != 0 || queue_count == 0, "이전 주사의 큐 잔여");
    ++task_starts;
    running_task = task;
    task_kind = slot % 2;
    current_stage = expected_stage; current_mode = expected_mode;
    task->entry(task->argument);
    task_kind = -1;
    running_task = NULL;
    if (slot % 2) require(queue_count == 0, "소비 뒤 큐 잔여");
}
static int xTaskCreatePinnedToCore(void (*entry)(void *), const char *name,
                                  uint32_t stack, void *argument, unsigned priority,
                                  TaskHandle_t *handle, int core) {
    (void)name;
    unsigned slot = task_calls++;
    require(slot < 64, "주사·태스크 상한 초과");
    require(stack == 8192 && priority == 10 && core == (int)(slot % 2), "태스크 설정 변경");
    void (*expected_entry)(void *) = interleaved_case
        ? (slot % 2 == 0 ? bw_provide_out_task : bw_feed_display_task)
        : (slot % 2 == 0 ? (void (*)(void *))provide_out : (void (*)(void *))feed_display);
    require(entry == expected_entry, "실제 생산자·소비자 또는 gate 함수 미사용");
    if (fail_task == slot + 1) return 0;
    tasks[slot] = (Task){.live=1, .slot=slot, .entry=entry, .argument=argument};
    ++task_live; *handle = &tasks[slot];
    if (!interleaved_case) run_task(&tasks[slot]);
    return pdPASS;
}
static void xTaskNotifyGive(TaskHandle_t task) {
    require(task && task->live && !task->notified && task_live == 2,
            "두 작업 생성 전 통지 또는 중복 통지");
    require(task->slot == notifications, "생산자·소비자 시작 통지 순서");
    task->notified = 1; ++notifications;
    run_task(task);
}
static uint32_t ulTaskNotifyTake(int clear, uint32_t ticks) {
    require(clear == pdTRUE && ticks == portMAX_DELAY, "시작 gate 대기 조건");
    if (probing_gate) longjmp(gate_probe, 1);
    require(running_task && running_task->notified, "시작 통지 전 실제 작업 실행");
    ++notify_takes;
    return 1;
}
static void vTaskDelete(TaskHandle_t task) {
    require(task != NULL && task->live, "태스크 중복·잘못된 삭제");
    task->live = 0; --task_live; ++task_deletes;
}
static void vTaskDelay(TickType_t ticks) {
    if (task_kind == -1) {
        if (ticks == 10) ++api_wait10;
        else { require(ticks == 5, "API 주사 간 대기 변경"); ++api_wait5; }
    } else if (task_kind == 0 && ticks == 5) ++producer_wait5;
    else require(ticks == portMAX_DELAY, "태스크 종료 대기 변경");
}
"""

TESTS = r"""
static void reset_case(const char *name, Rect_t area) {
    case_name = name; expected_area = area;
    queue_head = queue_tail = queue_count = sent_rows = received_rows = queue_resets = 0;
    sem_calls = sem_live = task_calls = task_live = task_deletes = 0;
    task_starts = notifications = notify_takes = 0;
    running_task = NULL; probing_gate = false;
    fail_sem = fail_task = bw_lut_resets = 0;
    api_wait10 = api_wait5 = producer_wait5 = 0;
    dma_remaining = dma_polls = premature_end_ops = 0;
    cfg_calls = pulse_calls = scans = ends = active_rows = 0;
    skipping = 0; row_cursor = 0; task_kind = -1;
    phase_active = allow_premature_end = false; interleaved_case = true;
    conversion_lut = lut_storage; output_queue = &queue_identity;
    memset(semaphores, 0, sizeof(semaphores)); memset(tasks, 0, sizeof(tasks));
    memset(buffer, 0, sizeof(buffer)); memset(pending_bytes, 0, sizeof(pending_bytes));
    memset(delivered_bytes, 0, sizeof(delivered_bytes));
    memset(lut_storage, 0xA5, sizeof(lut_storage));
    memset(input_storage, 0xA5, sizeof(input_storage));
}
static void put_pixel(unsigned x, unsigned y, unsigned value) {
    uint8_t *byte = pixels + y * 480 + x / 2;
    if (x % 2) *byte = (*byte & 0x0F) | (value << 4);
    else *byte = (*byte & 0xF0) | value;
}
static void input_pattern(unsigned pattern) {
    memset(pixels, 0x22, (size_t)expected_area.height * 480);
    for (unsigned y = 0; y < (unsigned)expected_area.height; ++y) {
        for (unsigned x = 0; x < EPD_WIDTH; ++x) {
            unsigned value = pattern < 3 ? pattern : (x + 3 * y) % 16;
            put_pixel(x, y, value);
        }
    }
}
static void input_unchanged(void) {
    size_t bytes = (size_t)expected_area.height * 480;
    require(memcmp(pixels, saved_input, bytes) == 0, "입력 버퍼 변경");
    for (unsigned i = 0; i < 32; ++i) {
        require(input_storage[i] == 0xA5 && pixels[bytes + i] == 0xA5, "입력 버퍼 가드 손상");
    }
}
static void run_pipeline(void) {
    unsigned expected_scans = interleaved_case ? 30 : 15;
    memcpy(saved_input, pixels, (size_t)expected_area.height * 480);
    if (interleaved_case) require(epd_draw_bw_interleaved(expected_area, pixels), "유효 API 입력 거부");
    else epd_draw_image(expected_area, pixels, legacy_mode);
    require(scans == expected_scans && ends == scans, "주사·프레임 종료 수");
    require(api_wait10 == 1 && api_wait5 == scans && producer_wait5 == scans,
            "진입 10tick·각 주사 5tick 대기 불일치");
    require(task_calls == scans * 2 && task_deletes == task_calls && !task_live && !sem_live,
            "태스크·세마포어 수명");
    require(task_starts == task_calls && notifications == (interleaved_case ? task_calls : 0) &&
            notify_takes == notifications, "생성 후 시작 gate·통지 수");
    require(sent_rows == scans * (unsigned)expected_area.height && received_rows == sent_rows &&
            !queue_count && !dma_remaining && !phase_active, "종료 후 큐·DMA·프레임 잔여");
    require(bw_lut_resets == (interleaved_case ? 1U : 0U), "공유 LUT 초기화 횟수");
    input_unchanged();
    printf("PASS pipeline case=%s scans=%u rows=%u delays5=%u queue=empty dma=idle\n",
           case_name, scans, received_rows, api_wait5);
}
static void test_lut(void) {
    case_name = "LUT 전체 조합";
    actual_reset_bw_lut(lut_storage);
    for (unsigned word = 0; word < 65536; ++word) {
        for (unsigned position = 0; position < 4; ++position) {
            unsigned pixel = (word >> (position * 4)) & 15;
            unsigned command = (lut_storage[word] >> (position * 2)) & 3;
            require(command == (pixel < 2 ? pixel + 1 : 0), "3상태 LUT 매핑");
        }
    }
    DrawMode_t modes[] = {BLACK_ON_WHITE, WHITE_ON_BLACK, WHITE_ON_WHITE};
    for (unsigned m = 0; m < 3; ++m) {
        reset_lut(lut_storage, modes[m]); baseline_reset_lut(baseline_lut, modes[m]);
        require(memcmp(lut_storage, baseline_lut, 65536) == 0, "기존 LUT 초기화 회귀");
        for (uint8_t stage = 0; stage < 15; ++stage) {
            update_LUT(lut_storage, stage, modes[m]);
            baseline_update_LUT(baseline_lut, stage, modes[m]);
            require(memcmp(lut_storage, baseline_lut, 65536) == 0, "기존 3모드·15단계 회귀");
        }
    }
    require(memcmp(contrast_cycles_4, baseline_contrast_cycles_4, sizeof(contrast_cycles_4)) == 0 &&
            memcmp(contrast_cycles_4_white, baseline_contrast_cycles_4_white, sizeof(contrast_cycles_4_white)) == 0,
            "기본 극성별 시간 배열 변경");
    puts("PASS LUT words=65536 positions=4 reserved=NOP legacy_modes=3 stages=15 timings=unchanged");
}
static void no_access(void) {
    require(!sem_calls && !task_calls && !scans && !dma_polls && !bw_lut_resets &&
            !api_wait10 && !api_wait5 && !queue_count, "무효 입력에서 자원·출력 접근");
    for (unsigned i = 0; i < sizeof(lut_storage); ++i)
        require(lut_storage[i] == 0xA5, "무효 입력이 LUT 변경");
}
static void test_invalid(void) {
    Rect_t invalid[] = {
        {-1,238,960,64}, {1,238,960,64}, {0,238,959,64}, {0,238,961,64},
        {0,-1,960,64}, {0,540,960,1}, {0,238,960,0}, {0,238,960,-1},
        {0,539,960,2}, {0,0,960,541}, {0,INT32_MAX,960,1},
        {0,1,960,INT32_MAX}, {0,INT32_MIN,960,INT32_MAX}
    };
    for (unsigned i = 0; i < sizeof(invalid) / sizeof(invalid[0]); ++i) {
        reset_case("무효 영역", (Rect_t){0,238,960,64});
        require(!epd_draw_bw_interleaved(invalid[i], pixels), "무효 영역 허용"); no_access();
    }
    for (unsigned offset = 0; offset < 4; ++offset) {
        reset_case("NULL·정렬", (Rect_t){0,238,960,64});
        require(!epd_draw_bw_interleaved(expected_area, offset ? pixels + offset : NULL),
                "NULL·비정렬 입력 허용"); no_access();
    }
    for (unsigned missing = 0; missing < 2; ++missing) {
        reset_case("초기화 누락", (Rect_t){0,238,960,64});
        if (missing) output_queue = NULL; else conversion_lut = NULL;
        require(!epd_draw_bw_interleaved(expected_area, pixels), "초기화 없이 출력"); no_access();
    }
    puts("PASS invalid_inputs cases=19 resources=untouched");
}
static void test_failures(void) {
    for (unsigned kind = 0; kind < 2; ++kind) {
        for (unsigned failure = 1; failure <= 2; ++failure) {
            reset_case("생성 실패 정리", (Rect_t){0,238,960,64}); input_pattern(3);
            if (kind) fail_task = failure; else fail_sem = failure;
            require(!epd_draw_bw_interleaved(expected_area, pixels), "생성 실패를 성공으로 반환");
            require(!sem_live && !task_live && !queue_count && !scans, "실패 뒤 자원·큐 잔여");
            require(!task_starts && !notifications && !notify_takes && !sent_rows && !received_rows,
                    "생성 실패에서 params·큐 본문 접근");
            require(!queue_resets, "통지 전 실패에서 큐 변경");
        }
    }
    puts("PASS creation_failures cases=4 resources=released queue=empty");
}
static void test_task_gate(void) {
    reset_case("시작 gate 선행", (Rect_t){0,238,960,64});
    void (*wrappers[])(void *) = {bw_provide_out_task, bw_feed_display_task};
    for (unsigned i = 0; i < 2; ++i) {
        probing_gate = true;
        if (setjmp(gate_probe) == 0) {
            wrappers[i](NULL); /* gate 전 역참조가 있으면 ASan이 실패한다. */
            require(false, "통지 없는 wrapper가 반환됨");
        }
        probing_gate = false;
        require(!sent_rows && !received_rows && !scans && !sem_calls && !task_starts,
                "gate 대기 전에 실제 본문 실행");
    }
    puts("PASS task_gate wrappers=2 null_params_before_notification=untouched");
}
static void start_fake_dma(void) {
    memcpy(pending_bytes, buffer, sizeof(buffer)); dma_remaining = 3;
}
static void test_lifetimes(void) {
    reset_case("원본 시간 폭 재현", (Rect_t){0,238,960,64});
    uint32_t duration = 300;
    baseline_skip_row(duration);
    require(last_output_time == 44, "원본 300→44를 재현하지 못함");
    reset_case("수정 시간 폭", (Rect_t){0,238,960,64});
    skip_row(duration); require(last_output_time == 300, "수정 후 300 전달 실패");
    reset_case("원본 버퍼 반환", (Rect_t){0,238,960,64}); start_fake_dma();
    require(baseline_i2s_get_current_buffer() == buffer && dma_remaining == 3,
            "원본 조기 버퍼 반환을 재현하지 못함");
    reset_case("수정 버퍼 반환", (Rect_t){0,238,960,64}); start_fake_dma();
    require(i2s_get_current_buffer() == buffer && !dma_remaining && dma_polls >= 3,
            "수정 getter가 DMA 완료 전에 반환");
    reset_case("원본 프레임 종료", (Rect_t){0,238,960,64}); start_fake_dma();
    allow_premature_end = true; baseline_epd_end_frame();
    require(premature_end_ops == 4 && dma_remaining == 3, "원본 조기 종료 제어를 재현하지 못함");
    reset_case("수정 프레임 종료", (Rect_t){0,238,960,64}); start_fake_dma();
    epd_end_frame(); require(!dma_remaining && !premature_end_ops, "프레임 종료 DMA 대기 누락");
    puts("PASS baseline_regressions truncation=300_to_44 early_buffer=1 early_end=1 fixed=pass");
}
int main(void) {
    const uint16_t little_endian = 1;
    require(*(const uint8_t *)&little_endian == 1, "S3처럼 little-endian 호스트가 필요함");
    test_lut(); test_invalid(); test_task_gate(); test_failures(); test_lifetimes();
    for (unsigned pattern = 0; pattern < 4; ++pattern) {
        reset_case("64행 전체 패턴", (Rect_t){0,238,960,64}); input_pattern(pattern); run_pipeline();
    }
    for (unsigned frame = 1; frame < 12; ++frame) {
        reset_case("이동 마스크", (Rect_t){0,238,960,64});
        memset(pixels, 0x22, 64 * 480);
        unsigned old_x = 184 + 48 * (frame - 1), new_x = 184 + 48 * frame;
        for (unsigned y = 0; y < 64; ++y) {
            for (unsigned x = old_x; x < old_x + 48; ++x) put_pixel(x, y, 1);
            for (unsigned x = new_x + 16; x < new_x + 64; ++x) put_pixel(x, y, 0);
        }
        run_pipeline();
    }
    Rect_t boundaries[] = {{0,0,960,1}, {0,539,960,1}, {0,476,960,64}, {0,0,960,540}};
    for (unsigned i = 0; i < 4; ++i) {
        reset_case("패널 상하 경계", boundaries[i]); input_pattern(3); run_pipeline();
    }
    DrawMode_t modes[] = {BLACK_ON_WHITE, WHITE_ON_BLACK, WHITE_ON_WHITE};
    for (unsigned i = 0; i < 3; ++i) {
        reset_case("기존 API 경로", (Rect_t){0,238,960,64});
        interleaved_case = false; legacy_mode = modes[i]; input_pattern(3); run_pipeline();
    }
    puts("PASS all pipeline_cases=22 device_access=none RTOS_concurrency=not_tested");
    return 0;
}
"""


def functions(source, name):
    """선언을 제외하고 지정 C 함수의 실제 정의를 추출한다."""
    result = []
    pattern = re.compile(r"^[^\n;{}]*\b" + re.escape(name) + r"\s*\([^;{}]*?\)\s*\{", re.M)
    for match in pattern.finditer(source):
        depth = 1
        end = match.end()
        while end < len(source) and depth:
            depth += (source[end] == "{") - (source[end] == "}")
            end += 1
        if depth:
            raise ValueError(f"닫히지 않은 함수: {name}")
        result.append(source[match.start():end])
    if not result:
        raise ValueError(f"함수 정의를 찾지 못함: {name}")
    return result


def definition(source, name):
    found = functions(source, name)
    if len(found) != 1:
        raise ValueError(f"함수 정의가 유일하지 않음: {name}")
    return found[0]


def renamed(body, name):
    return re.sub(r"\b" + re.escape(name) + r"\b", "baseline_" + name, body)


def source_at_base(name):
    return subprocess.run(["git", "-C", str(REPO), "show", f"{BASE}:src/{name}"],
                          check=True, capture_output=True, text=True, timeout=15).stdout


def main():
    BUILD.mkdir(parents=True, exist_ok=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    evidence = []
    try:
        compiler = shutil.which("clang")
        if not compiler:
            raise RuntimeError("clang이 없습니다. 도구를 설치하지 않습니다.")
        paths = {name: REPO / "src" / name for name in
                 ("epd_driver.c", "epd_driver.h", "ed047tc1.c", "i2s_data_bus.c")}
        sources = {name: path.read_text() for name, path in paths.items()}
        original = {name: source_at_base(name) for name in
                    ("epd_driver.c", "ed047tc1.c", "i2s_data_bus.c")}
        driver, header = sources["epd_driver.c"], sources["epd_driver.h"]
        types = []
        for text, kind, name in ((header, "struct", "Rect_t"), (header, "enum", "DrawMode_t"),
                                 (driver, "struct", "OutputParams"),
                                 (sources["ed047tc1.c"], "struct", "epd_config_register_t")):
            match = re.search(r"typedef\s+" + kind + r"\s*\{[^{}]*\}\s*" + name + r"\s*;", text)
            if not match:
                raise ValueError(f"실제 타입 추출 실패: {name}")
            types.append(match.group())
        arrays = []
        for name in ("contrast_cycles_4", "contrast_cycles_4_white"):
            pattern = r"static const int32_t " + name + r"\[15\]\s*=\s*\{[^}]*\};"
            arrays.append(re.search(pattern, driver).group())
            arrays.append(renamed(re.search(pattern, original["epd_driver.c"]).group(), name))
        names = ("reset_bw_lut", "reset_lut", "update_LUT", "nibble_shift_buffer_right",
                 "calc_epd_input_4bpp", "write_row", "skip_row", "provide_out", "feed_display",
                 "bw_provide_out_task", "bw_feed_display_task",
                 "epd_draw_image", "epd_draw_bw_interleaved")
        bodies = [definition(driver, name) for name in names]
        bodies[0] = re.sub(r"\breset_bw_lut\b", "actual_reset_bw_lut", bodies[0])
        prototypes = [body[:body.index("{")].strip() + ";" for body in bodies]
        getter = functions(sources["i2s_data_bus.c"], "i2s_get_current_buffer")[1]
        old_getter = functions(original["i2s_data_bus.c"], "i2s_get_current_buffer")[1]
        end = definition(sources["ed047tc1.c"], "epd_end_frame")
        baseline_bodies = [renamed(definition(original["epd_driver.c"], name), name)
                           for name in ("reset_lut", "update_LUT", "skip_row")]
        baseline_bodies.extend((renamed(old_getter, "i2s_get_current_buffer"),
                                renamed(definition(original["ed047tc1.c"], "epd_end_frame"), "epd_end_frame")))
        harness = "\n\n".join((PREFIX, *types, *arrays, GLOBALS, *prototypes,
                                 end[:end.index("{")].strip() + ";",
                                 "#define output_done mock_output_done()\n" + getter + "\n#undef output_done",
                                 *bodies, end, *baseline_bodies, MOCKS, TESTS))
        target = BUILD / "driver_harness.c"
        target.write_text(harness)
        evidence.append("실제 C 드라이버 호스트 검증\n")
        for name, path in paths.items():
            evidence.append(f"source={name} sha256={hashlib.sha256(path.read_bytes()).hexdigest()}\n")
        evidence.append(f"baseline={BASE} harness_sha256={hashlib.sha256(harness.encode()).hexdigest()}\n")
        command = [compiler, "-std=c11", "-O1", "-g", "-fsanitize=address", "-fno-omit-frame-pointer",
                   "-Wall", "-Wextra", "-Werror", "-Wno-sign-compare", "-Wno-pointer-to-int-cast",
                   "-Wno-unused-parameter", str(target), "-o", str(BUILD / "driver_harness")]
        evidence.append("compile=" + json.dumps(command, ensure_ascii=False) + "\n")
        compiled = subprocess.run(command, capture_output=True, text=True, timeout=30)
        evidence.extend((compiled.stdout, compiled.stderr))
        if compiled.returncode:
            raise RuntimeError("실제 C 하네스 컴파일 실패")
        executed = subprocess.run([str(BUILD / "driver_harness")], capture_output=True, text=True, timeout=40)
        evidence.extend((executed.stdout, executed.stderr))
        print(executed.stdout, end="")
        if executed.returncode:
            raise RuntimeError("실제 C 하네스 검증 실패")
        limitation = "결정적 태스크·DMA 모의 검증입니다. 실제 RTOS 동시성·큐 역압·GPIO·전압·패널 효과는 미검증입니다.\n"
        evidence.append(limitation)
        print(limitation, end="")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        evidence.append(f"FAIL {error}\n")
        print(f"검증 실패: {error}", file=sys.stderr)
        return 1
    finally:
        LOG.write_text("".join(evidence))
        print(f"원본 결과: {LOG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
