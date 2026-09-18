"use strict";
/* ============================================================================
   브라우저 측 신고 저장소
   ----------------------------------------------------------------------------
   Vercel 같은 서버리스 배포에서는 파일시스템이 읽기 전용이라 서버가 SQLite에
   신고를 쌓을 수 없다. 그래서 배포판에서는 이 모듈이 app/store.py 자리를 대신하고,
   신고는 방문자의 브라우저에만 남는다.

   - 로컬 실행(`BEE_STORE=sqlite`)에서는 이 모듈이 동작하지 않고 서버 API를 쓴다.
   - 어느 쪽이든 화면 코드는 같은 함수(BeeStore.*)만 호출한다.
   - 사진은 여기에도 저장하지 않는다. 판별 결과 텍스트만 남는다.
   ========================================================================== */

const BeeStore = (() => {
  const KEY = "bee.reports.v1";
  const RETENTION_DAYS = 90;

  const GRADE_RANK = { "저위험": 0, "중위험": 1, "고위험": 2, "판별불가": 2 };

  const STATUS = {
    PENDING_119: "119확인대기",
    PENDING_PARTNER: "업체배정대기",
    ACCEPTED: "업체수락",
    DONE: "처리완료",
    REJECTED: "반려",
  };

  /* localStorage는 시크릿 모드·차단 설정에서 던질 수 있으므로 전부 감싼다 */
  function readAll() {
    try {
      const raw = localStorage.getItem(KEY);
      const rows = raw ? JSON.parse(raw) : [];
      return Array.isArray(rows) ? rows : [];
    } catch {
      return [];
    }
  }

  function writeAll(rows) {
    try {
      localStorage.setItem(KEY, JSON.stringify(rows));
      return true;
    } catch {
      return false;
    }
  }

  /* 보존기간이 지난 신고는 읽는 시점에 파기한다 (서버의 purge_expired 대응) */
  function purged() {
    const cutoff = Date.now() - RETENTION_DAYS * 86400000;
    const rows = readAll();
    const kept = rows.filter((r) => new Date(r.created_at).getTime() >= cutoff);
    if (kept.length !== rows.length) writeAll(kept);
    return kept;
  }

  /** /api/assess 응답을 그대로 받아 신고 레코드로 저장한다. */
  function saveFromAssess(assess, location) {
    const rows = purged();
    const row = {
      id: assess.report_id,
      created_at: new Date().toISOString(),
      lat: location.lat,
      lon: location.lon,
      address_label: location.label,
      route: assess.route,
      route_reason: assess.route_reason,
      risk_grade: assess.vision.risk_grade,
      species: assess.vision.species_guess,
      confidence: assess.vision.confidence,
      final_score: assess.score.final_score,
      status: assess.route === "119" ? STATUS.PENDING_119 : STATUS.PENDING_PARTNER,
      assigned: null,
      summary: assess.report_summary || null,
      slots: assess.slots,
      vision: assess.vision,
      score: assess.score,
      companies: assess.companies || [],
      feedback: [],
    };
    rows.unshift(row);
    writeAll(rows);
    return row;
  }

  function list() {
    return purged().sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
  }

  function get(id) {
    return purged().find((r) => r.id === id) || null;
  }

  /** 저위험 선택지 확정. 라우팅과 상태를 함께 바꾼다. */
  function setRoute(id, route, reason) {
    const rows = purged();
    const row = rows.find((r) => r.id === id);
    if (!row) return false;
    row.route = route;
    row.route_reason = reason;
    row.status = route === "자가대응" ? STATUS.DONE : STATUS.PENDING_PARTNER;
    writeAll(rows);
    return true;
  }

  function setStatus(id, status, assigned) {
    const rows = purged();
    const row = rows.find((r) => r.id === id);
    if (!row) return false;
    row.status = status;
    if (assigned) row.assigned = assigned;
    writeAll(rows);
    return true;
  }

  function addFeedback(id, { actual_grade, actual_species, note, reporter }) {
    const rows = purged();
    const row = rows.find((r) => r.id === id);
    if (!row) return false;
    row.feedback = row.feedback || [];
    row.feedback.unshift({
      created_at: new Date().toISOString(),
      actual_grade: actual_grade || null,
      actual_species: actual_species || null,
      note: note || "",
      reporter: reporter || "",
    });
    writeAll(rows);
    return true;
  }

  /** 서버 store.kpi() 와 같은 모양을 돌려준다. */
  function kpi() {
    const rows = purged();
    const by_route = {};
    const by_status = {};
    for (const r of rows) {
      by_route[r.route] = (by_route[r.route] || 0) + 1;
      by_status[r.status] = (by_status[r.status] || 0) + 1;
    }

    const paired = [];
    for (const r of rows) {
      for (const f of r.feedback || []) {
        if (f.actual_grade) paired.push({ ai: r.risk_grade, actual: f.actual_grade });
      }
    }
    const matched = paired.filter((p) => p.ai === p.actual).length;
    const underestimated = paired.filter(
      (p) => (GRADE_RANK[p.ai] ?? 2) < (GRADE_RANK[p.actual] ?? 2)
    ).length;

    const diverted = (by_route["방역업체"] || 0) + (by_route["양봉협회"] || 0);
    const selfCare = by_route["자가대응"] || 0;
    // 소방력 절감 = 119로 가지 않은 모든 건. 자가대응은 출동 자체를 만들지 않는다.
    const relieved = diverted + selfCare;
    const total = rows.length;

    return {
      total,
      by_route,
      by_status,
      unknown: rows.filter((r) => r.risk_grade === "판별불가").length,
      diverted,
      self_care: selfCare,
      diversion_rate: total ? Math.round((relieved / total) * 1000) / 10 : 0,
      feedback: {
        count: paired.length,
        matched,
        accuracy: paired.length ? Math.round((matched / paired.length) * 1000) / 10 : null,
        underestimated,
      },
      retention_days: RETENTION_DAYS,
    };
  }

  function clear() {
    try { localStorage.removeItem(KEY); } catch { /* 무시 */ }
  }

  function available() {
    try {
      localStorage.setItem("bee.probe", "1");
      localStorage.removeItem("bee.probe");
      return true;
    } catch {
      return false;
    }
  }

  return { STATUS, saveFromAssess, list, get, setRoute, setStatus, addFeedback, kpi, clear, available };
})();

