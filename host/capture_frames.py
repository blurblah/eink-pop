#!/usr/bin/env python3
"""실행 중 게임의 최신 PPM을 제한 시간 동안 저장한다. 보드·게임 실행 기능은 없다."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time

HEADER = re.compile(rb"P6\n# eink sequence ([1-9][0-9]*)\n320 200\n255\n")


def read_ppm(path):
    with Path(path).open("rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("PPM 입력은 일반 파일이어야 합니다")
        raw = source.read(192129)
    match = HEADER.match(raw)
    if not match or match.end() > 128 or len(raw) != match.end() + 192000:
        raise ValueError("최신 PPM 헤더·데이터 길이 오류")
    return raw, int(match[1])


def capture(frame_path, output, seconds=10.0, max_frames=120):
    if not math.isfinite(seconds) or not 0 < seconds <= 30 or not 1 <= max_frames <= 360:
        raise ValueError("수집 제한: 0초 초과~30초, 1~360장")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    result = {"schema": 1, "status": "진행 중", "seconds_limit": seconds,
              "frame_limit": max_frames, "frames": [],
              "limitations": ["타임스탬프는 파일을 읽은 시점입니다. 게임 렌더링의 정확한 시각이 아닙니다.",
                              "최신 슬롯을 읽으므로 누락된 게임 프레임 번호가 있을 수 있습니다."]}
    previous_sequence, previous_hash = 0, None
    try:
        while time.monotonic() - started < seconds:
            try:
                raw, sequence = read_ppm(frame_path)
            except FileNotFoundError:
                time.sleep(.01)
                continue
            digest = hashlib.sha256(raw).hexdigest()
            if sequence < previous_sequence or sequence == previous_sequence and digest != previous_hash:
                raise ValueError("게임 추출 번호가 되돌아갔거나 같은 번호의 내용이 바뀌었습니다")
            if sequence != previous_sequence:
                name = f"frame-{len(result['frames'])+1:04d}.ppm"
                (output / name).write_bytes(raw)
                result["frames"].append({"file": name, "source_sequence": sequence,
                                         "captured_seconds": time.monotonic()-started,
                                         "skipped_before": max(0, sequence-previous_sequence-1) if previous_sequence else 0,
                                         "sha256": digest, "bytes": len(raw)})
                previous_sequence, previous_hash = sequence, digest
                if len(result["frames"]) == max_frames:
                    result["reason"] = "frame_limit"
                    break
            time.sleep(.01)
        if not result["frames"]:
            raise ValueError("제한 시간 내 PPM을 수집하지 못했습니다")
        result.update(status="완료", reason=result.get("reason", "time_limit"))
        return result
    except BaseException as error:
        result.update(status="오류", error=str(error))
        raise
    finally:
        result["elapsed_seconds"] = time.monotonic()-started
        (output / "capture.json").write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frame-path", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="존재하지 않는 새 저장 디렉터리")
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--max-frames", type=int, default=120)
    args = parser.parse_args(argv)
    try:
        result = capture(args.frame_path, args.output, args.seconds, args.max_frames)
        print(f"{len(result['frames'])}장 저장: {args.output / 'capture.json'} / 보드 접근 없음")
        return 0
    except (OSError, ValueError) as error:
        print(f"수집 중단: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
