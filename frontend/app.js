// PDFT frontend — no build step.
// The viewer shows the ORIGINAL pages with the translation typeset in (server-rendered PNG of composed.pdf)
// with transparent hit boxes over every piece of text. Clicking one opens the 4-control panel; a change is saved
// and the server typesets the affected page again, then the pages reload.
// Context / glossary live only on the server: the UI receives token counts (the gauge) and nothing else.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const LANGS = [
  ["Korean", "한국어"], ["English", "영어"], ["Japanese", "일본어"], ["Chinese (Simplified)", "중국어 (간체)"],
  ["Chinese (Traditional)", "중국어 (번체)"], ["Spanish", "스페인어"], ["French", "프랑스어"], ["German", "독일어"],
  ["Russian", "러시아어"], ["Portuguese", "포르투갈어"], ["Vietnamese", "베트남어"], ["Indonesian", "인도네시아어"],
  ["Thai", "태국어"],
];

const S = {
  docling: { available: false }, models: [], modelErrors: [], fonts: [], settings: {},
  job: null, pages: [], ver: 0, specKey: "", zoom: 1, fitMode: true, gauge: null, limitInfo: null,
  sel: null, pollTimer: null, busy: false, waitCompose: false, shownError: null,
  picked: new Set(), lastPick: null, curPage: 0, pstate: [],   // page list: pages the work is scoped to
};

// ------------------------------------------------------------------ helpers
async function api(path, { method = "GET", json, body } = {}) {
  const init = { method, headers: {} };
  if (json !== undefined) { init.body = JSON.stringify(json); init.headers["Content-Type"] = "application/json"; }
  if (body) init.body = body;
  const r = await fetch(path, init);
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch { /* keep */ }
    throw new Error(msg);
  }
  return r.headers.get("content-type")?.includes("json") ? r.json() : r;
}

let toastTimer;
function toast(msg, bad = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = bad ? "bad" : ""; t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), bad ? 7000 : 3500);
}
const guard = (fn) => async (...a) => { try { return await fn(...a); } catch (e) { toast(e.message || String(e), true); } };

function fmtTok(n) {
  if (n >= 1e6) return (n / 1e6).toFixed(n % 1e6 ? 2 : 0).replace(/\.?0+$/, "") + "M";
  if (n >= 1e4) return Math.round(n / 1e3) + "k";
  if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, "") + "k";
  return String(n);
}
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const familyInfo = (name) => S.fonts.find((f) => f.family === name);
const defaultFont = () => S.settings.default_font;
const model = () => S.models.find((m) => m.id === S.settings.model_id);

// ------------------------------------------------------------------ boot
async function init() {
  const boot = await api("/api/bootstrap");
  S.models = boot.models; S.modelErrors = boot.errors; S.fonts = boot.fonts; S.settings = boot.settings; S.docling = boot.docling || { available: false };
  buildLangSelect(); renderModels(); renderFonts(); applyDefaultFont(); renderSettingsUI();
  await refreshLimit();
  bindUI();
  const last = store.get("pdft.job");
  if (last) { try { await openJob(last); } catch { store.set("pdft.job", ""); } }
  renderControls();
}

