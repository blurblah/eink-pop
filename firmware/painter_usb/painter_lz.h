/* 고정16000바이트 무손실 블록. 리터럴1~128 / 후방참조4~131, 거리LE16. */
#ifndef PAINTER_LZ_H
#define PAINTER_LZ_H
#include <stddef.h>
#include <stdint.h>
#include <string.h>

static unsigned painter_lz_hash(const uint8_t *p)
{
    uint32_t word = (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
                    ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
    return (word * 2654435761U) >> 20;
}

static int painter_lz_literals(const uint8_t *src, size_t from, size_t end,
                               uint8_t *out, size_t *used)
{
    while (from < end) {
        size_t count = end - from;
        if (count > 128) count = 128;
        if (*used + 1 + count >= 16000) return 0;
        out[(*used)++] = (uint8_t)(count - 1);
        memcpy(out + *used, src + from, count);
        *used += count; from += count;
    }
    return 1;
}

/* 양수=압축길이,0=원본을보낼것,-1=잘못된버퍼. 입력을변경하지않는다. */
static int encode_painter_lz(const uint8_t *src, size_t size, uint8_t *out, size_t capacity)
{
    if (!src || !out || size != 16000 || capacity != 16000) return -1;
    uint16_t seen[4096]; memset(seen, 0xff, sizeof seen);
    size_t at = 0, literal = 0, used = 0;
    while (at + 4 <= size) {
        unsigned hash = painter_lz_hash(src + at);
        size_t previous = seen[hash]; seen[hash] = (uint16_t)at;
        if (previous == 65535 || memcmp(src + previous, src + at, 4)) { ++at; continue; }
        size_t count = 4, limit = size - at;
        if (limit > 131) limit = 131;
        while (count < limit && src[previous + count] == src[at + count]) ++count;
        if (!painter_lz_literals(src, literal, at, out, &used) || used + 3 >= size) return 0;
        size_t distance = at - previous;
        out[used++] = (uint8_t)(128 + count - 4);
        out[used++] = (uint8_t)distance;
        out[used++] = (uint8_t)(distance >> 8);
        for (size_t i = at + 1; i < at + count && i + 4 <= size; ++i)
            seen[painter_lz_hash(src + i)] = (uint16_t)i;
        at += count; literal = at;
    }
    if (!painter_lz_literals(src, literal, size, out, &used)) return 0;
    return (int)used;
}

/* 0=정확히16000바이트복원,-1=실패. 중첩참조는앞에서부터복사한다. */
static int decode_painter_lz(const uint8_t *src, size_t size, uint8_t *out, size_t capacity)
{
    if (!src || !out || !size || size >= 16000 || capacity != 16000) return -1;
    size_t at = 0, used = 0;
    while (at < size) {
        unsigned token = src[at++];
        size_t count = (token & 127U) + ((token & 128U) ? 4 : 1);
        if (count > capacity - used) return -1;
        if (token & 128U) {
            if (size - at < 2) return -1;
            size_t distance = (size_t)src[at] | ((size_t)src[at + 1] << 8); at += 2;
            if (!distance || distance > used) return -1;
            for (size_t i = 0; i < count; ++i) { out[used] = out[used - distance]; ++used; }
        } else {
            if (count > size - at) return -1;
            memcpy(out + used, src + at, count); used += count; at += count;
        }
    }
    return used == capacity ? 0 : -1;
}
#endif
