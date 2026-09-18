"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  sessionId: null,
  meta: null,
  slots: {},
  busy: false,
  location: null, // {label, lat, lon, source}
  assess: null,   // 마지막 판정 결과 (선택지 확정에 재사용)
};

const SLOT_FORMAT = {
  location_type: (v) => v,
  nest_size_cm: (v) => `${v}cm`,
  discovered_days_ago: (v) => (v === 0 ? "오늘" : `${v}일 전`),
  aggression_observed: (v) => (v ? "관찰됨" : "관찰 안 됨"),
  reporter_risk_self_assessment: (v) => v,
};

const escapeHtml = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );

async function api(path, options) {
  const res = await fetch(path, options);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `요청 실패 (${res.status})`);
  return body;
}

function unlock(id) {
  $(id).classList.remove("locked");
}

/* ─────────────────────────── 초기화 ─────────────────────────── */

async function boot() {
  try {
    state.meta = await api("/api/meta");
  } catch (err) {
    $("mode-badge").textContent = "서버 연결 실패";
    return;
  }
  const m = state.meta;
  BeeReports.init(m);

  const badge = $("mode-badge");
  badge.textContent = m.mode === "live" ? `LIVE · ${m.model}` : "MOCK 모드";
  badge.className = `badge ${m.mode}`;

  $("data-line").textContent =
    `매칭 DB ${m.data.업체_총건수.toLocaleString()}곳 ` +
    `(연락가능 ${m.data.업체_연락가능.toLocaleString()}) · ` +
    `119안전센터 ${m.data.안전센터_건수}곳`;

  $("conf-th").textContent = `${Math.round(m.thresholds.confidence * 100)}%`;
  $("retention-days").textContent = m.privacy.retention_days;

  $("fewshot-tag").innerHTML = m.few_shot.count
    ? `<span class="pill ok">few-shot 참조 사진 ${m.few_shot.count}장 적용</span>`
    : `<span class="pill">few-shot 텍스트 예시 3건 (참조 사진 미등록)</span>`;

  $("scenario-list").innerHTML = m.scenarios
    .map(
      (s) =>
        `<button class="scen-btn" data-scenario="${escapeHtml(s.key)}">` +
        `<b>${escapeHtml(s.label)}</b><span>${escapeHtml(s.hint)}</span></button>`
    )
    .join("");

  $("loc-select").innerHTML =
    `<option value="">데모 위치 선택…</option>` +
    m.demo_locations.map((l, i) => `<option value="${i}">${escapeHtml(l.label)}</option>`).join("");

  renderStats();
}

/* ─────────────────────────── 동의 ─────────────────────────── */

$("consent-check").addEventListener("change", (e) => {
  $("consent-btn").disabled = !e.target.checked;
});

$("consent-btn").addEventListener("click", async () => {
  $("step0").classList.add("locked");
  unlock("step1");
  await startTriage();
  $("step1").scrollIntoView({ behavior: "smooth", block: "start" });
});

/* ─────────────────────────── 1단계 ─────────────────────────── */

function pushMsg(text, kind) {
  const div = document.createElement("div");
  div.className = `msg ${kind}`;
  div.textContent = text;
  $("chat").appendChild(div);
  $("chat").scrollTop = $("chat").scrollHeight;
  return div;
}

function renderSlots(progress, slots) {
  state.slots = slots;
  $("slot-count").textContent = `${progress.filled} / ${progress.total}`;
  $("slot-bar").style.width = `${(progress.filled / progress.total) * 100}%`;

  $("slot-list").innerHTML = Object.entries(progress.labels)
    .map(([key, label]) => {
      const v = slots[key];
      const has = v !== null && v !== undefined;
      const text = has ? SLOT_FORMAT[key](v) : "미확인";
      return (
        `<li><span class="k">${escapeHtml(label)}</span>` +
        `<span class="v ${has ? "" : "empty"}">${escapeHtml(text)}</span></li>`
      );
    })
    .join("");

  if (progress.ready_for_photo) unlock("step2");
}

async function startTriage() {
  $("chat").innerHTML = "";
  $("vision-result").innerHTML = "";
  $("assess-result").innerHTML = "";
  $("step2").classList.add("locked");
  $("step3").classList.add("locked");

  const data = await api("/api/triage/start", { method: "POST" });
  state.sessionId = data.session_id;

  pushMsg(data.message, "bot");
  pushMsg(data.question, "bot q");
  renderSlots(data.progress, data.slots);

  $("chat-input").disabled = false;
  $("chat-send").disabled = false;
  $("chat-input").focus();
}

