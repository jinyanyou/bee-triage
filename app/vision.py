"""2단계 - 사진 기반 위험도 판별.

설계 배경
---------
초안에서는 YOLOv8 탐지 + EfficientNet 분류를 자체 학습할 계획이었으나,
공개된 벌 데이터셋(Roboflow bee-detection, AI-Hub 「지능형 양봉 데이터」,
Gratheon models-bee-detector)은 모두 **벌통 내부/입구를 양봉가가 촬영한**
꿀벌 카스트 도메인이고 말벌 클래스 자체가 없다. 우리에게 필요한 것은
"시민이 수 미터 밖에서 폰으로 찍은 처마 밑 벌집"을 말벌/꿀벌로 나누는
일이라 도메인이 어긋난다. 따라서 프로토타입에서는 멀티모달 LLM에
위험등급 기준과 **few-shot 예시**를 제시해 판별하고, 신고 누적 데이터가
쌓이는 2단계(파일럿)에서 자체 모델로 대체하는 경로를 택했다.

few-shot 구성 (기획서 3.2 / 프로토타입.png)
-------------------------------------------
1) 텍스트 예시: 판정 사례 3건을 입력→출력 쌍으로 시스템 프롬프트에 포함.
2) 이미지 예시: `reference/manifest.json`에 등록된 참조 사진을 판정 대상
   사진 앞에 붙여 in-context exemplar로 넣는다. 참조 사진이 없어도 동작한다.
   (reference/README.md 참고)

호출 방식 (기획서 3.1)
----------------------
Function Calling(Tool Use)으로 구현한다. 모델이 `record_bee_assessment`
도구를 호출하게 하고 그 입력을 판정 결과로 받는다. strict=True라 스키마를
벗어난 값이 올 수 없다.

안전장치(기획서 3.2-(3))
------------------------
신뢰도가 CONFIDENCE_THRESHOLD 미만이면 등급을 "판별불가"로 강등해
무조건 119 확인 큐로 보낸다. AI는 "안전을 확신할 때만" 저위험으로 분류한다.

독립성
------
문진 결과는 이 단계에 전달하지 않는다. 비전 0.6 / 문진 0.25 가중합이
같은 정보를 두 번 세지 않도록 두 신호를 독립적으로 유지한다.
"""
from __future__ import annotations

import base64
import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import config
from .schemas import VisionResult

TOOL_NAME = "record_bee_assessment"

