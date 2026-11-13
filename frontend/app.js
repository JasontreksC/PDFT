// PDFT frontend — no build step.
// Views of one page: the home (`#/`) that introduces PDFT, "my documents" (`#/docs`: see, pick, add and delete the files),
// the workspace of one document (`#/work/<id>`) and the administrators' dashboard (`#/admin`).
// The workspace shows the ORIGINAL pages with the translation typeset in (server-rendered PNG of composed.pdf) with
// transparent hit boxes over every piece of text. Clicking one opens the 4-control panel; a change is saved, the
// server typesets the affected page again, then the pages reload.
// Nothing here is for engineers: the model, the context and the layout analysis stay on the server. The user picks a
// language and 기본/고급, and sees the progress.
// People sign in with a Google account (`/auth/login`); a document belongs to the member who uploaded it, and the server
// only ever lists and serves one's own. The language and the mode are remembered in this browser.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const LANGS = [
  ["Korean", "한국어"], ["English", "영어"], ["Japanese", "일본어"], ["Chinese (Simplified)", "중국어 (간체)"],
  ["Chinese (Traditional)", "중국어 (번체)"], ["Spanish", "스페인어"], ["French", "프랑스어"], ["German", "독일어"],
  ["Russian", "러시아어"], ["Portuguese", "포르투갈어"], ["Vietnamese", "베트남어"], ["Indonesian", "인도네시아어"],
  ["Thai", "태국어"],
];
const MODE_KO = { basic: "기본 번역", advanced: "고급 번역" };
const STATUS_KO = { uploaded: "준비 중", analyzing: "준비 중", ready: "번역 전", translating: "번역 중", paused: "멈춤", done: "번역 완료", error: "오류" };

