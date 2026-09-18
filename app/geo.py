"""위치 보조 기능.

1) 사진 EXIF에서 촬영 좌표 추출 — 시민이 주소를 입력하는 단계를 없앤다.
2) 주소/지명 검색 — 카카오 지오코딩 키 없이, 1팀 매칭 DB와 119안전센터
   주소를 색인해 만든 자체 가제티어로 처리한다. 정식 서비스에서는
   카카오/도로명주소 API로 교체한다(기획서 3.4).
"""
from __future__ import annotations

import io
import re
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

from . import data_store

# ------------------------------------------------------------------ EXIF
_GPS_IFD_TAG = 0x8825
_GPS_TAGS = {1: "lat_ref", 2: "lat", 3: "lon_ref", 4: "lon"}


def _dms_to_deg(dms: Any) -> Optional[float]:
    try:
        d, m, s = (float(x) for x in dms)
    except (TypeError, ValueError):
        return None
    return d + m / 60.0 + s / 3600.0


def exif_gps(image_bytes: bytes) -> Optional[Tuple[float, float]]:
    """사진에 촬영 좌표가 있으면 (위도, 경도)를 돌려준다.

    카카오톡 등으로 전달된 사진은 EXIF가 제거된 경우가 많아 대부분 None이다.
    실패하면 조용히 None을 준다. 위치 입력은 어차피 수동 대체가 가능하다.
    """
    try:
        from PIL import Image
    except ImportError:
        return None

    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            exif = img.getexif()
            if not exif:
                return None
            gps = exif.get_ifd(_GPS_IFD_TAG)
    except Exception:
        return None

    if not gps:
        return None

    values: Dict[str, Any] = {}
    for tag, name in _GPS_TAGS.items():
        if tag in gps:
            values[name] = gps[tag]

    lat = _dms_to_deg(values.get("lat"))
    lon = _dms_to_deg(values.get("lon"))
    if lat is None or lon is None:
        return None

    if str(values.get("lat_ref", "N")).upper().startswith("S"):
        lat = -lat
    if str(values.get("lon_ref", "E")).upper().startswith("W"):
        lon = -lon

    # 한반도 범위를 크게 벗어나면 잘못 읽은 것으로 본다.
    if not (32.0 <= lat <= 40.0 and 124.0 <= lon <= 132.0):
        return None
    return round(lat, 6), round(lon, 6)


# -------------------------------------------------------------- 가제티어
_ROAD_RE = re.compile(r"([가-힣A-Za-z0-9]+(?:대로|로|길))")


@lru_cache(maxsize=1)
def _gazetteer() -> List[Dict[str, Any]]:
    """검색 가능한 장소 후보 목록.

    - 시군구 중심점 (업체 좌표 평균)
    - 119안전센터 (이름 + 주소)
    - 도로명 단위 대표점 (같은 도로명 업체들의 평균)
    """
    places: List[Dict[str, Any]] = []

    # 1) 시군구 중심
    buckets: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    roads: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    for c in data_store.companies():
        key = (c["시도"], c["시군구"])
        buckets.setdefault(key, []).append(c)
        m = _ROAD_RE.search(c["도로명주소"].replace(c["시도"], "").replace(c["시군구"], ""))
        if m:
            roads.setdefault((c["시도"], c["시군구"], m.group(1)), []).append(c)

    for (sido, sigungu), items in buckets.items():
        places.append(
            {
                "label": "{} {}".format(sido, sigungu),
                "detail": "시군구 중심 (업체 {}곳 평균)".format(len(items)),
                "kind": "시군구",
                "lat": round(sum(i["위도"] for i in items) / len(items), 6),
                "lon": round(sum(i["경도"] for i in items) / len(items), 6),
            }
        )

    # 2) 119안전센터
    for f in data_store.fire_centers():
        places.append(
            {
                "label": "{} {}".format(f["소방서명"], f["안전센터명"]),
                "detail": f["주소"],
                "kind": "안전센터",
                "lat": f["위도"],
                "lon": f["경도"],
            }
        )

    # 3) 도로명 대표점 (업체가 2곳 이상인 도로만 — 대표성이 없는 단일 지점 제외)
    for (sido, sigungu, road), items in roads.items():
        if len(items) < 2:
            continue
        places.append(
            {
                "label": "{} {} {}".format(sido, sigungu, road),
                "detail": "도로명 대표점 (업체 {}곳 평균)".format(len(items)),
                "kind": "도로명",
                "lat": round(sum(i["위도"] for i in items) / len(items), 6),
                "lon": round(sum(i["경도"] for i in items) / len(items), 6),
            }
        )

    return places


_KIND_ORDER = {"시군구": 0, "안전센터": 1, "도로명": 2}


def search_places(query: str, limit: int = 8) -> List[Dict[str, Any]]:
    q = query.strip()
    if len(q) < 2:
        return []

    hits = []
    for p in _gazetteer():
        haystack = p["label"] + " " + p["detail"]
        if q in haystack:
            # 라벨 앞쪽에서 맞을수록 상위
            pos = p["label"].find(q)
            rank = (_KIND_ORDER.get(p["kind"], 9), 0 if pos >= 0 else 1, pos if pos >= 0 else 999)
            hits.append((rank, p))

    hits.sort(key=lambda x: x[0])
    return [p for _, p in hits[:limit]]


def parse_coords(text: str) -> Optional[Tuple[float, float]]:
    """"35.8690, 128.5947" 같은 직접 입력을 받아준다."""
    m = re.fullmatch(
        r"\s*(-?\d+(?:\.\d+)?)\s*[,/ ]\s*(-?\d+(?:\.\d+)?)\s*", text or ""
    )
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if not (32.0 <= lat <= 40.0 and 124.0 <= lon <= 132.0):
        return None
    return lat, lon
