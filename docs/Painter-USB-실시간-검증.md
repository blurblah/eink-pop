# Painter NORMAL 최신 게임 화면 전송

## 목적과 기준

NORMAL 압축 입력 v7은 같은12장 재생에서 완료 간격211ms였고, 사용자는 직전 NORMAL과 전반적으로 큰 차이를 느끼지 못했다. 따라서 같은 파형·밝기·2bpp 확대를 실시간 게임 입력에 연결한다. 드라이버는 `repos/EPD_Painter`의7544e2d, 게임은 `repos/SDLPoP`의53b2ff3이다. 기존 제조사 USB v3는 그대로 보존한다.

녹화12장의 원래 시간 범위는0.922883초지만 v7의 첫~마지막 완료 간격은2.320997초였다. 모두 차례로 보여주는 시험의 느린 움직임과, 실제 게임 시간을 유지하면서 중간 화면을 건너뛰는 표시를 구분한다. 호스트는 ACK 뒤 파일을 새로 열어 최신 화면을 선택한다. 새 원본 번호가 아직 없으면 최대1초 기다리고, 번호 역행·정지에는 실패한다. 과거 화면을 큐에 쌓지 않는다. USB·PPM 변환 비용은 이전 Flash 재생 측정에 없었으므로4.74장/초가 실시간 보장치는 아니다.

## 남은 구동 비용의 오프라인 검토

H716 NORMAL 직접 표의3→2 전이는19주사이며 이 클립의 모든 후속 장에 존재한다. 주사별4ms 간격만76ms이고, 이전 FAST rowloop5.25ms/주사를 차용하면19주사 행 처리 약99.75ms가 더해진다. 후자는 NORMAL 실측 분리가 아닌 추정이다. v7의204.410ms를 그대로 이 둘로 완전히 분해할 수는 없다.

`EPD_Painter.h`는4명도 간격이 파형 보정 조건이라고 명시한다. `How_It_Works.md`의 charge-retention 설명에 따르면 행의 다음 래치까지 시간이 실제 구동량에 영향을 준다. 따라서4ms 대기 제거나 빈 행의 빠른 건너뛰기는 화질을 유지하는 단순 CPU 최적화로 간주할 수 없다. 직접 전이 검색은 이미64픽셀 묶음의 동일 데이터를 건너뛴다(`epd_painter_decision.cpp`). 이번에는 구동 표·전압·주사 시간을 바꾸지 않는다.

이동 중 하얗게 보이는 현상에는3→2의 흰색14주사 후 검정5주사,3→1의 흰색13주사 후 검정2주사 같은 중간 전이도 관여할 수 있다. 사용자 관찰과 부합하는 후보 원인이지만 입력 명도와 분리 측정하지 않았으므로 확정 진단은 아니다.

## EPK1 v4 계약

- 헤더20바이트: magic `EPK1`, version4, type1/2/3, reserved0, uint32 little-endian session/sequence/payload length. 끝에는 헤더+본문의 IEEE CRC-32 little-endian4바이트.
- START: sequence0, nonzero session, 본문1바이트의1~12장 한도.
- FRAME: sequence1부터 순서대로,16000바이트의320×200 2bpp/MSB-first/흰0·검정3. 총16024바이트.
- STOP: 다음 sequence, 본문0바이트. 재시도·자동 재연결 없음.
- READY의 `PAINTER_USB_` 마커·version/driver/board/quality/gray/direct/profile/packed를 엄격히 확인한다. 구형 `USB_FRAME_` 호스트와 오접속하지 않는다.
- ACCEPT 뒤 할당·CPU/물리PSRAM 검사, RX 이후에만 FRAME 전송. START 자체는 패널을 초기화하지 않는다.
- 첫 FRAME의 CRC 검증 뒤 begin→NORMAL/direct→clearBuffers→clear→idle을 확인한다. 다음부터 변경된 화면만paintPacked→idle, 모두 같은4명도 구동이다.
- ACK: session/seq/CRC/result, rx_us/display_us/clear_us/prepare_us/paint_us/cycles/elapsed_us. 변경된 화면의 실제 완료 주기1을 확인한다. 첫 흰 화면은 전체 정리만 실행하고 cycles0일 수 있다.
- 동일 화면도 수신 프레임 한도에는 센다. 첫 프레임의 정리 후에는 전체 정리0회. ACK는 광학 응답이나 버튼 입력 지연의 직접 측정이 아니다.
- 부팅당1세션·최대12장·첫 출력 시작부터30초·수신5초·호스트45초. 모든 종료에서 idle 확인과 OFF를 먼저 수행하고 버퍼를 해제한다. OFF 실패 시 DONE을 보내지 않는다.
- DONE: session/reason/frames/applied/poweroff_returned. 완료 후 호스트가10초 추가 출력 없음을 확인한다. 초기화 전 오류나 첫FRAME 전STOP은poweroff_returned0이다.

## 구현·검증

`firmware/painter_usb/painter_usb.ino`는 기존 EPK1 수신 상태 머신과 검증된 Painter 수명 제어를 조합했다. 원본32KB와 확대129600바이트는 PSRAM에 둔다. 드라이버의 자동 BootCtl·프로파일·NVS 조정은 비활성화한다. USB RX32768바이트를 begin 전에 고정한다.

`host/painter_usb.py`는 기존 SerialIO·응답 필드 검증과 `prepare_painter_clip.convert()`를 재사용한다. 파일 한 번의 열린 fd에서 원본·mtime을 확보하고 해시·선택된 원본 번호·전송/ACK 시각을 남긴다. 고정250ms 대기는 없다. 적용 여부·주기를 실제 전송한 픽셀과 비교하고 후속 전체 정리를 거부한다.