const S = {
  auth: "google", user: null,                                   // PDFT_AUTH on the server; the signed-in member
  models: [], fonts: [], settings: {}, ready: false, entered: false,
  lang: "Korean", mode: "basic",
  job: null, pages: [], ver: 0, specKey: "", zoom: 1, fitMode: true,
  sel: null, pollTimer: null, busy: false, uploading: false, waitCompose: false, shownError: null,
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
    if (r.status === 401 && !path.startsWith("/api/session")) onSignedOut();
    throw Object.assign(new Error(msg), { status: r.status });
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

const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const familyInfo = (name) => S.fonts.find((f) => f.family === name);
const defaultFont = () => S.settings.default_font;
const model = () => S.models.find((m) => m.id === S.settings.model_id) || S.models[0];
const atWork = () => document.body.classList.contains("at-work");
const signedIn = () => S.auth === "off" || !!S.user;
const isAdmin = () => S.auth === "google" && S.user?.role === "admin";      // an account of PDFT_ADMIN_EMAILS, signed in
const loginUrl = (next = "/#/docs") => "/auth/login?next=" + encodeURIComponent(next);

// ------------------------------------------------------------------ boot / views
async function init() {
  bindUI();
  paintView();
  const ses = await api("/api/session");
  S.auth = ses.auth; S.user = ses.user;
  renderAccount(); showLoginMessage();
  window.addEventListener("hashchange", route);
  await route();
}

// what the workspace needs from the server (members only)
async function boot() {
  if (S.ready) return;
  const b = await api("/api/bootstrap");
  S.models = b.models || []; S.fonts = b.fonts || []; S.settings = b.settings || {};
  const lang = store.get("pdft.lang"), mode = store.get("pdft.mode");
  S.lang = LANGS.some(([v]) => v === lang) ? lang : LANGS.some(([v]) => v === S.settings.target_lang) ? S.settings.target_lang : "Korean";
  S.mode = MODE_KO[mode] ? mode : S.settings.translate_mode === "advanced" ? "advanced" : "basic";
  renderChoices();
  S.ready = true;
}

const view = () => (location.hash.startsWith("#/work") ? "work" : location.hash.startsWith("#/docs") ? "docs" : location.hash.startsWith("#/admin") ? "admin" : "home");
const workId = () => (location.hash.match(/^#\/work\/([\w-]+)/) || [])[1] || "";
function paintView(v = view()) {
  for (const n of ["home", "docs", "work", "admin"]) document.body.classList.toggle("at-" + n, n === v);
}

async function route() {
  const v = view();
  clearTimeout(D.timer); closePops(); deselect();
  if (v !== "home" && !signedIn()) { location.replace(loginUrl("/" + location.hash)); return; }   // back here after the login
  if (v === "admin" && !isAdmin()) { location.hash = "#/docs"; return; }
  if (v === "work" && !workId()) { location.replace("#/docs"); return; }                          // a document is picked first
  paintView(v);
  window.scrollTo(0, 0);
  try {
    if (v === "docs") { if (S.job) closeJob(); await loadDocs(); }
    else if (v === "work") await enterWork(workId());
    else if (v === "admin") await enterAdmin();
  } catch (e) { toast(e.message, true); }
}

// the session ended (expired, signed out elsewhere, blocked): back to the home page, the last document is remembered
function onSignedOut() {
  if (!S.user) return;
  S.user = null; S.ready = false;
  closeJob();
  renderAccount();
  if (view() !== "home") location.hash = "#/";
  toast("로그인이 끝났어요. 다시 로그인해 주세요.", true);
}

// ------------------------------------------------------------------ account
const LOGIN_MSG = {
  denied: "로그인을 취소했어요.",
  expired: "로그인 시간이 지났어요. 다시 시도해 주세요.",
  failed: "Google 로그인에 실패했어요. 잠시 뒤에 다시 시도해 주세요.",
  unverified: "이메일 인증이 끝나지 않은 Google 계정으로는 로그인할 수 없어요.",
  not_allowed: "이 서비스를 쓸 수 있는 계정이 아니에요. 관리자에게 문의해 주세요.",
  blocked: "사용이 중지된 계정이에요. 관리자에게 문의해 주세요.",
};
function showLoginMessage() {
  const code = new URLSearchParams(location.search).get("login");
  if (!code) return;
  history.replaceState(null, "", location.pathname + location.hash);
  const el = $("#login-msg");
  el.textContent = LOGIN_MSG[code] || LOGIN_MSG.failed; el.hidden = false;
}

function setAvatar(el, u) {
  const img = $("img", el);
  $("span", el).textContent = (u.name || u.email || "?").trim().charAt(0).toUpperCase();
  img.hidden = !u.picture;
  if (u.picture) { img.onerror = () => (img.hidden = true); img.src = u.picture; }
}

function renderAccount() {
  const u = S.user, on = signedIn();
  const cta = $("#cta");
  cta.href = on ? "#/docs" : loginUrl();
  cta.classList.toggle("google", !on);
  $("#cta-label").textContent = on ? "시작하기" : "Google 계정으로 시작하기";
  $("#cta-note").textContent = on ? (S.auth !== "off" && u ? `${u.name || u.email} 님으로 로그인되어 있어요` : "") : "따로 가입할 필요 없이 Google 계정이면 돼요";
  const link = $("#h-link");
  link.textContent = on ? "내 문서" : "로그인"; link.href = on ? "#/docs" : loginUrl();
  $("#dash-link").hidden = $("#h-dash").hidden = $("#docs-dash").hidden = !isAdmin();   // the way to the dashboard: administrators only
  const show = !!u && S.auth !== "off";
  for (const p of ["acct", "d-acct"]) {
    $(`#${p}-pop`).hidden = !show;
    if (!show) continue;
    setAvatar($(`#${p}-btn`), u);
    $(`#${p}-name`).textContent = u.name || u.email; $(`#${p}-email`).textContent = u.email;
  }
}

async function logout() {
  await api("/auth/logout", { method: "POST" });
  S.user = null; S.ready = false;
  closeJob(); renderAccount();
  location.hash = "#/";
}

async function leave() {
  const { jobs } = await api("/api/jobs");
  if (!confirm(`회원 탈퇴를 하면 계정과 올린 문서 ${jobs.length}개가 모두 지워지고 되돌릴 수 없어요.\n정말 탈퇴할까요?`)) return;
  await fetch("/api/me", { method: "DELETE" }).then(async (r) => { if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText); });
  S.user = null; S.ready = false;
  closeJob(); renderAccount();
  location.hash = "#/";
  toast("탈퇴했어요. 계정과 문서를 모두 지웠어요.");
}

// ------------------------------------------------------------------ administrators: dashboard
// Only an account of PDFT_ADMIN_EMAILS gets here (the server decides; the buttons are just not shown to the others).
const A = { data: null, tab: "jobs", user: "", q: "", job: null, pg: null, view: "out" };
const KIND_KO = { login: "로그인", upload: "문서 올림", translate: "번역 시작", finish: "번역 끝", export: "PDF 저장", reset: "번역 지움", delete: "문서 삭제" };
const two = (n) => String(n).padStart(2, "0");
const fmtDay = (iso) => { if (!iso) return "없음"; const d = new Date(iso); return `${d.getFullYear()}.${d.getMonth() + 1}.${d.getDate()}`; };
const fmtTime = (t) => { if (!t) return "없음"; const d = new Date(typeof t === "number" ? t * 1000 : t); return `${d.getMonth() + 1}.${d.getDate()} ${two(d.getHours())}:${two(d.getMinutes())}`; };
function fmtTok(n = 0) {
  if (n >= 1e6) return (n / 1e6).toFixed(2).replace(/\.?0+$/, "") + "M";
  if (n >= 1e4) return Math.round(n / 1e3) + "k";
  if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, "") + "k";
  return String(n);
}
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const memberOf = (id) => A.data?.users.find((u) => u.id === id);
const whoLabel = (id) => { const u = memberOf(id); return u ? (u.name ? `${u.name} · ${u.email}` : u.email) : id ? "탈퇴한 회원" : "주인 없음 (회원 기능 이전)"; };

async function enterAdmin() {
  const m = location.hash.match(/^#\/admin\/job\/([\w-]+)(?:\/(\d+))?/);
  $("#a-dash").hidden = !!m; $("#a-review").hidden = !m;
  window.scrollTo(0, 0);
  if (!A.data || !m) A.data = await api("/api/admin/overview");
  if (m) return loadReview(m[1], +(m[2] || 0));
  renderTiles(); renderUserFilter(); await renderTab();
}

function renderTiles() {
  const t = A.data.totals;
  const tiles = [
    ["회원", t.users.toLocaleString(), t.blocked ? `차단 ${t.blocked}명` : ""],
    ["문서", t.documents.toLocaleString(), t.running ? `번역 중 ${t.running}개` : `${t.pages.toLocaleString()}쪽`],
    ["LLM 요청", t.requests.toLocaleString(), ""],
    ["입력 토큰", fmtTok(t.input), ""],
    ["출력 토큰", fmtTok(t.output), ""],
  ];
  $("#a-tiles").replaceChildren(...tiles.map(([k, v, sub]) => {
    const d = el("div", "tile");
    d.append(el("span", "tile-k", k), el("strong", "tile-v", v), el("span", "tile-s muted", sub));
    return d;
  }));
}

function renderUserFilter() {
  const sel = $("#a-user");
  sel.replaceChildren(new Option("모든 회원", ""), ...A.data.users.map((u) => new Option(u.name ? `${u.name} (${u.email})` : u.email, u.id)));
  if (![...sel.options].some((o) => o.value === A.user)) A.user = "";
  sel.value = A.user;
}

async function renderTab() {
  $$("#a-tabs button").forEach((b) => { const on = b.dataset.tab === A.tab; b.classList.toggle("on", on); b.setAttribute("aria-selected", String(on)); });
  for (const t of ["jobs", "events", "users", "policy"]) $("#a-" + t).hidden = t !== A.tab;
  $("#a-user").closest(".pick").hidden = A.tab === "users" || A.tab === "policy";
  $("#a-q").hidden = A.tab !== "jobs";
  if (A.tab === "jobs") renderJobs();
  else if (A.tab === "users") renderMembers();
  else if (A.tab === "policy") renderPolicy(await api("/api/admin/policy"));
  else await renderEvents();
}

const empty = (text) => el("p", "a-empty muted", text);

function renderJobs() {
  const q = A.q.trim().toLowerCase();
  const jobs = A.data.jobs.filter((j) => (!A.user || j.owner === A.user) && (!q || j.filename.toLowerCase().includes(q)));
  $("#a-jobs").replaceChildren(...(jobs.length ? jobs.map(jobRow) : [empty("해당하는 문서가 없어요.")]));
}

function jobRow(j) {
  const a = el("a", "d-row");
  a.href = `#/admin/job/${j.id}`;
  const main = el("div", "d-main");
  const title = el("strong", "", j.filename);
  title.append(" ", statusChip(j));
  main.append(title, el("span", "muted", whoLabel(j.owner)));
  const meta = el("div", "d-meta muted");
  const prog = j.total ? `번역 ${j.done ?? 0}/${j.total}쪽` : "번역할 쪽 없음";
  meta.append(el("span", "", `${j.pages}쪽 · ${prog}${j.mode ? " · " + MODE_KO[j.mode] : ""}${j.lang ? " · " + j.lang : ""}`),
    el("span", "", `올림 ${fmtTime(j.created)} · 마지막 작업 ${fmtTime(j.updated)}`));
  const use = el("div", "d-use muted");
  use.append(el("span", "", j.usage.requests ? `요청 ${j.usage.requests}회` : "요청 없음"),
    el("span", "", j.usage.requests ? `입력 ${fmtTok(j.usage.input)} · 출력 ${fmtTok(j.usage.output)}` : ""));
  a.append(main, meta, use);
  if (j.error) a.append(el("div", "d-err", j.error));
  return a;
}

function statusChip(j) {
  const cls = j.running ? "accent" : j.status === "done" ? "ok" : j.status === "error" || j.error ? "bad" : "";
  return el("span", "chip " + cls, j.running ? "번역 중" : STATUS_KO[j.status] || j.status);
}

function eventText(e) {
  const d = e.detail || {};
  if (e.kind === "upload" || e.kind === "delete") return d.pages ? `${d.pages}쪽` : "";
  if (e.kind === "translate") return [MODE_KO[d.mode], d.lang, d.pages ? `${d.pages}쪽` : ""].filter(Boolean).join(" · ");
  if (e.kind === "finish") {
    const u = d.usage || {};
    return [STATUS_KO[d.status] || d.status, d.total ? `${d.done}/${d.total}쪽` : "", u.requests ? `누적 입력 ${fmtTok(u.input)} · 출력 ${fmtTok(u.output)}` : ""].filter(Boolean).join(" · ");
  }
  if (e.kind === "export") return d.selected ? "선택한 쪽만" : "";
  if (e.kind === "reset") return d.pages ? `${d.pages}쪽` : "";
  return "";
}

async function renderEvents() {
  const box = $("#a-events");
  const q = A.user ? `?user=${encodeURIComponent(A.user)}` : "";
  const { events } = await api("/api/admin/events" + q);
  const live = new Set(A.data.jobs.map((j) => j.id));
  box.replaceChildren(...(events.length ? events.map((e) => {
    const row = el(e.job_id && live.has(e.job_id) ? "a" : "div", "e-row");
    if (row.tagName === "A") row.href = `#/admin/job/${e.job_id}`;
    row.append(el("span", "e-at muted", fmtTime(e.at)), el("span", "chip", KIND_KO[e.kind] || e.kind),
      el("span", "e-who", e.name || e.email), el("span", "e-file", e.filename || ""), el("span", "e-more muted", eventText(e)));
    return row;
  }) : [empty("기록된 활동이 아직 없어요.")]));
}

// the blanks of the privacy policy page: empty ones are highlighted on the page itself until they are filled in
function renderPolicy(p) {
  $("#pol-sum").textContent = p.missing ? ` · 아직 비어 있는 항목 ${p.missing}개 (페이지에 노란색으로 표시돼요)` : " · 모두 채웠어요";
  $("#pol-fields").replaceChildren(...p.fields.map((f) => {
    const row = el("label", "pol-row" + (f.value ? "" : " missing"));
    const input = el("input");
    input.name = f.key; input.value = f.value; input.placeholder = "예: " + f.suggest; input.maxLength = 400; input.autocomplete = "off";
    const use = el("button", "btn small ghost", "예시 넣기");
    use.type = "button"; use.title = f.suggest;
    use.onclick = () => { input.value = f.suggest; input.focus(); };
    row.append(el("span", "", f.label), input, use);
    return row;
  }));
}
async function savePolicy() {
  const values = Object.fromEntries($$("#pol-fields input").map((i) => [i.name, i.value]));
  renderPolicy(await api("/api/admin/policy", { method: "PUT", json: { values } }));
  toast("처리방침에 반영했어요.");
}

function renderMembers() {
  const users = A.data.users;
  $("#a-users").replaceChildren(...(users.length ? users.map(memberRow) : [empty("회원이 아직 없어요.")]));
}

function memberRow(u) {
  const me = u.id === S.user?.id, blocked = u.status === "blocked", boss = u.role === "admin";
  const row = el("div", "m-row" + (blocked ? " blocked" : ""));
  row.innerHTML = '<span class="avatar"><img alt="" referrerpolicy="no-referrer" hidden><span></span></span>' +
    '<div class="m-id"><strong></strong><span class="muted"></span></div><div class="m-meta muted"></div><div class="m-acts"></div>';
  setAvatar($(".avatar", row), u);
  const name = $(".m-id strong", row);
  name.textContent = u.name || u.email;
  for (const [on, label, cls] of [[me, "나", ""], [boss, "관리자", "accent"], [blocked, "차단됨", "bad"]]) {
    if (on) name.append(" ", el("span", "chip " + cls, label));
  }
  $(".m-id .muted", row).textContent = u.email;
  $(".m-meta", row).textContent = `가입 ${fmtDay(u.created_at)} · 최근 로그인 ${fmtTime(u.last_login_at)} · 문서 ${u.documents}개 (${u.pages}쪽)` +
    (u.requests ? ` · 요청 ${u.requests}회 · 입력 ${fmtTok(u.input)} · 출력 ${fmtTok(u.output)}` : "");
  const act = (label, fn, { danger = false } = {}) => {
    const b = el("button", "btn small" + (danger ? " stop" : ""), label);
    b.onclick = guard(async () => { if ((await fn()) !== false) { A.data = await api("/api/admin/overview"); renderTiles(); renderUserFilter(); await renderTab(); } });
    $(".m-acts", row).append(b);
  };
  act("작업 보기", () => { A.user = u.id; A.tab = "jobs"; $("#a-user").value = u.id; });
  if (me || boss) return row;          // administrators are set on the server (PDFT_ADMIN_EMAILS), not here
  act(blocked ? "차단 해제" : "차단", () => {
    if (!blocked && !confirm(`${u.email} 회원을 차단할까요?\n바로 로그아웃되고 다시 로그인할 수 없어요. 문서는 그대로 남아요.`)) return false;
    return api(`/api/admin/users/${u.id}`, { method: "PATCH", json: { status: blocked ? "active" : "blocked" } });
  });
  act("삭제", () => {
    if (!confirm(`${u.email} 회원을 삭제할까요?\n계정과 문서 ${u.documents}개, 활동 기록이 모두 지워지고 되돌릴 수 없어요.`)) return false;
    return api(`/api/admin/users/${u.id}`, { method: "DELETE" });
  }, { danger: true });
  return row;
}

// ------------------------------------------------------------------ administrators: review of one document
const PSTATE_KO = ["번역 전", "일부 번역", "번역됨", "번역 안 함 (스캔·표지)"];

async function loadReview(jid, n) {
  const d = await api(`/api/admin/jobs/${jid}`);
  A.job = d;
  $("#r-name").textContent = d.filename;
  $("#r-owner").textContent = d.owner_user ? `${d.owner_user.name || ""} ${d.owner_user.email}`.trim() : whoLabel(d.owner);
  const done = d.page_list.filter((p) => p.state === 2).length;
  const dl = $("#r-download");
  dl.hidden = !(d.compose?.version > 0 && d.page_list.some((p) => p.state === 1 || p.state === 2));
  dl.href = `/api/jobs/${d.id}/export`; dl.setAttribute("download", "");
  const fit = d.compose?.fit || {};
  const facts = [
    ["상태", d.running ? "번역 중" : STATUS_KO[d.status] || d.status],
    ["쪽", `${d.pages}쪽 중 ${done}쪽 번역됨` + (d.translatable != null ? ` (번역 가능 ${d.translatable}쪽)` : "")],
    ["번역 방식", d.mode ? MODE_KO[d.mode] : "없음"],
    ["모델", d.model || "없음"],
    ["언어", d.lang || "없음"],
    ["LLM 요청", `${d.usage.requests || 0}회`],
    ["토큰", `입력 ${fmtTok(d.usage.input)}${d.usage.cached ? ` (캐시 ${fmtTok(d.usage.cached)})` : ""} · 출력 ${fmtTok(d.usage.output)}`],
    ["조판", fit.scaled || fit.failed ? `글 ${fit.scaled || 0}개 축소 · ${fit.failed || 0}개 넘침` : "문제 없음"],
    ["올린 때", fmtTime(d.created)],
    ["마지막 작업", fmtTime(d.updated)],
  ];
  if (d.error) facts.push(["오류", d.error]);
  for (const w of d.warnings) facts.push(["알림", w]);
  $("#r-facts").replaceChildren(...facts.map(([k, v]) => { const div = el("div", k === "오류" ? "bad" : ""); div.append(el("dt", "", k), el("dd", "", v)); return div; }));
  $("#r-pages").replaceChildren(...d.page_list.map((p) => {
    const b = el("button", "r-pg");
    b.dataset.n = p.n; b.title = `${p.n + 1}쪽 · ${PSTATE_KO[p.state] || ""}`;
    const im = el("img");
    im.loading = "lazy"; im.alt = ""; im.src = `/api/jobs/${d.id}/pages/${p.n}/image?scale=0.25&v=${d.compose?.version || 0}`;
    im.style.aspectRatio = `${p.w} / ${p.h}`;
    b.append(im, el("span", "r-no", String(p.n + 1)), el("i", "r-st s" + p.state, p.state === 2 ? "✓" : p.state === 1 ? "…" : p.state === 3 ? "원본" : ""));
    if (p.lost) b.append(el("i", "r-lost", `!${p.lost}`));
    b.onclick = guard(() => showReviewPage(p.n));
    return b;
  }));
  if (!d.page_list.length) { $("#r-side").replaceChildren(empty("이 문서는 아직 준비되지 않았어요.")); $("#r-boxes").replaceChildren(); $("#r-img").removeAttribute("src"); return; }
  await showReviewPage(Math.min(n, d.page_list.length - 1));
}

async function showReviewPage(n) {
  const d = A.job;
  A.pg = await api(`/api/admin/jobs/${d.id}/pages/${n}`);
  history.replaceState(null, "", `#/admin/job/${d.id}/${n}`);
  $$("#r-pages .r-pg").forEach((b) => b.classList.toggle("on", +b.dataset.n === n));
  $(`#r-pages .r-pg[data-n="${n}"]`)?.scrollIntoView({ block: "nearest", inline: "nearest" });
  renderReviewSheet(); renderReviewSide();
}

function renderReviewSheet() {
  const d = A.job, pg = A.pg;
  $$("#r-mode button").forEach((b) => b.classList.toggle("on", b.dataset.v === A.view));
  const sheet = $("#r-sheet");
  sheet.style.aspectRatio = `${pg.w} / ${pg.h}`;
  sheet.dataset.v = A.view;
  $("#r-img").src = `/api/jobs/${d.id}/pages/${pg.n}/image?scale=2&v=${d.compose?.version || 0}${A.view === "out" ? "" : "&orig=1"}`;
  const box = (cls, r, title, id) => {
    const b = el("div", "rb " + cls);
    const [x0, y0, x1, y1] = r;
    Object.assign(b.style, { left: (x0 / pg.w) * 100 + "%", top: (y0 / pg.h) * 100 + "%", width: ((x1 - x0) / pg.w) * 100 + "%", height: ((y1 - y0) / pg.h) * 100 + "%" });
    if (title) b.title = title;
    if (id) { b.dataset.id = id; b.onmouseenter = () => hlPair(id, true); b.onmouseleave = () => hlPair(id, false); b.onclick = () => $(`.pair[data-id="${id}"]`)?.scrollIntoView({ block: "center", behavior: "smooth" }); }
    return b;
  };
  $("#r-boxes").replaceChildren(
    ...pg.excluded.map((r) => box("ex", r, "번역하지 않고 원본 그대로 둔 글")),
    ...pg.parser.map((p) => box("dl", p.rect, `Docling 구역 · ${p.kind}`)),
    ...pg.frames.map((f) => box("fr" + (f.chrome ? " chrome" : ""), f.rect, "", f.id)),
  );
}

function hlPair(id, on) {
  $(`.rb[data-id="${id}"]`)?.classList.toggle("hl", on);
  $(`.pair[data-id="${id}"]`)?.classList.toggle("hl", on);
}

function renderReviewSide() {
  const pg = A.pg, side = $("#r-side");
  const head = el("div", "s-head");
  head.append(el("strong", "", `${pg.n + 1}쪽`), el("span", "muted", [
    pg.mode ? MODE_KO[pg.mode] : pg.done ? "" : "아직 번역하지 않음", pg.model, pg.calls ? `호출 ${pg.calls}회` : "",
    pg.mode === "basic" ? (pg.hints ? "Docling 힌트 사용" : "Docling 힌트 없음") : "",
    `글상자 ${pg.frames.length}개`, `남긴 글 ${pg.excluded.length}개`,
  ].filter(Boolean).join(" · ")));
  const parts = [head];
  if (pg.lost.length) {
    const lost = el("div", "s-lost");
    lost.append(el("strong", "", `번역하지 못한 항목 ${pg.lost.length}개`));
    for (const x of pg.lost) {
      const li = el("p", "", x.original || "");
      if (x.reason) li.append(el("span", "muted", ` — ${x.reason}`));
      lost.append(li);
    }
    parts.push(lost);
  }
  if (!pg.frames.length) parts.push(empty(pg.done ? "이 쪽에는 번역한 글이 없어요." : "아직 번역하지 않은 쪽이에요."));
  for (const f of pg.frames) {
    const p = el("div", "pair" + (f.tgt == null ? " open" : ""));
    p.dataset.id = f.id;
    p.append(el("p", "src", f.src), el("p", "tgt", f.tgt == null ? "(번역문 없음)" : f.tgt));
    p.onmouseenter = () => hlPair(f.id, true); p.onmouseleave = () => hlPair(f.id, false);
    parts.push(p);
  }
  side.replaceChildren(...parts);
  side.scrollTop = 0;
}

// ------------------------------------------------------------------ workspace
async function enterWork(jid) {
  await boot();
  if (S.job?.id === jid) { if (S.fitMode) applyZoom(); return; }
  if (S.job) closeJob();
  try { await openJob(jid); }
  catch (e) { toast(e.status === 404 ? "문서를 찾을 수 없어요. 삭제되었을 수 있어요." : e.message, true); location.replace("#/docs"); }
}

// ------------------------------------------------------------------ toolbar
function bindUI() {
  $$(".pop").forEach((p) => {
    $(".pop-btn", p).addEventListener("click", (e) => {
      e.stopPropagation();
      const open = p.classList.contains("open");
      closePops();
      if (!open) { p.classList.add("open"); $(".pop-btn", p).setAttribute("aria-expanded", "true"); }
    });
    $(".sheet-menu", p).addEventListener("click", (e) => e.stopPropagation());
  });
  document.addEventListener("click", closePops);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closePops(); deselect(); } });

  const pick = () => { if (!S.uploading) $("#file-input").click(); };
  $("#docs-add").onclick = pick;
  $("#docs-grid").addEventListener("click", (e) => { if (e.target.closest(".doc-add")) pick(); });
  $("#m-open").onclick = () => { closePops(); pick(); };
  $("#file-input").onchange = (e) => { const fs = [...e.target.files]; e.target.value = ""; upload(fs); };
  $("#docs-all").onchange = (e) => { for (const j of shownDocs()) e.target.checked ? D.sel.add(j.id) : D.sel.delete(j.id); renderDocs(); };
  $("#docs-q").oninput = (e) => { D.q = e.target.value; renderDocs(); };
  $("#docs-del").onclick = guard(() => deleteDocs(D.jobs.filter((j) => D.sel.has(j.id))));
  $("#d-logout").onclick = guard(() => { closePops(); return logout(); });
  $("#d-leave").onclick = guard(() => { closePops(); return leave(); });
  let depth = 0;
  const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
  document.addEventListener("dragenter", (e) => { if (!hasFiles(e)) return; e.preventDefault(); depth++; document.body.classList.add("dragging"); });
  document.addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
  document.addEventListener("dragleave", (e) => { if (!hasFiles(e)) return; depth = Math.max(0, depth - 1); if (!depth) document.body.classList.remove("dragging"); });
  document.addEventListener("drop", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault(); depth = 0; document.body.classList.remove("dragging");
    const files = [...(e.dataTransfer.files || [])];
    if (!files.length) return;
    if (!signedIn()) return toast("먼저 Google 계정으로 로그인해 주세요.", true);
    if (view() === "home") location.hash = "#/docs";      // a file dropped on the home page goes to "my documents"
    upload(files);
  });

  $("#s-lang").innerHTML = LANGS.map(([v, l]) => `<option value="${v}">${l}</option>`).join("");
  $("#s-lang").onchange = (e) => { S.lang = e.target.value; store.set("pdft.lang", S.lang); };
  $$("#mode-pop .opt").forEach((b) => (b.onclick = () => {
    S.mode = b.dataset.mode; store.set("pdft.mode", S.mode);
    closePops(); renderChoices();
  }));

  $("#btn-main").onclick = guard(mainAction);
  $("#btn-export").onclick = guard(() => exportPdf(false));
  $("#m-export-sel").onclick = guard(() => { closePops(); return exportPdf(true); });
  $("#m-reset").onclick = guard(() => { closePops(); return revertSelected(); });
  $("#m-delete").onclick = guard(async () => {
    closePops();
    if (!S.job || !confirm(`‘${S.job.filename}’ 문서를 삭제할까요?\n올린 PDF와 번역이 모두 지워지고 되돌릴 수 없어요.`)) return;
    await deleteJob(S.job.id);
    location.hash = "#/docs";
    toast("문서를 삭제했어요.");
  });

  $("#m-logout").onclick = guard(() => { closePops(); return logout(); });
  $("#m-leave").onclick = guard(() => { closePops(); return leave(); });
  $$("#a-tabs button").forEach((b) => (b.onclick = guard(() => { A.tab = b.dataset.tab; return renderTab(); })));
  $("#a-user").onchange = guard((e) => { A.user = e.target.value; return renderTab(); });
  $("#a-policy").onsubmit = guard((e) => { e.preventDefault(); return savePolicy(); });
  $("#a-q").oninput = (e) => { A.q = e.target.value; renderJobs(); };
  $$("#r-mode button").forEach((b) => (b.onclick = () => { A.view = b.dataset.v; if (A.pg) renderReviewSheet(); }));

  $("#sb-all").onchange = (e) => (e.target.checked ? selectAll() : selectNone());
  $("#thumbs").addEventListener("click", onThumbClick);
  $("#viewer").addEventListener("scroll", () => {
    if (!S.curRaf) S.curRaf = requestAnimationFrame(() => { S.curRaf = 0; updateCurrent(); positionPanel(); });
  }, { passive: true });

  $("#z-in").onclick = () => setZoom(S.zoom * 1.15);
  $("#z-out").onclick = () => setZoom(S.zoom / 1.15);
  $("#z-fit").onclick = () => { S.fitMode = true; applyZoom(); };
  window.addEventListener("resize", debounce(() => { if (atWork() && S.fitMode) applyZoom(); positionPanel(); }, 120));

  bindPanel();
  document.addEventListener("pointerdown", (e) => { if (!e.target.closest(".hit, #panel")) deselect(); });
}
function closePops() {
  $$(".pop.open").forEach((p) => { p.classList.remove("open"); $(".pop-btn", p).setAttribute("aria-expanded", "false"); });
}

