/* ctypes 시험/호스트용. 같은 디코더를 이후 펌웨어에서도 사용한다. */
#include "../firmware/painter_usb/painter_lz.h"
int painter_lz_encode(const uint8_t *src, size_t size, uint8_t *out, size_t capacity)
{
    return encode_painter_lz(src, size, out, capacity);
}
int painter_lz_decode(const uint8_t *src, size_t size, uint8_t *out, size_t capacity)
{
    return decode_painter_lz(src, size, out, capacity);
}