실제 스케치31사례 ASan/UBSan 검증: 패킷 분할·CRC·세션·순서·길이·구형버전·한도·선행 데이터·할당/PSRAM/CPU·초기화/직접 설정·clear/paint/종료 실패·무변경·흰 첫 장·12장 종료·전 픽셀 확대. 호스트8개 테스트(오류16종 subtest 포함)는 ACK 후 최신 읽기·원본 번호 건너뛰기/역행·취소·STOP·CRC·DONE/OFF·추가/불완전 출력·입력 변환을 확인했다. 호스트 상태 검증은 모의 I/O 위에서 수행했으며 직렬 I/O 자체는 기존 모듈을 사용한다.

ESP32 Arduino2.0.17/C++17 빌드 성공. 앱331568바이트 SHA-256 `54586efbfef6521c669c1678dbb7142d4a550ee258274c235523dd15b39a463f`, 프로그램331145바이트·정적 전역25920바이트. 빌드/검증 원본은 `artifacts/painter-live-20260928/`, 빌드는 `.local/build/painter_usb/`에 보존한다. 실제 업로드·게임 전송은 아직 전이다.

## 실행

새 로그 경로와 확인된 장치 포트에서 다음 명령을 쓴다. 게임은 기존 최신PPM 추출 빌드로 별도 실행한다.

```sh
python3 host/painter_usb.py --frame-path <새 게임의 latest.ppm> --port <현재 확인한 포트> --frames 12 --log-prefix <새 로그 경로>
```

게임 실행기와 하드웨어 관찰기는 이번 생성한 자식 프로세스·새 로그 경로만 관리하며 기존 게임을 종료하지 않는다. 실패 시 자동 반복하지 않고 로그를 보존한다. 최신화면 선택으로도 단순 프레임 수가 늘지는 않는다. 실제 입력 지연·잔상·플레이 감각은 제한 실측에서 따로 확인한다.


## 첫 실시간 12장 실행 (2026-09-28)

구현2d5d856 후 제조사 전체 정리 한 번(clear1324160µs·추가 흑/백 그림0회·이후10.004632초 확인)을 수행했다. 새 USB 앱을 한 번 업로드하고 Flash4구간 해시 및 READY10회/8.055879초·시작 명령0회를 확인했다.

실행 중인 기존 게임이 없음을 확인하고, 이전에 검증한 SDLPoP 바이너리 SHA-256 `7287efa3ee9780f7c188f5e52f1ffe746253f3053a4eef404d4b09520672d09c`로 새 게임 자식만 실행했다. 새 경로의 최신PPM이 갱신됨을 확인한 뒤60초 창 전환 시간을 제공하고 FRAME12장을 한 번 전송했다. 자동 반복은 없었다.

| 항목 | 관측 |
| --- | ---: |
| 전송한 원본 번호 | 749,757,760,764,767,770,774,777,781,784,788,791 |
| 전체 TX | 192313바이트 = START25 + FRAME16024×12 |
| 첫 display | 503.895ms, 초기 정리 포함 |
| 후속 평균 display | 196.208ms |
| 후속 평균 준비 / paint | 6.842ms / 189.237ms |
| 후속 평균 보드 수신 | 38.462ms |
| ACK 수신 간격 평균 | 292.171ms, 역수3.42장/초 |
| 선택 원본 파일 갱신 시각 범위 | 3.500582초 |
| 첫~마지막 ACK 수신 범위 | 3.213877초 |
| 파일 읽기 시작~ACK 평균 | 316.798ms, 첫 초기화 포함 |
| 종료 후 추가 로그 없는 시간 | 10.003296초 |

START1회·FRAME12개 각각의 wire CRC·본문 SHA-256·ACK 세션/순서/CRC를 원본TX와 다시 대조했다. 모두applied/실제 완료1주기였고 첫 화면 뒤 전체 정리0회였다. DONE limit/frames12/applied12/poweroff_returned1 및10초 무출력을 확인했다. 포트는 닫혔고 보드는 새 USB 앱 terminal 상태로 마지막 화면을 유지한다. 게임은 정상 종료(code0), 강제 종료 없음·임시PPM 잔여 없음도 확인했다.

최초 초기화 뒤 원본 번호가8장, 이후3~4장씩 건너뛰어 최신 화면 선택을 확인했다. 녹화12장을 모두 느리게 재생한 시험과 달리 게임 로직은 계속 진행했다. 프레임 건너뛰기를 누락/CRC 오류로 해석하지 않는다. 게임 장면이 이전 녹화 클립과 다르므로 display189~196ms를 동일 화면 드라이버 가속으로 주장하지 않는다. 3.42는 호스트 ACK 간격의 역수이며 광학fps·버튼 입력 지연은 아니다. 파일mtime 범위도 실제 게임 로직 시각·입력 시각과 별도다.

사용자는 시작이 늦어 몇 장면만 보았으며 느림인지 끊김인지 판단하지 못했다고 답했다. 따라서 실시간 조작감·잔상·하얗게 보임은 확인되지 않았다. 2026-09-29부터 사용자 승인으로 [성능 개선 반복](성능-개선-반복.md)의 수치·사용량 종료 조건에 따라 진행한다. 원본은 `artifacts/painter-live-20260928/{live-runtime.log,live-tx.bin,live-observation.json,timing-summary.json,play-result.json}`, 게임 상태·로그는 `artifacts/painter-live-game-20260928/`, 초기 정리는 `artifacts/painter-live-clean-20260928/`에 보존한다.
