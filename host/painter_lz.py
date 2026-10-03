"""프로토콜 연결 전 검증용 후방참조 코덱. 입력/출력은 고정16000바이트."""
import struct
SIZE=16000

def encode(data):
    if len(data)!=SIZE:raise ValueError('입력크기')
    result=bytearray();seen={};at=literal=0
    def literals(end):
        nonlocal literal
        while literal<end:
            count=min(128,end-literal);result.append(count-1);result.extend(data[literal:literal+count]);literal+=count
    while at+4<=SIZE:
        key=data[at:at+4];previous=seen.get(key);seen[key]=at
        if previous is None:at+=1;continue
        low,high=4,min(131,SIZE-at)
        while low<high:
            middle=(low+high+1)//2
            if data[previous:previous+middle]==data[at:at+middle]:low=middle
            else:high=middle-1
        literals(at);result.append(128+low-4);result.extend(struct.pack('<H',at-previous));at+=low;literal=at
        if len(result)>=SIZE:return None
    literals(SIZE)
    return bytes(result) if len(result)<SIZE else None

def decode(data):
    if not 0<len(data)<SIZE:raise ValueError('압축크기')
    result=bytearray();at=0
    while at<len(data):
        token=data[at];at+=1
        count=(token&127)+(4 if token&128 else 1)
        if len(result)+count>SIZE:raise ValueError('복원초과')
        if token&128:
            if at+2>len(data):raise ValueError('잘린참조')
            distance=struct.unpack_from('<H',data,at)[0];at+=2
            if not 1<=distance<=len(result):raise ValueError('이전범위초과')
            for _ in range(count):result.append(result[-distance])
        else:
            if at+count>len(data):raise ValueError('잘린리터럴')
            result.extend(data[at:at+count]);at+=count
    if len(result)!=SIZE:raise ValueError('복원부족')
    return bytes(result)

