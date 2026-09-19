/**
 * 원본: LilyGo-EPD47 examples/drawImages/drawImages.ino
 * Copyright (c) 2024 Shenzhen Xin Yuan Electronic Technology Co., Ltd
 * 출처: https://github.com/Xinyuan-LilyGO/LilyGo-EPD47
 * 기준 커밋: 50224221fb81a8fcf9068532374b775af84a38d4
 * 원본 저장소 라이선스: GNU GPL v3 (저장소 LICENSE 참조)
 *
 * 변경: 2026-09-19, 첫 화면 검증을 위해 부팅당 그림 3장을 각 1회만
 * 표시하고 메모리·진행·종료 로그를 추가했다. src/pic1.h~pic3.h는
 * 위 커밋의 공식 예제 이미지를 변경 없이 복사했다.
 */

#include <Arduino.h>
#include <esp_heap_caps.h>
#include "epd_driver.h"
#include "src/pic1.h"
#include "src/pic2.h"
#include "src/pic3.h"

#if !defined(CONFIG_IDF_TARGET_ESP32S3)
#error "Select ESP32S3 Dev Module."
#endif

#ifndef BOARD_HAS_PSRAM
#error "Enable OPI PSRAM."
#endif

// 이 시간은 실물 관찰 간격이며, 패널의 장시간 갱신 안전 기준이 아니다.
static const uint32_t IMAGE_HOLD_MS = 3000;
static const size_t FRAMEBUFFER_BYTES = EPD_WIDTH * EPD_HEIGHT / 2;

static void showImage(uint32_t number, Rect_t area, uint8_t *pixels)
{
    Serial.printf("IMAGE_BEGIN index=%lu area=%ldx%ld x=%ld y=%ld\r\n",
                  (unsigned long)number, (long)area.width, (long)area.height,
                  (long)area.x, (long)area.y);

    epd_poweron();
    uint32_t clearStart = millis();
    epd_clear();
    uint32_t clearMs = millis() - clearStart;
    uint32_t drawStart = millis();
    epd_draw_grayscale_image(area, pixels);
    uint32_t drawMs = millis() - drawStart;
    epd_poweroff();

    // 함수 호출 시간과 실제 패널 변화 시간은 별도 관측값이다.
    Serial.printf("IMAGE_DONE index=%lu clear_call_ms=%lu draw_call_ms=%lu panel_power=off\r\n",
                  (unsigned long)number, (unsigned long)clearMs,
                  (unsigned long)drawMs);
    delay(IMAGE_HOLD_MS);
}

void setup()
{
    Serial.begin(115200);
    // USB 직렬 연결은 최대 5초만 기다리며, 연결이 없어도 시험은 진행한다.
    uint32_t serialStart = millis();
    while (!Serial && millis() - serialStart < 5000) {
        delay(10);
    }

    Serial.println("FIRST_IMAGE_START images=3 repeat=0 grayscale_bits=4");
    Serial.printf("MEMORY flash_bytes=%lu psram_bytes=%lu free_psram_bytes=%lu free_heap_bytes=%lu\r\n",
                  (unsigned long)ESP.getFlashChipSize(),
                  (unsigned long)ESP.getPsramSize(),
                  (unsigned long)ESP.getFreePsram(),
                  (unsigned long)ESP.getFreeHeap());

    // 할당 실패 시 패널을 초기화하거나 갱신하지 않고 대기한다.
    uint8_t *framebuffer = (uint8_t *)heap_caps_malloc(FRAMEBUFFER_BYTES, MALLOC_CAP_SPIRAM);
    if (!framebuffer) {
        Serial.printf("FIRST_IMAGE_ERROR framebuffer_allocation_failed bytes=%lu updates=stopped\r\n",
                      (unsigned long)FRAMEBUFFER_BYTES);
        return;
    }
    memset(framebuffer, 0xFF, FRAMEBUFFER_BYTES);
    Serial.printf("FRAMEBUFFER_READY bytes=%lu free_psram_bytes=%lu\r\n",
                  (unsigned long)FRAMEBUFFER_BYTES,
                  (unsigned long)ESP.getFreePsram());

    epd_init();

    // 원본의 좌측 상단 배치와 첫 이미지 직접 출력을 유지한다.
    Rect_t area1 = {0, 0, (int32_t)pic1_width, (int32_t)pic1_height};
    showImage(1, area1, (uint8_t *)pic1_data);

    // 두 번째 원본은 높이 576이며, 공식 함수가 패널 높이 540으로 자른다.
    Rect_t area2 = {0, 0, (int32_t)pic2_width, (int32_t)pic2_height};
    epd_copy_to_framebuffer(area2, (uint8_t *)pic2_data, framebuffer);
    showImage(2, epd_full_screen(), framebuffer);

    Rect_t area3 = {0, 0, (int32_t)pic3_width, (int32_t)pic3_height};
    epd_copy_to_framebuffer(area3, (uint8_t *)pic3_data, framebuffer);
    showImage(3, epd_full_screen(), framebuffer);

    heap_caps_free(framebuffer);
    Serial.println("FIRST_IMAGE_DONE images=3 panel_power=off updates=stopped");
}

void loop()
{
    // 마지막 이미지를 유지한다. 재갱신·딥슬립·자동 재시작은 하지 않는다.
    delay(1000);
}