function renderChoices() {
  $("#s-lang").value = S.lang;
  $("#mode-label").textContent = MODE_KO[S.mode];
  $$("#mode-pop .opt").forEach((b) => {
    const on = b.dataset.mode === S.mode;
    b.classList.toggle("on", on); b.setAttribute("aria-checked", String(on));
  });
}

// ------------------------------------------------------------------ my documents (#/docs)
const D = { jobs: [], sel: new Set(), q: "", timer: 0 };
const fmtDate = (t) => { const d = new Date(t * 1000); return `${d.getFullYear()}.${d.getMonth() + 1}.${d.getDate()}`; };
const shownDocs = () => { const q = D.q.trim().toLowerCase(); return D.jobs.filter((j) => !q || j.filename.toLowerCase().includes(q)); };
const docBusy = (j) => ["translating", "analyzing", "uploaded"].includes(j.status);

async function loadDocs(quiet = false) {
  clearTimeout(D.timer);
  let jobs = D.jobs;
  try { jobs = (await api("/api/jobs")).jobs; } catch (e) { if (!quiet) throw e; }
  if (view() !== "docs") return;
  D.jobs = jobs;
  const ids = new Set(jobs.map((j) => j.id));
  D.sel = new Set([...D.sel].filter((id) => ids.has(id)));
  renderDocs();
  if (jobs.some(docBusy)) D.timer = setTimeout(() => loadDocs(true), 3000);      // a translation in progress shows up here
}

