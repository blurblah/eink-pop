#!/usr/bin/env python3
"""장치 접근 없이 v3 기록을 검증하고 실측 기반 시간표·화면 재생을 만든다."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import statistics
import struct
import zlib

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "host/data/panel_timing_v3.json"
HEADER = struct.Struct("<4sBBHIII")


def integer(value, label):
    if isinstance(value, bool) or not str(value).isdigit():
        raise ValueError(f"잘못된 정수: {label}")
    return int(value)


def transitions(old, new):
    if len(old) != 8000 or len(new) != 8000:
        raise ValueError("1bpp 화면은 8000바이트여야 합니다")
    changed = [i for i, (a, b) in enumerate(zip(old, new)) if a != b]
    white = sum((a & ~b).bit_count() for a, b in zip(old, new))
    black = sum((~a & b).bit_count() for a, b in zip(old, new))
    first, last = (changed[0] // 40, changed[-1] // 40) if changed else (None, None)
    return {"white_pixels": white, "black_pixels": black,
            "changed_pixels": white + black, "first_row": first, "last_row": last,
            "physical_rows": 0 if not changed else (last - first + 1) * 2}


def fit(samples):
    """행 높이에 대한 선형 경험식. 물리 모델·신뢰구간이 아니다."""
    xs = [s["rows"] for s in samples]
    ys = [s["display_us"] for s in samples]
    xm, ym = statistics.mean(xs), statistics.mean(ys)
    denominator = sum((x - xm) ** 2 for x in xs)
    if not denominator:
        raise ValueError("시간 모델에는 서로 다른 행 높이가 필요합니다")
    slope = sum((x - xm) * (y - ym) for x, y in zip(xs, ys)) / denominator
    return {"intercept_us": ym - slope * xm, "per_row_us": slope}


def predict(coefficients, rows):
    return coefficients["intercept_us"] + coefficients["per_row_us"] * rows


def timing_model(profile):
    samples = profile["samples"]
    if profile.get("schema") != 1 or len(samples) < 6:
        raise ValueError("지원하지 않는 실측 프로필")
    for sample in samples:
        for key in ("rows", "white_us", "black_us", "display_us", "rx_us", "interval_us"):
            sample[key] = integer(sample[key], key)
        if not 1 <= sample["rows"] <= 400 or min(sample["white_us"], sample["black_us"]) <= 0:
            raise ValueError("현재 모델은 유효한 양극성 표본만 지원합니다")
        if sample["white_us"] + sample["black_us"] > sample["display_us"]:
            raise ValueError("극성 호출 시간이 전체 처리 시간을 초과합니다")
    coefficients = fit(samples)
    training, heldout = samples[::2], samples[1::2]
    validation_fit = fit(training)
    errors = [predict(validation_fit, s["rows"]) - s["display_us"] for s in heldout]
    heights = sorted({s["rows"] for s in samples})
    bands = [[heights[0], heights[0]]]
    for height in heights[1:]:
        if height - bands[-1][1] > 4:
            bands.append([height, height])
        else:
            bands[-1][1] = height
    return {**coefficients, "samples": len(samples), "range_rows": [heights[0], heights[-1]],
            "observed_bands": bands,
            "holdout": {"training": len(training), "validation": len(heldout),
                        "mae_us": statistics.mean(abs(e) for e in errors),
                        "max_error_us": max(abs(e) for e in errors),
                        "note": "동일 시험의 교대 분할입니다. 독립 실험 검증이 아닙니다."},
            "measured_display_us": statistics.mean(s["display_us"] for s in samples),
            "measured_interval_us": statistics.mean(s["interval_us"] for s in samples),
            "pause_us": profile["pause_us"]}


def estimate(model, rows, white=True, black=True):
    if not isinstance(rows, int) or isinstance(rows, bool) or not 0 <= rows <= 400:
        raise ValueError("물리 행 범위는 0~400 정수여야 합니다")
    if not white or not black or not model["range_rows"][0] <= rows <= model["range_rows"][1]:
        return {"status": "예측 불가: 실측 범위·극성 밖", "display_us": None}
    observed = any(low <= rows <= high for low, high in model["observed_bands"])
    return {"status": "실측 범위 내 추정" if observed else "보간 추정: 해당 행 범위 미검증",
            "display_us": predict(model, rows)}


def load_session(prefix, driver_commit):
    """원본 로그의 개인정보는 결과에 복사하지 않는다."""
    prefix = Path(prefix)
    with Path(str(prefix) + "-tx.bin").open("rb") as source:
        wire = source.read(25 + 12 * 8024 + 25)
    observation = json.loads(Path(str(prefix) + "-observation.json").read_text())
    if len(wire) > 25 + 12 * 8024 + 24:
        raise ValueError("기록 크기 상한 초과")
    if observation.get("status") != "통과" or observation.get("protocol_version") != 3:
        raise ValueError("정상 종료된 v3 기록만 지원합니다")
    configs = [r["line"] for r in observation.get("responses", [])
               if r.get("line", "").startswith("USB_FRAME_STATS metric=config ")]
    required = {f"driver={driver_commit}", "source=320x200", "bpp=1", "msb_first=1",
                "black=1", "white=0", "scale=2", "x=160", "y=70", "refresh=initial_clear_then_delta"}
    if len(configs) != 1 or not required.issubset(configs[0].split()):
        raise ValueError("실측 모델과 드라이버·표시 설정이 다릅니다")
    packets, position = [], 0
    while position < len(wire):
        if len(wire) - position < 24:
            raise ValueError("잘린 패킷")
        magic, version, kind, reserved, session, sequence, length = HEADER.unpack_from(wire, position)
        if magic != b"EPK1" or version != 3 or reserved or not session:
            raise ValueError("패킷 헤더 오류")
        if length != {1: 1, 2: 8000, 3: 0}.get(kind) or position + 24 + length > len(wire):
            raise ValueError("패킷 길이 오류")
        end = position + 20 + length
        crc, = struct.unpack_from("<I", wire, end)
        if zlib.crc32(wire[position:end]) & 0xFFFFFFFF != crc:
            raise ValueError("패킷 CRC 오류")
        packets.append((kind, session, sequence, wire[position+20:end], crc))
        position = end + 4
    if not packets or packets[0][0] != 1 or packets[0][2] or not 1 <= packets[0][3][0] <= 12:
        raise ValueError("START 오류")
    sid, limit = packets[0][1], packets[0][3][0]
    if sid != observation.get("session"):
        raise ValueError("기록 세션 불일치")
    frame_packets = packets[1:]
    stopped = bool(frame_packets and frame_packets[-1][0] == 3)
    if stopped:
        stop = frame_packets.pop()
        if stop[1] != sid or stop[2] != len(frame_packets) + 1:
            raise ValueError("STOP 순서 오류")
    records = observation.get("frames", [])
    if not 1 <= len(frame_packets) <= limit or len(frame_packets) != len(records):
        raise ValueError("프레임 수 불일치")
    previous, last_elapsed, frames = bytes(8000), -1, []
    for index, (packet, record) in enumerate(zip(frame_packets, records), 1):
        kind, session, sequence, payload, crc = packet
        ack = record["ack"]
        if kind != 2 or session != sid or sequence != index:
            raise ValueError("프레임 순서 오류")
        if record["bitmap_sha256"] != hashlib.sha256(payload).hexdigest():
            raise ValueError("이미지 해시 불일치")
        if integer(ack["session"], "session") != sid or integer(ack["seq"], "seq") != index or ack["crc"] != f"{crc:08x}":
            raise ValueError("ACK 불일치")
        times = {key: integer(ack[key], key) for key in ("rx_us", "display_us", "clear_us", "white_us", "black_us", "elapsed_us")}
        delta = transitions(previous, payload)
        changed = index == 1 or delta["changed_pixels"] > 0
        if ack["result"] != ("applied" if changed else "unchanged"):
            raise ValueError("ACK 적용 상태 불일치")
        if sum(times[k] for k in ("clear_us", "white_us", "black_us")) > times["display_us"] or times["elapsed_us"] <= last_elapsed:
            raise ValueError("ACK 시간 오류")
        if bool(times["clear_us"]) != (index == 1) or bool(times["white_us"]) != bool(delta["white_pixels"]) or bool(times["black_us"]) != bool(delta["black_pixels"]):
            raise ValueError("ACK 구동 방식 불일치")
        frames.append({"sequence": index, "source_sequence": integer(record["source_sequence"], "source_sequence"),
                       "payload": base64.b64encode(payload).decode(), **delta, **times,
                       "interval_us": None if index == 1 else times["elapsed_us"] - last_elapsed})
        previous, last_elapsed = payload, times["elapsed_us"]
    done = observation["done"]
    if integer(done["session"], "session") != sid or integer(done["frames"], "frames") != len(frames) or integer(done["applied"], "applied") != sum(f["sequence"] == 1 or f["changed_pixels"] > 0 for f in frames):
        raise ValueError("DONE 프레임 수 불일치")
    if done["reason"] != ("stop" if stopped else "limit") or not stopped and len(frames) != limit:
        raise ValueError("지원하지 않는 종료 상태")
    return frames


def build_report(prefix, profile):
    model = timing_model(profile)
    frames = load_session(prefix, profile["driver_commit"])
    for frame in frames:
        frame["prediction"] = estimate(model, frame["physical_rows"], bool(frame["white_pixels"]), bool(frame["black_pixels"]))
    mean_interval = model["measured_interval_us"]
    return {"title": "e-paper 오프라인 분석", "profile": {k:v for k,v in profile.items() if k != "samples"},
            "model": model, "frames": frames,
            "budget": {"target_us": 100000, "current_interval_us": mean_interval,
                       "without_pause_us": mean_interval - model["pause_us"],
                       "display_only_us": model["measured_display_us"]},
            "limits": ["완료된 비트맵과 시간 간격만 재생합니다. 실제 잔상·전환 중 깜빡임은 재현하지 않습니다.",
                       "저장된 12장 이하의 선택 화면입니다. 누락된 중간 게임 프레임은 복원하지 않습니다.",
                       "대기 제거 재생은 같은 저장 화면의 간격을 압축한 가정이며 실제 게임 재실행 결과가 아닙니다.",
                       "단일 시험 표본에 맞춘 모델입니다. 새 보드·드라이버·파형·온도에 일반화할 수 없습니다.",
                       "첫 화면의 전체 지우기와 실제 입자 움직임은 재생에서 생략합니다. 첫 화면 시간은 별도로 표시합니다."]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-prefix", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="새 보고서 디렉터리")
    args = parser.parse_args(argv)
    try:
        profile = json.loads(PROFILE.read_text())
        report = build_report(args.log_prefix, profile)
        template = Path(__file__).with_suffix(".html").read_text()
        encoded = json.dumps(report, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c")
        html = template.replace("__REPORT_DATA__", encoded)
        args.output.mkdir(parents=True, exist_ok=False)
        (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)+"\n")
        (args.output / "index.html").write_text(html)
        print(f"보고서 생성: {args.output / 'index.html'} / {len(report['frames'])}장 / 장치 접근 없음")
        return 0
    except (OSError, ValueError, KeyError, TypeError, struct.error) as error:
        print(f"분석 중단: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