// ------------------------------------------------------------------ menus / toolbar
function bindUI() {
  $$(".menu").forEach((m) => {
    $(".menu-btn", m).addEventListener("click", (e) => {
      e.stopPropagation();
      const open = m.classList.contains("open");
      closeMenus();
      if (!open) { m.classList.add("open"); if (m.dataset.menu === "file") loadRecent(); }
    });
    $(".dropdown", m).addEventListener("click", (e) => e.stopPropagation());
  });
  document.addEventListener("click", closeMenus);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeMenus(); deselect(); } });

  const pick = () => $("#file-input").click();
  $("#m-open").onclick = () => { closeMenus(); pick(); };
  $("#btn-pick").onclick = pick;
  $("#file-input").onchange = (e) => { const f = e.target.files[0]; e.target.value = ""; if (f) upload(f); };
  for (const ev of ["dragenter", "dragover"]) document.addEventListener(ev, (e) => { e.preventDefault(); document.body.classList.add("dragging"); });
  for (const ev of ["dragleave", "drop"]) document.addEventListener(ev, (e) => { e.preventDefault(); document.body.classList.remove("dragging"); });
  document.addEventListener("drop", (e) => { const f = e.dataTransfer?.files?.[0]; if (f) upload(f); });

  $("#btn-analyze").onclick = guard(startAnalyze);
  $("#s-parser").onchange = guard(async (e) => { await saveSettings({ parser_hints: e.target.checked }); updateEstimate(); });
  window.addEventListener("focus", () => refreshDocling());
  $("#btn-main").onclick = guard(mainAction);
  $("#m-start").onclick = guard(() => { closeMenus(); return startTranslate(); });
  $("#m-stop").onclick = guard(() => { closeMenus(); return stopTranslate(); });
  $("#m-reset").onclick = guard(async () => {
    closeMenus();
    const partial = S.job && S.picked.size < nPages();
    if (!S.job || !confirm(partial ? `선택한 ${S.picked.size}쪽의 번역 결과를 지웁니다. (다른 쪽과 분석 결과·서식은 유지) 계속할까요?`
      : "번역 결과와 내부 컨텍스트를 지우고 처음부터 다시 번역할 수 있게 합니다. (분석 결과와 서식은 유지) 계속할까요?")) return;
    deselect();
    await pushSelection();
    await api(`/api/jobs/${S.job.id}/reset`, { method: "POST" });
    S.waitCompose = true; await poll();
  });
  $("#m-rebuild").onclick = guard(async () => {
    closeMenus();
    if (!S.job?.analysis) return;
    if (!confirm("문서를 다시 분석합니다 (비용 없음).\n번역문은 원문이 같은 글에 한해 그대로 옮겨지고, 글 묶음이 바뀐 곳만 다시 번역해야 합니다.")) return;
    deselect();
    const r = await api(`/api/jobs/${S.job.id}/rebuild`, { method: "POST" });
    toast(`구조를 다시 정리했습니다 — 번역 ${r.carried.toLocaleString()}개 유지, 새로 번역할 글 ${r.remaining.toLocaleString()}개`);
    S.waitCompose = true; markStale(true); await poll();
  });
  $("#m-delete").onclick = guard(async () => {
    closeMenus();
    if (!S.job || !confirm(`'${S.job.filename}' 작업을 삭제할까요?`)) return;
    await deleteJob(S.job.id);
  });
  $("#btn-export").onclick = $("#m-export").onclick = guard(() => { closeMenus(); return exportPdf(false); });
  $("#m-export-sel").onclick = guard(() => { closeMenus(); return exportPdf(true); });
  $("#sb-all").onclick = selectAll; $("#sb-none").onclick = selectNone;
  $("#sb-revert").onclick = guard(revertSelected);
  $("#thumbs").addEventListener("click", onThumbClick);
  $("#viewer").addEventListener("scroll", () => { if (!S.curRaf) S.curRaf = requestAnimationFrame(() => { S.curRaf = 0; updateCurrent(); }); }, { passive: true });
  if (store.get("pdft.sidebar") === "off") document.body.classList.add("sb-off");
  $("#btn-sidebar").setAttribute("aria-pressed", String(!document.body.classList.contains("sb-off")));
  $("#btn-sidebar").onclick = () => {
    const off = document.body.classList.toggle("sb-off");
    store.set("pdft.sidebar", off ? "off" : "on"); $("#btn-sidebar").setAttribute("aria-pressed", String(!off));
    if (S.fitMode) applyZoom();
  };

  $("#s-lang").onchange = guard((e) => saveSettings({ target_lang: e.target.value }));
  $("#s-auto").onchange = guard(async (e) => { await saveSettings({ auto_compress: e.target.checked }); await refreshGauge(); });
  const savePct = debounce(guard(async (v) => { await saveSettings({ compress_pct: v }); await refreshGauge(); }), 300);
  const saveConc = debounce(guard((v) => saveSettings({ concurrency: v })), 300);
  $("#s-conc").oninput = (e) => { $("#s-conc-out").textContent = e.target.value; saveConc(+e.target.value); };
  $("#s-pct").oninput = (e) => { $("#s-pct-out").textContent = e.target.value + "%"; savePct(+e.target.value); if (S.gauge) renderGauge({ ...S.gauge, trigger: Math.round(Math.min(S.gauge.limit * e.target.value / 100, S.gauge.cap ?? Infinity)) }); };
  $("#ctx-refresh").onclick = guard(async () => { await refreshLimit(true); toast("모델 컨텍스트 한도를 다시 조회했습니다."); });
  $("#ctx-compress").onclick = guard(async () => {
    if (!S.job) return toast("먼저 PDF를 여세요.", true);
    const r = await api(`/api/jobs/${S.job.id}/compress?model_id=${encodeURIComponent(S.settings.model_id)}`, { method: "POST" });
    toast(r.compressed ? "컨텍스트를 압축했습니다." : "압축할 내용이 아직 없습니다.");
    await refreshGauge();
  });
  $("#models-reload").onclick = guard(async () => {
    const r = await api("/api/models/reload", { method: "POST" });
    S.models = r.models; S.modelErrors = r.errors;
    if (!model() && S.models[0]) await saveSettings({ model_id: S.models[0].id });
    renderModels(); await refreshLimit(true); toast("models.json을 다시 읽었습니다.");
  });
  $("#gauge").onclick = (e) => { e.stopPropagation(); closeMenus(); $('[data-menu="model"]').classList.add("open"); };

  $("#z-in").onclick = () => setZoom(S.zoom * 1.15);
  $("#z-out").onclick = () => setZoom(S.zoom / 1.15);
  $("#z-fit").onclick = () => { S.fitMode = true; applyZoom(); };
  window.addEventListener("resize", debounce(() => { if (S.fitMode) applyZoom(); positionPanel(); }, 120));
  $("#viewer").addEventListener("scroll", () => requestAnimationFrame(positionPanel), { passive: true });

  bindPanel();
  document.addEventListener("pointerdown", (e) => { if (!e.target.closest(".hit, #panel")) deselect(); });
}
function closeMenus() { $$(".menu.open").forEach((m) => m.classList.remove("open")); }

function buildLangSelect() {
  $("#s-lang").innerHTML = LANGS.map(([v, l]) => `<option value="${v}">${l} (${v})</option>`).join("");
  $("#s-lang").value = S.settings.target_lang;
}

const scanPages = () => S.job?.scan_pages || 0;
const usesParser = () => !!S.job && (scanPages() > 0 || !!S.settings.parser_hints);
async function refreshDocling() {
  try { S.docling = await api("/api/docling"); } catch { S.docling = { available: false }; }
  updateEstimate();
}
function updateEstimate() {
  const d = S.docling, dev = d.available ? ` (${d.device || "?"})` : " — 서비스 꺼짐";
  let txt;
  if (!S.job) txt = "텍스트 층이 있으면 PDF만으로 분석";
  else if (scanPages() > 0) txt = `스캔 ${scanPages()}쪽 → 로컬 Docling${dev}`;
  else if (S.settings.parser_hints) txt = `Docling 구역 힌트${dev}`;
  else txt = "PDF 텍스트 층만 사용";
  $("#an-est").textContent = txt;
  $("#an-est").classList.toggle("bad", !!S.job && usesParser() && !d.available);
}

function renderSettingsUI() {
  $("#s-auto").checked = !!S.settings.auto_compress;
  $("#s-pct").value = S.settings.compress_pct; $("#s-pct-out").textContent = S.settings.compress_pct + "%";
  $("#s-conc").value = S.settings.concurrency ?? 4; $("#s-conc-out").textContent = S.settings.concurrency ?? 4;
  $("#s-lang").value = S.settings.target_lang;
  if ($("#s-parser")) $("#s-parser").checked = scanPages() > 0 || !!S.settings.parser_hints;
}

