"""4단계 - 채널 연계.

기획서 3.3의 세 갈래를 실제 데이터로 채운다.
 - 119: 실제 시스템 연동 대신 상황실 접수 양식용 신고 요약을 자동 생성(API 스텁)
 - 방역업체: 매칭 DB에서 거리순 상위 N곳
 - 양봉협회: 시도 지회 연락 창구
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from . import config, data_store
from .schemas import ScoreBreakdown, Slots, VisionResult

_GRADE_ACTION = {
    "고위험": "보호복 착용 후 즉시 제거 필요",
    "중위험": "접근 차단 후 제거 권고",
    "저위험": "긴급도 낮음",
    "판별불가": "현장 확인 필요",
}


def sido_of(lat: float, lon: float) -> str:
    """가장 가까운 안전센터의 시도를 신고 지점의 시도로 본다."""
    nearest, best = "", float("inf")
    for f in data_store.fire_centers():
        d = data_store.haversine_km(lat, lon, f["위도"], f["경도"])
        if d < best:
            best, nearest = d, f["주소"]
    if nearest.startswith("대구"):
        return "대구광역시"
    if nearest.startswith("경상북도"):
        return "경상북도"
    return ""


def build_report_summary(
    slots: Slots,
    vision: VisionResult,
    score: ScoreBreakdown,
    route_reason: str,
    address_label: str,
    lat: float,
    lon: float,
    fire_center: Optional[Dict[str, Any]],
) -> str:
    """119 상황실 접수 양식용 요약문.

    실제 119 시스템에 전송하지 않는다. 프로토타입 범위는 '자동 생성'까지다.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    size = vision.estimated_nest_diameter_cm or slots.nest_size_cm or 0
    center_line = (
        "{} {} (약 {}km)".format(
            fire_center.get("소방서명", ""),
            fire_center.get("안전센터명", ""),
            fire_center.get("거리km", "-"),
        )
        if fire_center
        else "관할 센터 확인 필요"
    )

    lines = [
        "[벌집 신고 자동 요약 - AI 사전 분류 결과]",
        "접수시각 : {}".format(now),
        "신고위치 : {} (위도 {:.5f}, 경도 {:.5f})".format(address_label or "-", lat, lon),
        "관할센터 : {}".format(center_line),
        "",
        "판정등급 : {} / 추정종 {} / AI 신뢰도 {:.0%}".format(
            vision.risk_grade, vision.species_guess, vision.confidence
        ),
        "위험점수 : {:.2f} (비전 {:.2f} + 문진 {:.2f} + 크기 {:.2f})".format(
            score.final_score,
            score.vision_component,
            score.interview_component,
            score.size_component,
        ),
        "권고조치 : {}".format(_GRADE_ACTION.get(vision.risk_grade, "-")),
        "",
        "현장정보 : 설치위치 {} / 추정지름 {:.0f}cm / 발견 후 {}일 경과".format(
            slots.location_type or "-", size, slots.discovered_days_ago or 0
        ),
        "           공격성 {} / 신고자 체감 위험 {}".format(
            "관찰됨" if slots.aggression_observed else "관찰되지 않음",
            slots.reporter_risk_self_assessment or "-",
        ),
        "사진첨부 : {}".format("있음" if slots.photo_uploaded else "없음"),
        "",
        "판정근거 : {}".format(vision.evidence),
        "연계사유 : {}".format(route_reason),
        "",
        "※ AI 판정은 참고 정보이며 최종 판단은 상황실/현장 대원이 합니다.",
    ]
    return "\n".join(lines)


def build_dispatch(
    route: str,
    route_reason: str,
    slots: Slots,
    vision: VisionResult,
    score: ScoreBreakdown,
    lat: float,
    lon: float,
    address_label: str,
) -> Dict[str, Any]:
    notes: List[str] = []
    fire_center = data_store.nearest_fire_center(lat, lon)

    companies: List[Dict[str, Any]] = []
    branch: Optional[Dict[str, Any]] = None
    report_summary: Optional[str] = None

    if route == "119":
        report_summary = build_report_summary(
            slots, vision, score, route_reason, address_label, lat, lon, fire_center
        )
        notes.append(
            "프로토타입 단계에서는 실제 119 시스템에 전송하지 않고 상황실 접수 양식용 "
            "요약 생성까지만 구현했습니다(기획서 3.3)."
        )
    else:
        companies = data_store.nearest_companies(lat, lon)
        if not companies:
            notes.append(
                "반경 {:.0f}km 안에 연락 가능한 소독·방역업체가 없습니다. "
                "매칭 공백 지역으로, 파일럿 단계에서 지자체·협회 협력으로 "
                "연락망을 보완해야 하는 사례입니다.".format(config.MATCH_MAX_RADIUS_KM)
            )
        if route == "양봉협회":
            branch = data_store.branch_for_sido(sido_of(lat, lon))
            if branch is None:
                notes.append("해당 시도의 양봉협회 지회 정보를 찾지 못했습니다.")
            notes.append(
                "양봉인 개인 연락망은 공공데이터로 제공되지 않아, 협회 안내에 따라 "
                "본회 사무국을 경유하는 구조로 구현했습니다."
            )
            if companies:
                notes.append("회수 희망 양봉인이 없을 경우를 대비해 인근 방역업체도 함께 제시합니다.")

    return {
        "companies": companies,
        "beekeeping_branch": branch,
        "fire_center": fire_center,
        "report_summary": report_summary,
        "notes": notes,
    }