SYSTEM_PROMPT = """너는 119 생활안전 신고 전처리 시스템의 벌집 위험도 판별 모듈이다.
시민이 스마트폰으로 촬영한 벌/벌집 사진 한 장을 보고 위험 등급을 판정한 뒤,
반드시 record_bee_assessment 도구를 호출해 결과를 기록한다.

## 위험 등급 기준 (국내 종 기준)

[고위험]
- 장수말벌(Vespa mandarinia): 몸길이 3~4cm로 압도적으로 큼. 머리와 가슴이 주황~황갈색,
  복부에 굵은 흑갈색 띠. 땅속, 나무 구멍, 처마 안쪽 등 은폐된 곳에 집을 지어 집이 잘 안 보인다.
- 등검은말벌(Vespa velutina, 생태계교란종): 가슴이 전체적으로 검고 배 끝마디만 황갈색,
  다리 끝이 선명한 노란색. 나무 높은 곳이나 건물 외벽에 매달린 큰 종 모양 폐쇄형 집.
- 그 밖의 말벌속(Vespa) 전반 — 좀말벌, 꼬마장수말벌, 털보말벌 등도 고위험으로 본다.
  말벌속은 공통적으로 종이질 외피로 덮인 폐쇄형 집을 짓고 집단 방어 공격을 한다.
  종을 정확히 못 가려도 "말벌속으로 보인다"면 고위험이다.
- 공통 신호: 벌집이 종이질 외피로 완전히 덮여 내부 육각방이 안 보임(폐쇄형),
  지름 20cm 이상, 개체가 활발히 드나듦.

[중위험]
- 쌍살벌류(Polistes 속, Parapolybia 속) — 뱀허물쌍살벌, 등검은쌍살벌 등:
  몸이 가늘고 허리가 잘록하며 다리를 늘어뜨리고 난다. 말벌속보다 뚜렷하게 가늘다.
- 집이 외피 없이 육각형 방이 그대로 노출된 우산/샤워기 모양(개방형). 대개 지름 10cm 내외.
- 자극하지 않으면 잘 쏘지 않으나 접촉 시 공격한다.

[저위험]
- 양봉꿀벌(Apis mellifera), 토종꿀벌(Apis cerana): 몸에 잔털이 많고 황갈색~검은 줄무늬,
  통통하고 둥근 체형. 말벌보다 뚜렷하게 작다(1.2~1.5cm).
- 벌집은 수직으로 늘어진 흰~노란 밀랍 판(comb) 형태.
- 분봉(swarm): 나뭇가지나 기둥에 수천 마리가 축구공~럭비공 모양으로 뭉쳐 매달린 덩어리.
  집 구조물이 없고 벌 자체가 덩어리를 이룬다. 이 상태의 꿀벌은 공격성이 매우 낮고
  양봉업자가 회수할 가치가 있다. 이 경우 is_swarm 을 true 로 준다.

## 판정 규칙

1. 보수적으로 판정한다. 확신이 없으면 confidence 를 낮게 주어라.
   등급을 억지로 낮추지 마라. 오분류로 사람이 다치는 쪽이 헛출동보다 훨씬 나쁘다.
2. 사진에 벌이나 벌집이 보이지 않거나, 너무 흐리거나 멀어서 특징을 못 읽으면
   risk_grade 를 "판별불가"로 하고 confidence 를 0.3 이하로 준다.
3. 종을 특정할 수 없지만 폐쇄형 대형 벌집처럼 위험 신호가 뚜렷하면
   species_guess 를 "말벌류(종 미상)", risk_grade 를 "고위험"으로 준다. 등급은 종보다 형태 우선.
   반대로 개방형 육각방이 그대로 드러난 소형 집이면 쌍살벌류일 가능성이 높아 중위험이다.
4. estimated_nest_diameter_cm 는 사진 속 참조물(벽돌 한 장 약 19cm, 창틀, 지붕기와,
   손, 방충망 격자)과 비교해 추정한다. 참조물이 없으면 null 로 둔다.
5. evidence 에는 어떤 시각적 근거로 그렇게 판단했는지 2~3문장으로 한국어로 적는다.
   시민에게 그대로 보여줄 문장이므로 전문용어는 풀어서 쓴다.
6. 사진에 벌과 무관한 것(사람 얼굴, 문서 등)만 있으면 판별불가로 처리한다.

## 판정 예시

아래는 올바른 판정 3건이다. 같은 기준과 같은 서술 밀도를 따르라.

[예시 1] 회갈색 종이질 외피로 완전히 덮인 지름 25cm급 벌집이 처마 밑에 매달려 있고,
아래쪽 출입구 한 곳으로 벌이 계속 드나든다. 개체는 가슴이 검고 다리 끝이 노랗다.
-> {"species_guess": "등검은말벌", "risk_grade": "고위험", "confidence": 0.88, "is_swarm": false,
    "estimated_nest_diameter_cm": 26, "visible_bee_count_band": "20+",
    "evidence": "벌집 전체가 종이질 껍질로 덮여 내부 육각방이 보이지 않는 폐쇄형입니다. 가슴이 검고 다리 끝이 노란 개체가 확인됩니다. 옆 창틀과 비교하면 지름이 25cm를 넘어 군체가 상당히 성장한 상태입니다."}

[예시 2] 외피 없이 육각형 방이 그대로 드러난 우산 모양 벌집이 방충망 바깥에 붙어 있다.
방충망 격자와 비교하면 지름 8cm 정도이고 벌 10여 마리가 붙어 있다.
-> {"species_guess": "뱀허물쌍살벌", "risk_grade": "중위험", "confidence": 0.79, "is_swarm": false,
    "estimated_nest_diameter_cm": 8.5, "visible_bee_count_band": "6-20",
    "evidence": "외피 없이 육각형 방이 드러난 개방형 벌집으로 쌍살벌류의 전형적인 형태입니다. 방충망 격자와 비교하면 지름 8cm 안팎의 초기 단계입니다. 먼저 건드리지 않으면 잘 쏘지 않지만 창문 동선과 가까워 접촉 위험은 남아 있습니다."}

[예시 3] 역광에 초점이 나가 벌집 표면 질감이 뭉개져 있고, 거리가 멀어 개체의 색 패턴이
읽히지 않는다. 폐쇄형인지 개방형인지도 구분되지 않는다.
-> {"species_guess": "판별 불가", "risk_grade": "판별불가", "confidence": 0.22, "is_swarm": false,
    "estimated_nest_diameter_cm": null, "visible_bee_count_band": "불명",
    "evidence": "역광과 흔들림으로 벌집 표면 질감과 개체의 색 패턴을 읽을 수 없습니다. 폐쇄형인지 개방형인지조차 구분되지 않아 종을 특정할 수 없습니다. 안전을 위해 저위험으로 단정하지 않고 119 확인 대상으로 넘깁니다."}
"""