async function saveSettings(patch) {
  S.settings = await api("/api/settings", { method: "PUT", json: patch });
  renderSettingsUI();
}

function renderModels() {
  const list = $("#model-list");
  list.replaceChildren(...S.models.map((m) => {
    const row = document.createElement("label");
    row.className = "model-opt";
    row.innerHTML = `<input type="radio" name="model" value="${m.id}"><div><div class="t"></div><div class="s"></div></div><span class="badge ${m.has_key ? "" : "bad"}">${m.has_key ? m.type : "API 키 없음"}</span>`;
    $(".t", row).textContent = m.name;
    $(".s", row).textContent = `${m.model} · ${m.host}`;
    const radio = $("input", row);
    radio.checked = m.id === S.settings.model_id;
    radio.onchange = guard(async () => { await saveSettings({ model_id: m.id }); renderModelName(); await refreshLimit(); });
    return row;
  }));
  if (!S.models.length) list.innerHTML = '<div class="warn">models.json에 모델이 없습니다.</div>';
  const err = $("#model-errors");
  err.hidden = !S.modelErrors.length; err.textContent = S.modelErrors.join("\n");
  renderModelName();
}
function renderModelName() { $("#model-name").textContent = model()?.name.split("·").pop().trim() || ""; }

function renderFonts() {
  const list = $("#font-list");
  list.replaceChildren(...S.fonts.map((f) => {
    const b = document.createElement("button");
    b.className = "font-opt" + (f.family === defaultFont() ? " on" : "");
    b.style.fontFamily = `"${f.family}", var(--ui)`;
    b.innerHTML = `<span>${f.family} — 가나다 ABC 123</span><small>${f.variable ? "가변" : f.weights.length + "굵기"}</small>`;
    b.onclick = guard(async () => {
      await saveSettings({ default_font: f.family }); applyDefaultFont(); renderFonts();
      if (S.job?.analysis) { S.waitCompose = true; markStale(true); poll(); }
      if (S.sel) fillPanel();
    });
    return b;
  }));
  $("#font-name").textContent = defaultFont() || "";
}
function applyDefaultFont() {
  const f = defaultFont();
  document.documentElement.style.setProperty("--default-font", `${f ? `"${f}", ` : ""}"Noto Sans KR", "Noto Sans", sans-serif`);
}

const STATUS_KO = { uploaded: "미분석", analyzing: "분석중", ready: "준비됨", translating: "번역중", paused: "중지", done: "완료", error: "오류" };

async function loadRecent() {
  const { jobs } = await api("/api/jobs");
  const box = $("#recent");
  const fmtDate = (t) => { const d = new Date(t * 1000); return `${d.getMonth() + 1}/${d.getDate()}`; };
  const rows = jobs.map((j) => {
    const row = document.createElement("div");
    row.className = "item recent-row" + (S.job?.id === j.id ? " current" : "");
    row.innerHTML = '<span class="name"></span><span class="meta"></span><button class="x" title="이 작업 삭제" aria-label="삭제">✕</button>';
    $(".name", row).textContent = j.filename;
    $(".name", row).title = j.filename;
    $(".meta", row).textContent = `${j.pages}쪽 · ${fmtDate(j.created)} · ${STATUS_KO[j.status] || j.status}`;
    const x = $(".x", row);
    x.disabled = j.status === "analyzing";
    if (x.disabled) x.title = "분석 중에는 삭제할 수 없습니다";
    x.onclick = guard(async (e) => {
      e.stopPropagation();
      if (!confirm(`'${j.filename}' 작업을 삭제할까요?\n업로드한 PDF, 분석 결과, 번역이 모두 지워지며 되돌릴 수 없습니다.`)) return;
      await deleteJob(j.id);
      toast("작업을 삭제했습니다.");
      await loadRecent();
    });
    row.onclick = guard(() => { closeMenus(); return openJob(j.id); });
    return row;
  });
  box.replaceChildren(...rows);
  if (!jobs.length) box.innerHTML = '<div class="muted small" style="padding:4px 10px">없음</div>';
  const others = jobs.filter((j) => j.id !== S.job?.id && j.status !== "analyzing");
  const bulk = $("#m-delete-others");
  bulk.hidden = !others.length;
  bulk.textContent = `다른 작업 ${others.length}개 모두 삭제`;
  bulk.onclick = guard(async () => {
    if (!confirm(`현재 열려 있는 작업을 뺀 ${others.length}개 작업을 모두 삭제할까요?\n업로드한 PDF, 분석 결과, 번역이 모두 지워지며 되돌릴 수 없습니다.`)) return;
    let n = 0;
    for (const j of others) { try { await deleteJob(j.id); n++; } catch (e) { toast(e.message, true); } }
    toast(`${n}개 작업을 삭제했습니다.`);
    await loadRecent();
  });
}

async function deleteJob(id) {
  await api(`/api/jobs/${id}`, { method: "DELETE" });
  if (S.job?.id === id) closeJob();
}

