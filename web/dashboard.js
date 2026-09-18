"use strict";

const $ = (id) => document.getElementById(id);
const escapeHtml = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );

const state = { filter: "all", reports: [], kpi: null, selected: null };

async function api(path, options) {
  const res = await fetch(path, options);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `요청 실패 (${res.status})`);
  return body;
}

const fmtTime = (iso) => {
  const d = new Date(iso);
  return `${String(d.getMonth() + 1).padStart(2, "0")}/${String(d.getDate()).padStart(2, "0")} ` +
         `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
};

function statusClass(s) {
  if (s.includes("대기")) return "wait";
  if (s === "처리완료" || s === "업체수락") return "done";
  return "";
}

/* ───────────────────────── KPI ───────────────────────── */

function renderKpi(k) {
  $("retention").textContent = k.retention_days;

  const acc = k.feedback.accuracy;
  const cards = [
    { lbl: "총 접수", num: k.total, sub: "누적 신고 건수" },
    { lbl: "119 연계", num: k.by_route["119"] || 0, sub: "실제 출동 필요 판정", cls: "hi" },
    { lbl: "민간 우회", num: k.diverted, sub: "방역업체 + 양봉협회", cls: "good" },
    { lbl: "소방력 절감률", num: `${k.diversion_rate}%`, sub: "전체 대비 민간 우회 비율", cls: "good" },
    { lbl: "판별불가", num: k.unknown, sub: "보수적으로 119 확인 대상" },
    {
      lbl: "사후 확인 정확도",
      num: acc === null ? "—" : `${acc}%`,
      sub: acc === null
        ? "확인 결과 입력 대기"
        : `${k.feedback.count}건 중 ${k.feedback.matched}건 일치 · 위험 과소평가 ${k.feedback.underestimated}건`,
      cls: k.feedback.underestimated > 0 ? "hi" : acc === null ? "" : "good",
    },
  ];

  $("kpi-row").innerHTML = cards
    .map(
      (c) =>
        `<div class="kpi ${c.cls || ""}"><div class="lbl">${escapeHtml(c.lbl)}</div>` +
        `<div class="num">${escapeHtml(String(c.num))}</div>` +
        `<div class="sub">${escapeHtml(c.sub)}</div></div>`
    )
    .join("");
}

/* ───────────────────────── 목록 ───────────────────────── */

function visible() {
  const f = state.filter;
  return state.reports.filter((r) => {
    if (f === "all") return true;
    if (f === "119") return r.route === "119";
    if (f === "diverted") return r.route !== "119";
    if (f === "wait") return r.status.includes("대기");
    return r.risk_grade === f;
  });
}

function renderList() {
  const rows = visible();
  if (!rows.length) {
    $("list").innerHTML =
      '<div class="empty">해당 조건의 신고가 없습니다. 시민 신고 화면에서 신고를 몇 건 만들어 보세요.</div>';
    return;
  }

  $("list").innerHTML = `
    <table>
      <thead><tr>
        <th>접수</th><th>ID</th><th>등급</th><th>추정 종</th>
        <th class="num">신뢰도</th><th class="num">점수</th><th>위치</th><th>연계</th><th>상태</th>
      </tr></thead>
      <tbody>
        ${rows
          .map(
            (r) => `<tr class="clickable ${state.selected === r.id ? "sel" : ""}" data-id="${r.id}">
            <td>${fmtTime(r.created_at)}</td>
            <td>${escapeHtml(r.id)}</td>
            <td><span class="grade ${escapeHtml(r.risk_grade)}">${escapeHtml(r.risk_grade)}</span></td>
            <td>${escapeHtml(r.species)}</td>
            <td class="num">${Math.round((r.confidence || 0) * 100)}%</td>
            <td class="num">${(r.final_score || 0).toFixed(2)}</td>
            <td>${escapeHtml(r.address_label || "-")}</td>
            <td>${escapeHtml(r.route)}</td>
            <td><span class="status ${statusClass(r.status)}">${escapeHtml(r.status)}</span></td>
          </tr>`
          )
          .join("")}
      </tbody>
    </table>`;
}

$("list").addEventListener("click", (e) => {
  const tr = e.target.closest("[data-id]");
  if (!tr) return;
  state.selected = state.selected === tr.dataset.id ? null : tr.dataset.id;
  renderList();
  if (state.selected) showDetail(state.selected);
  else $("detail").innerHTML = "";
});

/* ───────────────────────── 상세 ───────────────────────── */

async function showDetail(id) {
  $("detail").innerHTML = '<div class="result"><span class="spinner"></span>불러오는 중…</div>';
  let r;
  try {
    r = await BeeReports.get(id);
  } catch (err) {
    $("detail").innerHTML = `<div class="result"><div class="alert danger">${escapeHtml(err.message)}</div></div>`;
    return;
  }

  const scoreRows = (r.score?.detail || [])
    .map(
      (d) => `<tr><td><b>${escapeHtml(d.구분)}</b><br><span class="sub-detail">${escapeHtml(d.입력)}</span></td>
              <td class="num">${d.비중.toFixed(2)}</td><td class="num">${d.정규화값.toFixed(3)}</td>
              <td class="num"><b>${d.기여.toFixed(3)}</b></td></tr>`
    )
    .join("");

  const fbList = (r.feedback || []).length
    ? `<ul class="notes">${r.feedback
        .map(
          (f) =>
            `<li>${fmtTime(f.created_at)} · 실제 ${escapeHtml(f.actual_grade || "-")} ` +
            `${escapeHtml(f.actual_species || "")} ${escapeHtml(f.note || "")} ` +
            `<span class="sub-detail">(${escapeHtml(f.reporter || "익명")})</span></li>`
        )
        .join("")}</ul>`
    : "";

  $("detail").innerHTML = `
    <div class="result">
      <h3>${escapeHtml(r.id)} <span class="grade ${escapeHtml(r.risk_grade)}">${escapeHtml(r.risk_grade)}</span>
        <span class="status ${statusClass(r.status)}">${escapeHtml(r.status)}</span></h3>

      <div class="kv">
        <span class="k">접수</span><span>${fmtTime(r.created_at)}</span>
        <span class="k">위치</span><span>${escapeHtml(r.address_label || "-")} (${(r.lat ?? 0).toFixed(5)}, ${(r.lon ?? 0).toFixed(5)})</span>
        <span class="k">연계</span><span>${escapeHtml(r.route)} — ${escapeHtml(r.route_reason)}</span>
      </div>

      <div class="evidence">${escapeHtml(r.vision?.evidence || "")}</div>

      <h3 style="margin-top:16px">스코어 분해</h3>
      <table>
        <thead><tr><th>구분</th><th class="num">비중</th><th class="num">정규화</th><th class="num">기여</th></tr></thead>
        <tbody>${scoreRows}</tbody>
        <tfoot><tr><td colspan="3"><b>최종 위험점수</b></td><td class="num"><b>${(r.final_score || 0).toFixed(3)}</b></td></tr></tfoot>
      </table>

      ${r.summary ? `<h3 style="margin-top:16px">상황실 접수 요약</h3><pre class="summary">${escapeHtml(r.summary)}</pre>` : ""}

      <div class="actions">
        <button class="ok" data-act="처리완료" data-id="${r.id}">확인 완료</button>
        <button class="sec" data-act="반려" data-id="${r.id}">반려 (민간 이관)</button>
      </div>

      <div class="fb-form">
        <h4>사후 확인 결과 등록</h4>
        <p>현장에서 확인된 실제 벌 종을 입력하면 재학습 데이터로 쌓이고, 위 정확도 지표에 반영됩니다. (기획서 5.2 데이터 선순환)</p>
        <div class="fb-row">
          <select id="fb-grade">
            <option value="">실제 등급 선택</option>
            <option>고위험</option><option>중위험</option><option>저위험</option><option>판별불가</option>
          </select>
          <input id="fb-species" placeholder="실제 종 (예: 등검은말벌)">
          <input id="fb-note" placeholder="비고 (선택)">
          <button data-fb="${r.id}">등록</button>
        </div>
      </div>

      ${fbList}
    </div>`;
}

$("detail").addEventListener("click", async (e) => {
  const actBtn = e.target.closest("[data-act]");
  if (actBtn) {
    try {
      await BeeReports.setStatus(actBtn.dataset.id, actBtn.dataset.act);
      await load();
      showDetail(actBtn.dataset.id);
    } catch (err) {
      alert(err.message);
    }
    return;
  }

  const fbBtn = e.target.closest("[data-fb]");
  if (fbBtn) {
    const grade = $("fb-grade").value;
    const species = $("fb-species").value.trim();
    if (!grade && !species) {
      alert("실제 등급 또는 실제 종 중 하나는 입력해주세요.");
      return;
    }
    try {
      await BeeReports.addFeedback(fbBtn.dataset.fb, {
        actual_grade: grade || null,
        actual_species: species || null,
        note: $("fb-note").value.trim(),
        reporter: "상황실",
      });
      await load();
      showDetail(fbBtn.dataset.fb);
    } catch (err) {
      alert(err.message);
    }
  }
});

/* ───────────────────────── 로드 ───────────────────────── */

$("filters").addEventListener("click", (e) => {
  const chip = e.target.closest("[data-filter]");
  if (!chip) return;
  state.filter = chip.dataset.filter;
  [...$("filters").children].forEach((c) => c.classList.toggle("on", c === chip));
  renderList();
});

$("refresh-btn").addEventListener("click", () => load());

async function load() {
  try {
    const data = await BeeReports.list();
    state.reports = data.reports;
    state.kpi = data.kpi;
    renderKpi(data.kpi);
    renderList();
    $("refresh-line").textContent = `${new Date().toLocaleTimeString("ko-KR")} 기준`;
  } catch (err) {
    $("list").innerHTML = `<div class="empty">${escapeHtml(err.message)}</div>`;
  }
}

(async () => {
  try { BeeReports.init(await api("/api/meta")); } catch { /* 기본값 유지 */ }
  load();
  setInterval(load, 15000);
})();