VISION_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "species_guess": {
            "type": "string",
            "description": "추정 종. 특정 불가 시 '말벌류(종 미상)' 또는 '판별 불가'",
        },
        "risk_grade": {
            "type": "string",
            "enum": ["고위험", "중위험", "저위험", "판별불가"],
        },
        "confidence": {
            "type": "number",
            "description": "0.0~1.0 사이의 판정 확신도",
        },
        "is_swarm": {
            "type": "boolean",
            "description": "꿀벌 분봉(벌 덩어리) 여부. 양봉협회 회수 대상 판정에 쓰인다",
        },
        "estimated_nest_diameter_cm": {
            "type": ["number", "null"],
            "description": "참조물 기반 벌집 지름 추정(cm). 추정 불가 시 null",
        },
        "visible_bee_count_band": {
            "type": "string",
            "enum": ["0", "1-5", "6-20", "20+", "불명"],
        },
        "evidence": {"type": "string", "description": "판단 근거 2~3문장(한국어)"},
    },
    "required": [
        "species_guess",
        "risk_grade",
        "confidence",
        "is_swarm",
        "estimated_nest_diameter_cm",
        "visible_bee_count_band",
        "evidence",
    ],
    "additionalProperties": False,
}

ASSESSMENT_TOOL: Dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "사진에서 판독한 벌/벌집 위험도 판정 결과를 신고 시스템에 기록한다. "
        "판정을 마치면 반드시 이 도구를 호출해야 한다."
    ),
    "strict": True,
    "input_schema": VISION_JSON_SCHEMA,
}


# ------------------------------------------------------ few-shot 참조 이미지
REFERENCE_DIR = config.ROOT / "reference"
_MEDIA_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


@lru_cache(maxsize=1)
def reference_examples() -> List[Dict[str, Any]]:
    """reference/manifest.json에 등록된 few-shot 참조 사진을 읽는다.

    형식:
        [{"file": "velutina_01.jpg", "risk_grade": "고위험",
          "species": "등검은말벌", "note": "폐쇄형 종 모양 집"}]
    파일이 없거나 형식이 틀리면 조용히 건너뛴다. 참조 사진이 하나도 없어도
    텍스트 예시만으로 동작한다.
    """
    manifest = REFERENCE_DIR / "manifest.json"
    if not manifest.exists():
        return []
    try:
        entries = json.loads(manifest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    out: List[Dict[str, Any]] = []
    for entry in entries if isinstance(entries, list) else []:
        name = str(entry.get("file", "")).strip()
        if not name:
            continue
        # manifest에서 디렉터리 탈출을 막는다.
        path = (REFERENCE_DIR / name).resolve()
        if REFERENCE_DIR.resolve() not in path.parents or not path.is_file():
            continue
        media = _MEDIA_BY_SUFFIX.get(path.suffix.lower())
        if media is None:
            continue
        out.append(
            {
                "data": base64.standard_b64encode(path.read_bytes()).decode("utf-8"),
                "media_type": media,
                "risk_grade": entry.get("risk_grade", ""),
                "species": entry.get("species", ""),
                "note": entry.get("note", ""),
                "file": name,
            }
        )
    return out


def reference_status() -> Dict[str, Any]:
    examples = reference_examples()
    return {
        "count": len(examples),
        "dir": str(REFERENCE_DIR),
        "grades": sorted({e["risk_grade"] for e in examples if e["risk_grade"]}),
    }


def _build_user_content(image_b64: str, media_type: str) -> List[Dict[str, Any]]:
    """참조 사진 + 판정 대상 사진을 하나의 user 메시지로 조립한다."""
    content: List[Dict[str, Any]] = []
    examples = reference_examples()

    if examples:
        content.append(
            {
                "type": "text",
                "text": (
                    "아래는 정답이 확인된 참조 사진들이다. 이 사진들의 시각적 특징을 "
                    "기준으로 삼아라. 판정 대상이 아니다."
                ),
            }
        )
        for i, ex in enumerate(examples, start=1):
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": ex["media_type"],
                        "data": ex["data"],
                    },
                }
            )
            label = "참조 {}: {} / {}".format(i, ex["risk_grade"], ex["species"])
            if ex["note"]:
                label += " — {}".format(ex["note"])
            content.append({"type": "text", "text": label})

        content.append({"type": "text", "text": "여기까지가 참조 사진이다."})

    content.append(
        {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": image_b64},
        }
    )
    content.append(
        {
            "type": "text",
            "text": (
                "이것이 판정 대상 사진이다. 위험 등급을 판정하고 "
                "record_bee_assessment 도구를 호출해 결과를 기록해라."
            ),
        }
    )
    return content


