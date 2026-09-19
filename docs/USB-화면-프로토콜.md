# USB 화면 전송 계약 v2 (16계조)

현재 호스트·펌웨어는 v2를 함께 사용한다. v1의 1bpp 8,000바이트 계약은 이전 Git 이력과 보존한 바이너리에 남아 있으며 서로 혼용하지 않는다. 이 문서는 현재 구현의 고정 계약이다. 게임·USB·패널은 각각 독립적으로 진행하고 USB는 ACK 이후에만 다음 프레임을 보낸다. 재전송·재접속·재시작은 자동으로 하지 않는다.

## 최신 게임 화면

`SDLPOP_EINK_FRAME_PATH`가 지정된 경우에만 SDLPoP에서 추출한다. 320×200 RGB24 최종 합성 화면을 SDL 비차단 잠금으로 최신 슬롯에 복사하고 작업 스레드가 같은 디렉터리의 임시 파일을 닫은 뒤 rename한다. 메인 렌더링 스레드는 파일 I/O·USB·작업 스레드 완료를 기다리지 않는다. 정상 종료 때만 작업 스레드를 join한다.

PPM 바이트는 `P6\n# eink sequence N\n320 200\n255\n` 뒤 RGB 192,000바이트다. N은 1부터 증가하는 10진 정수이며 추출 중 빠진 번호는 허용한다. 작업 스레드는 종료 시 마지막 대기 화면까지 저장한다. 같은 출력 경로에 생산자는 하나만 둔다.

호스트는 정확한 크기·해상도·최댓값을 검사하고 매 선택 프레임에 다음 변환을 적용한다.

1. 기본 회색은 `((77*R+150*G+29*B)>>8)>>4`로 0=검정, 15=흰색이다.
2. 기본 생명력 영역 `0≤x<69, 194≤y<199`의 RGB `(228,0,0)`은 15, `(184,0,0)`은 12로 우선 매핑한다. 삼각형의 검정 빈 부분과 다른 위치는 이 보정을 받지 않는다.
3. 나머지 픽셀의 회색 1~13은 1단계 밝게, 0·14·15는 그대로 둔다.

기본 게임 팔레트·HUD 위치를 전제로 하며 디더링은 하지 않는다. 전송은 행 우선, 행당 160바이트, 짝수 x는 하위 nibble·홀수 x는 상위 nibble, 정확히 32,000바이트다. 최초 선택 PPM과 회색 PGM(회색값×17)을 로그 옆에 보존한다. 정지화면의 보정과 동일한 규칙을 실행 중 새 화면에도 적용한다.

## 호스트에서 보드로 보내는 패킷

모든 정수는 little endian. 헤더는 20바이트이며 Python 표기는 `<4sBBHIII`다.

| 필드 | 크기 | 값 |
| --- | --- | --- |
| magic | 4 | ASCII `EPK1` |
| version | 1 | 2 |
| type | 1 | 1=START, 2=FRAME, 3=STOP |
| reserved | 2 | 0 |
| session | 4 | 호스트가 생성한 0이 아닌 uint32 |
| sequence | 4 | START=0, 이후 1부터 순서대로 |
| length | 4 | START=1, FRAME=32000, STOP=0 |

헤더 뒤 payload와 CRC32 4바이트가 붙는다. CRC는 헤더 전체+payload의 IEEE CRC-32로 `zlib.crc32(data)&0xffffffff`와 같다. 시험 벡터 `123456789`=0xCBF43926. FRAME 총 크기는 32,024바이트다.

START payload는 최대 프레임 수 1~12를 나타내는 한 바이트다. 부팅당 한 START만 소비한다. FRAME sequence는 1부터 단조 증가하며 변경 없는 프레임도 한도에 센다. STOP은 다음 FRAME과 같은 예상 sequence를 쓰며 패널 접근 없이 종료한다. 헤더가 잘못되면 payload를 기다리지 않고 terminal 오류로 끝낸다. 수신 중 payload 바이트는 명령이나 magic으로 재해석하지 않는다.

## 보드 응답

ASCII 한 줄, LF 또는 CRLF. 필드는 공백으로 나눈 `key=value`, 줄은 최대 512바이트다. session/seq 등 정수는 10진수, crc만 소문자 8자리 16진수다.

