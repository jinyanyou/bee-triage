"""1단계 - AI 챗봇 1차 문진 (대화형 트리아지).

기획서 3.1 그대로, 단순 FAQ 챗봇이 아니라 구조화된 슬롯을 다 채워야
끝나는 목적지향형 대화(Task-oriented Dialogue)다.

핵심 설계: FSM은 파이썬이 하드코딩해서 소유하고, LLM은 "자연어 → 슬롯 값"
추출과 재질문 문장 생성만 담당한다. 어떤 슬롯이 남았는지, 다음에 뭘 물을지,
사진 없이 다음 단계로 넘어가도 되는지는 LLM이 정하지 않는다.
LLM이 환각을 일으켜도 불완전한 신고가 접수되지 않게 하기 위함이다.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from . import config
from .schemas import Slots

# FSM이 채워야 하는 슬롯의 순서. photo_uploaded는 대화가 아니라
# 업로드 엔드포인트가 채우므로 여기 포함하지 않는다.
SLOT_ORDER: List[str] = [
    "location_type",
    "nest_size_cm",
    "discovered_days_ago",
    "aggression_observed",
    "reporter_risk_self_assessment",
]

QUESTIONS: Dict[str, str] = {
    "location_type": "벌집이 어디에 있나요? (집 안 / 처마나 창틀 / 바닥 근처 / 바깥 나무나 외벽)",
    "nest_size_cm": "벌집 크기가 대략 어느 정도인가요? 야구공, 주먹, 축구공처럼 비교해서 말씀해주셔도 됩니다.",
    "discovered_days_ago": "벌집을 처음 발견하신 게 언제쯤인가요? (오늘, 3일 전, 2주 전처럼요)",
    "aggression_observed": "벌이 사람 쪽으로 달려들거나 벌집 주변을 왕성하게 날아다니나요?",
    "reporter_risk_self_assessment": "지금 상황이 얼마나 위험하다고 느끼시나요? (낮음 / 보통 / 높음)",
}

SLOT_LABELS: Dict[str, str] = {
    "location_type": "위치",
    "nest_size_cm": "벌집 크기",
    "discovered_days_ago": "발견 경과일",
    "aggression_observed": "공격성 관찰",
    "reporter_risk_self_assessment": "신고자 체감 위험",
    "photo_uploaded": "사진 첨부",
}

OPENING = (
    "안녕하세요. 벌집 신고 도우미입니다. 몇 가지만 여쭤보고 사진 한 장 받으면 "
    "위험한 벌인지, 119를 불러야 하는 상황인지 바로 알려드릴게요. "
    "무리해서 벌집에 가까이 가지 마시고 답해주세요."
)

EXTRACT_TOOL_NAME = "fill_report_slots"

EXTRACT_SYSTEM = """너는 119 벌집 신고 전처리 챗봇의 슬롯 추출기다.
겁먹은 시민이 쓴 한 문장에서 아래 슬롯 값을 뽑아 반드시 fill_report_slots
도구를 호출해 기록한다.

- location_type: 실내 / 실외 / 처마/창틀 / 지면 중 하나.
  베란다 방충망, 창문틀, 지붕 처마, 에어컨 실외기 위는 "처마/창틀".
  방, 거실, 창고 안, 다락은 "실내". 땅속, 화단, 잔디, 담벼락 아래는 "지면".
  나무, 전봇대, 건물 외벽 높은 곳은 "실외".
- nest_size_cm: 지름 추정치(cm). 비유는 이렇게 환산한다.
  탁구공 4, 야구공 7, 주먹 10, 손바닥 12, 축구공 22, 농구공 24, 수박 30.
- discovered_days_ago: 처음 본 뒤 지난 날수(정수). 오늘/방금은 0, 어제는 1,
  "일주일 전"은 7, "한 달쯤"은 30.
- aggression_observed: 벌이 달려들거나 왕성하게 드나든다 -> true.
  몇 마리만 보이거나 조용하다 -> false.
- reporter_risk_self_assessment: 낮음 / 보통 / 높음. "무섭다", "위험해 보인다"는 높음.

규칙:
1. 그 발화에서 확실히 읽히는 슬롯만 채운다. 나머지는 null로 둔다. 추측하지 마라.
2. reply 는 시민에게 보여줄 공감 한 문장이다. 짧고 침착하게, 존댓말로 쓴다.
   여기서 위험도를 단정하거나 벌 종류를 추측하지 마라. 그건 다음 단계가 한다.
3. 사용자가 "몰라요", "그냥 무서워요"처럼 모호하게 답했고 지금 물어본 슬롯이
   안 채워졌으면 needs_clarification 을 true 로 하고, 답하기 쉬운 형태로 쪼갠
   재질문을 clarification_question 에 넣는다.
   예: "혹시 벌이 벌집 주변을 왕성하게 날아다니나요, 아니면 몇 마리만 보이나요?"
