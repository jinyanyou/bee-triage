"""프로토타입 전역 설정.

기획서 3.3의 가중치·임계값을 모두 여기 모아두어, 심사/시연 중에도
값을 바꿔가며 라우팅 결과가 어떻게 달라지는지 보여줄 수 있게 한다.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]          # .../Bee/prototype
PROJECT_ROOT = ROOT.parent                           # .../Bee

load_dotenv(ROOT / ".env")

# ---------------------------------------------------------------- 데이터
# 레포 안(배포용)을 먼저 보고, 없으면 팀 작업 폴더(Bee/data/…)를 본다.
# 배포 번들에는 비ASCII 경로를 넣지 않으려고 레포 쪽은 ASCII 이름을 쓴다.
_DATA_CANDIDATES = [
    ROOT / "data",
    PROJECT_ROOT / "data" / "벌집플랫폼_데이터",
]


def _resolve_data_dir() -> Path:
    override = os.getenv("BEE_DATA_DIR", "").strip()
    if override:
        return Path(override)
    for candidate in _DATA_CANDIDATES:
        if (candidate / "matching_db_disinfection_daegu_gb.csv").exists():
            return candidate
    return _DATA_CANDIDATES[0]


DATA_DIR = _resolve_data_dir()

WEB_DIR = ROOT / "web"

# ---------------------------------------------------------------- 저장소
# sqlite : 로컬 실행. var/reports.db 에 신고를 남긴다.
# client : 서버리스 배포(Vercel 등). 파일시스템이 읽기 전용이라 신고를
#          브라우저 localStorage 에 저장하고 서버는 상태를 갖지 않는다.
STORE_MODE = os.getenv("BEE_STORE", "").strip().lower()
if STORE_MODE not in ("sqlite", "client"):
    # Vercel 등 서버리스 환경은 자동 감지
    STORE_MODE = "client" if os.getenv("VERCEL") else "sqlite"

# ---------------------------------------------------------------- 모델
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
MODEL = os.getenv("BEE_MODEL", "claude-opus-5").strip()
FORCE_MOCK = os.getenv("BEE_FORCE_MOCK", "").strip() not in ("", "0", "false", "False")

LIVE_MODE = bool(ANTHROPIC_API_KEY) and not FORCE_MOCK

# ---------------------------------------------------------------- 스코어링
# 기획서 3.3: 최종위험점수 = 비전 0.6 + 문진 0.25 + 크기 0.15
W_VISION = 0.60
W_INTERVIEW = 0.25
W_SIZE = 0.15

# 기획서 3.2 (3) Human-in-the-loop 안전장치
CONFIDENCE_THRESHOLD = 0.70

# 라우팅 임계값
ROUTE_119_THRESHOLD = 0.65       # 이상이면 119 연계

# TODO(팀 논의 중): 저위험과 비긴급 유상 처리를 어떻게 가를지 결정되면 반영한다.
#   현재 scoring.decide_route()는 이 값을 쓰지 않는다. 119(0.65) 아래는 분봉
#   여부로만 갈라서, 중위험 쌍살벌과 저위험 소형 벌집이 똑같이 방역업체로 간다.
#   후보 A) 0.35 미만은 "자가 대응 안내"로 분리해 아예 출동을 만들지 않는다
#   후보 B) 이 설정을 삭제하고 2단계 라우팅으로 문서를 통일한다
ROUTE_PRIVATE_THRESHOLD = 0.35

# 비전 위험등급 → 가중치
VISION_GRADE_WEIGHT = {
    "고위험": 1.00,
    "중위험": 0.60,
    "저위험": 0.15,
}

# 문진 장소 위험 가중치
LOCATION_WEIGHT = {
    "실내": 1.00,
    "처마/창틀": 0.70,
    "지면": 0.55,
    "실외": 0.30,
}

SELF_ASSESS_WEIGHT = {"높음": 1.0, "보통": 0.5, "낮음": 0.15}

# 벌집 크기 정규화 기준 (지름 40cm 이상이면 만점)
SIZE_NORM_CM = 40.0

# 매칭
MATCH_TOP_K = 3
MATCH_MAX_RADIUS_KM = 30.0