$("chat-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("chat-input");
  const text = input.value.trim();
  if (!text || state.busy) return;

  pushMsg(text, "user");
  input.value = "";
  state.busy = true;
  // 응답 대기 중 입력을 막지 않으면 다음 답변이 입력창에 뭉쳐 들어간다.
  input.disabled = true;
  $("chat-send").disabled = true;

  const thinking = pushMsg("…", "bot");
  try {
    const data = await api("/api/triage/message", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: state.sessionId, message: text }),
    });
    thinking.remove();

    $("engine-tag").textContent =
      data.engine === "claude" ? "슬롯 추출: Claude (Tool Use)" : "슬롯 추출: 규칙 기반";

    pushMsg(data.reply, "bot");
    if (data.assumed) {
      pushMsg("보수적 기본값이 적용된 항목이 있습니다 (안전장치)", "sys");
    }
    if (data.question) {
      pushMsg(data.question, "bot q");
    } else {
      pushMsg("문진이 끝났습니다. 아래 2단계에서 사진을 올려주세요.", "sys");
    }
    renderSlots(data.progress, data.slots);
  } catch (err) {
    thinking.remove();
    pushMsg(`오류: ${err.message}`, "sys");
  } finally {
    state.busy = false;
    input.disabled = false;
    $("chat-send").disabled = false;
    input.focus();
  }
});

$("restart-btn").addEventListener("click", () => startTriage());

/* ─────────────────────────── 2단계 ─────────────────────────── */

async function submitVision(formData) {
  $("vision-result").innerHTML =
    '<div class="result"><span class="spinner"></span>판별 중…</div>';
  try {
    const data = await api("/api/vision", { method: "POST", body: formData });
    state.slots = data.slots;
    renderVision(data);
    if (data.exif_location) {
      setLocation({
        label: "사진 촬영 위치 (EXIF)",
        lat: data.exif_location.lat,
        lon: data.exif_location.lon,
        source: "exif",
      });
    }
    unlock("step3");
  } catch (err) {
    $("vision-result").innerHTML =
      `<div class="result"><div class="alert danger">${escapeHtml(err.message)}</div></div>`;
  }
}

function renderVision(data) {
  const v = data.vision;
  const size =
    v.estimated_nest_diameter_cm !== null
      ? `${v.estimated_nest_diameter_cm}cm (사진 기반 추정)`
      : "추정 불가";

  const alerts = [];
  if (v.downgraded_by_threshold) {
    alerts.push(
      `<div class="alert danger">신뢰도 ${Math.round(v.confidence * 100)}%가 기준치 ` +
        `${Math.round(state.meta.thresholds.confidence * 100)}%에 못 미쳐 등급을 ` +
        `<b>판별불가</b>로 강등했습니다. 보수적 안전장치가 작동했습니다.</div>`
    );
  }
  if (data.warning) {
    alerts.push(`<div class="alert warn">${escapeHtml(data.warning)}</div>`);
  }
  if (data.exif_location) {
    alerts.push(
      `<div class="alert warn">사진에서 촬영 위치를 읽어 3단계 신고 위치에 자동 입력했습니다. ` +
        `원치 않으시면 직접 바꾸실 수 있습니다.</div>`
    );
  }

  $("vision-result").innerHTML = `
    <div class="result">
      <h3>판별 결과 <span class="grade ${escapeHtml(v.risk_grade)}">${escapeHtml(v.risk_grade)}</span></h3>
      <div class="kv">
        <span class="k">추정 종</span><span>${escapeHtml(v.species_guess)}</span>
        <span class="k">신뢰도</span><span>${Math.round(v.confidence * 100)}%</span>
        <span class="k">벌집 지름</span><span>${escapeHtml(size)}</span>
        <span class="k">관찰 개체</span><span>${escapeHtml(v.visible_bee_count_band)}</span>
        <span class="k">분봉 여부</span><span>${v.is_swarm ? "꿀벌 분봉으로 판단" : "해당 없음"}</span>
        <span class="k">판별 엔진</span><span>${v.source === "claude" ? "Claude Vision (Tool Use)" : v.source === "mock" ? "목업 시나리오" : "호출 실패"}</span>
      </div>
      <div class="evidence">${escapeHtml(v.evidence)}</div>
      ${alerts.join("")}
    </div>`;
}