// ------------------------------------------------------------------ gauge
async function refreshLimit(refresh = false) {
  const m = model();
  if (!m) return;
  S.limitInfo = await api(`/api/models/${encodeURIComponent(m.id)}/context${refresh ? "?refresh=1" : ""}`);
  await refreshGauge();
}
async function refreshGauge() {
  if (S.job) {
    const q = new URLSearchParams({ model_id: S.settings.model_id, pct: S.settings.compress_pct, lang: S.settings.target_lang });
    S.gauge = (await api(`/api/jobs/${S.job.id}/status?${q}`)).gauge;
  } else if (S.limitInfo) {
    S.gauge = { used: 0, limit: S.limitInfo.limit, limit_source: S.limitInfo.source, trigger: Math.round(Math.min(S.limitInfo.limit * S.settings.compress_pct / 100, S.limitInfo.cap ?? Infinity)), cap: S.limitInfo.cap, compressions: 0 };
  }
  renderGauge(S.gauge);
}
const SOURCE_LABEL = { config: "models.json 설정값", api: "모델 API 조회", builtin: "내장 표 기준 추정", default: "조회 불가 → 임시 8,192 (models.json에 context_window를 지정하세요)" };
function renderGauge(g) {
  if (!g) return;
  const pct = Math.min(100, (g.used / g.limit) * 100);
  const level = g.used >= g.limit * 0.9 ? "bad" : g.used >= g.trigger ? "warn" : "ok";
  const markPct = Math.min(100, (g.trigger / g.limit) * 100);
  $(".g-fill").style.width = pct + "%"; $(".g-mark").style.left = markPct + "%";
  $("#gauge").dataset.level = level; $(".g-num").textContent = `${fmtTok(g.used)} / ${fmtTok(g.limit)}`;
  $("#gauge").title = `컨텍스트 사용량 ${g.used.toLocaleString()} / ${g.limit.toLocaleString()} tokens (${pct.toFixed(1)}%)`;
  const big = $("#gauge-big");
  big.dataset.level = level; $("i", big).style.width = pct + "%"; $("b", big).style.left = markPct + "%";
  $("#ctx-limit").textContent = g.limit.toLocaleString() + " tokens";
  $("#ctx-source").textContent = SOURCE_LABEL[g.limit_source] || "";
  $("#gauge-detail").textContent = `사용 ${g.used.toLocaleString()} (${pct.toFixed(1)}%) · 압축 시작 ${g.trigger.toLocaleString()}${g.cap && g.trigger >= g.cap ? " (비용 보호 상한)" : ""} · 압축 ${g.compressions || 0}회`;
}

// ------------------------------------------------------------------ jobs
async function upload(file) {
  if (!/\.pdf$/i.test(file.name) && file.type !== "application/pdf") return toast("PDF 파일만 열 수 있습니다.", true);
  try {
    setStatus("업로드 중…", { busy: true });
    const fd = new FormData(); fd.append("file", file);
    const job = await api("/api/jobs", { method: "POST", body: fd });
    await openJob(job.id);
  } catch (e) { toast(e.message, true); setStatus("업로드 실패", { bad: true }); }
}

async function openJob(id) {
  clearTimeout(S.pollTimer);
  deselect();
  const job = await api(`/api/jobs/${id}`);
  S.job = job; S.shownError = null; store.set("pdft.job", id);
  S.ver = job.compose?.version || 0;
  document.body.classList.add("has-doc");
  S.picked = new Set(job.selected_pages ?? Array.from({ length: job.pages.length }, (_, i) => i)); S.lastPick = null; S.curPage = 0; S.pstate = [];
  buildPages(); buildThumbs();
  updateEstimate(); renderProgress(); renderControls();
  await refreshGauge();
  poll();
}

function closeJob() {
  clearTimeout(S.pollTimer);
  S.job = null; S.pages = []; $("#pages").replaceChildren(); $("#thumbs").replaceChildren(); S.picked = new Set(); store.set("pdft.job", "");
  document.body.classList.remove("has-doc");
  renderControls(); updateEstimate(); setStatus("PDF를 열어 시작하세요."); refreshGauge().catch(() => {});
}

function setStatus(msg, { busy = false, bad = false, pct = null, right = "", rightTitle = "" } = {}) {
  const m = $("#st-msg"); m.textContent = msg; m.classList.toggle("bad", bad);
  const bar = $(".st-bar"); bar.classList.toggle("busy", busy);
  $("#st-fill").style.width = busy && pct == null ? "100%" : (pct ?? 0) + "%";
  const r = $("#st-right"); r.textContent = right; r.title = rightTitle;
}

async function poll() {
  clearTimeout(S.pollTimer);
  if (!S.job) return;
  const id = S.job.id;
  try {
    const q = new URLSearchParams({ model_id: S.settings.model_id, pct: S.settings.compress_pct, lang: S.settings.target_lang });
    const st = await api(`/api/jobs/${id}/status?${q}`);
    if (!S.job || S.job.id !== id) return;
    S.gauge = st.gauge; renderGauge(st.gauge);
    Object.assign(S.job, { status: st.status, error: st.error, progress: st.progress, analysis: st.analysis, compose: st.compose, usage: st.usage });
    if (st.page_state) { S.pstate = st.page_state; refreshBadges(); }
    if (st.compose && st.compose.version !== S.ver) onComposeVersion(st.compose.version);
    const composing = st.compose && (st.compose.busy || st.compose.pending);
    if (!composing && S.waitCompose) { S.waitCompose = false; markStale(false); }
    if (st.error && st.error !== S.shownError && !["analyzing", "translating"].includes(st.status)) { S.shownError = st.error; toast(st.error, true); }
    renderProgress(); renderControls();
    if (["analyzing", "translating"].includes(st.status) || composing || S.waitCompose) S.pollTimer = setTimeout(poll, 800);
  } catch (e) { S.pollTimer = setTimeout(poll, 3000); }
}

