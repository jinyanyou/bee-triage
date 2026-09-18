"""시장 규모 추정 — 본선 심사 '시장성' 항목(30점) 대비.

무엇을 하는가
-------------
1팀이 정제한 공공데이터(소방청 출동 통계, 행안부 소독업 인허가, 119안전센터
현황)만으로 계산할 수 있는 것을 전부 계산하고, **계산할 수 없는 것은 가정으로
분리해 민감도 분석**으로 처리한다.

왜 이렇게 하는가
----------------
시장 규모를 단일 숫자 하나로 제시하면 "그 숫자 근거가 뭐냐"는 질문 하나에
무너진다. 모르는 값은 모른다고 하고 범위로 제시하는 편이 방어하기 쉽다.

  확정값 : 출동 건수, 업체 수, 센터별 접근성  (공공데이터에서 직접)
  가정값 : 건당 단가, 비긴급 우회 비율, 업체 처리량  (아래 ASSUMPTIONS)

가정값은 전부 이 파일 상단에 모아두었다. 실측치가 나오면 숫자만 바꿔 다시 돌리면
리포트가 갱신된다. 실측 방법은 analysis/interview_guide.md 참고.

실행
----
    .venv\\Scripts\\python.exe -m analysis.market_sizing
"""
from __future__ import annotations

import csv
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent


# ============================================================================
# 가정값 — 실측되지 않은 값. 전부 여기 모아둔다.
# ============================================================================

# 민간 벌집 제거 건당 단가(원).
# 아직 실측하지 않았다. 업체 전화 견적으로 확인해야 한다.
# 세 시나리오로 민감도를 본다.
PRICE_SCENARIOS_KRW = [50_000, 80_000, 120_000]

# 전체 벌집제거 출동 중 '민간으로 우회 가능한 비긴급' 비율.
# 소방청 통계에 이 구분이 없다. 정보공개청구 전까지는 범위로 둔다.
DIVERSION_SCENARIOS = [0.30, 0.50, 0.70]

# 우회 대상 중 '저위험'(선택지가 제시되는 구간, 위험점수 0.35 미만)의 비중.
# 나머지는 중위험대라 선택지 없이 유상 처리로 간다.
# 소방서에 "출동해보니 그냥 두셔도 된다고 안내하고 온 비율"을 물으면 좁힐 수 있다.
LOW_RISK_SHARE = 0.40

# 저위험 구간에서 '그래도 불안해서 맡기겠다'를 고르는 비율.
# 자가 대응을 고르면 매출이 0이지만 소방력 절감 효과는 같다.
# 이 값이 낮을수록 공익 가치는 커지고 사업 매출은 작아진다 — 본질적 긴장이다.
PAID_CHOICE_SCENARIOS = [0.30, 0.50, 0.70]

# 업체 1곳이 성수기에 하루 처리 가능한 벌집 제거 건수.
# 소독 본업과 병행하므로 보수적으로 잡았다. 견적 전화에서 확인할 항목.
CASES_PER_COMPANY_PER_DAY = 2.0

# 성수기 길이(일). 벌집 출동은 7~9월에 집중되지만 통계연보에 월별 분포가 없다.
# 3개월을 성수기로 보되, 주말·우천을 감안해 가동일을 70%로 둔다.
PEAK_DAYS = 92
PEAK_WORKING_RATIO = 0.70

# 성수기에 발생하는 연간 출동 비중. 월별 데이터가 없어 가정한다.
PEAK_SHARE_OF_YEAR = 0.75

# 출동 1건당 소요 인시(기획서 1.2: 출동~종료 30분~1시간, 고소작업·보호복 착용).
# 3인 1팀 기준으로 환산한다.
HOURS_PER_DISPATCH = 0.75
CREW_SIZE = 3

# 수익 모델 가정
MATCHING_FEE_RATE = 0.10          # 매칭 수수료율
SUBSCRIPTION_KRW_PER_MONTH = 30_000  # 업체 구독료(월)
MUNICIPAL_CONTRACT_KRW_PER_YEAR = 30_000_000  # 지자체 위탁 운영비(연, 1곳)


# ============================================================================
# 데이터 로드 (확정값)
# ============================================================================