$("file-input").addEventListener("change", (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("session_id", state.sessionId);
  fd.append("file", file);
  submitVision(fd);
});

$("scenario-list").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-scenario]");
  if (!btn) return;
  const fd = new FormData();
  fd.append("session_id", state.sessionId);
  fd.append("scenario", btn.dataset.scenario);
  submitVision(fd);
});

/* ─────────────────────────── 위치 선택 ─────────────────────────── */

function setLocation(loc) {
  state.location = loc;
  const badge =
    loc.source === "exif" ? " (사진 EXIF)" : loc.source === "search" ? " (검색)" : "";
  $("loc-current").textContent =
    `선택된 위치: ${loc.label}${badge} — ${loc.lat.toFixed(5)}, ${loc.lon.toFixed(5)}`;
}

let geoTimer = null;
$("geo-input").addEventListener("input", (e) => {
  const q = e.target.value.trim();
  clearTimeout(geoTimer);
  if (q.length < 2) {
    $("geo-results").hidden = true;
    return;
  }
  geoTimer = setTimeout(async () => {
    try {
      const { results } = await api(`/api/geocode?q=${encodeURIComponent(q)}`);
      if (!results.length) {
        $("geo-results").hidden = true;
        return;
      }
      $("geo-results").innerHTML = results
        .map(
          (r, i) =>
            `<button data-geo="${i}"><b>${escapeHtml(r.label)}</b><span>${escapeHtml(r.detail)}</span></button>`
        )
        .join("");
      $("geo-results").dataset.payload = JSON.stringify(results);
      $("geo-results").hidden = false;
    } catch {
      $("geo-results").hidden = true;
    }
  }, 250);
});

$("geo-results").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-geo]");
  if (!btn) return;
  const results = JSON.parse($("geo-results").dataset.payload || "[]");
  const r = results[Number(btn.dataset.geo)];
  if (!r) return;
  setLocation({ label: r.label, lat: r.lat, lon: r.lon, source: "search" });
  $("geo-input").value = r.label;
  $("geo-results").hidden = true;
  $("loc-select").value = "";
});

document.addEventListener("click", (e) => {
  if (!e.target.closest(".geo-wrap")) $("geo-results").hidden = true;
});

$("loc-select").addEventListener("change", (e) => {
  if (e.target.value === "") return;
  const l = state.meta.demo_locations[Number(e.target.value)];
  setLocation({ label: l.label, lat: l.lat, lon: l.lon, source: "demo" });
  $("geo-input").value = "";
});

/* ─────────────────────────── 3단계 ─────────────────────────── */

$("assess-btn").addEventListener("click", async () => {
  if (!state.location) {
    $("assess-result").innerHTML =
      '<div class="result"><div class="alert warn">신고 위치를 먼저 선택해주세요. 주소를 검색하거나 데모 위치를 고르시면 됩니다.</div></div>';
    return;
  }
  const loc = state.location;
  $("assess-result").innerHTML =
    '<div class="result"><span class="spinner"></span>스코어링 및 매칭 중…</div>';
  try {
    const data = await api("/api/assess", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: state.sessionId,
        lat: loc.lat,
        lon: loc.lon,
        address_label: loc.label,
      }),
    });
    BeeReports.saveAssess(data, loc);
    renderAssess(data);
  } catch (err) {
    $("assess-result").innerHTML =
      `<div class="result"><div class="alert danger">${escapeHtml(err.message)}</div></div>`;
  }
});

function routeClass(route) {
  if (route === "119") return "r119";
  if (route === "양봉협회" || route === "자가대응") return "rbee";
  if (route === "선택") return "rchoice";
  return "rprivate";
}

function routeTitle(route) {
  if (route === "119") return "119 상황실 연계";
  if (route === "양봉협회") return "양봉협회 지회 연계";
  if (route === "자가대응") return "자가 대응 — 출동 없이 종결";
  if (route === "선택") return "어떻게 하시겠어요?";
  return "민간 소독·방역업체 연계";
}