function renderProgress() {
  const j = S.job;
  if (!j) return;
  const p = j.progress || {}, a = j.analysis;
  const c = j.compose;
  const composing = c && (c.busy || c.pending);
  const pct = p.total ? (p.done / p.total) * 100 : 0;
  let right = "";
  const bits = [];
  if (a) { bits.push(a.parser ? "Docling(로컬)" : "텍스트 층 분석"); }
  if (p.total && ["translating", "paused", "done"].includes(j.status)) bits.unshift(`${p.done}/${p.total} 개`);
  if (S.job && S.picked.size < nPages() && j.analysis) bits.unshift(`선택 ${S.picked.size}/${nPages()}쪽`);
  if (j.usage?.requests) bits.push(`LLM 입력 ${fmtTok(j.usage.input)}${j.usage.cached ? ` (캐시 ${Math.round(j.usage.cached / j.usage.input * 100)}%)` : ""} · 출력 ${fmtTok(j.usage.output)} tok · ${j.usage.requests}회`);
  if (a?.warnings?.length) bits.push(`분석 경고 ${a.warnings.length}`);
  if (c?.fit && (c.fit.scaled || c.fit.failed)) bits.push(`글 ${c.fit.scaled}개 축소${c.fit.failed ? ` · ${c.fit.failed}개 넘침` : ""}`);
  if (composing && j.status !== "analyzing") bits.push("레이아웃 갱신 중…");
  right = bits.join(" · ");
  const title = a?.warnings?.join("\n") || "";
  if (j.status === "uploaded") setStatus(j.error ? `분석 실패 — ${j.error}` : `${j.filename} — '문서 분석'을 누르세요 (텍스트 층이 있으면 무료).`, { bad: !!j.error, right });
  else if (j.status === "analyzing") setStatus(p.message || "문서 분석 중", { busy: true, right });
  else if (j.status === "translating") setStatus(p.message || "번역 중", { busy: true, pct, right: right + (p.warnings ? ` · 누락 ${p.warnings}` : ""), rightTitle: title });
  else if (j.status === "error") setStatus(j.error || "오류가 발생했습니다.", { bad: true, pct, right, rightTitle: title });
  else if (j.status === "done") setStatus("번역 완료 — 글을 클릭해 글꼴·굵기·크기·색을 조정하세요.", { pct: 100, right, rightTitle: title });
  else if (j.status === "paused") setStatus("일시 중지됨 — '이어서 번역'으로 계속할 수 있습니다.", { pct, right, rightTitle: title });
  else setStatus(j.error ? `분석 실패 — ${j.error}` : `분석 완료 — 글 ${a?.blocks ?? "?"}개. 번역을 시작할 수 있습니다.`, { bad: !!j.error, pct, right, rightTitle: title });
  if (!["analyzing", "translating"].includes(j.status)) $(".st-bar").classList.remove("busy");
}

function renderControls() {
  const j = S.job, st = j?.status;
  const analyzing = st === "analyzing", translating = st === "translating";
  const hasDoc = !!j?.analysis;
  const a = $("#btn-analyze");
  a.disabled = !j || analyzing || translating;
  a.textContent = analyzing ? "분석 중…" : hasDoc ? "다시 분석" : "문서 분석";
  a.classList.toggle("attn", !!j && !hasDoc && !analyzing);
  const main = $("#btn-main");
  const none = !!j && S.picked.size === 0, part = j && S.picked.size < nPages() ? ` (${S.picked.size}쪽)` : "";
  main.disabled = !hasDoc || analyzing || st === "done" || none;
  main.title = none ? "번역할 쪽을 선택하세요 (왼쪽 쪽 목록)" : "";
  main.classList.toggle("stop", translating);
  main.textContent = translating ? "중지" : st === "done" ? "번역 완료" + part : st === "paused" ? "이어서 번역" + part : "번역 시작" + part;
  const canExport = !!hasDoc && (j.compose?.version || 0) > 0 && !analyzing;
  $("#btn-export").disabled = !canExport; $("#m-export").disabled = !canExport;
  $("#m-export-sel").disabled = !canExport || none || allPicked();
  $("#m-start").disabled = !hasDoc || analyzing || translating || st === "done" || none;
  $("#m-stop").disabled = !translating;
  $("#m-reset").disabled = !hasDoc || analyzing || translating || none;
  $("#m-rebuild").disabled = !hasDoc || analyzing || translating;
  $("#m-delete").disabled = !j;
  $("#ctx-compress").disabled = !hasDoc || translating;
  $("#s-parser").disabled = !j || analyzing || translating || scanPages() > 0;
  $("#s-parser").checked = usesParser();
  document.body.classList.toggle("busy-job", analyzing || translating);
  $("#sb-revert").disabled = !hasDoc || analyzing || translating || none;
}

const mainAction = () => (S.job?.status === "translating" ? stopTranslate() : startTranslate());

async function startAnalyze() {
  if (!S.job) return;
  await refreshDocling();
  if (scanPages() > 0 && !S.docling.available) throw new Error("스캔 쪽을 읽으려면 로컬 Docling 서비스가 필요합니다. 맥에서 docling_host/run.sh 를 실행하세요.");
  if (S.job.analysis && !confirm("다시 분석하면 지금까지의 번역과 내부 컨텍스트가 모두 초기화됩니다. 계속할까요?")) return;
  deselect();
  await api(`/api/jobs/${S.job.id}/analyze`, { method: "POST", json: { hints: !!S.settings.parser_hints } });
  S.job.status = "analyzing"; S.job.error = null; S.shownError = null;
  renderProgress(); renderControls(); poll();
}

async function startTranslate() {
  if (!S.job) return;
  const m = model();
  if (!m) throw new Error("모델을 선택하세요.");
  if (!m.has_key) throw new Error(`'${m.name}'에 API 키가 없습니다. config/models.json 또는 .env를 확인하세요.`);
  await pushSelection();
  await api(`/api/jobs/${S.job.id}/translate`, {
    method: "POST",
    json: { model_id: m.id, target_lang: S.settings.target_lang, auto_compress: S.settings.auto_compress, compress_pct: S.settings.compress_pct, concurrency: S.settings.concurrency ?? 4 },
  });
  S.job.status = "translating"; renderControls(); poll();
}
async function stopTranslate() {
  await api(`/api/jobs/${S.job.id}/cancel`, { method: "POST" });
  await poll(); renderControls();
}

