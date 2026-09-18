"""FastAPI 진입점.

파이프라인 4단계를 각각 독립 엔드포인트로 두어(기획서 3장) 단계별 검증이
가능하게 했다. 3면 플랫폼의 세 주체가 각자 화면을 갖는다.

  /            시민 신고 (수요자)
  /dashboard   소방 관제 (공급자 A / 페르소나 ② 박정민)
  /partner     업체·양봉인 수락 (공급자 B / 페르소나 ③ 이근처)
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, data_store, dispatch, geo, scoring, store, triage, vision
from .schemas import (
    AssessRequest,
    AssessResponse,
    Company,
    FeedbackRequest,
    FireCenter,
    MessageRequest,
    MessageResponse,
    Slots,
    StartResponse,
    StatusRequest,
    VisionResponse,
)

app = FastAPI(
    title="벌집 신고 사전 분류 프로토타입",
    description="AI 문진 → 사진 위험도 판별 → 위험도 기반 자동 매칭",
    version="0.2.0",
)

# 세션ID → {"slots": Slots, "vision": VisionResult|None, "attempts": {}, "exif": (lat,lon)|None}
SESSIONS: Dict[str, Dict[str, Any]] = {}

DEMO_LOCATIONS = [
    {"label": "대구 중구 동성로 (도심)", "lat": 35.8690, "lon": 128.5947},
    {"label": "대구 수성구 범어동 (주거지)", "lat": 35.8574, "lon": 128.6299},
    {"label": "경북 경산시 하양읍", "lat": 35.9133, "lon": 128.8189},
    {"label": "경북 안동시 옥동", "lat": 36.5600, "lon": 128.7200},
    {"label": "경북 울진군 울진읍 (농어촌)", "lat": 36.9930, "lon": 129.4000},
]

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


@app.on_event("startup")
def _startup() -> None:
    store.init()


def _session(session_id: str) -> Dict[str, Any]:
    state = SESSIONS.get(session_id)
    if state is None:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다. 처음부터 다시 시작해주세요.")
    return state


# ------------------------------------------------------------------ 메타
@app.get("/api/meta")
def meta() -> Dict[str, Any]:
    return {
        "mode": "live" if config.LIVE_MODE else "mock",
        "model": config.MODEL if config.LIVE_MODE else None,
        "data": data_store.data_health(),
        "demo_locations": DEMO_LOCATIONS,
        "scenarios": [
            {"key": k, "label": v["label"], "hint": v["hint"]}
            for k, v in vision.MOCK_SCENARIOS.items()
        ],
        "few_shot": vision.reference_status(),
        "thresholds": {
            "confidence": config.CONFIDENCE_THRESHOLD,
            "route_119": config.ROUTE_119_THRESHOLD,
            "weights": {
                "vision": config.W_VISION,
                "interview": config.W_INTERVIEW,
                "size": config.W_SIZE,
            },
        },
        "privacy": {
            "photo_stored": False,
            "retention_days": store.RETENTION_DAYS,
        },
        # sqlite = 서버가 신고를 보관 / client = 브라우저(localStorage)가 보관
        "store": config.STORE_MODE,
    }


@app.get("/api/stats")
def stats() -> Dict[str, Any]:
    return data_store.dispatch_stats()


@app.get("/api/geocode")
def geocode(q: str) -> Dict[str, Any]:
    """주소·지명 검색. 좌표를 직접 붙여넣어도 받아준다."""
    coords = geo.parse_coords(q)
    if coords:
        return {
            "results": [
                {
                    "label": "직접 입력 좌표",
                    "detail": "{:.5f}, {:.5f}".format(*coords),
                    "kind": "좌표",
                    "lat": coords[0],
                    "lon": coords[1],
                }
            ]
        }
    return {"results": geo.search_places(q)}


# --------------------------------------------------------------- 1단계 문진
@app.post("/api/triage/start", response_model=StartResponse)
def triage_start() -> StartResponse:
    session_id = uuid.uuid4().hex[:12]
    slots = Slots()
    SESSIONS[session_id] = {"slots": slots, "vision": None, "attempts": {}, "exif": None}
    first = triage.next_slot(slots)
    return StartResponse(
        session_id=session_id,
        message=triage.OPENING,
        question=triage.question_for(first) or "",
        slots=slots,
        progress=triage.progress(slots),
    )


@app.post("/api/triage/message", response_model=MessageResponse)
def triage_message(req: MessageRequest) -> MessageResponse:
    state = _session(req.session_id)
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="메시지가 비어 있습니다.")

    result = triage.handle_message(req.message, state["slots"], state.setdefault("attempts", {}))
    state["slots"] = result["slots"]
    state["attempts"] = result["attempts"]
    slots: Slots = result["slots"]

    return MessageResponse(
        reply=result["reply"],
        question=result["question"],
        slots=slots,
        filled=triage.filled_slots(slots),
        ready_for_photo=triage.next_slot(slots) is None,
        progress=triage.progress(slots),
        engine=result["engine"],
        assumed=result["assumed"],
    )


# --------------------------------------------------------------- 2단계 비전
@app.post("/api/vision", response_model=VisionResponse)
async def vision_classify(
    session_id: str = Form(...),
    scenario: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
) -> JSONResponse:
    state = _session(session_id)
    warning: Optional[str] = None
    exif_location: Optional[Dict[str, float]] = None

    if scenario:
        try:
            result = vision.classify_scenario(scenario)
        except KeyError:
            raise HTTPException(status_code=400, detail="알 수 없는 데모 시나리오입니다.")
    elif file is not None:
        media_type = file.content_type or "image/jpeg"
        if media_type not in ALLOWED_IMAGE_TYPES:
            raise HTTPException(
                status_code=400,
                detail="지원하지 않는 이미지 형식입니다: {}".format(media_type),
            )
        data = await file.read()
        if len(data) > 8 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="이미지는 8MB 이하만 업로드할 수 있습니다.")

        coords = geo.exif_gps(data)
        if coords:
            exif_location = {"lat": coords[0], "lon": coords[1]}
            state["exif"] = coords

        result, warning = vision.classify_image(data, media_type)
        # 사진은 여기서 끝. 디스크에 쓰지 않고 참조를 버린다.
        del data
    else:
        raise HTTPException(status_code=400, detail="사진 또는 데모 시나리오가 필요합니다.")

    slots: Slots = state["slots"]
    slots.photo_uploaded = True
    state["vision"] = result

    payload = VisionResponse(session_id=session_id, vision=result, slots=slots).model_dump()
    payload["warning"] = warning
    payload["exif_location"] = exif_location
    return JSONResponse(payload)


# ------------------------------------------------------- 3·4단계 스코어링/매칭
@app.post("/api/assess", response_model=AssessResponse)
def assess(req: AssessRequest) -> AssessResponse:
    state = _session(req.session_id)
    slots: Slots = state["slots"]
    result = state.get("vision")

    missing = [triage.SLOT_LABELS[k] for k in triage.SLOT_ORDER if getattr(slots, k) is None]
    if missing:
        raise HTTPException(
            status_code=400,
            detail="문진이 끝나지 않았습니다. 남은 항목: {}".format(", ".join(missing)),
        )
    if result is None:
        raise HTTPException(status_code=400, detail="사진 판별이 아직 수행되지 않았습니다.")

    score = scoring.compute(slots, result)
    route, reason = scoring.decide_route(score, result)
    out = dispatch.build_dispatch(
        route, reason, slots, result, score, req.lat, req.lon, req.address_label
    )

    report_id = store.save_report(
        lat=req.lat,
        lon=req.lon,
        address_label=req.address_label,
        route=route,
        route_reason=reason,
        slots=slots.model_dump(),
        vision=result.model_dump(),
        score=score.model_dump(),
        companies=out["companies"],
        summary=out["report_summary"],
    )

    return AssessResponse(
        report_id=report_id,
        route=route,  # type: ignore[arg-type]
        route_reason=reason,
        score=score,
        vision=result,
        slots=slots,
        companies=[Company(**c) for c in out["companies"]],
        beekeeping_branch=out["beekeeping_branch"],
        fire_center=FireCenter(**out["fire_center"]) if out["fire_center"] else None,
        report_summary=out["report_summary"],
        notes=out["notes"],
    )


# --------------------------------------------------- 관제 / 파트너 공용 API
@app.get("/api/reports")
def reports(
    route: Optional[str] = None,
    status: Optional[str] = None,
    grade: Optional[str] = None,
    limit: int = 200,
) -> Dict[str, Any]:
    return {"reports": store.list_reports(route, status, grade, limit), "kpi": store.kpi()}


@app.get("/api/reports/{report_id}")
def report_detail(report_id: str) -> Dict[str, Any]:
    report = store.get_report(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="신고를 찾을 수 없습니다.")
    report["feedback"] = store.feedback_for(report_id)
    return report


@app.post("/api/reports/{report_id}/status")
def update_status(report_id: str, req: StatusRequest) -> Dict[str, Any]:
    valid = {
        store.STATUS_119_PENDING,
        store.STATUS_PARTNER_PENDING,
        store.STATUS_ACCEPTED,
        store.STATUS_DONE,
        store.STATUS_REJECTED,
    }
    if req.status not in valid:
        raise HTTPException(status_code=400, detail="알 수 없는 상태입니다: {}".format(req.status))
    if not store.set_status(report_id, req.status, req.assigned):
        raise HTTPException(status_code=404, detail="신고를 찾을 수 없습니다.")
    return {"ok": True, "report": store.get_report(report_id)}


@app.post("/api/reports/{report_id}/feedback")
def submit_feedback(report_id: str, req: FeedbackRequest) -> Dict[str, Any]:
    """사후 확인 결과 등록 → 재학습 데이터 (기획서 5.2 데이터 선순환)."""
    if store.get_report(report_id) is None:
        raise HTTPException(status_code=404, detail="신고를 찾을 수 없습니다.")
    store.add_feedback(
        report_id, req.actual_grade, req.actual_species, req.note, req.reporter
    )
    return {"ok": True, "kpi": store.kpi()}


# ------------------------------------------------------------------ 화면
def _page(name: str) -> FileResponse:
    # HTML은 항상 재검증시킨다. 시연 중에 브라우저가 옛 화면을 캐시에서 꺼내
    # 보여주는 사고를 막기 위함이다. (정적 자산은 ?v= 로 버전을 붙였다)
    return FileResponse(
        config.WEB_DIR / name,
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@app.get("/")
def index() -> FileResponse:
    return _page("index.html")


@app.get("/dashboard")
def dashboard() -> FileResponse:
    return _page("dashboard.html")


@app.get("/partner")
def partner() -> FileResponse:
    return _page("partner.html")


app.mount("/static", StaticFiles(directory=str(config.WEB_DIR)), name="static")