/** 저위험 구간에서 시민이 고를 두 갈래. */
function renderChoice(d) {
  const n = d.companies.length;
  return `
    <div class="choice-grid">
      <button class="choice-card" data-choose="자가대응">
        <span class="choice-tag ok">출동 없음</span>
        <b>그대로 두겠습니다</b>
        <span>제거하지 않고 안전 수칙만 안내받습니다. 상황이 바뀌면 언제든 다시 신고하실 수 있습니다.</span>
      </button>
      <button class="choice-card" data-choose="방역업체">
        <span class="choice-tag">유상 처리</span>
        <b>그래도 불안해서 맡기겠습니다</b>
        <span>${n ? `인근 소독·방역업체 ${n}곳에 매칭 요청을 보냅니다. 비용은 업체와 직접 조율하십니다.` : "인근에 연락 가능한 업체가 없어 즉시 매칭되지 않을 수 있습니다."}</span>
      </button>
    </div>`;
}

function renderSelfCare(sc) {
  const li = (arr) => arr.map((t) => `<li>${escapeHtml(t)}</li>`).join("");
  return `
    <div class="evidence">${escapeHtml(sc.summary)}</div>

    <h3 style="margin-top:18px">지켜주실 것</h3>
    <ul class="guide-list">${li(sc.rules)}</ul>

    <h3 style="margin-top:18px">이럴 때는 다시 신고해주세요</h3>
    <ul class="guide-list warn">${li(sc.recall_when)}</ul>

    <h3 style="margin-top:18px">혹시 쏘였다면</h3>
    <ul class="guide-list">${li(sc.if_stung)}</ul>

    <div class="alert danger">${escapeHtml(sc.emergency)}</div>`;
}

function renderAssess(d) {
  state.assess = d;
  const rows = d.score.detail
    .map(
      (r) => `
      <tr>
        <td><b>${escapeHtml(r.구분)}</b><br><span class="sub-detail">${escapeHtml(r.입력)}</span></td>
        <td class="num">${r.비중.toFixed(2)}</td>
        <td class="num">${r.정규화값.toFixed(3)}</td>
        <td class="num"><b>${r.기여.toFixed(3)}</b></td>
      </tr>`
    )
    .join("");

  let channel = "";
  if (d.route === "선택") {
    channel = renderChoice(d);
  } else if (d.route === "자가대응") {
    channel = renderSelfCare(d.self_care);
  } else if (d.route === "119") {
    channel = `
      <h3 style="margin-top:18px">자동 생성된 상황실 접수 요약</h3>
      <pre class="summary">${escapeHtml(d.report_summary || "")}</pre>`;
  } else {
    const list = d.companies.length
      ? `<table>
          <thead><tr><th>업체</th><th>연락처</th><th class="num">거리</th><th class="num">보호복</th></tr></thead>
          <tbody>${d.companies
            .map(
              (c) => `<tr>
                <td><b>${escapeHtml(c.업체명)}</b><br><span class="sub-detail">${escapeHtml(c.시군구)} · ${escapeHtml(c.도로명주소)}</span></td>
                <td>${escapeHtml(c.전화번호 || "-")}</td>
                <td class="num">${c.거리km.toFixed(2)}km</td>
                <td class="num">${c.보호복_수}벌</td>
              </tr>`
            )
            .join("")}</tbody>
        </table>`
      : `<div class="alert warn">반경 내 연락 가능한 업체를 찾지 못했습니다.</div>`;

    const branch = d.beekeeping_branch
      ? `<div class="evidence">
           <b>${escapeHtml(d.beekeeping_branch.지회)}</b> · 회원 ${d.beekeeping_branch.회원수.toLocaleString()}명<br>
           ${escapeHtml(d.beekeeping_branch.공식_문의창구)}
         </div>`
      : "";

    channel = `
      ${branch}
      <h3 style="margin-top:18px">거리순 매칭 결과 (최대 3곳 동시 알림)</h3>
      ${list}
      <p class="sub-detail" style="margin-top:8px">
        프로토타입에서는 하버사인 거리 계산으로 구현했습니다. 정식 서비스는 PostGIS 반경 검색 + 카카오 알림톡 발송입니다.
        수락 과정은 <a href="/partner">업체·양봉인 화면</a>에서 확인하실 수 있습니다.
      </p>`;
  }

  const fc = d.fire_center
    ? `<p class="sub-detail" style="margin-top:10px">관할 추정: ${escapeHtml(d.fire_center.소방서명)} ${escapeHtml(d.fire_center.안전센터명)} · 약 ${d.fire_center.거리km.toFixed(2)}km · ${escapeHtml(d.fire_center.전화번호 || "-")}</p>`
    : "";

  const notes = d.notes.length
    ? `<ul class="notes">${d.notes.map((n) => `<li>${escapeHtml(n)}</li>`).join("")}</ul>`
    : "";

  $("assess-result").innerHTML = `
    <div class="result">
      <div class="route-banner ${routeClass(d.route)}">
        <div class="score-ring">
          <div class="num">${d.score.final_score.toFixed(2)}</div>
          <div class="lbl">최종 위험점수</div>
        </div>
        <div>
          <div class="big">${escapeHtml(routeTitle(d.route))}</div>
          <p>${escapeHtml(d.route_reason)}</p>
        </div>
      </div>

      <p class="sub-detail">접수번호 <b>${escapeHtml(d.report_id)}</b> —
        <a href="/dashboard">소방 관제 화면</a>에서 조회됩니다.</p>

      <h3>스코어 분해</h3>
      <table>
        <thead><tr><th>구분</th><th class="num">비중</th><th class="num">정규화</th><th class="num">기여</th></tr></thead>
        <tbody>${rows}</tbody>
        <tfoot><tr><td colspan="3"><b>최종 위험점수</b></td><td class="num"><b>${d.score.final_score.toFixed(3)}</b></td></tr></tfoot>
      </table>
      ${fc}
      ${channel}
      ${notes}
    </div>`;
}

