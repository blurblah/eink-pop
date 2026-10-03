"""16000바이트 2bpp 전체 프레임의 무손실 바이트 반복 압축."""
import re

SIZE = 16000
RUN = re.compile(rb'(.)\1{2,}', re.S)


def encode(data):
    if len(data) != SIZE:
        raise ValueError('2bpp 크기 오류')
    result = bytearray()

    def literal(part):
        for at in range(0, len(part), 128):
            chunk = part[at:at + 128]
            result.append(len(chunk) - 1)
            result.extend(chunk)

    cursor = 0
    for match in RUN.finditer(data):
        literal(data[cursor:match.start()])
        size = match.end() - match.start()
        value = data[match.start()]
        while size:
            count = min(size, 128)
            result.extend((0x80 | (count - 1), value))
            size -= count
        cursor = match.end()
        if len(result) >= SIZE:
            return None
    literal(data[cursor:])
    return bytes(result) if len(result) < SIZE else None


def decode(encoded):
    if not 0 < len(encoded) < SIZE:
        raise ValueError('RLE 크기 오류')
    result = bytearray()
    at = 0
    while at < len(encoded):
        control = encoded[at]
        at += 1
        count = (control & 127) + 1
        if len(result) + count > SIZE:
            raise ValueError('RLE 복원 크기 초과')
        needed = 1 if control & 128 else count
        if at + needed > len(encoded):
            raise ValueError('잘린 RLE 데이터')
        result.extend(bytes([encoded[at]]) * count if control & 128 else encoded[at:at + count])
        at += needed
    if len(result) != SIZE:
        raise ValueError('RLE 복원 크기 부족')
    return bytes(result)
