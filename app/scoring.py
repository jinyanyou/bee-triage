"""3단계 - 위험도 기반 스코어링과 라우팅 결정.

기획서 3.3의 가중합을 그대로 구현한다.

    최종위험점수 = 비전모델 위험등급 가중치 x 0.60
                 + 문진 공격성/장소 위험 가중치 x 0.25
                 + 벌집 크기 정규화 점수 x 0.15

심사 때 "그래서 왜 이 결정이 나왔는데?"에 답할 수 있어야 하므로,
점수만 내지 않고 각 항목의 입력값과 기여분을 detail에 그대로 담는다.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from . import config
from .schemas import ScoreBreakdown, Slots, VisionResult


def _interview_score(slots: Slots) -> Tuple[float, List[Dict[str, Any]]]:
    parts: List[Dict[str, Any]] = []

    loc_w = config.LOCATION_WEIGHT.get(slots.location_type or "", 0.3)
    parts.append({"항목": "장소", "입력": slots.location_type, "가중치": loc_w, "비중": 0.45})

    agg = 1.0 if slots.aggression_observed else 0.0
    parts.append(
        {"항목": "공격성 관찰", "입력": slots.aggression_observed, "가중치": agg, "비중": 0.30}
    )

    self_w = config.SELF_ASSESS_WEIGHT.get(slots.reporter_risk_self_assessment or "", 0.5)
    parts.append(
        {
            "항목": "신고자 체감",
            "입력": slots.reporter_risk_self_assessment,
            "가중치": self_w,
            "비중": 0.15,
        }
    )

    days = slots.discovered_days_ago or 0
    aged = min(days / 30.0, 1.0)
    parts.append({"항목": "방치 기간", "입력": "{}일".format(days), "가중치": round(aged, 3), "비중": 0.10})

    score = 0.45 * loc_w + 0.30 * agg + 0.15 * self_w + 0.10 * aged
    return min(max(score, 0.0), 1.0), parts


def _effective_size_cm(slots: Slots, vision: VisionResult) -> Tuple[float, str]:
    """기획서 3.2: 비전의 크기 추정으로 문진의 크기 슬롯을 보정한다."""
    if vision.estimated_nest_diameter_cm is not None:
        return float(vision.estimated_nest_diameter_cm), "비전 추정(문진값 보정)"
    return float(slots.nest_size_cm or 0.0), "문진 응답"


def compute(slots: Slots, vision: VisionResult) -> ScoreBreakdown:
    interview, interview_parts = _interview_score(slots)
    size_cm, size_source = _effective_size_cm(slots, vision)
    size_norm = min(size_cm / config.SIZE_NORM_CM, 1.0)

    # 판별불가는 점수로 안전을 판단할 수 없는 상태다. 라우팅에서 점수와 무관하게
    # 119로 우회되며, 점수 표시상으로도 최대 위험으로 간주한다는 점을 명시한다.
    is_unknown = vision.risk_grade == "판별불가"
    vision_w = 1.0 if is_unknown else config.VISION_GRADE_WEIGHT[vision.risk_grade]

    v_comp = config.W_VISION * vision_w
    i_comp = config.W_INTERVIEW * interview
    s_comp = config.W_SIZE * size_norm
    final = v_comp + i_comp + s_comp

    vision_input = "{} (신뢰도 {:.0%})".format(vision.risk_grade, vision.confidence)
    if is_unknown:
        vision_input += " · 보수적으로 최대 가중치 적용"

    detail: List[Dict[str, Any]] = [
        {
            "구분": "비전 모델",
            "비중": config.W_VISION,
            "입력": vision_input,
            "정규화값": round(vision_w, 3),
            "기여": round(v_comp, 3),
        },
        {
            "구분": "챗봇 문진",
            "비중": config.W_INTERVIEW,
            "입력": "장소 {} / 공격성 {}".format(
                slots.location_type, "관찰됨" if slots.aggression_observed else "없음"
            ),
            "정규화값": round(interview, 3),
            "기여": round(i_comp, 3),
            "세부": interview_parts,
        },
        {
            "구분": "벌집 크기",
            "비중": config.W_SIZE,
            "입력": "{:.0f}cm ({})".format(size_cm, size_source),
            "정규화값": round(size_norm, 3),
            "기여": round(s_comp, 3),
        },
    ]

    return ScoreBreakdown(
        vision_grade=vision.risk_grade,
        vision_component=round(v_comp, 3),
        interview_component=round(i_comp, 3),
        size_component=round(s_comp, 3),
        final_score=round(final, 3),
        detail=detail,
    )


def decide_route(score: ScoreBreakdown, vision: VisionResult) -> Tuple[str, str]:
    """라우팅 결정. (경로, 사유)"""
    # 1) 안전장치가 최우선이다. 확신 없는 건은 점수와 무관하게 119.
    if vision.risk_grade == "판별불가":
        if vision.downgraded_by_threshold:
            reason = (
                "AI 판별 신뢰도가 {:.0%}로 기준치 {:.0%}에 못 미쳐 "
                "자동 분류하지 않고 119 상황실 확인 대상으로 넘겼습니다."
            ).format(vision.confidence, config.CONFIDENCE_THRESHOLD)
        else:
            reason = (
                "사진에서 벌 종류를 특정할 수 없어 저위험으로 단정하지 않고 "
                "119 상황실 확인 대상으로 넘겼습니다."
            )
        return "119", reason

    # 2) 가중합이 임계값을 넘으면 119
    if score.final_score >= config.ROUTE_119_THRESHOLD:
        return "119", (
            "최종 위험점수 {:.2f}가 119 연계 임계값 {:.2f} 이상입니다. "
            "{} 판정에 현장 조건이 더해져 긴급 대응이 필요한 건으로 분류했습니다."
        ).format(score.final_score, config.ROUTE_119_THRESHOLD, vision.risk_grade)

    # 3) 꿀벌 분봉은 회수 가치가 있어 양봉협회로
    if vision.is_swarm:
        return "양봉협회", (
            "꿀벌 분봉으로 판정됐습니다(위험점수 {:.2f}). 분봉 중인 꿀벌은 공격성이 낮고 "
            "양봉업자가 회수하면 벌통으로 살릴 수 있어 양봉협회 지회로 연계합니다."
        ).format(score.final_score)

    # 4) 저위험 구간은 시민이 고른다.
    #    "불안한지"는 주관적이라 시스템이 대신 정하지 않는다.
    if score.final_score < config.SELF_CARE_OPTION_THRESHOLD:
        return "선택", (
            "최종 위험점수 {:.2f}로 당장 제거하지 않아도 안전에 큰 지장이 없는 단계입니다. "
            "그대로 두고 안전 수칙만 지키실지, 그래도 불안하셔서 업체에 맡기실지 "
            "직접 고르실 수 있습니다."
        ).format(score.final_score)

    # 5) 중위험대는 선택지 없이 업체로. 자극 시 공격하는 등급이라 방치를 권하지 않는다.
    return "방역업체", (
        "최종 위험점수 {:.2f}로 119 임계값 {:.2f} 미만입니다. 즉각적인 인명 위협 "
        "단계는 아니지만 자극하면 쏘일 수 있어 인근 소독·방역업체로 연계합니다."
    ).format(score.final_score, config.ROUTE_119_THRESHOLD)