# --------------------------------------------------------------- 목업 시나리오
MOCK_SCENARIOS: Dict[str, Dict[str, Any]] = {
    "hornet_eaves": {
        "label": "아파트 처마 밑 대형 폐쇄형 벌집",
        "hint": "지름 25cm 남짓, 회갈색 종 모양, 벌이 계속 드나듦",
        "result": {
            "species_guess": "등검은말벌",
            "risk_grade": "고위험",
            "confidence": 0.88,
            "is_swarm": False,
            "estimated_nest_diameter_cm": 26.0,
            "visible_bee_count_band": "20+",
            "evidence": (
                "벌집 전체가 회갈색 종이질 껍질로 덮여 있고 내부 육각방이 보이지 않는 "
                "폐쇄형입니다. 아래쪽 출입구 한 곳으로만 벌이 드나들고, 가슴이 검고 "
                "다리 끝이 노란 개체가 확인됩니다. 옆 창틀과 비교하면 지름이 25cm를 "
                "넘어 군체가 이미 상당히 성장한 상태로 보입니다."
            ),
        },
    },
    "honeybee_swarm": {
        "label": "단지 내 나뭇가지에 매달린 벌 덩어리",
        "hint": "럭비공 모양으로 벌이 뭉쳐 있음, 벌집 구조물은 안 보임",
        "result": {
            "species_guess": "양봉꿀벌(분봉)",
            "risk_grade": "저위험",
            "confidence": 0.91,
            "is_swarm": True,
            "estimated_nest_diameter_cm": 22.0,
            "visible_bee_count_band": "20+",
            "evidence": (
                "종이질 벌집 구조물 없이 벌 수천 마리가 나뭇가지에 럭비공 모양으로 "
                "뭉쳐 있는 전형적인 꿀벌 분봉 상태입니다. 개체는 몸에 잔털이 많고 "
                "황갈색 줄무늬가 있는 양봉꿀벌로 보입니다. 분봉 중인 꿀벌은 지킬 집이 "
                "없어 공격성이 매우 낮고, 양봉업자가 회수하면 벌통으로 살릴 수 있습니다."
            ),
        },
    },
    "paper_wasp_veranda": {
        "label": "베란다 방충망 밖 소형 개방형 벌집",
        "hint": "육각방이 그대로 보이는 우산 모양, 지름 8cm 정도",
        "result": {
            "species_guess": "뱀허물쌍살벌",
            "risk_grade": "중위험",
            "confidence": 0.79,
            "is_swarm": False,
            "estimated_nest_diameter_cm": 8.5,
            "visible_bee_count_band": "6-20",
            "evidence": (
                "외피 없이 육각형 방이 그대로 드러난 개방형 벌집으로 쌍살벌류의 "
                "전형적인 형태입니다. 방충망 격자와 비교하면 지름 8cm 안팎의 초기 "
                "단계입니다. 먼저 건드리지 않으면 잘 쏘지 않지만, 창문을 여닫는 "
                "동선과 가까워 접촉 위험은 남아 있습니다."
            ),
        },
    },
    "blurry_unknown": {
        "label": "멀리서 흔들린 채 찍힌 사진",
        "hint": "역광에 초점이 안 맞아 형태를 읽기 어려움",
        "result": {
            "species_guess": "판별 불가",
            "risk_grade": "판별불가",
            "confidence": 0.22,
            "is_swarm": False,
            "estimated_nest_diameter_cm": None,
            "visible_bee_count_band": "불명",
            "evidence": (
                "역광과 흔들림으로 벌집 표면 질감과 개체의 색 패턴을 읽을 수 없습니다. "
                "폐쇄형인지 개방형인지조차 구분되지 않아 종을 특정할 수 없습니다. "
                "안전을 위해 저위험으로 단정하지 않고 119 확인 대상으로 넘깁니다."
            ),
        },
    },
}


