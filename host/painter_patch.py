"""320×200 2bpp의 행별 변경 바이트. 픽셀 값은 손실 없이 보존한다."""
import struct

SIZE, ROW = 16000, 80


def encode(previous, current):
    if len(previous)!=SIZE or len(current)!=SIZE: raise ValueError('2bpp 크기 오류')
    result=bytearray()
    for start in range(0,SIZE,ROW):
        old=previous[start:start+ROW];new=current[start:start+ROW]
        if old==new: continue
        first=0
        while old[first]==new[first]:first+=1
        last=ROW-1
        while old[last]==new[last]:last-=1
        result.extend(struct.pack('<HB',start+first,last-first+1))
        result.extend(new[first:last+1])
        if len(result)>=SIZE:return None  # 전체 화면을 보내는 편이 작다.
    return bytes(result)


def decode(previous, encoded):
    if len(previous)!=SIZE or len(encoded)>=SIZE: raise ValueError('패치 크기 오류')
    current=bytearray(previous);at=0;last_row=-1
    while at<len(encoded):
        if len(encoded)-at<3: raise ValueError('잘린 패치 헤더')
        offset,count=struct.unpack_from('<HB',encoded,at);at+=3
        row=offset//ROW
        if (not 1<=count<=ROW or offset+count>SIZE or offset%ROW+count>ROW or
                row<=last_row or at+count>len(encoded)):
            raise ValueError('패치 범위·순서·길이 오류')
        current[offset:offset+count]=encoded[at:at+count];at+=count;last_row=row
    return bytes(current)