def _read(name: str) -> List[Dict[str, str]]:
    path = config.DATA_DIR / name
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _i(v: Any) -> int:
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def _norm_sigungu(name: str) -> str:
    """시군구 표기를 두 파일 간에 맞춘다.

    업체 CSV는 `포항시 남구`/`포항시 북구`로, 안전센터 CSV는 `포항시`로 적혀 있어
    그대로 조인하면 포항시 업체 51곳이 통째로 누락된다.
    일반구를 둔 행정시는 상위 시 이름으로 합친다.
    """
    name = (name or "").strip()
    if " " in name and name.split()[0].endswith("시"):
        return name.split()[0]
    return name


def _f(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def load_facts() -> Dict[str, Any]:
    companies = _read("matching_db_disinfection_daegu_gb.csv")
    centers = _read("fire_centers_daegu_gb_with_proximity.csv")
    regional = _read("stats_beehive_dispatch_daegu_gb_2023_2025.csv")
    national = _read("stats_beehive_dispatch_national_2016_2025.csv")

    contactable = [c for c in companies if c["연락가능"].strip().lower() == "true"]

    dispatch_2025 = {
        r["본부"]: _i(r["벌집제거_출동"]) for r in regional if r["연도"] == "2025"
    }

    return {
        "companies": companies,
        "contactable": contactable,
        "centers": centers,
        "dispatch_2025": dispatch_2025,
        "national_series": [
            {"연도": _i(r["연도"]), "출동": _i(r["벌집제거_출동"]),
             "비중": _f(r["벌집제거_비중"])}
            for r in national
        ],
    }


# ============================================================================
# 1. TAM / SAM / SOM
# ============================================================================

def market_size(facts: Dict[str, Any]) -> Dict[str, Any]:
    d = facts["dispatch_2025"]
    national, gb, dg = d.get("전국", 0), d.get("경북", 0), d.get("대구", 0)
    daegu_gb = gb + dg

    # SOM은 파일럿 2개 지자체(대구 + 경북 1개 시) 기준.
    # 경북에서 연락가능 업체가 가장 많은 시군구를 파일럿 후보로 본다.
    by_sigungu: Dict[str, int] = defaultdict(int)
    for c in facts["contactable"]:
        if c["시도"].startswith("경상북도"):
            by_sigungu[_norm_sigungu(c["시군구"])] += 1
    pilot_city = max(by_sigungu.items(), key=lambda kv: kv[1])[0] if by_sigungu else "-"
    # 경북 출동을 시군구 수로 균등 배분한 보수적 추정 (시군구별 출동 통계 없음)
    gb_sigungu_count = len({_norm_sigungu(c["시군구"]) for c in facts["contactable"]
                            if c["시도"].startswith("경상북도")}) or 1
    pilot_dispatch = dg + gb // gb_sigungu_count

    rows = []
    for div in DIVERSION_SCENARIOS:
        for price in PRICE_SCENARIOS_KRW:
            rows.append({
                "우회율": div,
                "단가": price,
                "TAM_전국": int(national * div * price),
                "SAM_대구경북": int(daegu_gb * div * price),
                "SOM_파일럿": int(pilot_dispatch * div * price),
            })

    return {
        "national": national, "daegu_gb": daegu_gb, "gb": gb, "dg": dg,
        "pilot_city": pilot_city, "pilot_dispatch": pilot_dispatch,
        "rows": rows,
    }


# ============================================================================
# 2. 공급 수용력 — 수요가 커도 공급이 못 받으면 시장이 아니다
# ============================================================================

def supply_capacity(facts: Dict[str, Any]) -> Dict[str, Any]:
    """공급을 '상한'이 아니라 '필요 참여 업체 수'로 계산한다.

    등록 업체 433곳이 전부 벌집 제거를 한다고 가정하면 공급 상한이 수요를 크게
    웃돌아 분석이 무의미해진다. 실제로는 소독이 본업이라 참여율이 관건이다.
    그래서 질문을 뒤집는다 — "우회율 X%를 감당하려면 몇 곳이 참여해야 하는가".
    이 값이 곧 사업 초기의 업체 모집 목표가 된다.
    """
    n = len(facts["contactable"])
    working_days = PEAK_DAYS * PEAK_WORKING_RATIO
    per_company = CASES_PER_COMPANY_PER_DAY * working_days  # 업체 1곳의 성수기 처리량

    demand_year = facts["dispatch_2025"].get("경북", 0) + facts["dispatch_2025"].get("대구", 0)
    demand_peak = demand_year * PEAK_SHARE_OF_YEAR

    rows = []
    for div in DIVERSION_SCENARIOS:
        need = demand_peak * div
        required = need / per_company if per_company else 0
        rows.append({
            "우회율": div,
            "성수기_우회물량": int(need),
            "필요_참여업체": int(-(-required // 1)),          # 올림
            "필요_참여율": round(required / n * 100, 1) if n else 0.0,
            "업체당_연간건수": int(need / n) if n else 0,
        })

    return {
        "companies": n,
        "working_days": round(working_days),
        "per_company": int(per_company),
        "demand_year": demand_year,
        "demand_peak": int(demand_peak),
        "rows": rows,
    }


# ============================================================================
# 3. 시군구별 수급 매트릭스 — 어디부터 진입할 것인가
# ============================================================================

def sigungu_matrix(facts: Dict[str, Any]) -> List[Dict[str, Any]]:
    """안전센터를 수요 대리지표로 삼는다. 출동은 센터 단위로 나가는데
    시군구별 출동 통계가 공개되어 있지 않기 때문이다."""
    supply: Dict[tuple, int] = defaultdict(int)
    for c in facts["contactable"]:
        supply[(c["시도"], _norm_sigungu(c["시군구"]))] += 1

    agg: Dict[tuple, Dict[str, Any]] = {}
    for f in facts["centers"]:
        key = (f["시도"], _norm_sigungu(f["시군구"]))
        a = agg.setdefault(key, {"센터수": 0, "거리합": 0.0, "거리표본": 0, "공백센터": 0})
        a["센터수"] += 1
        near = f.get("최근접_연락가능업체_거리km", "")
        if near not in (None, ""):
            a["거리합"] += _f(near)
            a["거리표본"] += 1
        if _i(f.get("반경10km_연락가능업체수")) == 0:
            a["공백센터"] += 1

    rows = []
    for key, a in agg.items():
        sido, sigungu = key
        n_supply = supply.get(key, 0)
        rows.append({
            "시도": sido,
            "시군구": sigungu,
            "안전센터": a["센터수"],
            "연락가능업체": n_supply,
            "센터당업체": round(n_supply / a["센터수"], 2) if a["센터수"] else 0,
            "최근접거리_평균km": round(a["거리합"] / a["거리표본"], 2) if a["거리표본"] else None,
            "매칭공백센터": a["공백센터"],
        })

    rows.sort(key=lambda r: (-r["센터당업체"], r["시군구"]))
    return rows


# ============================================================================
# 4. 수익 모델 3안
# ============================================================================

def paid_split(volume: float, paid_ratio: float) -> Dict[str, float]:
    """우회 물량을 유상 처리와 자가 대응으로 가른다.

    저위험 구간(LOW_RISK_SHARE)만 신고자가 고르고, 중위험대는 전부 유상이다.
    자가 대응은 매출이 0이지만 소방력 절감 효과는 유상 처리와 동일하다.
    """
    choice_volume = volume * LOW_RISK_SHARE          # 선택지가 제시되는 물량
    auto_paid = volume * (1 - LOW_RISK_SHARE)        # 중위험대 — 선택 없이 유상
    paid = auto_paid + choice_volume * paid_ratio
    self_care = choice_volume * (1 - paid_ratio)
    return {"paid": paid, "self_care": self_care, "choice_volume": choice_volume}


def revenue_models(facts: Dict[str, Any], sizing: Dict[str, Any]) -> List[Dict[str, Any]]:
    """매출은 **유상 처리 건수**에만 붙는다. 자가 대응은 0원이다."""
    rows = []
    for div in DIVERSION_SCENARIOS:
        volume = sizing["daegu_gb"] * div
        for paid_ratio in PAID_CHOICE_SCENARIOS:
            split = paid_split(volume, paid_ratio)
            for price in PRICE_SCENARIOS_KRW:
                rows.append({
                    "우회율": div,
                    "유상선택률": paid_ratio,
                    "단가": price,
                    "유상건수": int(split["paid"]),
                    "자가대응건수": int(split["self_care"]),
                    "매칭수수료": int(split["paid"] * price * MATCHING_FEE_RATE),
                    "업체구독": int(len(facts["contactable"]) * SUBSCRIPTION_KRW_PER_MONTH * 12),
                    "지자체위탁_2곳": MUNICIPAL_CONTRACT_KRW_PER_YEAR * 2,
                })
    return rows


# ============================================================================
# 5. 공익 가치 — 소방력 절감
# ============================================================================

def public_value(sizing: Dict[str, Any]) -> List[Dict[str, Any]]:
    """소방력 절감은 유상·자가 대응을 가리지 않는다. 둘 다 119 출동을 만들지 않는다.

    다만 자가 대응은 시민이 돈도 내지 않으므로 **시민 비용 절감**이라는 가치가
    추가로 생긴다. 사업 매출과 반대 방향이라 정직하게 같이 적는다.
    """
    rows = []
    mid_price = PRICE_SCENARIOS_KRW[len(PRICE_SCENARIOS_KRW) // 2]
    for div in DIVERSION_SCENARIOS:
        diverted = sizing["daegu_gb"] * div
        for paid_ratio in PAID_CHOICE_SCENARIOS:
            split = paid_split(diverted, paid_ratio)
            rows.append({
                "우회율": div,
                "유상선택률": paid_ratio,
                "우회건수": int(diverted),
                "절감_인시": int(diverted * HOURS_PER_DISPATCH * CREW_SIZE),
                "자가대응건수": int(split["self_care"]),
                "시민비용절감": int(split["self_care"] * mid_price),
            })
    return rows


# ============================================================================
# 리포트
# ============================================================================

def won(v: int) -> str:
    if v >= 100_000_000:
        return "{:,.1f}억원".format(v / 100_000_000)
    return "{:,.0f}만원".format(v / 10_000)


def build_report(facts, sizing, capacity, matrix, revenue, value) -> str:
    L: List[str] = []
    a = L.append

    a("# 시장 규모 추정")
    a("")
    a("> 본선 심사 **시장성(30점)** — 「타겟 시장 구체화 및 이용자 확보 전략」 대응 자료.")
    a("> `analysis/market_sizing.py` 실행 결과이며, 가정값을 바꾸면 자동 갱신됩니다.")
    a("")
    a("## 0. 확정값과 가정값의 구분")
    a("")
    a("시장 규모를 단일 숫자로 제시하지 않았습니다. 공공데이터에서 직접 나오는 값과")
    a("아직 실측하지 않은 값을 나누고, 후자는 **범위로** 제시합니다.")
    a("")
    a("| 구분 | 항목 | 값 | 출처 / 상태 |")
    a("|---|---|---|---|")
    a("| 확정 | 전국 벌집제거 출동(2025) | {:,}건 | 소방청 통계연보 |".format(sizing["national"]))
    a("| 확정 | 대구·경북 출동(2025) | {:,}건 | 경북 {:,} + 대구 {:,} |".format(
        sizing["daegu_gb"], sizing["gb"], sizing["dg"]))
    a("| 확정 | 연락 가능 소독·방역업체 | {:,}곳 | 행안부 인허가 (전화번호 보유분) |".format(
        capacity["companies"]))
    a("| 확정 | 119안전센터 | {:,}곳 | 소방청 |".format(len(facts["centers"])))
    a("| **가정** | 건당 단가 | {} | **미실측** — 업체 견적 필요 |".format(
        " / ".join("{:,}원".format(p) for p in PRICE_SCENARIOS_KRW)))
    a("| **가정** | 비긴급 우회 비율 | {} | **미실측** — 소방청 통계에 구분 없음 |".format(
        " / ".join("{:.0%}".format(d) for d in DIVERSION_SCENARIOS)))
    a("| **가정** | 업체 1곳 일 처리량 | {}건 | **미실측** — 견적 시 확인 |".format(
        CASES_PER_COMPANY_PER_DAY))
    a("| **가정** | 저위험 구간 비중 | {:.0%} | **미실측** — 소방서 문의로 좁힘 |".format(
        LOW_RISK_SHARE))
    a("| **가정** | 유상 선택률 | {} | **미실측** — 시민 선택 행동 |".format(
        " / ".join("{:.0%}".format(r) for r in PAID_CHOICE_SCENARIOS)))
    a("")

    a("## 1. 시장 규모 (TAM / SAM / SOM)")
    a("")
    a("- **TAM** 전국 벌집제거 출동 중 민간 우회 가능 물량")
    a("- **SAM** 대구·경북 (매칭 DB 확보 지역)")
    a("- **SOM** 파일럿 2개 지자체 — 대구 + 경북 `{}`(연락가능 업체 최다), 연 {:,}건".format(
        sizing["pilot_city"], sizing["pilot_dispatch"]))
    a("")
    a("| 우회율 | 단가 | TAM (전국) | SAM (대구·경북) | SOM (파일럿) |")
    a("|---|---|---|---|---|")
    for r in sizing["rows"]:
        a("| {:.0%} | {:,}원 | {} | {} | {} |".format(
            r["우회율"], r["단가"], won(r["TAM_전국"]), won(r["SAM_대구경북"]), won(r["SOM_파일럿"])))
    a("")
    a("> 시군구별 출동 통계가 공개되지 않아, SOM의 경북 몫은 경북 전체 출동을")
    a("> 시군구 수로 균등 배분한 보수적 추정입니다.")
    a("")

    a("## 2. 공급 수용력 — 이 시장이 실제로 감당되는가")
    a("")
    a("수요가 크더라도 받아줄 업체가 없으면 시장이 아닙니다. 다른 팀이 잘 다루지 않는")
    a("공급 측을 실제 업체 수로 점검했습니다.")
    a("")
    a("등록 업체 {:,}곳이 전부 벌집 제거를 한다고 가정하면 공급이 수요를 크게 웃돌아".format(
        capacity["companies"]))
    a("분석이 무의미해집니다. 소독이 본업이라 **실제 참여율**이 관건입니다.")
    a("그래서 질문을 뒤집었습니다 — *우회율 X%를 감당하려면 몇 곳이 참여해야 하는가.*")
    a("이 값이 곧 **사업 초기의 업체 모집 목표**가 됩니다.")
    a("")
    a("- 업체 1곳의 성수기 처리량 = 하루 {}건 × 가동 {}일 = **{:,}건**".format(
        CASES_PER_COMPANY_PER_DAY, capacity["working_days"], capacity["per_company"]))
    a("- 대구·경북 연간 출동 {:,}건 중 성수기(7~9월) 추정 **{:,}건**".format(
        capacity["demand_year"], capacity["demand_peak"]))
    a("")
    a("| 우회율 | 성수기 우회 물량 | 필요 참여 업체 | 필요 참여율 | 업체당 연 건수 |")
    a("|---|---|---|---|---|")
    for r in capacity["rows"]:
        a("| {:.0%} | {:,}건 | **{:,}곳** | {}% | {}건 |".format(
            r["우회율"], r["성수기_우회물량"], r["필요_참여업체"],
            r["필요_참여율"], r["업체당_연간건수"]))
    a("")
    worst = max(capacity["rows"], key=lambda r: r["필요_참여업체"])
    a("> 가장 공격적인 시나리오(우회율 {:.0%})에서도 연락 가능 업체 {:,}곳 중".format(
        worst["우회율"], capacity["companies"]))
    a("> **{:,}곳({}%)만 참여하면** 물량이 소화됩니다. 전수 모집이 필요 없다는 뜻이고,".format(
        worst["필요_참여업체"], worst["필요_참여율"]))
    a("> 1단계 진입 시군구(아래)에 집중하면 달성 가능한 규모입니다.")
    a("")

    a("## 3. 진입 순서 — 시군구별 수급")
    a("")
    a("119안전센터를 수요 대리지표로 삼았습니다(시군구별 출동 통계 비공개).")
    a("**센터당 연락가능 업체 수**가 많은 곳부터 서비스가 성립합니다.")
    a("")
    a("### 1단계 진입 후보 (공급 충분)")
    a("")
    a("| 시도 | 시군구 | 안전센터 | 연락가능업체 | 센터당 | 최근접 평균 |")
    a("|---|---|---|---|---|---|")
    for r in matrix[:8]:
        a("| {} | {} | {} | {} | **{}** | {}km |".format(
            r["시도"][:2], r["시군구"], r["안전센터"], r["연락가능업체"],
            r["센터당업체"], r["최근접거리_평균km"]))
    a("")
    gaps = [r for r in matrix if r["매칭공백센터"] > 0]
    a("### 매칭 공백 지역 (반경 10km 내 연락 가능 업체가 없는 센터 보유)")
    a("")
    if gaps:
        a("| 시도 | 시군구 | 안전센터 | 공백 센터 | 연락가능업체 |")
        a("|---|---|---|---|---|")
        for r in sorted(gaps, key=lambda x: -x["매칭공백센터"]):
            a("| {} | {} | {} | **{}** | {} |".format(
                r["시도"][:2], r["시군구"], r["안전센터"], r["매칭공백센터"], r["연락가능업체"]))
        a("")
        a("> 공백 센터를 보유한 시군구 **{}곳**. 이 지역은 업체 발굴·협회 협력이".format(len(gaps)))
        a("> 선행되어야 하므로 3단계 확장 대상으로 둡니다. 숨기지 않고 로드맵에 반영합니다.")
    else:
        a("공백 센터 없음.")
    a("")

    a("## 4. 수익 모델")
    a("")
    a("### 선택 구조가 매출에 미치는 영향")
    a("")
    a("저위험 구간(위험점수 0.35 미만, 우회 물량의 {:.0%} 가정)은 신고자가 직접 고릅니다.".format(
        LOW_RISK_SHARE))
    a("**자가 대응을 고르면 매출은 0원이지만 소방력 절감 효과는 유상 처리와 같습니다.**")
    a("공익 가치와 사업 매출이 반대 방향으로 움직이는 구조라, 이 긴장을 숨기지 않고 드러냅니다.")
    a("")
    a("| 우회율 | 유상 선택률 | 유상 건수 | 자가 대응 건수 |")
    a("|---|---|---|---|")
    seen = set()
    for r in revenue:
        key = (r["우회율"], r["유상선택률"])
        if key in seen:
            continue
        seen.add(key)
        a("| {:.0%} | {:.0%} | {:,}건 | {:,}건 |".format(
            r["우회율"], r["유상선택률"], r["유상건수"], r["자가대응건수"]))
    a("")
    a("### 모델별 연 매출 (단가 {:,}원 기준)".format(PRICE_SCENARIOS_KRW[1]))
    a("")
    a("| 우회율 | 유상 선택률 | ① 매칭 수수료({:.0%}) | ② 업체 구독({:,}원/월) | ③ 지자체 위탁(2곳) |".format(
        MATCHING_FEE_RATE, SUBSCRIPTION_KRW_PER_MONTH))
    a("|---|---|---|---|---|")
    for r in revenue:
        if r["단가"] != PRICE_SCENARIOS_KRW[1]:
            continue
        a("| {:.0%} | {:.0%} | {} | {} | {} |".format(
            r["우회율"], r["유상선택률"], won(r["매칭수수료"]),
            won(r["업체구독"]), won(r["지자체위탁_2곳"])))
    a("")
    a("①은 유상 선택률에 직접 흔들립니다. 반면 ②③은 흔들리지 않습니다.")
    a("**자가 대응이 많아질수록 ①은 줄지만 ③의 명분은 커집니다** — 지자체 입장에서는")
    a("출동을 가장 많이 줄여주는 경로이기 때문입니다. 따라서 **①로 시작하되 ③으로**")
    a("**무게를 옮기는 것**이 이 서비스의 구조에 맞습니다.")
    a("")

    a("## 5. 공익 가치 — 소방력 절감")
    a("")
    a("출동 1건당 {}시간, {}인 1팀 기준(기획서 1.2).".format(HOURS_PER_DISPATCH, CREW_SIZE))
    a("")
    a("| 우회율 | 유상 선택률 | 우회 건수 | 절감 인시 | 자가 대응 | 시민 비용 절감 |")
    a("|---|---|---|---|---|---|")
    for r in value:
        a("| {:.0%} | {:.0%} | {:,}건 | **{:,}인시** | {:,}건 | {} |".format(
            r["우회율"], r["유상선택률"], r["우회건수"], r["절감_인시"],
            r["자가대응건수"], won(r["시민비용절감"])))
    a("")
    a("**절감 인시는 유상 선택률과 무관합니다.** 자가 대응이든 유상 처리든 119 출동을")
    a("만들지 않는 것은 같기 때문입니다. 반면 **시민 비용 절감**은 자가 대응에서만")
    a("발생합니다 — 시민이 돈을 내지 않아도 되는 건이라는 뜻입니다.")
    a("")
    a("이 표는 수익이 아니라 **지자체를 설득하는 근거**입니다. 위탁 운영비(③)의")
    a("정당성이 여기서 나옵니다.")
    a("")

    a("## 6. 이 추정의 한계")
    a("")
    a("- **건당 단가 미실측.** 업체 견적 3~5곳으로 확정해야 합니다. (`interview_guide.md`)")
    a("- **비긴급 비율 미실측.** 소방청 통계에 긴급/비긴급 구분이 없습니다.")
    a("  정보공개청구로 건별 데이터를 받으면 범위를 좁힐 수 있습니다.")
    a("- **저위험 구간 비중·유상 선택률 미실측.** 전자는 소방서 문의로, 후자는")
    a("  업체가 이미 받고 있는 '작은 벌집인데도 불안해서 부르는 고객' 비율로 근사할 수 있습니다.")
    a("- **월별 분포 미확보.** 성수기 비중 {:.0%}는 가정입니다.".format(PEAK_SHARE_OF_YEAR))
    a("- **시군구별 출동 통계 비공개.** 안전센터 수를 대리지표로 썼습니다.")
    a("- 소독업 인허가가 곧 벌집 제거 수행 가능을 뜻하지는 않습니다.")
    a("  견적 전화에서 실제 수행 여부를 함께 확인해야 합니다.")
    return "\n".join(L) + "\n"


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

    facts = load_facts()
    sizing = market_size(facts)
    capacity = supply_capacity(facts)
    matrix = sigungu_matrix(facts)
    revenue = revenue_models(facts, sizing)
    value = public_value(sizing)

    report = build_report(facts, sizing, capacity, matrix, revenue, value)
    (OUT_DIR / "market_report.md").write_text(report, encoding="utf-8")

    with (OUT_DIR / "sigungu_matrix.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(matrix[0].keys()))
        w.writeheader()
        w.writerows(matrix)

    print("=" * 66)
    print("시장 규모 추정 완료")
    print("=" * 66)
    print("대구·경북 연간 출동   {:,}건".format(sizing["daegu_gb"]))
    print("연락 가능 업체        {:,}곳".format(capacity["companies"]))
    worst = max(capacity["rows"], key=lambda r: r["필요_참여업체"])
    print("필요 참여 업체        {:,}곳 ({}%)  ← 우회율 {:.0%} 기준".format(
        worst["필요_참여업체"], worst["필요_참여율"], worst["우회율"]))
    print()
    print("SAM (대구·경북) 범위:")
    lo = min(r["SAM_대구경북"] for r in sizing["rows"])
    hi = max(r["SAM_대구경북"] for r in sizing["rows"])
    print("  {} ~ {}  (우회율 {:.0%}~{:.0%} × 단가 {:,}~{:,}원)".format(
        won(lo), won(hi), DIVERSION_SCENARIOS[0], DIVERSION_SCENARIOS[-1],
        PRICE_SCENARIOS_KRW[0], PRICE_SCENARIOS_KRW[-1]))
    print()
    gaps = sum(1 for r in matrix if r["매칭공백센터"] > 0)
    print("매칭 공백 시군구      {}곳 / {}곳".format(gaps, len(matrix)))
    print()
    print("리포트 : {}".format(OUT_DIR / "market_report.md"))
    print("매트릭스: {}".format(OUT_DIR / "sigungu_matrix.csv"))


if __name__ == "__main__":
    main()
