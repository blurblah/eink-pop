/* Python convert와 같은 정수식·생명력 위치 보정. 프레임/픽셀 축소 없음. */
#include <stddef.h>
#include <stdint.h>

int painter_convert_abi(void) { return 2; }

int painter_convert(const uint8_t *rgb, size_t rgb_size, uint8_t *out, size_t out_size)
{
    if (!rgb || !out || rgb_size != 192000 || out_size != 16000) return -1;
    for (size_t y = 0; y < 200; ++y) {
        for (size_t x = 0; x < 320; x += 4) {
            unsigned packed = 0;
            for (size_t q = 0; q < 4; ++q) {
                size_t at = (y * 320 + x + q) * 3;
                unsigned r = rgb[at], g = rgb[at + 1], b = rgb[at + 2];
                unsigned gray = (77 * r + 150 * g + 29 * b) >> 12;
                if (gray >= 1 && gray <= 13) ++gray;
                unsigned level = 3 - (gray + 2) / 5;
                if (y >= 194 && y < 199 && x + q < 69 && g == 0 && b == 0) {
                    if (r == 228) level = 0;
                    else if (r == 184) level = 1;
                }
                packed = (packed << 2) | level;
            }
            out[y * 80 + x / 4] = (uint8_t)packed;
        }
    }
    return 0;
}