async function exportPdf(selectedOnly = false) {
  if (!S.job || S.busy) return;
  if (selectedOnly) await pushSelection();
  S.busy = true; $("#btn-export").disabled = true;
  const prev = $("#st-msg").textContent;
  setStatus("번역된 PDF를 만드는 중…", { busy: true });
  try {
    const r = await fetch(`/api/jobs/${S.job.id}/export${selectedOnly ? "?selected=true" : ""}`);
    if (!r.ok) { let m = r.statusText; try { m = (await r.json()).detail; } catch { /* */ } throw new Error(m); }
    const blob = await r.blob();
    const cd = r.headers.get("content-disposition") || "";
    const name = decodeURIComponent((cd.match(/filename\*=utf-8''([^;]+)/i) || cd.match(/filename="?([^";]+)"?/i) || [])[1] || "translated.pdf");
    const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: name });
    document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 10000);
    toast("PDF를 저장했습니다.");
  } catch (e) { toast(e.message, true); }
  finally { S.busy = false; renderControls(); setStatus(prev); renderProgress(); }
}

// ------------------------------------------------------------------ viewer
const observer = new IntersectionObserver((entries) => {
  for (const en of entries) {
    const pg = S.pages[+en.target.dataset.i];
    if (!pg) continue;
    if (en.isIntersecting) loadPage(pg); else unloadPage(pg);
  }
}, { root: $("#viewer"), rootMargin: "900px 0px" });

function pageSpecs() {
  const c = S.job?.compose;
  if (c && c.version > 0 && c.pages) { const [w, h] = c.page_size; return Array.from({ length: c.pages }, () => ({ w, h })); }
  return (S.job?.pages || []).map((p) => ({ w: p.w, h: p.h }));
}
const specKey = (specs) => JSON.stringify([specs.length, specs[0]?.w, specs[0]?.h, S.job?.id]);

function buildPages() {
  observer.disconnect();
  const specs = pageSpecs();
  S.specKey = specKey(specs);
  const wrap = $("#pages");
  S.pages = specs.map((p, i) => ({ ...p, i, loaded: false, fetching: false }));
  wrap.replaceChildren(...S.pages.map((pg) => {
    const el = document.createElement("section");
    el.className = "page"; el.dataset.i = pg.i;
    el.innerHTML = `<div class="page-inner" style="width:${pg.w}px;height:${pg.h}px"><img class="bg" alt="" draggable="false"><div class="hits"></div></div><span class="page-no">${pg.i + 1}</span>`;
    pg.el = el; pg.img = $("img", el); pg.hitsEl = $(".hits", el); pg.inner = $(".page-inner", el);
    return el;
  }));
  applyZoom();
  S.pages.forEach((pg) => observer.observe(pg.el));
}

function onComposeVersion(v) {
  S.ver = v;
  refreshThumbImages();
  const specs = pageSpecs();
  if (specKey(specs) !== S.specKey) {
    const top = $("#viewer").scrollTop;
    buildPages();
    $("#viewer").scrollTop = top;
  } else {
    S.pages.forEach((pg) => { if (pg.loaded) loadPage(pg, true); });
  }
}
function markStale(on) { $$(".page").forEach((p) => p.classList.toggle("stale", on)); }

function bucket() {
  const need = S.zoom * Math.min(window.devicePixelRatio || 1, 1.5);
  return [1, 1.5, 2, 3, 4].find((x) => x >= need) ?? 4;
}

function applyZoom() {
  if (!S.pages.length) return;
  if (S.fitMode) {
    const maxW = Math.max(...S.pages.map((p) => p.w));
    S.zoom = Math.max(0.3, Math.min(1.8, ($("#viewer").clientWidth - 70) / maxW));
  }
  for (const pg of S.pages) {
    pg.el.style.width = pg.w * S.zoom + "px"; pg.el.style.height = pg.h * S.zoom + "px";
    pg.inner.style.transform = `scale(${S.zoom})`;
  }
  $("#z-fit").textContent = Math.round(S.zoom * 100) + "%";
  reloadImages(); positionPanel();
}
function setZoom(z) { S.fitMode = false; S.zoom = Math.max(0.25, Math.min(4, z)); applyZoom(); }
const reloadImages = debounce(() => S.pages.forEach((pg) => pg.loaded && setImage(pg)), 250);

function setImage(pg) {
  const url = `/api/jobs/${S.job.id}/pages/${pg.i}/image?scale=${bucket()}&v=${S.ver}`;
  if (pg.img.dataset.src !== url) { pg.img.dataset.src = url; pg.img.src = url; }
}
function loadPage(pg, force = false) {
  pg.loaded = true; setImage(pg);
  if (S.ver > 0 && (force || !pg.boxes) && !pg.fetching) {
    pg.fetching = true;
    const ver = S.ver;
    api(`/api/jobs/${S.job.id}/pages/${pg.i}/boxes`).then((r) => {
      if (ver !== S.ver) return;   // a newer layout arrived meanwhile
      pg.boxes = r.boxes; renderHits(pg);
    }).catch(() => {}).finally(() => (pg.fetching = false));
  }
  if (S.ver === 0) pg.hitsEl.replaceChildren();
}
function unloadPage(pg) {
  if (!pg.loaded || (S.sel && pg.hitsEl.querySelector(`.hit[data-id="${S.sel.id}"]`))) return;
  pg.loaded = false; pg.img.removeAttribute("src"); delete pg.img.dataset.src;
}

function renderHits(pg) {
  pg.hitsEl.replaceChildren(...pg.boxes.map((b) => {
    const el = document.createElement("div");
    el.className = "hit" + (S.sel?.id === b.id ? " sel" : ""); el.dataset.id = b.id; el.dataset.kind = b.kind;
    const [x0, y0, x1, y1] = b.rect;
    Object.assign(el.style, { left: x0 + "px", top: y0 + "px", width: Math.max(4, x1 - x0) + "px", height: Math.max(4, y1 - y0) + "px" });
    el.addEventListener("click", (e) => { e.stopPropagation(); select(b.id); });
    return el;
  }));
  if (S.sel) positionPanel();
}