function renderDocs() {
  const all = D.jobs, shown = shownDocs();
  $("#docs-sum").textContent = all.length ? `문서 ${all.length}개` : "아직 문서가 없어요. PDF를 추가해 시작해 보세요.";
  $("#docs-bar").hidden = !all.length;
  const cards = shown.map(docCard);
  const add = el("button", "doc-add");
  add.type = "button"; add.disabled = S.uploading;
  add.innerHTML = '<span class="drop-icon"><svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 16V5M7 10l5-5 5 5M5 19h14"/></svg></span><strong></strong><span class="muted"></span>';
  $("strong", add).textContent = S.uploading ? S.upText || "올리는 중…" : "PDF 추가";
  $(".muted", add).textContent = S.uploading ? "잠시만 기다려 주세요" : "끌어다 놓거나 눌러서 고르세요 (여러 개 가능)";
  const grid = $("#docs-grid");
  grid.replaceChildren(add, ...cards);
  if (all.length && !shown.length) grid.append(el("p", "a-empty muted", "검색 결과가 없어요."));
  $("#docs-q").disabled = !all.length;
  renderDocBar();
}

function renderDocBar() {
  const shown = shownDocs(), n = D.sel.size, on = shown.filter((j) => D.sel.has(j.id)).length;
  const all = $("#docs-all");
  all.checked = !!shown.length && on === shown.length; all.indeterminate = on > 0 && on < shown.length; all.disabled = !shown.length;
  $("#docs-count").textContent = n ? `${n}개 선택됨` : "";
  const del = $("#docs-del");
  del.disabled = !n; del.textContent = n ? `선택 삭제 (${n})` : "선택 삭제";
}

