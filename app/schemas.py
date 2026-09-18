"""API 요청/응답 스키마."""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

LocationType = Literal["실내", "실외", "처마/창틀", "지면"]
RiskSelf = Literal["낮음", "보통", "높음"]
RiskGrade = Literal["고위험", "중위험", "저위험", "판별불가"]
RouteKind = Literal["119", "방역업체", "양봉협회"]


class Slots(BaseModel):
    """기획서 3.1의 문진 슬롯."""

    location_type: Optional[LocationType] = None
    nest_size_cm: Optional[float] = None
    discovered_days_ago: Optional[int] = None
    aggression_observed: Optional[bool] = None
    reporter_risk_self_assessment: Optional[RiskSelf] = None
    photo_uploaded: bool = False


class SlotUpdate(BaseModel):
    """LLM이 한 번의 발화에서 추출해낸 슬롯 값(전부 선택)."""

    location_type: Optional[LocationType] = None
    nest_size_cm: Optional[float] = None
    discovered_days_ago: Optional[int] = None
    aggression_observed: Optional[bool] = None
    reporter_risk_self_assessment: Optional[RiskSelf] = None


class VisionResult(BaseModel):
    species_guess: str
    risk_grade: RiskGrade
    confidence: float
    is_swarm: bool = False
    estimated_nest_diameter_cm: Optional[float] = None
    visible_bee_count_band: str = "불명"
    evidence: str = ""
    source: Literal["claude", "mock", "none"] = "none"
    downgraded_by_threshold: bool = False


class Company(BaseModel):
    업체명: str
    시도: str
    시군구: str
    도로명주소: str
    전화번호: Optional[str] = None
    연락가능: bool = False
    거리km: float
    보호복_수: int = 0
    진공청소기_수: int = 0
    위도: float
    경도: float


class FireCenter(BaseModel):
    소방서명: str
    안전센터명: str
    주소: str
    전화번호: Optional[str] = None
    거리km: float


class ScoreBreakdown(BaseModel):
    vision_grade: str
    vision_component: float
    interview_component: float
    size_component: float
    final_score: float
    detail: List[Dict[str, Any]] = Field(default_factory=list)


# ------------------------------------------------------------ 요청/응답
class StartResponse(BaseModel):
    session_id: str
    message: str
    question: str
    slots: Slots
    progress: Dict[str, Any]


class MessageRequest(BaseModel):
    session_id: str
    message: str


class MessageResponse(BaseModel):
    reply: str
    question: Optional[str] = None
    slots: Slots
    filled: List[str]
    ready_for_photo: bool
    progress: Dict[str, Any]
    engine: Literal["claude", "rule"] = "rule"
    assumed: Optional[str] = None


class VisionResponse(BaseModel):
    session_id: str
    vision: VisionResult
    slots: Slots


class AssessRequest(BaseModel):
    session_id: str
    lat: float
    lon: float
    address_label: str = ""


class StatusRequest(BaseModel):
    status: str
    assigned: Optional[str] = None


class FeedbackRequest(BaseModel):
    actual_grade: Optional[RiskGrade] = None
    actual_species: Optional[str] = None
    note: str = ""
    reporter: str = ""


class AssessResponse(BaseModel):
    report_id: str
    route: RouteKind
    route_reason: str
    score: ScoreBreakdown
    vision: VisionResult
    slots: Slots
    companies: List[Company] = Field(default_factory=list)
    beekeeping_branch: Optional[Dict[str, Any]] = None
    fire_center: Optional[FireCenter] = None
    report_summary: Optional[str] = None
    notes: List[str] = Field(default_factory=list)
