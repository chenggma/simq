/* simq UI — vanilla JS, no build step. */
const $ = (s) => document.querySelector(s);
const api = (p, opts) => fetch(p, opts).then(async (r) => {
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.headers.get("content-type")?.includes("json") ? r.json() : r.text();
});

let workloads = [];
let selectedJob = null;

// ---- submit panel ----------------------------------------------------
async function initWorkloads() {
  workloads = await api("/api/workloads");
  const sel = $("#wl-select");
  sel.innerHTML = workloads.map((w) => `<option value="${w.name}">${w.name}</option>`).join("");
  sel.onchange = () => {
    const w = workloads.find((x) => x.name === sel.value);
    $("#wl-desc").textContent = w.description;
    $("#params").value = JSON.stringify(w.example_params, null, 2);
  };
  sel.onchange();
}

$("#submit-btn").onclick = async () => {
  const msg = $("#submit-msg");
  msg.textContent = "…";
  try {
    const body = {
      type: $("#wl-select").value,
      params: JSON.parse($("#params").value || "{}"),
      priority: parseInt($("#priority").value || "0", 10),
    };
    const t = $("#timeout").value;
    if (t) body.timeout_s = parseInt(t, 10);
    const job = await api("/api/jobs", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    msg.textContent = `submitted ${job.id.slice(0, 8)}`;
    refresh();
  } catch (e) { msg.textContent = `error: ${e.message}`; }
};

// ---- jobs table ------------------------------------------------------
const fmtAge = (iso) => {
  const s = (Date.now() - new Date(iso)) / 1000;
  if (s < 90) return `${s.toFixed(0)}s ago`;
  if (s < 5400) return `${(s / 60).toFixed(0)}m ago`;
  return `${(s / 3600).toFixed(1)}h ago`;
};
const fmtTook = (j) => {
  if (!j.started_at) return "";
  const end = j.finished_at ? new Date(j.finished_at) : new Date();
  const s = (end - new Date(j.started_at)) / 1000;
  return s < 120 ? `${s.toFixed(1)}s` : `${(s / 60).toFixed(1)}m`;
};

async function refresh() {
  const [jobs, stats] = await Promise.all([api("/api/jobs?limit=100"), api("/api/stats")]);
  $("#stats").innerHTML = ["queued", "running", "succeeded", "failed", "cancelled"]
    .map((s) => `<span class="chip">${s}&nbsp;<b>${stats.by_state[s] || 0}</b></span>`).join("");
  const tb = $("#jobs tbody");
  tb.innerHTML = jobs.map((j) => {
    const p = j.progress && j.progress.total
      ? `<div class="bar"><div style="width:${(100 * j.progress.done / j.progress.total).toFixed(0)}%"></div></div>`
      : "";
    const cancel = (j.state === "queued" || j.state === "running")
      ? `<button class="small" data-cancel="${j.id}">cancel</button>` : "";
    return `<tr data-id="${j.id}">
      <td class="mono">${j.id.slice(0, 8)}</td><td>${j.type}</td>
      <td><span class="badge ${j.state}">${j.state}</span></td>
      <td>${j.attempts}/${j.max_attempts}</td><td>${p}</td>
      <td>${fmtAge(j.created_at)}</td><td>${fmtTook(j)}</td><td>${cancel}</td></tr>`;
  }).join("");
  // (row handlers are delegated to <tbody> once, in boot — auto-refresh
  // replaces the rows every 2s, so per-row listeners would be lost and
  // clicks landing mid-rerender would vanish)
  const done = jobs.filter((j) => j.state === "succeeded");
  for (const sel of [$("#cmp-a"), $("#cmp-b")]) {
    const cur = sel.value;
    sel.innerHTML = done.map((j) =>
      `<option value="${j.id}">${j.type} ${j.id.slice(0, 8)} (${fmtAge(j.created_at)})</option>`).join("");
    if ([...sel.options].some((o) => o.value === cur)) sel.value = cur;
  }
  if (selectedJob) showDetail(selectedJob, false);
}

// ---- detail ----------------------------------------------------------
async function showDetail(id, scroll = true) {
  selectedJob = id;
  const j = await api(`/api/jobs/${id}`);
  $("#detail").hidden = false;
  $("#d-id").textContent = id.slice(0, 8);
  $("#d-body").innerHTML = `<dl class="kv">
    <dt>type</dt><dd>${j.type}</dd>
    <dt>state</dt><dd><span class="badge ${j.state}">${j.state}</span></dd>
    <dt>params</dt><dd>${JSON.stringify(j.params)}</dd>
    <dt>attempts</dt><dd>${j.attempts}/${j.max_attempts}</dd>
    <dt>worker</dt><dd>${j.worker_id || "—"}</dd>
    <dt>error</dt><dd>${j.error ? j.error.split("\n")[0] : "—"}</dd></dl>`;
  $("#d-events").innerHTML = `<dl class="kv">` + j.events.map((e) =>
    `<dt>${new Date(e.at).toLocaleTimeString()}</dt>
     <dd>${e.event}${e.worker_id ? " @ " + e.worker_id : ""}${e.detail ? " " + JSON.stringify(e.detail) : ""}</dd>`
  ).join("") + `</dl>`;
  $("#d-log").textContent = await api(`/api/jobs/${id}/log?tail_bytes=8000`).catch(() => "(no log yet)");
  $("#d-result").textContent = j.result ? JSON.stringify(j.result, null, 2).slice(0, 20000) : "(no result)";
  if (scroll) $("#detail").scrollIntoView({ behavior: "smooth", block: "nearest" });
}
$("#d-close").onclick = () => { $("#detail").hidden = true; selectedJob = null; };

// ---- compare ---------------------------------------------------------
const flat = (obj, prefix = "") => Object.entries(obj || {}).reduce((acc, [k, v]) => {
  if (typeof v === "number") acc[prefix + k] = v;
  else if (v && typeof v === "object" && !Array.isArray(v)) Object.assign(acc, flat(v, `${prefix}${k}.`));
  return acc;
}, {});

$("#cmp-btn").onclick = async () => {
  const [a, b] = await Promise.all([api(`/api/jobs/${$("#cmp-a").value}`), api(`/api/jobs/${$("#cmp-b").value}`)]);
  const fa = flat(a.result), fb = flat(b.result);
  const keys = [...new Set([...Object.keys(fa), ...Object.keys(fb)])];
  $("#cmp-out").innerHTML = `<table><thead><tr>
    <th>metric</th><th>A ${a.id.slice(0, 8)}</th><th>B ${b.id.slice(0, 8)}</th><th>Δ (B−A)</th><th>Δ%</th>
    </tr></thead><tbody>` + keys.map((k) => {
      const va = fa[k], vb = fb[k];
      const d = (va != null && vb != null) ? vb - va : null;
      const pct = d != null && va ? (100 * d / Math.abs(va)) : null;
      const cls = d > 0 ? "delta-pos" : d < 0 ? "delta-neg" : "";
      const f = (x) => x == null ? "—" : (Math.abs(x) >= 1000 ? x.toFixed(0) : x.toPrecision(4));
      return `<tr><td class="mono">${k}</td><td>${f(va)}</td><td>${f(vb)}</td>
        <td class="${cls}">${f(d)}</td><td class="${cls}">${pct == null ? "—" : pct.toFixed(1) + "%"}</td></tr>`;
    }).join("") + "</tbody></table>";
};

// ---- boot ------------------------------------------------------------
document.querySelector("#jobs tbody").addEventListener("click", (e) => {
  const cancelId = e.target.dataset && e.target.dataset.cancel;
  if (cancelId) {
    api(`/api/jobs/${cancelId}/cancel`, { method: "POST" }).then(refresh);
    return;
  }
  const tr = e.target.closest("tr[data-id]");
  if (tr) showDetail(tr.dataset.id);
});
initWorkloads().then(refresh);
setInterval(() => { if ($("#autorefresh").checked) refresh().catch(() => {}); }, 2000);
