# 작업: 세 하위 저장소 독립화와 개인 fork 동기화

| 항목 | 내용 |
| --- | --- |
| 생성일 | 2026-10-10 01:29 KST |
| 상태 | 실행 중 |
| 승인 | 사용자: “각각 분리한 후 push 해줘. 로컬에도 submodule처럼 되어있다면 잘 분리하고” |
| 위험도 | 중간 — Git 메타데이터·원격 브랜치 변경 |

## 요청과 범위

- `repos/SDLPoP`, `repos/EPD_Painter`, `repos/LilyGo-EPD47`의 위치와 소스를 유지하며 각각 독립 저장소로 전환한다.
- 개인 fork를 origin, 공식 저장소를 upstream으로 설정한다. 작업 브랜치는 `eink-pop`으로 통일하고 확인 후 원격 `codex/eink-pop`을 제거한다.
- 상위 저장소의 gitlink와 `.gitmodules`를 제거하고 세 경로를 추적 제외한다. 최신 구성 문서·README·AGENTS·인계를 수정한다.
- 기존 원본 작업 공간에도 전환 커밋을 fast-forward하고 main으로 이름을 바꾼다. 이 작업 공간의 브랜치는 portable-board-plan이다.
- 장치 접근·코드 변경·빌드·Pi 배포는 하지 않는다. 별도 원본 `/Users/blurblah/workspace/SDLPoP`는 건드리지 않는다.

## 성공 조건과 실행 순서

- [x] 1. 각 저장소의 HEAD·refs·상태·작업 파일 해시 및 Git 메타데이터를 보존한다.
- [x] 2. `.git` 파일을 자체 `.git` 디렉터리로 전환한다. 외부 객체 의존성 없음, 전후 파일·커밋 일치, fsck 확인.
- [x] 3. 세 개인 fork의 eink-pop 브랜치를 push하고 원격 SHA를 대조한 후 기존 codex/eink-pop 브랜치를 제거한다.
- [ ] 4. 상위 gitlink 제거·추적 제외·문서 수정 후 커밋한다. 원본 작업 공간을 fast-forward하고 main push.
- [ ] 5. 일반 clone과 개별 clone으로 복원 검증. 로컬 상태·원격 SHA·독립 Git 경로 확인.

## 위험과 복구

- `.git/modules/repos`의 기존 메타데이터는 삭제하지 않는다. 각 `.git` 포인터와 설정·검증 기록은 무시된 artifacts 아래에 보존한다.
- 소스·빌드 파일을 재배치하거나 submodule deinit으로 지우지 않는다. 상위 추적만 `git rm --cached`로 해제한다.
- 브랜치 제거 전에 같은 커밋이 새 원격 브랜치에 있는지 확인한다. 문제 발생 시 보존한 포인터와 메타데이터로 연결을 복구할 수 있다.
- 원격 main은 fast-forward만 사용한다. 타인의 변경이나 미커밋 변경이 발견되면 덮어쓰지 않는다.

## 실행 기록

- 시작: 상위·하위 모두 미커밋 변경 없음, 원격 main cded7cc와 세 codex/eink-pop 커밋 확인. 계정 잔여 78%.
- 세 저장소 자체 `.git/`로 메타데이터 복사, 원래 refs 대조, 소스·빌드 파일 전체 SHA-256 일치, `git fsck --connectivity-only` 통과. 기존 Git 객체와 포인터·설정은 보존했다.
- `artifacts/git-separation-20261010-0129/before.json`: 219154바이트, SHA-256 `3c28534d9f01ac933c35b4d03f9641d31e85b8f82c5de3d99233472af0a6ea5d`. `after.json`: 538바이트, SHA-256 `bff371881e033739ea34eb85f95394f586017d171cf143093d0d500a5dda9348`.
- 세 로컬 작업 브랜치 이름 eink-pop, 개인 fork origin과 공식 upstream 설정 완료. 남아 있던 제조사 과거 작업 브랜치도 커밋을 유지하며 codex 접두사 제거.
- 상위 Git 인덱스에 repos 아래 항목이 없고 각 저장소의 대표 파일이 ignore되는 것을 확인. 호스트·펌웨어·테스트 코드는 수정하지 않았다.
- 현재 사용자가 알려준 추론 수준은 medium이며 이 작업에 충분하다고 안내했다. 변경 권장이 필요하면 작업 전에 이유와 함께 안내한다.