4. 시민이 직접 벌집에 접근하려는 낌새가 있으면 reply 에서 한 번만 만류한다.

## 추출 예시

[예시 1] 이번에 물어본 슬롯: location_type / 답변: "아파트 12층 베란다 방충망 바깥에 붙어 있어요"
-> location_type="처마/창틀", 나머지 null, reply="베란다 방충망 바깥쪽이군요. 창문은 당분간 열지 말아 주세요.",
   needs_clarification=false

[예시 2] 이번에 물어본 슬롯: nest_size_cm / 답변: "잘 모르겠어요 무서워서 가까이 못 갔어요"
-> 모든 슬롯 null, reply="가까이 가지 않으신 게 맞습니다.",
   needs_clarification=true,
   clarification_question="멀리서 보신 느낌으로 괜찮아요. 야구공, 주먹, 축구공 중에 어느 쪽에 가까운가요?"

[예시 3] 이번에 물어본 슬롯: aggression_observed / 답변: "한 일주일 됐고 벌이 계속 들락날락해요"
-> discovered_days_ago=7, aggression_observed=true, 나머지 null,
   reply="일주일 정도 됐고 벌이 활발히 드나드는 상태로 기록했습니다.", needs_clarification=false
"""

EXTRACT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "location_type": {
            "type": ["string", "null"],
            "enum": ["실내", "실외", "처마/창틀", "지면", None],
        },
        "nest_size_cm": {"type": ["number", "null"]},
        "discovered_days_ago": {"type": ["integer", "null"]},
        "aggression_observed": {"type": ["boolean", "null"]},
        "reporter_risk_self_assessment": {
            "type": ["string", "null"],
            "enum": ["낮음", "보통", "높음", None],
        },
        "reply": {"type": "string"},
        "needs_clarification": {"type": "boolean"},
        "clarification_question": {"type": ["string", "null"]},
    },
    "required": [
        "location_type",
        "nest_size_cm",
        "discovered_days_ago",
        "aggression_observed",
        "reporter_risk_self_assessment",
        "reply",
        "needs_clarification",
        "clarification_question",
    ],
    "additionalProperties": False,
}

EXTRACT_TOOL: Dict[str, Any] = {
    "name": EXTRACT_TOOL_NAME,
    "description": (
        "시민의 답변에서 읽어낸 신고 슬롯 값을 채운다. 확실히 읽히는 슬롯만 채우고 "
        "나머지는 null로 둔다. 답변을 처리할 때마다 반드시 이 도구를 호출해야 한다."
    ),
    "strict": True,
    "input_schema": EXTRACT_SCHEMA,
}


# ------------------------------------------------------------------ FSM
def next_slot(slots: Slots) -> Optional[str]:
    """아직 안 채워진 첫 슬롯. 전부 찼으면 None."""
    data = slots.model_dump()
    for key in SLOT_ORDER:
        if data.get(key) is None:
            return key
    return None


def filled_slots(slots: Slots) -> List[str]:
    data = slots.model_dump()
    return [k for k in SLOT_ORDER if data.get(k) is not None]


def progress(slots: Slots) -> Dict[str, Any]:
    done = len(filled_slots(slots))
    return {
        "filled": done,
        "total": len(SLOT_ORDER),
        "photo_uploaded": slots.photo_uploaded,
        "ready_for_photo": done == len(SLOT_ORDER),
        "ready_for_assessment": done == len(SLOT_ORDER) and slots.photo_uploaded,
        "labels": {k: SLOT_LABELS[k] for k in SLOT_ORDER},
    }


def question_for(slot: Optional[str]) -> Optional[str]:
    return QUESTIONS.get(slot) if slot else None


# ------------------------------------------------------ 규칙 기반 추출(폴백)
_SIZE_WORDS = [
    ("탁구공", 4.0), ("야구공", 7.0), ("계란", 5.0), ("주먹", 10.0),
    ("손바닥", 12.0), ("배구공", 21.0), ("축구공", 22.0), ("농구공", 24.0),
    ("럭비공", 25.0), ("수박", 30.0),
]

# 아라비아 숫자가 없는 한글 기간 표현. 긴 표현부터 검사해야
# "일주일"이 "일"로 잘못 걸리지 않는다.
_DAY_WORDS = [
    ("반년", 180), ("한달", 30), ("한 달", 30), ("두달", 60), ("두 달", 60),
    ("일주일", 7), ("이주일", 14), ("한주", 7), ("한 주", 7), ("이틀", 2),
    ("사흘", 3), ("나흘", 4), ("닷새", 5), ("엿새", 6), ("일주", 7),
    ("며칠", 3), ("얼마 안", 1),
]

_AGGRESSIVE_WORDS = (
    "달려들", "쏘", "공격", "왕성", "많이 날", "윙윙", "떼로", "드나들",
    "왔다갔다", "왔다 갔다", "들락", "계속 날", "바쁘게", "우글", "새까맣",
)

_CALM_WORDS = (
    "안 그래", "안그래", "조용", "가만", "몇 마리", "없어", "없습니다",
    "아니", "한두 마리", "거의 안",
)


def _rule_extract(message: str, target: Optional[str]) -> Dict[str, Any]:
    text = message.strip()
    out: Dict[str, Any] = {k: None for k in SLOT_ORDER}

    # 위치
    if any(w in text for w in ("처마", "창틀", "창문", "베란다", "방충망", "실외기", "새시", "샷시")):
        out["location_type"] = "처마/창틀"
    elif any(w in text for w in ("실내", "방 안", "거실", "안방", "창고 안", "다락", "집 안", "화장실")):
        out["location_type"] = "실내"
    elif any(w in text for w in ("땅", "지면", "바닥", "화단", "잔디", "풀숲", "담벼락")):
        out["location_type"] = "지면"
    elif any(w in text for w in ("나무", "전봇대", "외벽", "지붕", "밖", "실외", "옥상")):
        out["location_type"] = "실외"

    # 크기
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:cm|센티|센치)", text)
    if m:
        out["nest_size_cm"] = float(m.group(1))
    else:
        for word, cm in _SIZE_WORDS:
            if word in text:
                out["nest_size_cm"] = cm
                break

    # 경과일
    if any(w in text for w in ("오늘", "방금", "아까", "조금 전")):
        out["discovered_days_ago"] = 0
    elif "어제" in text:
        out["discovered_days_ago"] = 1
    elif "그저께" in text or "그제" in text:
        out["discovered_days_ago"] = 2
    else:
        m = re.search(r"(\d+)\s*(개월|주일|일|주|달|년)", text)
        if m:
            n, unit = int(m.group(1)), m.group(2)
            mult = {"일": 1, "주": 7, "주일": 7, "달": 30, "개월": 30, "년": 365}[unit]
            out["discovered_days_ago"] = n * mult
        else:
            for word, days in _DAY_WORDS:
                if word in text:
                    out["discovered_days_ago"] = days
                    break

    # 공격성
    if any(w in text for w in _AGGRESSIVE_WORDS):
        out["aggression_observed"] = True
    elif any(w in text for w in _CALM_WORDS):
        out["aggression_observed"] = False

    # 체감 위험
    if any(w in text for w in ("높음", "무서", "위험", "겁", "불안", "큰일")):
        out["reporter_risk_self_assessment"] = "높음"
    elif "보통" in text:
        out["reporter_risk_self_assessment"] = "보통"
    elif any(w in text for w in ("낮음", "괜찮", "별로", "안 위험")):
        out["reporter_risk_self_assessment"] = "낮음"

    # 지금 물어본 슬롯이 예/아니오형인데 단답으로 답한 경우 보정
    if target == "aggression_observed" and out["aggression_observed"] is None:
        if re.fullmatch(r"\s*(네|넵|예|응|어|yes|y)\s*[.!]?\s*", text, re.I):
            out["aggression_observed"] = True
        elif re.fullmatch(r"\s*(아니요|아니오|아뇨|아니|no|n)\s*[.!]?\s*", text, re.I):
            out["aggression_observed"] = False

    ambiguous = bool(re.search(r"몰라|모르겠|글쎄|잘 안 보|안 보여|기억 안", text))
    out["reply"] = ""
    out["needs_clarification"] = ambiguous and (target is not None and out.get(target) is None)
    out["clarification_question"] = None
    if out["needs_clarification"] and target == "aggression_observed":
        out["clarification_question"] = (
            "괜찮습니다. 멀리서 보시기에 벌이 벌집 주변을 계속 바쁘게 날아다니나요, "
            "아니면 몇 마리만 보이나요?"
        )
    elif out["needs_clarification"] and target == "nest_size_cm":
        out["clarification_question"] = (
            "정확하지 않아도 괜찮아요. 야구공, 주먹, 축구공 중에 어느 쪽에 가까운가요?"
        )
    elif out["needs_clarification"]:
        out["reply"] = "괜찮습니다. 아시는 만큼만 답해주셔도 돼요."
    return out


# 규칙 기반 모드에서 쓰는 확인 문구. LLM 모드에서는 모델이 직접 생성한다.
_ACK = {
    "location_type": "{}에 있는 벌집으로 기록했습니다.",
    "nest_size_cm": "지름 {:.0f}cm 정도로 기록했습니다.",
    "discovered_days_ago": "발견한 지 {}일 지난 것으로 기록했습니다.",
    "aggression_observed": "{} 상태로 기록했습니다.",
    "reporter_risk_self_assessment": "체감 위험은 '{}'으로 기록했습니다.",
}


def _ack_message(newly_filled: List[str], slots: Slots) -> str:
    if not newly_filled:
        return "네, 말씀 확인했습니다."
    parts = []
    for key in newly_filled:
        value = getattr(slots, key)
        if key == "aggression_observed":
            value = "벌의 움직임이 활발한" if value else "벌이 크게 움직이지 않는"
        parts.append(_ACK[key].format(value))
    return " ".join(parts)


# 안전장치: 같은 슬롯을 이 횟수만큼 물어도 답을 못 받으면 보수적 기본값으로 넘어간다.
# 시연 중에 챗봇이 같은 질문을 무한 반복하는 상황을 막는다.
MAX_ATTEMPTS = 3

# 확인이 안 될 때는 위험한 쪽으로 가정한다.
CONSERVATIVE_DEFAULTS: Dict[str, Any] = {
    "location_type": "처마/창틀",
    "nest_size_cm": 15.0,
    "discovered_days_ago": 0,
    "aggression_observed": True,
    "reporter_risk_self_assessment": "높음",
}


# ------------------------------------------------------------- LLM 추출
def _llm_extract(
    message: str, slots: Slots, target: Optional[str]
) -> Tuple[Dict[str, Any], Optional[str]]:
    import anthropic

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, timeout=60.0)
    known = {k: v for k, v in slots.model_dump().items() if v is not None}

    user_text = (
        "지금까지 확보된 슬롯: {known}\n"
        "이번에 물어본 슬롯: {target}\n"
        "시민의 답변: {msg}"
    ).format(
        known=json.dumps(known, ensure_ascii=False),
        target=target or "(없음)",
        msg=message,
    )

    try:
        response = client.messages.create(
            model=config.MODEL,
            max_tokens=4000,
            system=EXTRACT_SYSTEM,
            messages=[{"role": "user", "content": user_text}],
            tools=[EXTRACT_TOOL],
            tool_choice={"type": "tool", "name": EXTRACT_TOOL_NAME},
            output_config={"effort": "low"},
        )
    except Exception as exc:
        return _rule_extract(message, target), "슬롯 추출 API 실패, 규칙 기반으로 대체: {}".format(
            type(exc).__name__
        )

    block = next((b for b in response.content if b.type == "tool_use"), None)
    if block is None:
        return _rule_extract(message, target), "모델이 슬롯 도구를 호출하지 않아 규칙 기반으로 대체"
    return dict(block.input), None


# ---------------------------------------------------------------- 공개 API
def handle_message(
    message: str, slots: Slots, attempts: Optional[Dict[str, int]] = None
) -> Dict[str, Any]:
    """시민 발화 한 건을 처리해 슬롯을 갱신하고 다음 질문을 정한다.

    attempts는 슬롯별 질문 횟수다. 같은 슬롯을 MAX_ATTEMPTS번 물어도 답이
    안 나오면 보수적 기본값으로 채우고 넘어간다.
    """
    attempts = attempts if attempts is not None else {}
    target = next_slot(slots)

    if config.LIVE_MODE:
        extracted, warning = _llm_extract(message, slots, target)
        engine = "rule" if warning else "claude"
    else:
        extracted, warning = _rule_extract(message, target), None
        engine = "rule"

    # FSM이 슬롯 적용을 소유한다. 이미 채워진 슬롯은 덮어쓰지 않는다.
    updated = slots.model_copy()
    newly_filled: List[str] = []
    for key in SLOT_ORDER:
        value = extracted.get(key)
        if value is None or getattr(updated, key) is not None:
            continue
        try:
            setattr(updated, key, value)
            Slots.model_validate(updated.model_dump())  # 잘못된 enum 값 방어
            newly_filled.append(key)
        except Exception:
            setattr(updated, key, None)

    assumed: Optional[str] = None
    if target is not None and getattr(updated, target) is None:
        attempts[target] = attempts.get(target, 0) + 1
        if attempts[target] >= MAX_ATTEMPTS:
            setattr(updated, target, CONSERVATIVE_DEFAULTS[target])
            assumed = target

    still = next_slot(updated)
    if (
        extracted.get("needs_clarification")
        and extracted.get("clarification_question")
        and still == target
    ):
        question = extracted["clarification_question"]
    else:
        question = question_for(still)

    reply = extracted.get("reply") or _ack_message(newly_filled, updated)
    if assumed:
        reply = (
            "확인이 어려우시군요. 안전을 위해 '{}' 항목은 위험한 쪽으로 가정하고 "
            "넘어가겠습니다. 실제 확인은 담당자가 합니다."
        ).format(SLOT_LABELS[assumed])

    return {
        "reply": reply,
        "question": question,
        "slots": updated,
        "engine": engine,
        "warning": warning,
        "attempts": attempts,
        "assumed": assumed,
    }
