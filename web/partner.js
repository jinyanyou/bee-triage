"use strict";

const $ = (id) => document.getElementById(id);
const escapeHtml = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );

const state = { filter: "pending", reports: [] };

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

function visible() {
  // 119로 간 건은 민간에 노출하지 않는다.
  const civil = state.reports.filter((r) => r.route !== "119");
  if (state.filter === "pending") return civil.filter((r) => r.status === "업체배정대기");
  if (state.filter === "mine") return civil.filter((r) => r.status === "업체수락" || r.status === "처리완료");
  return civil;
}

function card(r) {
  const companies = r.companies || [];
  const isSwarm = r.vision?.is_swarm;

  const options = companies.length
    ? companies
        .map(
          (c) =>
            `<option value="${escapeHtml(c.업체명)}">${escapeHtml(c.업체명)} · ${c.거리km.toFixed(2)}km · ${escapeHtml(c.전화번호 || "-")}</option>`
        )
        .join("")
    : `<option value="직접 처리">직접 처리</option>`;

  const accept =
    r.status === "업체배정대기"
      ? `<div class="fb-row" style="margin-top:12px">
           <select id="who-${r.id}">${options}</select>
           <button data-accept="${r.id}">수락</button>
           <button class="sec" data-reject="${r.id}">거절</button>
         </div>`
      : "";

  const done =
    r.status === "업체수락"
      ? `<div class="fb-form">
           <h4>처리 결과 등록</h4>
           <p>현장에서 확인한 실제 벌 종을 남겨주시면 판별 모델 재학습에 쓰입니다.</p>
           <div class="fb-row">
             <select id="g-${r.id}">
               <option value="">실제 등급</option>
               <option>고위험</option><option>중위험</option><option>저위험</option>
             </select>
             <input id="s-${r.id}" placeholder="실제 종 (예: 양봉꿀벌)">
             <button class="ok" data-done="${r.id}">처리 완료</button>
           </div>
         </div>`
      : "";

  const assigned = r.assigned
    ? `<span class="pill ok">${escapeHtml(r.assigned)} 배정</span>`
    : "";

  const list = companies.length
    ? `<table>
        <thead><tr><th>업체</th><th>연락처</th><th class="num">거리</th><th class="num">보호복</th></tr></thead>
        <tbody>${companies
          .map(
            (c) => `<tr>
              <td><b>${escapeHtml(c.업체명)}</b><br><span class="sub-detail">${escapeHtml(c.도로명주소)}</span></td>
              <td>${escapeHtml(c.전화번호 || "-")}</td>
              <td class="num">${c.거리km.toFixed(2)}km</td>
              <td class="num">${c.보호복_수}벌</td>
            </tr>`
          )
          .join("")}</tbody>
      </table>`
    : `<div class="alert warn">반경 내 연락 가능한 업체가 없어 매칭 후보가 비어 있습니다. 매칭 공백 지역입니다.</div>`;

  return `
    <div class="result">
      <h3>
        ${escapeHtml(r.id)}
        <span class="grade ${escapeHtml(r.risk_grade)}">${escapeHtml(r.risk_grade)}</span>
        <span class="status ${r.status.includes("대기") ? "wait" : "done"}">${escapeHtml(r.status)}</span>
        ${isSwarm ? '<span class="pill ok">꿀벌 분봉 · 회수 가치 있음</span>' : ""}
        ${assigned}
      </h3>

      <div class="kv">
        <span class="k">접수</span><span>${fmtTime(r.created_at)}</span>
        <span class="k">위치</span><span>${escapeHtml(r.address_label || "-")}</span>
        <span class="k">추정 종</span><span>${escapeHtml(r.species)} (신뢰도 ${Math.round((r.confidence || 0) * 100)}%)</span>
        <span class="k">현장</span><span>${escapeHtml(r.slots?.location_type || "-")} · 지름 ${
          r.vision?.estimated_nest_diameter_cm ?? r.slots?.nest_size_cm ?? "-"
        }cm · 공격성 ${r.slots?.aggression_observed ? "관찰됨" : "없음"}</span>
      </div>

      <div class="evidence">${escapeHtml(r.vision?.evidence || "")}</div>

      <h3 style="margin-top:16px">매칭 후보 (거리순)</h3>
      ${list}
      ${accept}
      ${done}
    </div>`;
}

function render() {
  const rows = visible();
  $("list").innerHTML = rows.length
    ? rows.map(card).join("")
    : '<div class="empty">해당 조건의 매칭 요청이 없습니다.</div>';
}

$("filters").addEventListener("click", (e) => {
  const chip = e.target.closest("[data-filter]");
  if (!chip) return;
  state.filter = chip.dataset.filter;
  [...$("filters").children].forEach((c) => c.classList.toggle("on", c === chip));
  render();
});

$("list").addEventListener("click", async (e) => {
  const acc = e.target.closest("[data-accept]");
  const rej = e.target.closest("[data-reject]");
  const fin = e.target.closest("[data-done]");

  try {
    if (acc) {
      const id = acc.dataset.accept;
      await BeeReports.setStatus(id, "업체수락", $(`who-${id}`).value);
    } else if (rej) {
      await BeeReports.setStatus(rej.dataset.reject, "반려");
    } else if (fin) {
      const id = fin.dataset.done;
      const grade = $(`g-${id}`).value;
      const species = $(`s-${id}`).value.trim();
      if (grade || species) {
        await BeeReports.addFeedback(id, {
          actual_grade: grade || null,
          actual_species: species || null,
          note: "",
          reporter: "처리 업체",
        });
      }
      await BeeReports.setStatus(id, "처리완료");
    } else {
      return;
    }
    await load();
  } catch (err) {
    alert(err.message);
  }
});

$("refresh-btn").addEventListener("click", () => load());

async function load() {
  try {
    const data = await BeeReports.list();
    state.reports = data.reports;
    render();
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
