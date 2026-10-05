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

/* 색 기반 시험 프로파일. 기본 변환 및 비피부 픽셀은 변경하지 않는다. */
static int is_skin(const uint8_t *p)
{
    return (p[0]==220 && p[1]==128 && p[2]==108) ||
           (p[0]==216 && p[1]==184 && p[2]==160) ||
           (p[0]==176 && p[1]==112 && p[2]==96);
}

static int same_gray_background(const uint8_t *rgb, const uint8_t *out, size_t i)
{
    return !is_skin(rgb+i*3) && ((out[i/4] >> (6-2*(i%4))) & 3)==2;
}

int painter_convert_skin(const uint8_t *rgb, size_t rgb_size, uint8_t *out, size_t out_size)
{
    if (painter_convert(rgb,rgb_size,out,out_size)) return -1;
    for (size_t y=0;y<200;++y) for (size_t x=0;x<320;++x) {
        size_t i=y*320+x;
        if (!is_skin(rgb+i*3)) continue;
        if ((x && same_gray_background(rgb,out,i-1)) ||
            (x<319 && same_gray_background(rgb,out,i+1)) ||
            (y && same_gray_background(rgb,out,i-320)) ||
            (y<199 && same_gray_background(rgb,out,i+320))) continue;
        unsigned shift=6-2*(i%4);
        out[i/4]=(uint8_t)((out[i/4] & ~(3U<<shift)) | (2U<<shift));
    }
    return 0;
}