function docCard(j) {
  const card = el("article", "doc-card" + (D.sel.has(j.id) ? " on" : ""));
  const open = () => { location.hash = `#/work/${j.id}`; };
  card.tabIndex = 0; card.setAttribute("role", "button"); card.setAttribute("aria-label", `${j.filename} 열기`);
  card.onclick = open;
  card.onkeydown = (e) => { if (e.target === card && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); open(); } };

  const tk = el("label", "doc-tk"), cb = el("input");
  cb.type = "checkbox"; cb.checked = D.sel.has(j.id); cb.setAttribute("aria-label", `${j.filename} 선택`);
  tk.onclick = (e) => e.stopPropagation();
  cb.onchange = () => { cb.checked ? D.sel.add(j.id) : D.sel.delete(j.id); card.classList.toggle("on", cb.checked); renderDocBar(); };
  tk.append(cb);
  const x = el("button", "doc-x", "✕");
  x.type = "button"; x.title = "삭제"; x.setAttribute("aria-label", `${j.filename} 삭제`);
  x.onclick = guard(async (e) => { e.stopPropagation(); await deleteDocs([j]); });

  const th = el("div", "doc-th"), im = el("img");
  im.alt = ""; im.loading = "lazy"; im.draggable = false;
  im.src = `/api/jobs/${j.id}/pages/0/image?scale=0.25&v=${j.status}${j.progress?.done ?? 0}`;
  im.onerror = () => im.remove();
  th.append(im);
  const p = j.progress || {};
  if (j.status === "translating" && p.total) { const bar = el("i", "doc-prog"); bar.style.width = Math.round((p.done / p.total) * 100) + "%"; th.append(bar); }

  const state = j.status === "translating" ? `번역 중${p.total ? ` ${p.done}/${p.total}쪽` : ""}` : STATUS_KO[j.status] || "";
  const info = el("div", "doc-info"), name = el("strong", "", j.filename);
  name.title = j.filename;
  info.append(name, el("span", "", `${j.pages}쪽 · ${state}`), el("span", "", fmtDate(j.created)));
  card.append(th, info, tk, x);
  return card;
}