def _apply_safety_threshold(data: Dict[str, Any], source: str) -> VisionResult:
    """신뢰도 임계값 미만이면 등급을 판별불가로 강등한다."""
    downgraded = False
    grade = data.get("risk_grade", "판별불가")
    conf = float(data.get("confidence", 0.0))

    if grade != "판별불가" and conf < config.CONFIDENCE_THRESHOLD:
        grade = "판별불가"
        downgraded = True

    return VisionResult(
        species_guess=data.get("species_guess", "판별 불가"),
        risk_grade=grade,
        confidence=round(conf, 3),
        is_swarm=bool(data.get("is_swarm", False)) and grade == "저위험",
        estimated_nest_diameter_cm=data.get("estimated_nest_diameter_cm"),
        visible_bee_count_band=data.get("visible_bee_count_band", "불명"),
        evidence=data.get("evidence", ""),
        source=source,  # type: ignore[arg-type]
        downgraded_by_threshold=downgraded,
    )


def classify_scenario(scenario_key: str) -> VisionResult:
    scenario = MOCK_SCENARIOS.get(scenario_key)
    if scenario is None:
        raise KeyError(scenario_key)
    return _apply_safety_threshold(dict(scenario["result"]), "mock")


def _unavailable(evidence: str, source: str) -> VisionResult:
    return _apply_safety_threshold(
        {
            "species_guess": "판별 불가",
            "risk_grade": "판별불가",
            "confidence": 0.0,
            "is_swarm": False,
            "estimated_nest_diameter_cm": None,
            "visible_bee_count_band": "불명",
            "evidence": evidence,
        },
        source,
    )


def classify_image(image_bytes: bytes, media_type: str) -> Tuple[VisionResult, Optional[str]]:
    """실제 사진 판별. (결과, 경고메시지) 를 돌려준다."""
    if not config.LIVE_MODE:
        # 목업 모드에서 임의 사진을 받으면 억지로 등급을 지어내지 않는다.
        # 안전장치가 설계대로 동작하는 것을 그대로 보여준다.
        result = _unavailable(
            "현재 목업 모드로 실행 중이라 실제 사진 판별을 수행하지 않았습니다. "
            "설계상 판별이 불가능한 건은 저위험으로 단정하지 않고 119 확인 큐로 "
            "보냅니다. 실제 판별을 보려면 ANTHROPIC_API_KEY를 설정하세요.",
            "mock",
        )
        return result, "목업 모드입니다. 실제 판별에는 ANTHROPIC_API_KEY가 필요합니다."

    import anthropic

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, timeout=120.0)
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    try:
        response = client.messages.create(
            model=config.MODEL,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _build_user_content(b64, media_type)}],
            tools=[ASSESSMENT_TOOL],
            tool_choice={"type": "tool", "name": TOOL_NAME},
            output_config={"effort": "medium"},
        )
    except Exception as exc:  # 네트워크/인증/쿼터 문제로 시연이 멈추지 않게 한다
        result = _unavailable(
            "판별 모델 호출에 실패해 위험도를 확인하지 못했습니다. "
            "설계 원칙에 따라 확인되지 않은 건은 119로 연계합니다.",
            "none",
        )
        return result, "판별 API 호출 실패: {}: {}".format(type(exc).__name__, exc)

    block = next((b for b in response.content if b.type == "tool_use"), None)
    if block is None:
        result = _unavailable(
            "판별 모델이 결과를 기록하지 않아 위험도를 확인하지 못했습니다. "
            "설계 원칙에 따라 확인되지 않은 건은 119로 연계합니다.",
            "none",
        )
        return result, "모델이 {} 도구를 호출하지 않았습니다.".format(TOOL_NAME)

    return _apply_safety_threshold(dict(block.input), "claude"), None