// ------------------------------------------------------------------ page list (sidebar): select the pages to work on
const nPages = () => S.job?.pages.length || 0;
const allPicked = () => !!S.job && S.picked.size === nPages();
const jobBusy = () => ["analyzing", "translating"].includes(S.job?.status);
const thumbUrl = (i) => `/api/jobs/${S.job.id}/pages/${i}/image?scale=0.25&v=${S.ver}`;

const thumbObserver = new IntersectionObserver((entries) => {
  for (const en of entries) if (en.isIntersecting && S.job) setThumb(en.target);
}, { root: $("#thumbs"), rootMargin: "600px 0px" });

function setThumb(el) {
  const img = $("img", el), url = thumbUrl(+el.dataset.i);
  if (img.dataset.src !== url) { img.dataset.src = url; img.src = url; }
}
function buildThumbs() {
  thumbObserver.disconnect();
  const box = $("#thumbs");
  box.replaceChildren();
  if (!S.job) return;
  const els = S.job.pages.map((p, i) => {
    const el = document.createElement("div");
    el.className = "thumb"; el.dataset.i = i; el.setAttribute("role", "option");
    el.innerHTML = `<label class="tk" title="이 쪽 선택"><input type="checkbox" tabindex="-1"></label><i class="tb" data-s="0"></i>` +
      `<div class="timg" style="aspect-ratio:${p.w} / ${p.h}"><img alt="" draggable="false"></div><span class="tn">${i + 1}</span>`;
    return el;
  });
  box.append(...els);
  els.forEach((el) => thumbObserver.observe(el));
  refreshThumbs(); refreshBadges();
}
const refreshThumbImages = debounce(() => { $$(".thumb").forEach((el) => { if ($("img", el).dataset.src) setThumb(el); }); }, 1200);

function refreshThumbs() {
  if (!S.job) return;
  for (const el of $$(".thumb")) {
    const i = +el.dataset.i, on = S.picked.has(i);
    el.classList.toggle("on", on); el.classList.toggle("cur", i === S.curPage);
    el.setAttribute("aria-selected", String(on)); $("input", el).checked = on;
  }
  $("#sb-count").textContent = `선택 ${S.picked.size}/${nPages()}`;
  $("#sb-all").disabled = allPicked(); $("#sb-none").disabled = S.picked.size === 0;
}
function refreshBadges() {
  $$(".thumb .tb").forEach((b, i) => {
    const s = S.pstate[i] ?? 0;
    b.dataset.s = s === 2 ? "2" : s === 1 ? "1" : "0";
    b.textContent = s === 2 ? "✓" : s === 1 ? "…" : "";
    b.title = s === 2 ? "번역됨" : s === 1 ? "일부 번역됨" : "";
  });
}

function onThumbClick(e) {
  const th = e.target.closest(".thumb");
  if (!th) return;
  const i = +th.dataset.i;
  if (e.target.closest(".tk")) { e.preventDefault(); toggleThumb(i, e.shiftKey); return; }
  if (e.shiftKey) { toggleThumb(i, true); return; }
  if (e.metaKey || e.ctrlKey) { toggleThumb(i, false); return; }
  goToPage(i);
}
function toggleThumb(i, range) {
  if (!S.job || jobBusy()) return;
  const want = !S.picked.has(i);
  const [a, b] = range && S.lastPick != null ? [Math.min(S.lastPick, i), Math.max(S.lastPick, i)] : [i, i];
  for (let k = a; k <= b; k++) want ? S.picked.add(k) : S.picked.delete(k);
  S.lastPick = i;
  selectionChanged();
}
function selectAll() { if (!S.job || jobBusy()) return; S.picked = new Set(Array.from({ length: nPages() }, (_, i) => i)); selectionChanged(); }
function selectNone() { if (!S.job || jobBusy()) return; S.picked = new Set(); selectionChanged(); }
function selectionChanged() { refreshThumbs(); renderProgress(); renderControls(); saveSelection(); }

// send the selection to the server now (the server scopes translate / reset / export to it)
async function pushSelection() {
  const job = S.job;
  if (!job) return;
  const pages = allPicked() ? null : [...S.picked].sort((a, b) => a - b);
  const same = JSON.stringify(pages) === JSON.stringify(job.selected_pages ?? null);
  if (same) return;
  try {
    const r = await api(`/api/jobs/${job.id}/selection`, { method: "PUT", json: { pages } });
    if (S.job?.id !== job.id) return;
    Object.assign(S.job, { selected_pages: r.selected_pages, status: r.status, progress: r.progress });
    renderProgress(); renderControls();
  } catch (e) {            // the server refused (a job is running): show what it really has
    const j = await api(`/api/jobs/${job.id}`).catch(() => null);
    if (j && S.job?.id === job.id) {
      S.picked = new Set(j.selected_pages ?? Array.from({ length: j.pages.length }, (_, i) => i));
      refreshThumbs(); renderControls();
    }
    throw e;
  }
}
const saveSelection = debounce(guard(pushSelection), 250);

async function revertSelected() {
  if (!S.job || jobBusy() || !S.picked.size) return;
  const all = allPicked();
  if (!confirm(all ? "모든 쪽을 번역 전 상태(원본 글·서식)로 되돌립니다. 번역 결과와 서식 변경, 내부 컨텍스트가 지워집니다. 계속할까요?"
    : `선택한 ${S.picked.size}쪽을 번역 전 상태(원본 글·서식)로 되돌립니다. 다른 쪽은 그대로 유지됩니다. 계속할까요?`)) return;
  await pushSelection();
  deselect();
  await api(`/api/jobs/${S.job.id}/reset`, { method: "POST", json: { restore_styles: true } });
  toast(`${S.picked.size}쪽을 번역 전으로 되돌렸습니다.`);
  S.waitCompose = true; markStale(true); await poll();
}

