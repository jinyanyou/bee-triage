"""판별 정확도 평가 harness.

공모전 유의사항의 "주관기관은 필요 시 사용한 AI 도구 및 활용 방식에 대한
설명을 요구할 수 있음"과 본선 심사의 완성도·실현성 항목에 대비해,
판별 성능을 숫자로 말할 수 있게 한다.

사용법
------
    1) eval/images/ 에 사진을 넣는다
    2) eval/labels.csv 에 정답을 적는다 (labels.example.csv 참고)
    3) prototype 폴더에서:
         .venv\\Scripts\\python.exe -m eval.run_eval
       옵션:
         --limit N       앞에서 N장만
         --out report.md 결과를 파일로 저장

무엇을 보는가
-------------
단순 정확도보다 **안전 지표**가 중요하다.

  위험 과소평가(underestimation)
      실제보다 낮은 등급으로 판정한 건수. 이 값이 0이 아니면
      사람이 다칠 수 있는 오류다. 최우선으로 봐야 한다.
  불필요 119 이관(over-escalation)
      실제 저위험인데 119로 보낸 건수. 많으면 서비스의 존재 이유가 약해진다.

신뢰도 임계값 스윕은 이 둘의 트레이드오프를 보여준다.
현재 기본값 0.70이 적절한지 여기서 근거를 만든다.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# prototype 폴더를 import 경로에 올린다 (python -m eval.run_eval 로 실행)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config, vision  # noqa: E402

EVAL_DIR = Path(__file__).resolve().parent
IMAGES_DIR = EVAL_DIR / "images"
LABELS_CSV = EVAL_DIR / "labels.csv"

GRADES = ["저위험", "중위험", "고위험", "판별불가"]
RANK = {"저위험": 0, "중위험": 1, "고위험": 2, "판별불가": 2}
MEDIA = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}

SWEEP = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90]


def load_labels() -> List[Dict[str, str]]:
    if not LABELS_CSV.exists():
        sys.exit(
            "labels.csv가 없습니다.\n"
            "  eval/labels.example.csv 를 labels.csv 로 복사해 채워주세요."
        )
    with LABELS_CSV.open(encoding="utf-8-sig", newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if (r.get("file") or "").strip()]
    if not rows:
        sys.exit("labels.csv에 항목이 없습니다.")
    return rows


def classify_all(rows: List[Dict[str, str]], limit: Optional[int]) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    targets = rows[:limit] if limit else rows

    for i, row in enumerate(targets, start=1):
        path = IMAGES_DIR / row["file"].strip()
        if not path.is_file():
            print("  [{}/{}] {} — 파일 없음, 건너뜀".format(i, len(targets), row["file"]))
            continue
        media = MEDIA.get(path.suffix.lower())
        if media is None:
            print("  [{}/{}] {} — 지원하지 않는 형식, 건너뜀".format(i, len(targets), row["file"]))
            continue

        t0 = time.time()
        result, warning = vision.classify_image(path.read_bytes(), media)
        elapsed = time.time() - t0

        actual = (row.get("actual_grade") or "").strip()
        ok = "O" if result.risk_grade == actual else "X"
        print(
            "  [{}/{}] {:<28} 정답 {:<5} → 판정 {:<5} (신뢰도 {:.2f}, {:.1f}s) {}".format(
                i, len(targets), path.name, actual, result.risk_grade,
                result.confidence, elapsed, ok
            )
        )
        if warning:
            print("        ! {}".format(warning))

        results.append(
            {
                "file": path.name,
                "actual_grade": actual,
                "actual_species": (row.get("actual_species") or "").strip(),
                "pred_grade": result.risk_grade,
                "pred_species": result.species_guess,
                "confidence": result.confidence,
                "downgraded": result.downgraded_by_threshold,
                "is_swarm": result.is_swarm,
                "evidence": result.evidence,
                "seconds": round(elapsed, 1),
            }
        )
    return results


def grade_at(raw_conf: float, pred: str, downgraded: bool, threshold: float) -> str:
    """임계값을 바꿔 가정했을 때의 최종 등급.

    downgraded=True면 원래 모델 출력은 판별불가가 아니었다는 뜻이지만
    원본 등급을 복원할 수 없으므로 스윕 대상에서 제외한다(보수적).
    """
    if pred == "판별불가" and not downgraded:
        return "판별불가"  # 모델이 스스로 판별불가라고 한 건은 임계값과 무관
    return pred if raw_conf >= threshold else "판별불가"


def metrics(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(results)
    matched = sum(1 for r in results if r["pred_grade"] == r["actual_grade"])
    under = [
        r for r in results
        if RANK.get(r["pred_grade"], 2) < RANK.get(r["actual_grade"], 2)
    ]
    over = [
        r for r in results
        if r["actual_grade"] == "저위험" and r["pred_grade"] in ("고위험", "판별불가")
    ]

    confusion = {a: {p: 0 for p in GRADES} for a in GRADES}
    for r in results:
        if r["actual_grade"] in confusion and r["pred_grade"] in GRADES:
            confusion[r["actual_grade"]][r["pred_grade"]] += 1

    sweep = []
    for t in SWEEP:
        u = c = o = 0
        for r in results:
            g = grade_at(r["confidence"], r["pred_grade"], r["downgraded"], t)
            if g == r["actual_grade"]:
                c += 1
            if RANK.get(g, 2) < RANK.get(r["actual_grade"], 2):
                u += 1
            if r["actual_grade"] == "저위험" and g in ("고위험", "판별불가"):
                o += 1
        sweep.append(
            {"threshold": t, "accuracy": round(c / n * 100, 1) if n else 0,
             "underestimated": u, "over_escalated": o}
        )

    return {
        "n": n,
        "accuracy": round(matched / n * 100, 1) if n else 0,
        "underestimated": len(under),
        "under_cases": under,
        "over_escalated": len(over),
        "avg_seconds": round(sum(r["seconds"] for r in results) / n, 1) if n else 0,
        "confusion": confusion,
        "sweep": sweep,
    }


def report(m: Dict[str, Any], results: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    add = lines.append

    add("# 벌집 위험도 판별 정확도 평가")
    add("")
    add("- 평가 표본: **{}장**".format(m["n"]))
    add("- 모델: `{}`".format(config.MODEL))
    add("- few-shot 참조 사진: {}장".format(vision.reference_status()["count"]))
    add("- 현재 신뢰도 임계값: {:.2f}".format(config.CONFIDENCE_THRESHOLD))
    add("- 평균 판별 시간: {}초".format(m["avg_seconds"]))
    add("")
    add("## 핵심 지표")
    add("")
    add("| 지표 | 값 | 의미 |")
    add("|---|---|---|")
    add("| 등급 일치율 | **{}%** | 정답 등급과 판정이 같은 비율 |".format(m["accuracy"]))
    add("| **위험 과소평가** | **{}건** | 실제보다 낮게 본 건수. 사람이 다칠 수 있는 오류 |".format(m["underestimated"]))
    add("| 불필요 119 이관 | {}건 | 실제 저위험인데 119로 보낸 건수 |".format(m["over_escalated"]))
    add("")

    add("## 혼동 행렬 (행=정답, 열=판정)")
    add("")
    add("| 정답 \\ 판정 | " + " | ".join(GRADES) + " |")
    add("|---|" + "---|" * len(GRADES))
    for a in GRADES:
        add("| **{}** | ".format(a) + " | ".join(str(m["confusion"][a][p]) for p in GRADES) + " |")
    add("")

    add("## 신뢰도 임계값 스윕")
    add("")
    add("임계값을 올리면 과소평가는 줄지만 불필요한 119 이관이 늘어난다.")
    add("**위험 과소평가가 0이 되는 가장 낮은 임계값**을 고르는 것이 설계 원칙에 맞다.")
    add("")
    add("| 임계값 | 등급 일치율 | 위험 과소평가 | 불필요 119 이관 |")
    add("|---|---|---|---|")
    for s in m["sweep"]:
        mark = " ←현재" if abs(s["threshold"] - config.CONFIDENCE_THRESHOLD) < 1e-6 else ""
        add("| {:.2f}{} | {}% | {} | {} |".format(
            s["threshold"], mark, s["accuracy"], s["underestimated"], s["over_escalated"]))
    add("")

    safe = [s for s in m["sweep"] if s["underestimated"] == 0]
    if safe:
        best = min(safe, key=lambda s: s["threshold"])
        add("> 과소평가 0을 만족하는 최소 임계값: **{:.2f}** ".format(best["threshold"]) +
            "(일치율 {}%, 불필요 이관 {}건)".format(best["accuracy"], best["over_escalated"]))
    else:
        add("> 어떤 임계값에서도 위험 과소평가가 0이 되지 않았다. "
            "표본을 늘리거나 few-shot 참조 사진을 보강해야 한다.")
    add("")

    if m["under_cases"]:
        add("## 위험 과소평가 사례 (반드시 검토)")
        add("")
        for r in m["under_cases"]:
            add("- `{}` — 정답 **{}** ({}) → 판정 **{}** ({}, 신뢰도 {:.2f})".format(
                r["file"], r["actual_grade"], r["actual_species"],
                r["pred_grade"], r["pred_species"], r["confidence"]))
            add("  - 근거: {}".format(r["evidence"]))
        add("")

    add("## 전체 결과")
    add("")
    add("| 파일 | 정답 | 판정 | 신뢰도 | 강등 | 일치 |")
    add("|---|---|---|---|---|---|")
    for r in results:
        add("| {} | {} | {} | {:.2f} | {} | {} |".format(
            r["file"], r["actual_grade"], r["pred_grade"], r["confidence"],
            "O" if r["downgraded"] else "", "O" if r["pred_grade"] == r["actual_grade"] else "**X**"))

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="벌집 위험도 판별 정확도 평가")
    parser.add_argument("--limit", type=int, default=None, help="앞에서 N장만 평가")
    parser.add_argument("--out", type=str, default="eval/report.md", help="결과 저장 경로")
    args = parser.parse_args()

    if not config.LIVE_MODE:
        sys.exit(
            "LIVE 모드가 아닙니다. 평가에는 실제 판별이 필요합니다.\n"
            "  prototype/.env 에 ANTHROPIC_API_KEY 를 설정하세요."
        )

    rows = load_labels()
    print("평가 시작 — 표본 {}장, 모델 {}".format(len(rows), config.MODEL))
    print("few-shot 참조 사진: {}장\n".format(vision.reference_status()["count"]))

    results = classify_all(rows, args.limit)
    if not results:
        sys.exit("\n평가된 사진이 없습니다. eval/images/ 와 labels.csv를 확인하세요.")

    m = metrics(results)
    text = report(m, results)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    (out.parent / "results.json").write_text(
        json.dumps({"metrics": m, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("\n" + "=" * 62)
    print("등급 일치율      : {}%".format(m["accuracy"]))
    print("위험 과소평가    : {}건  <- 0이어야 한다".format(m["underestimated"]))
    print("불필요 119 이관  : {}건".format(m["over_escalated"]))
    print("=" * 62)
    print("\n리포트: {}".format(out))
    print("원시 결과: {}".format(out.parent / "results.json"))


if __name__ == "__main__":
    main()