async function deleteDocs(list) {
  if (!list.length) return;
  const what = list.length === 1 ? `‘${list[0].filename}’ 문서를` : `선택한 문서 ${list.length}개를`;
  if (!confirm(`${what} 삭제할까요?\n올린 PDF와 번역이 모두 지워지고 되돌릴 수 없어요.`)) return;
  let ok = 0;
  for (const j of list) {
    try { await api(`/api/jobs/${j.id}`, { method: "DELETE" }); ok++; D.sel.delete(j.id); }
    catch (e) { toast(`${j.filename}: ${e.message}`, true); }
  }
  if (ok) toast(ok === 1 ? "문서를 삭제했어요." : `${ok}개 문서를 삭제했어요.`);
  await loadDocs();
}

async function deleteJob(id) {
  await api(`/api/jobs/${id}`, { method: "DELETE" });
  if (S.job?.id === id) closeJob();
}

// ------------------------------------------------------------------ jobs
function setUploading(on, text = "") {
  S.uploading = on; S.upText = text;
  document.body.classList.toggle("uploading", on);
  $("#docs-add").disabled = on;
  if (on && view() === "work" && S.job) setStatus(text, { busy: true });
  if (view() === "docs") renderDocs();
}

async function upload(files) {
  files = [...files];
  if (!files.length || S.uploading) return;
  const bad = files.filter((f) => !/\.pdf$/i.test(f.name) && f.type !== "application/pdf");
  if (bad.length) toast(`PDF 파일만 올릴 수 있어요: ${bad[0].name}`, true);
  files = files.filter((f) => !bad.includes(f));
  const ids = [];
  try {
    for (const [k, f] of files.entries()) {
      setUploading(true, files.length > 1 ? `올리는 중 (${k + 1}/${files.length})…` : "올리는 중…");
      const fd = new FormData(); fd.append("file", f);
      try { ids.push((await api("/api/jobs", { method: "POST", body: fd })).id); }
      catch (e) { toast(`${f.name}: ${e.message}`, true); }
    }
  } finally { setUploading(false); }
  if (files.length === 1 && ids.length === 1) location.hash = `#/work/${ids[0]}`;      // one file: straight into it
  else if (ids.length) {
    if (view() !== "docs") location.hash = "#/docs"; else await loadDocs();
    toast(`${ids.length}개 문서를 추가했어요.`);
  }
}