function goToPage(i) {
  const pg = S.pages[i];
  if (pg) $("#viewer").scrollTo({ top: Math.max(0, pg.el.offsetTop - 14), behavior: "smooth" });
}
function updateCurrent() {
  if (!S.pages.length) return;
  const v = $("#viewer"), mid = v.scrollTop + v.clientHeight / 3;
  let cur = 0;
  for (const pg of S.pages) { if (pg.el.offsetTop <= mid) cur = pg.i; else break; }
  if (cur === S.curPage) return;
  S.curPage = cur;
  refreshThumbs();
  const el = $(`.thumb[data-i="${cur}"]`), box = $("#thumbs");
  if (el && !box.matches(":hover")) {
    const r = el.getBoundingClientRect(), b = box.getBoundingClientRect();
    if (r.top < b.top || r.bottom > b.bottom) box.scrollTo({ top: el.offsetTop - box.clientHeight / 2 + el.clientHeight / 2, behavior: "smooth" });
  }
}

// ------------------------------------------------------------------ control panel
async function select(id) {
  S.sel = { id, style: null };
  $$(".hit.sel").forEach((e) => e.classList.remove("sel"));
  $$(`.hit[data-id="${id}"]`).forEach((e) => e.classList.add("sel"));
  const b = await api(`/api/jobs/${S.job.id}/blocks/${id}`).catch((e) => { toast(e.message, true); return null; });
  if (!b || S.sel?.id !== id) return;
  S.sel.style = b.style;
  fillPanel(); $("#panel").hidden = false; positionPanel();
}
function deselect() {
  $$(".hit.sel").forEach((e) => e.classList.remove("sel"));
  S.sel = null; $("#panel").hidden = true;
}

function weightsFor(familyName) { return familyInfo(familyName || defaultFont())?.weights || [400, 700]; }
function nearest(list, w) { return list.reduce((a, b) => (Math.abs(b - w) < Math.abs(a - w) ? b : a), list[0]); }

function fillPanel() {
  if (!S.sel?.style) return;
  const st = S.sel.style;
  const sel = $("#p-font");
  sel.replaceChildren(new Option(`기본 글꼴 (${defaultFont() || "—"})`, ""), ...S.fonts.map((f) => new Option(f.family, f.family)));
  sel.value = st.font || "";
  const ws = weightsFor(st.font);
  const w = $("#p-weight");
  w.max = ws.length - 1; w.disabled = ws.length < 2;
  const cur = nearest(ws, st.weight);
  w.value = ws.indexOf(cur); $("#p-weight-out").textContent = cur;
  $("#p-size").value = st.size; $("#p-color").value = st.color;
}

const pending = new Map();
const sendPatch = debounce(guard(async () => {
  const jobId = S.job?.id;
  for (const [id, patch] of [...pending]) {
    pending.delete(id);
    await api(`/api/jobs/${jobId}/blocks/${id}`, { method: "PATCH", json: patch });
  }
  S.waitCompose = true; poll();     // the server re-flows the document; poll until the new pages are ready
}), 350);
function change(patch) {
  if (!S.sel?.style) return;
  Object.assign(S.sel.style, patch);
  pending.set(S.sel.id, { ...(pending.get(S.sel.id) || {}), ...patch });
  markStale(true);
  sendPatch();
}

function bindPanel() {
  $("#p-font").onchange = (e) => {
    const font = e.target.value || null;
    change({ font, weight: nearest(weightsFor(font), S.sel.style.weight) });
    fillPanel();
  };
  $("#p-weight").oninput = (e) => {
    const ws = weightsFor(S.sel.style.font);
    const w = ws[+e.target.value] ?? ws[0];
    $("#p-weight-out").textContent = w; change({ weight: w });
  };
  const size = (v) => { v = Math.max(2, Math.min(400, Math.round(v * 2) / 2)); if (!isNaN(v)) { $("#p-size").value = v; change({ size: v }); } };
  $("#p-size").oninput = (e) => { const v = parseFloat(e.target.value); if (v >= 2) change({ size: Math.min(400, v) }); };
  $("#p-size").onchange = (e) => size(parseFloat(e.target.value) || S.sel.style.size);
  $("#p-dec").onclick = () => size(S.sel.style.size - 0.5);
  $("#p-inc").onclick = () => size(S.sel.style.size + 0.5);
  $("#p-color").oninput = (e) => change({ color: e.target.value });
}

function positionPanel() {
  const panel = $("#panel");
  if (!S.sel || panel.hidden) return;
  const v = $("#viewer").getBoundingClientRect();
  const els = $$(`.hit[data-id="${S.sel.id}"]`);
  if (!els.length) { panel.style.visibility = "hidden"; return; }
  // the part of a split paragraph that is on screen (else the first part)
  const rects = els.map((e) => e.getBoundingClientRect());
  const r = rects.find((x) => x.bottom > v.top && x.top < v.bottom) || rects[0];
  if (r.bottom < v.top || r.top > v.bottom) { panel.style.visibility = "hidden"; return; }
  panel.style.visibility = "visible";
  const pr = panel.getBoundingClientRect();
  let top = r.top - pr.height - 12, below = false;
  if (top < v.top + 4) { top = Math.max(r.top, v.top) + 8; below = false; top = Math.max(top, v.top + 4); }
  const left = Math.max(8, Math.min(r.left, innerWidth - pr.width - 8));
  panel.style.transform = `translate(${Math.round(left)}px, ${Math.round(top)}px)`;
  panel.classList.toggle("below", below);
  panel.style.setProperty("--arrow-x", Math.max(16, Math.min(pr.width - 16, r.left + Math.min(r.width, 60) / 2 - left)) + "px");
}

init().catch((e) => { console.error(e); toast("초기화 실패: " + e.message, true); });