/* ----------------------------------------------------------------------------
   화면 코드가 서버/브라우저 저장소를 구분하지 않도록 감싸는 어댑터.
   meta.store 값에 따라 한쪽으로만 간다.
   -------------------------------------------------------------------------- */
const BeeReports = {
  mode: "sqlite",

  init(meta) {
    this.mode = meta && meta.store === "client" ? "client" : "sqlite";
    return this.mode;
  },

  get isClient() {
    return this.mode === "client";
  },

  async _api(path, options) {
    const res = await fetch(path, options);
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.detail || `요청 실패 (${res.status})`);
    return body;
  },

  async list() {
    if (this.isClient) return { reports: BeeStore.list(), kpi: BeeStore.kpi() };
    return this._api("/api/reports");
  },

  async get(id) {
    if (this.isClient) {
      const r = BeeStore.get(id);
      if (!r) throw new Error("신고를 찾을 수 없습니다.");
      return r;
    }
    return this._api(`/api/reports/${id}`);
  },

  async setRoute(id, route, reason) {
    if (this.isClient) {
      if (!BeeStore.setRoute(id, route, reason)) throw new Error("신고를 찾을 수 없습니다.");
      return { ok: true };
    }
    return this._api(`/api/reports/${id}/route`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ route, reason }),
    });
  },

  async setStatus(id, status, assigned) {
    if (this.isClient) {
      if (!BeeStore.setStatus(id, status, assigned)) throw new Error("신고를 찾을 수 없습니다.");
      return { ok: true };
    }
    return this._api(`/api/reports/${id}/status`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status, assigned: assigned || null }),
    });
  },

  async addFeedback(id, payload) {
    if (this.isClient) {
      if (!BeeStore.addFeedback(id, payload)) throw new Error("신고를 찾을 수 없습니다.");
      return { ok: true };
    }
    return this._api(`/api/reports/${id}/feedback`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },

  /** 시민 화면에서 판정이 끝난 직후 호출. 서버 모드면 이미 저장돼 있으므로 아무것도 안 한다. */
  saveAssess(assess, location) {
    if (this.isClient) BeeStore.saveFromAssess(assess, location);
  },
};