/* 저위험 선택지 — 시민이 고른 결과를 최종 라우팅으로 확정한다 */
$("assess-result").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-choose]");
  if (!btn || !state.assess) return;

  const chosen = btn.dataset.choose;
  const d = state.assess;
  d.route = chosen;
  d.route_reason =
    chosen === "자가대응"
      ? "신고자가 '그대로 두겠다'를 선택해 출동 없이 안전 수칙 안내로 종결했습니다."
      : "신고자가 '불안해서 맡기겠다'를 선택해 인근 소독·방역업체로 연계했습니다.";

  try {
    await BeeReports.setRoute(d.report_id, chosen, d.route_reason);
  } catch (err) {
    // 저장 실패해도 화면은 진행한다. 안내 자체가 더 중요하다.
    console.warn(err);
  }
  renderAssess(d);
  $("assess-result").scrollIntoView({ behavior: "smooth", block: "start" });
});

/* ─────────────────────────── 통계 ─────────────────────────── */

async function renderStats() {
  let s;
  try {
    s = await api("/api/stats");
  } catch {
    return;
  }
  const nat = s.national[s.national.length - 1];
  const gb = s.regional.find((r) => r.본부 === "경북" && r.연도 === 2025);
  const dg = s.regional.find((r) => r.본부 === "대구" && r.연도 === 2025);
  const prox = s.proximity;

  const card = (lbl, num, sub) =>
    `<div class="stat"><div class="lbl">${escapeHtml(lbl)}</div>` +
    `<div class="num">${num}</div><div class="delta">${sub}</div></div>`;

  $("stats-box").innerHTML = [
    card(
      `전국 벌집제거 출동 (${nat.연도})`,
      nat.벌집제거_출동.toLocaleString() + "건",
      `생활안전 출동의 ${nat.벌집제거_비중.toFixed(1)}%`
    ),
    card(
      "경북 (2025)",
      gb ? gb.벌집제거_출동.toLocaleString() + "건" : "-",
      gb ? `생활안전 출동의 ${gb.벌집제거_비중.toFixed(1)}%` : ""
    ),
    card(
      "대구 (2025)",
      dg ? dg.벌집제거_출동.toLocaleString() + "건" : "-",
      dg ? `생활안전 출동의 ${dg.벌집제거_비중.toFixed(1)}%` : ""
    ),
    ...prox.map((p) =>
      card(
        `${p.시도} 안전센터 근접성`,
        `${p.반경5km내_업체있음} / ${p.안전센터수}`,
        `반경 5km 내 업체 보유 · 최근접 연락가능 중앙값 ${p.최근접연락가능업체거리_중앙값km}km`
      )
    ),
  ].join("");
}

boot();
