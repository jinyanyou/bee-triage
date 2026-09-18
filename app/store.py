"""신고 영속화 저장소 (SQLite).

인메모리 세션만으로는 소방관 관제 대시보드도, 기획서 5.2의 데이터 선순환
(MLOps 재학습 루프)도 화면으로 보여줄 수가 없어서 도입했다.

개인정보 관점(본선 '실현성' 항목 대비)
--------------------------------------
- 업로드된 **사진은 디스크에 저장하지 않는다.** 메모리에서 판별에 쓰고 버린다.
  DB에는 판별 결과(등급/근거 텍스트)만 남는다.
- 신고 좌표는 매칭에 필요한 만큼만 저장하고 보존기간이 지나면 자동 파기한다.
- `purge_expired()`가 RETENTION_DAYS 지난 신고를 지운다.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config

DB_PATH = config.ROOT / "var" / "reports.db"

# 보존기간. 파일럿에서는 지자체 개인정보 처리방침에 맞춰 조정한다.
RETENTION_DAYS = 90

# 신고 상태 흐름
STATUS_119_PENDING = "119확인대기"
STATUS_PARTNER_PENDING = "업체배정대기"
STATUS_ACCEPTED = "업체수락"
STATUS_DONE = "처리완료"
STATUS_REJECTED = "반려"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id            TEXT PRIMARY KEY,
    created_at    TEXT NOT NULL,
    lat           REAL,
    lon           REAL,
    address_label TEXT,
    route         TEXT NOT NULL,
    route_reason  TEXT,
    risk_grade    TEXT,
    species       TEXT,
    confidence    REAL,
    final_score   REAL,
    status        TEXT NOT NULL,
    assigned      TEXT,
    summary       TEXT,
    slots_json    TEXT,
    vision_json   TEXT,
    score_json    TEXT,
    companies_json TEXT
);

CREATE TABLE IF NOT EXISTS feedback (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id     TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    actual_grade  TEXT,
    actual_species TEXT,
    note          TEXT,
    reporter      TEXT,
    FOREIGN KEY (report_id) REFERENCES reports(id)
);

CREATE INDEX IF NOT EXISTS idx_reports_created ON reports(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reports_route   ON reports(route);
CREATE INDEX IF NOT EXISTS idx_reports_status  ON reports(status);
"""