- `USB_FRAME_READY version=2 panel=untouched` — 시작 전 1초마다. 호스트가 이 문구를 확인한 뒤 START를 한 번만 보낸다.
- `USB_FRAME_ACCEPT session=N max_frames=N` — 검증된 START. 메모리·CPU·20초 지연 검사를 수행한다.
- `USB_FRAME_DELAY_DONE status=pass|fail ...` — 기존 지연 선행 검사의 결과. 부가 진단은 `USB_FRAME_STATS`를 사용한다.
- `USB_FRAME_RX session=N` — 검사 통과·프레임 수신 가능. 여기 전에는 FRAME을 보내지 않는다.
- `USB_FRAME_ACK session=N seq=N crc=XXXXXXXX result=applied|unchanged rx_us=N display_us=N clear_us=N draw_us=N elapsed_us=N` — CRC는 FRAME 패킷 CRC. 화면 처리와 poweroff 완료 후 이전 프레임을 확정한 뒤 응답한다. 응답 전 연결을 최대100ms 확인하며 ACK 후 flush·연결 재확인 뒤 다음 RX 제한을 시작한다. 연결 확인 실패는 ack_connection 오류로 끝낸다. flush는 호스트 수신 보장을 뜻하지 않는다. `rx_us`는 해당 패킷 첫 바이트부터 CRC 수신까지, `display_us`는 렌더 처리 전체, clear/draw는 전체 지우기·회색 그리기 호출, elapsed는 최초 panelon부터다.
- `USB_FRAME_ERROR reason=CODE` — 오류. 추가 패널 접근·파싱·재동기화 없음.
- `USB_FRAME_DONE session=N reason=limit|stop|time_limit|error frames=N applied=N` — terminal 상태. 이후 START/FRAME을 받아도 다시 실행하지 않는다.

호스트는 ACCEPT·RX·ACK·DONE의 session/순서를 검증하고 예상하지 않은 ACK, ERROR, ACCEPT 뒤 READY/재부팅, 연결 종료, 응답 시간초과에 중단한다. ACCEPT 전에는 USB에 남아 있던 정확한 READY를 허용하되 START를 재전송하거나 대기 시간을 연장하지 않는다. ERROR 뒤 DONE이 없더라도 성공으로 처리하지 않는다. 종료 후 10초 동안 추가 실행·재부팅 로그를 관찰하며 STATS도 추가 응답으로 거부한다.

## 출력·실행 제한

- Serial begin 전 `setRxBufferSize(32768)` 결과를 검사한다. RX 준비/ACK 이후 한 프레임만 송신한다.
- 부팅·START·지연 검사만으로 패널 초기화·전원·지우기를 하지 않는다. 첫 완전한 FRAME의 길이·CRC·세션·순서 검사가 통과해야 접근한다.
- 첫 유효 화면과 이후 변경된 화면마다 전체 지우기 후 회색조를 그린다. 첫 화면은 모두 흰색이어도 반드시 지운다. 이후 변경 없는 화면은 전원도 켜지 않는다. 제조사 grayscale API의 흰 바탕 전제를 지키며 새 회색 간 전환 파형은 만들지 않는다.
- 320×200 입력을 x=160,y=70에서 2×2로 확대한다. 4bpp 출력 버퍼는 960×400, 192,000바이트이며 좌우 160픽셀은 흰색이다. driver `f1607da`의 `epd_clear()`와 `epd_draw_grayscale_image({0,70,960,400}, ...)`를 사용한다. 전체 지우기와 회색 출력은 이전 정지화면 시험과 같은 경로다.
- 수신은 비차단·짧은 읽기를 누적하며 상한과 절대 시간을 검사한다. START는 첫 바이트부터 5초, FRAME/STOP은 RX_READY 또는 직전 ACK부터 5초 안에 전체 패킷을 받아야 한다.
- 최초 패널 전원 켜기부터 30초가 지나면 새 화면 처리를 시작하지 않는다. 수신 중에도 검사한다. 진행 중 드라이버 호출은 강제 중단하지 않으며 지우기를 시작한 프레임은 그림과 전원 끄기까지 마친다.
- 호스트는 ACK 뒤 최소 250ms 간격을 두고 최신 PPM을 읽는다. 한 세션은 1~12장이다. 정지화면 확인을 마친 뒤 첫 연속 시험은 사용자 관찰 준비 답변을 받아 시작한다.
- 타임아웃·길이·CRC·순서·할당·CPU/지연 검사 오류에서 전원 끄기 상태로 terminal 종료한다. 호스트도 자동 재전송하지 않는다.

ACK 시간은 드라이버 함수의 완료 기준이며 실제 e-paper 광학 전환 완료나 게임 입력 지연을 직접 측정한 값은 아니다.

매 변경 화면의 전체 지우기로 깜빡임과 지연이 발생한다. 정지 16계조 한 장의 기존 측정은 약 1.9초이며 연속 성능은 별도 실측한다. 게임 로직은 계속 진행하므로 표시 사이의 중간 게임 프레임은 건너뛴다.
