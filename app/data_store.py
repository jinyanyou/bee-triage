"""공공데이터 기반 매칭 DB 로더.

1팀이 정제한 `벌집플랫폼_데이터` 산출물을 그대로 읽는다.
pandas 없이 표준 csv 모듈만 사용해 설치 부담을 줄였다.
"""
from __future__ import annotations

import csv
import math
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config


def _read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    # 1팀 산출물은 BOM이 붙은 UTF-8이라 utf-8-sig로 읽는다.
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _f(value: str | None) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def _i(value: str | None) -> int:
    try:
        return int(float(value)) if value not in (None, "") else 0
    except ValueError:
        return 0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@lru_cache(maxsize=1)
def companies() -> List[Dict[str, Any]]:
    rows = _read_csv(config.DATA_DIR / "matching_db_disinfection_daegu_gb.csv")
    out: List[Dict[str, Any]] = []
    for r in rows:
        lat, lon = _f(r.get("위도")), _f(r.get("경도"))
        if lat is None or lon is None:
            continue
        out.append(
            {
                "업체ID": r.get("업체ID", ""),
                "업체명": r.get("업체명", ""),
                "시도": r.get("시도", ""),
                "시군구": r.get("시군구", ""),
                "도로명주소": r.get("도로명주소", ""),
                "전화번호": (r.get("전화번호") or "").strip() or None,
                "연락가능": (r.get("연락가능", "")).strip().lower() == "true",
                "보호복_수": _i(r.get("보호복_수")),
                "진공청소기_수": _i(r.get("진공청소기_수")),
                "위도": lat,
                "경도": lon,
            }
        )
    return out


@lru_cache(maxsize=1)
def fire_centers() -> List[Dict[str, Any]]:
    rows = _read_csv(config.DATA_DIR / "fire_centers_daegu_gb_with_proximity.csv")
    out: List[Dict[str, Any]] = []
    for r in rows:
        lat, lon = _f(r.get("위도")), _f(r.get("경도"))
        if lat is None or lon is None:
            continue
        out.append(
            {
                "소방서명": r.get("소방서명", ""),
                "안전센터명": r.get("119안전센터명", ""),
                "주소": r.get("주소", ""),
                "전화번호": (r.get("전화번호") or "").strip() or None,
                "위도": lat,
                "경도": lon,
            }
        )
    return out


@lru_cache(maxsize=1)
def association_branches() -> List[Dict[str, Any]]:
    rows = _read_csv(config.DATA_DIR / "beekeeping_association_branches.csv")
    return [
        {
            "지회": r.get("지회", ""),
            "회원수": _i(r.get("회원수")),
            "공식_문의창구": r.get("공식_문의창구", ""),
        }
        for r in rows
    ]


@lru_cache(maxsize=1)
def dispatch_stats() -> Dict[str, Any]:
    national = _read_csv(config.DATA_DIR / "stats_beehive_dispatch_national_2016_2025.csv")
    regional = _read_csv(config.DATA_DIR / "stats_beehive_dispatch_daegu_gb_2023_2025.csv")
    proximity = _read_csv(config.DATA_DIR / "summary_fire_center_proximity.csv")
    return {
        "national": [
            {
                "연도": _i(r.get("연도")),
                "생활안전출동_합계": _i(r.get("생활안전출동_합계")),
                "벌집제거_출동": _i(r.get("벌집제거_출동")),
                "벌집제거_비중": _f(r.get("벌집제거_비중")),
            }
            for r in national
        ],
        "regional": [
            {
                "본부": r.get("본부", ""),
                "연도": _i(r.get("연도")),
                "벌집제거_출동": _i(r.get("벌집제거_출동")),
                "벌집제거_비중": _f(r.get("벌집제거_비중")),
            }
            for r in regional
        ],
        "proximity": [
            {
                "시도": r.get("시도", ""),
                "안전센터수": _i(r.get("안전센터수")),
                "반경5km내_업체있음": _i(r.get("반경5km내_업체있음")),
                "최근접연락가능업체거리_중앙값km": _f(
                    r.get("최근접연락가능업체거리_중앙값km")
                ),
            }
            for r in proximity
        ],
    }


def nearest_companies(
    lat: float,
    lon: float,
    k: int = config.MATCH_TOP_K,
    contactable_only: bool = True,
    max_radius_km: float = config.MATCH_MAX_RADIUS_KM,
) -> List[Dict[str, Any]]:
    """신고 위치 기준 거리순 업체.

    PostGIS 반경 검색(기획서 3.3)의 프로토타입 대체 구현.
    전화번호가 없는 업체는 연락이 불가능해 매칭 의미가 없으므로 기본 제외한다.
    """
    pool = companies()
    if contactable_only:
        pool = [c for c in pool if c["연락가능"]]

    scored = []
    for c in pool:
        d = haversine_km(lat, lon, c["위도"], c["경도"])
        if d <= max_radius_km:
            scored.append({**c, "거리km": round(d, 2)})

    # 1순위 거리, 2순위 보호복 보유 수(벌집 제거 대응 역량 대리지표)
    scored.sort(key=lambda c: (c["거리km"], -c["보호복_수"]))
    return scored[:k]


def nearest_fire_center(lat: float, lon: float) -> Optional[Dict[str, Any]]:
    best, best_d = None, float("inf")
    for f in fire_centers():
        d = haversine_km(lat, lon, f["위도"], f["경도"])
        if d < best_d:
            best, best_d = f, d
    if best is None:
        return None
    return {**best, "거리km": round(best_d, 2)}


def branch_for_sido(sido: str) -> Optional[Dict[str, Any]]:
    key = "대구" if "대구" in sido else "경북" if ("경북" in sido or "경상북" in sido) else None
    if key is None:
        return None
    for b in association_branches():
        if b["지회"].startswith(key):
            return b
    return None


def data_health() -> Dict[str, Any]:
    comps = companies()
    return {
        "data_dir": str(config.DATA_DIR),
        "available": config.DATA_DIR.exists(),
        "업체_총건수": len(comps),
        "업체_연락가능": sum(1 for c in comps if c["연락가능"]),
        "안전센터_건수": len(fire_centers()),
        "양봉협회_지회": len(association_branches()),
    }