async function openJob(id) {
  clearTimeout(S.pollTimer);
  deselect();
  const job = await api(`/api/jobs/${id}`);
  S.job = job; S.shownError = null;
  S.ver = job.compose?.version || 0;
  document.body.classList.add("has-doc");
  $("#doc-name").textContent = job.filename; $("#doc-name").title = job.filename;
  S.picked = new Set(job.selected_pages ?? Array.from({ length: job.pages.length }, (_, i) => i)); S.lastPick = null; S.curPage = 0; S.pstate = [];
  buildPages(); buildThumbs();
  $("#viewer").scrollTop = 0;
  renderProgress(); renderControls();
  poll();
}

function closeJob() {
  clearTimeout(S.pollTimer);
  deselect();
  S.job = null; S.pages = []; $("#pages").replaceChildren(); $("#thumbs").replaceChildren(); S.picked = new Set(); S.pstate = [];
  document.body.classList.remove("has-doc");
  renderControls();
}

async function poll() {
  clearTimeout(S.pollTimer);
  if (!S.job) return;
  const id = S.job.id;
  try {
    const q = new URLSearchParams({ lang: S.lang });
    if (model()) q.set("model_id", model().id);
    const st = await api(`/api/jobs/${id}/status?${q}`);
    if (!S.job || S.job.id !== id) return;
    Object.assign(S.job, { status: st.status, error: st.error, progress: st.progress, analysis: st.analysis, compose: st.compose });
    if (st.page_state) { S.pstate = st.page_state; refreshBadges(); }
    if (st.compose && st.compose.version !== S.ver) onComposeVersion(st.compose.version);
    const composing = st.compose && (st.compose.busy || st.compose.pending);
    if (!composing && S.waitCompose) { S.waitCompose = false; markStale(false); }
    if (st.error && st.error !== S.shownError && !["analyzing", "translating"].includes(st.status)) { S.shownError = st.error; toast(st.error, true); }
    renderProgress(); renderControls();
    if (["analyzing", "translating"].includes(st.status) || composing || S.waitCompose) S.pollTimer = setTimeout(poll, 800);
  } catch (e) {
    if (e.status === 401) return;
    if (e.status === 404) { closeJob(); toast("문서를 찾을 수 없어요. 삭제되었을 수 있어요.", true); location.replace("#/docs"); return; }
    S.pollTimer = setTimeout(poll, 3000);
  }
}

// what the server is doing, in plain words
function phase(msg = "") {
  if (/Docling|글상자|시작/.test(msg)) return "문서 구조를 살펴보는 중";
  if (/요약/.test(msg)) return "용어를 정리하는 중";
  return "번역하는 중";
}

function setStatus(msg, { busy = false, bad = false, pct = 0 } = {}) {
  const el = $("#doc-status");
  el.textContent = msg; el.classList.toggle("bad", bad); el.title = msg;
  const bar = $("#prog");
  bar.classList.toggle("on", busy || (pct > 0 && pct < 100));
  bar.classList.toggle("busy", busy);
  bar.classList.toggle("wait", busy && !pct);            // nothing to measure yet: an indeterminate sweep
  $("i", bar).style.width = (busy && !pct ? 100 : pct) + "%";
}

function renderProgress() {
  const j = S.job;
  if (!j) return;
  const p = j.progress || {}, st = j.status;
  const pct = p.total ? Math.round((p.done / p.total) * 100) : 0;
  const count = p.total ? `${p.done} / ${p.total}쪽` : "";
  const none = S.picked.size === 0, part = S.picked.size < nPages();
  if (S.busy) return setStatus("PDF를 만드는 중…", { busy: true });
  if (st === "translating") return setStatus([phase(p.message), count, pct ? pct + "%" : ""].filter(Boolean).join(" · "), { busy: true, pct });
  if (st === "uploaded" || st === "analyzing") return setStatus(j.error || "문서를 준비하는 중…", { busy: !j.error, bad: !!j.error });
  if (st === "error") return setStatus(j.error || "문제가 생겼어요. 다시 시도해 주세요.", { bad: true, pct });
  if (st === "done") return setStatus(`번역 완료${part ? ` · ${count}` : ""} · 글을 누르면 글꼴과 크기를 바꿀 수 있어요`);
  if (none) return setStatus("번역할 쪽을 왼쪽에서 골라 주세요");
  if (st === "paused") {
    const retry = /모델이|자리를 찾지/.test(p.message || "");
    return setStatus(retry ? `일부 쪽을 끝까지 번역하지 못했어요 · ‘이어서 번역’으로 다시 시도해 보세요` : `멈춤 · ${count} 완료`, { pct });
  }
  if (j.error) return setStatus(j.error, { bad: true });
  if (!p.total) return setStatus(part ? "선택한 쪽에는 번역할 글이 없어요" : "번역할 글을 찾지 못했어요 · 스캔한 PDF는 번역할 수 없어요");
  setStatus(part ? `선택한 ${p.total}쪽을 번역할 수 있어요` : `번역할 준비가 됐어요 · ${p.total}쪽`);
}