def enabled() -> bool:
    """서버가 신고를 저장하는 모드인지.

    서버리스 배포에서는 파일시스템이 읽기 전용이라 False가 되고,
    신고 보관은 브라우저(localStorage)가 맡는다.
    """
    return config.STORE_MODE == "sqlite"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    if not enabled():
        return
    try:
        with _connect() as conn:
            conn.executescript(_SCHEMA)
        purge_expired()
    except sqlite3.OperationalError:
        # 쓰기 불가 환경으로 밝혀지면 조용히 클라이언트 저장 모드로 내려간다
        config.STORE_MODE = "client"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def save_report(
    *,
    lat: float,
    lon: float,
    address_label: str,
    route: str,
    route_reason: str,
    slots: Dict[str, Any],
    vision: Dict[str, Any],
    score: Dict[str, Any],
    companies: List[Dict[str, Any]],
    summary: Optional[str],
) -> str:
    report_id = "R" + uuid.uuid4().hex[:8].upper()
    status = STATUS_119_PENDING if route == "119" else STATUS_PARTNER_PENDING

    if not enabled():
        # 서버는 접수번호만 발급하고, 실제 보관은 브라우저가 한다.
        return report_id

    with _connect() as conn:
        conn.execute(
            """INSERT INTO reports (
                id, created_at, lat, lon, address_label, route, route_reason,
                risk_grade, species, confidence, final_score, status, assigned,
                summary, slots_json, vision_json, score_json, companies_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                report_id,
                _now(),
                lat,
                lon,
                address_label,
                route,
                route_reason,
                vision.get("risk_grade"),
                vision.get("species_guess"),
                vision.get("confidence"),
                score.get("final_score"),
                status,
                None,
                summary,
                json.dumps(slots, ensure_ascii=False),
                json.dumps(vision, ensure_ascii=False),
                json.dumps(score, ensure_ascii=False),
                json.dumps(companies, ensure_ascii=False),
            ),
        )
    return report_id


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    for key in ("slots_json", "vision_json", "score_json", "companies_json"):
        raw = d.pop(key, None)
        d[key.replace("_json", "")] = json.loads(raw) if raw else None
    return d


def list_reports(
    route: Optional[str] = None,
    status: Optional[str] = None,
    grade: Optional[str] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    if not enabled():
        return []
    sql = "SELECT * FROM reports WHERE 1=1"
    args: List[Any] = []
    if route:
        sql += " AND route = ?"
        args.append(route)
    if status:
        sql += " AND status = ?"
        args.append(status)
    if grade:
        sql += " AND risk_grade = ?"
        args.append(grade)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)

    with _connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_report(report_id: str) -> Optional[Dict[str, Any]]:
    if not enabled():
        return None
    with _connect() as conn:
        row = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    return _row_to_dict(row) if row else None


def set_status(report_id: str, status: str, assigned: Optional[str] = None) -> bool:
    if not enabled():
        return False
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE reports SET status = ?, assigned = COALESCE(?, assigned) WHERE id = ?",
            (status, assigned, report_id),
        )
    return cur.rowcount > 0


def add_feedback(
    report_id: str,
    actual_grade: Optional[str],
    actual_species: Optional[str],
    note: str = "",
    reporter: str = "",
) -> None:
    """사후 확인 결과. 기획서 5.2의 데이터 선순환 입력이 된다."""
    if not enabled():
        return
    with _connect() as conn:
        conn.execute(
            """INSERT INTO feedback (report_id, created_at, actual_grade,
                                     actual_species, note, reporter)
               VALUES (?,?,?,?,?,?)""",
            (report_id, _now(), actual_grade, actual_species, note, reporter),
        )


def feedback_for(report_id: str) -> List[Dict[str, Any]]:
    if not enabled():
        return []
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM feedback WHERE report_id = ? ORDER BY created_at DESC",
            (report_id,),
        ).fetchall()
    return [dict(r) for r in rows]


_GRADE_RANK = {"저위험": 0, "중위험": 1, "고위험": 2, "판별불가": 2}


def kpi() -> Dict[str, Any]:
    """관제 대시보드 상단 지표 + 데이터 선순환 현황."""
    if not enabled():
        return {
            "total": 0, "by_route": {}, "by_status": {}, "unknown": 0,
            "diverted": 0, "diversion_rate": 0.0,
            "feedback": {"count": 0, "matched": 0, "accuracy": None, "underestimated": 0},
            "retention_days": RETENTION_DAYS,
        }
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM reports").fetchone()["c"]
        by_route = {
            r["route"]: r["c"]
            for r in conn.execute("SELECT route, COUNT(*) c FROM reports GROUP BY route")
        }
        by_status = {
            r["status"]: r["c"]
            for r in conn.execute("SELECT status, COUNT(*) c FROM reports GROUP BY status")
        }
        unknown = conn.execute(
            "SELECT COUNT(*) c FROM reports WHERE risk_grade = '판별불가'"
        ).fetchone()["c"]

        # 사후 확인이 달린 건만 대상으로 정확도를 센다.
        paired = conn.execute(
            """SELECT r.risk_grade AS ai, f.actual_grade AS actual
               FROM reports r JOIN feedback f ON f.report_id = r.id
               WHERE f.actual_grade IS NOT NULL AND f.actual_grade != ''"""
        ).fetchall()

    matched = sum(1 for p in paired if p["ai"] == p["actual"])
    # 안전 관점에서 가장 나쁜 오류: 실제보다 낮게 본 경우
    underestimated = sum(
        1
        for p in paired
        if _GRADE_RANK.get(p["ai"], 2) < _GRADE_RANK.get(p["actual"], 2)
    )

    diverted = by_route.get("방역업체", 0) + by_route.get("양봉협회", 0)
    return {
        "total": total,
        "by_route": by_route,
        "by_status": by_status,
        "unknown": unknown,
        "diverted": diverted,
        "diversion_rate": round(diverted / total * 100, 1) if total else 0.0,
        "feedback": {
            "count": len(paired),
            "matched": matched,
            "accuracy": round(matched / len(paired) * 100, 1) if paired else None,
            "underestimated": underestimated,
        },
        "retention_days": RETENTION_DAYS,
    }


def purge_expired() -> int:
    """보존기간이 지난 신고를 파기한다."""
    if not enabled():
        return 0
    cutoff = (datetime.now() - timedelta(days=RETENTION_DAYS)).isoformat(timespec="seconds")
    with _connect() as conn:
        conn.execute(
            "DELETE FROM feedback WHERE report_id IN (SELECT id FROM reports WHERE created_at < ?)",
            (cutoff,),
        )
        cur = conn.execute("DELETE FROM reports WHERE created_at < ?", (cutoff,))
    return cur.rowcount