function renderControls() {
  const j = S.job, st = j?.status;
  const analyzing = st === "analyzing" || st === "uploaded", translating = st === "translating";
  const hasDoc = !!j?.analysis;
  const none = !!j && S.picked.size === 0, part = j && S.picked.size < nPages() ? ` (${S.picked.size}쪽)` : "";
  const nothing = hasDoc && !(j.progress?.total > 0);
  const translated = S.pstate.some((s) => s === 1 || s === 2);

  const main = $("#btn-main");
  main.disabled = !translating && (!hasDoc || analyzing || st === "done" || none || nothing);
  main.title = none ? "번역할 쪽을 왼쪽에서 골라 주세요" : "";
  main.classList.toggle("stop", translating);
  main.classList.toggle("primary", !translating && st !== "done");
  main.textContent = translating ? "중지" : st === "done" ? "번역 완료" : st === "paused" ? "이어서 번역" + part : "번역 시작" + part;

  const canExport = hasDoc && (j.compose?.version || 0) > 0 && !analyzing && translated && !S.busy;
  const exp = $("#btn-export");
  exp.disabled = !canExport;
  exp.classList.toggle("primary", st === "done");
  exp.title = canExport || !hasDoc ? "" : "번역한 뒤에 저장할 수 있어요";
  $("#m-export-sel").disabled = !canExport || none || allPicked();
  $("#m-reset").disabled = !hasDoc || analyzing || translating || none || !translated;
  $("#m-reset").textContent = part ? `선택한 쪽 번역 지우기` : "번역 지우기";
  $("#s-lang").disabled = translating; $("#mode-btn").disabled = translating; $("#sb-all").disabled = analyzing || translating;
  document.body.classList.toggle("busy-job", analyzing || translating);
}

const mainAction = () => (S.job?.status === "translating" ? stopTranslate() : startTranslate());

async function startTranslate() {
  if (!S.job) return;
  const m = model();
  if (!m || !m.has_key) throw new Error("번역 서버 설정이 아직 끝나지 않았어요. 관리자에게 문의해 주세요.");
  await pushSelection();
  await api(`/api/jobs/${S.job.id}/translate`, {
    method: "POST",
    json: { target_lang: S.lang, mode: S.mode },        // the model and the speed are the server's
  });
  S.job.status = "translating"; S.job.progress = { ...(S.job.progress || {}), message: "번역 시작" };
  renderProgress(); renderControls(); poll();
}
async function stopTranslate() {
  await api(`/api/jobs/${S.job.id}/cancel`, { method: "POST" });
  await poll(); renderControls();
}

async function exportPdf(selectedOnly = false) {
  if (!S.job || S.busy) return;
  if (selectedOnly) await pushSelection();
  S.busy = true; renderControls(); renderProgress();
  try {
    const r = await fetch(`/api/jobs/${S.job.id}/export${selectedOnly ? "?selected=true" : ""}`);
    if (!r.ok) { let m = r.statusText; try { m = (await r.json()).detail; } catch { /* */ } throw new Error(m); }
    const blob = await r.blob();
    const cd = r.headers.get("content-disposition") || "";
    const name = decodeURIComponent((cd.match(/filename\*=utf-8''([^;]+)/i) || cd.match(/filename="?([^";]+)"?/i) || [])[1] || "translated.pdf");
    const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: name });
    document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 10000);
    toast("PDF를 저장했어요.");
  } catch (e) { toast(e.message, true); }
  finally { S.busy = false; renderControls(); renderProgress(); }
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
  if (!S.pages.length || !atWork()) return;
  if (S.fitMode) {
    const maxW = Math.max(...S.pages.map((p) => p.w));
    S.zoom = Math.max(0.3, Math.min(1.6, ($("#viewer").clientWidth - 96) / maxW));
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
    el.innerHTML = `<label class="tk" title="이 쪽 선택"><input type="checkbox" tabindex="-1" aria-label="${i + 1}쪽 선택"></label><i class="tb" data-s="0"></i>` +
      `<div class="timg" style="aspect-ratio:${p.w} / ${p.h}"><img alt="" draggable="false"></div><span class="tn">${i + 1}</span>`;
    return el;
  });
  box.append(...els);
  box.scrollTop = 0;
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
  $("#sb-count").textContent = `${S.picked.size} / ${nPages()}`;
  const all = $("#sb-all");
  all.checked = allPicked(); all.indeterminate = S.picked.size > 0 && !allPicked(); all.disabled = jobBusy();
}
function refreshBadges() {
  const busy = S.job?.status === "translating";
  $$(".thumb .tb").forEach((b, i) => {
    const s = S.pstate[i] ?? 0;
    b.dataset.s = String(s);
    b.textContent = s === 2 ? "✓" : s === 1 ? "…" : s === 3 ? "원본" : "";
    b.title = s === 2 ? "번역됨" : s === 1 ? (busy ? "번역하는 중" : "일부만 번역됨") : s === 3 ? "글자를 읽을 수 없는 쪽(스캔·표지)이라 원본 그대로 둬요" : "";
  });
  const skipped = S.pstate.filter((s) => s === 3).length;
  $("#sb-hint").textContent = "선택한 쪽만 번역해요." + (skipped ? ` ‘원본’ 표시가 붙은 ${skipped}쪽은 스캔·표지라서 번역하지 않아요.` : "");
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
  if (!confirm(all ? "번역을 모두 지우고 원본으로 되돌릴까요?\n글꼴·크기를 바꾼 것도 함께 지워져요."
    : `선택한 ${S.picked.size}쪽의 번역을 지우고 원본으로 되돌릴까요?\n다른 쪽은 그대로 남아요.`)) return;
  await pushSelection();
  deselect();
  await api(`/api/jobs/${S.job.id}/reset`, { method: "POST" });
  toast(all ? "번역을 지웠어요." : `${S.picked.size}쪽의 번역을 지웠어요.`);
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

// ------------------------------------------------------------------ format panel
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
  sel.replaceChildren(new Option("기본 글꼴", ""), ...S.fonts.map((f) => new Option(f.family, f.family)));
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
  S.waitCompose = true; poll();     // the server typesets the page again; poll until the new pages are ready
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
  let top = r.top - pr.height - 12;
  const below = top < v.top + 4;                       // no room above: under the text instead
  if (below) top = Math.min(r.bottom + 12, v.bottom - pr.height - 4);
  const left = Math.max(8, Math.min(r.left, innerWidth - pr.width - 8));
  panel.style.transform = `translate(${Math.round(left)}px, ${Math.round(top)}px)`;
  panel.classList.toggle("below", below);
  panel.style.setProperty("--arrow-x", Math.max(16, Math.min(pr.width - 16, r.left + Math.min(r.width, 60) / 2 - left)) + "px");
}

init().catch((e) => { console.error(e); toast("서버에 연결하지 못했어요. 잠시 뒤에 다시 열어 주세요.", true); });
