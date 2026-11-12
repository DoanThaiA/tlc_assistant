/* Trang quản trị báo cáo ngày — JS thuần, không phụ thuộc thư viện ngoài. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const state = { me: null, overview: null, cats: [] };
const STATUS_LABEL = { kept: "Đã lưu", pending: "Chờ duyệt", discarded: "Đã loại" };
const STATUS_BADGE = { kept: "ok", pending: "warn", discarded: "danger" };

/* ------------------------------------------------------------------ */
/* Tiện ích                                                            */
/* ------------------------------------------------------------------ */
/** Tạo phần tử DOM an toàn (text luôn qua textContent, không dùng innerHTML). */
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k in el && !["list", "form", "style"].includes(k)) el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function api(method, path, body) {
  const opt = { method, credentials: "same-origin", headers: {} };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const res = await fetch(path, opt);
  let data = null;
  try { data = await res.json(); } catch { /* không phải JSON */ }
  if (res.status === 401 && path !== "/api/login") {
    showLogin();
    throw new ApiError(401, "Phiên đăng nhập đã hết hạn");
  }
  if (!res.ok) throw new ApiError(res.status, (data && data.message) || `Lỗi ${res.status}`);
  return data;
}

function qs(params) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== "" && v != null) p.set(k, v);
  const s = p.toString();
  return s ? "?" + s : "";
}

function toast(msg, isError = false) {
  const t = h("div", { class: "toast" + (isError ? " error" : "") }, msg);
  $("#toasts").append(t);
  setTimeout(() => t.remove(), isError ? 6000 : 3000);
}

function fail(e) {
  if (e instanceof ApiError && e.status === 401) return;
  toast(e.message || String(e), true);
}

const pad = (n) => String(n).padStart(2, "0");
function fmtDate(iso) {
  if (!iso) return "";
  const [y, m, d] = iso.slice(0, 10).split("-");
  return `${d}/${m}/${y}`;
}
function fmtDateTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return `${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${d.getFullYear()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
const WEEKDAYS = ["CN", "T2", "T3", "T4", "T5", "T6", "T7"];
function addDays(iso, n) {
  const d = new Date(iso + "T00:00:00");
  d.setDate(d.getDate() + n);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}
/** ISO có múi giờ -> giá trị cho <input type=datetime-local> (giờ trên máy người dùng). */
function toLocalInput(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function fromLocalInput(v) {
  if (!v) return null;
  const d = new Date(v);
  const off = -d.getTimezoneOffset();
  const sign = off >= 0 ? "+" : "-";
  return `${v}:00${sign}${pad(Math.floor(Math.abs(off) / 60))}:${pad(Math.abs(off) % 60)}`;
}

function debounce(fn, ms = 350) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

/* Danh mục phân loại + số email chờ duyệt (dùng chung các trang) */
async function loadMeta() {
  const ov = await api("GET", "/api/overview");
  state.overview = ov;
  state.cats = ov.category_list || [];
  setPendingBadge(ov.pending);
  return ov;
}
function setPendingBadge(n) {
  const b = $("#pending-count");
  b.textContent = n > 999 ? "999+" : String(n || "");
  b.hidden = !n;
}
const catLabel = (code) => (code ? code.replace(/_/g, " ") : "chưa phân loại");
function catName(code) {
  const c = state.cats.find((x) => x.code === code);
  return c ? c.name : catLabel(code);
}
function catSelect(value, attrs = {}, withEmpty = false) {
  return h("select", attrs,
    withEmpty ? h("option", { value: "" }, "Tất cả nhóm") : null,
    state.cats.map((c) => h("option", { value: c.code, selected: c.code === value, title: c.name }, catLabel(c.code))));
}
function tagChips(tags) {
  return tags && tags.length ? h("div", { class: "tags" }, tags.map((t) => h("span", { class: "tag" }, "#" + t))) : null;
}

/* ------------------------------------------------------------------ */
/* Hộp thoại                                                           */
/* ------------------------------------------------------------------ */
const modal = $("#modal");
function openModal({ title, body, footer, wide }) {
  const box = $("#modal-body");
  box.replaceChildren(
    h("div", { class: "modal-head" },
      h("h1", {}, title),
      h("button", { class: "btn ghost sm", type: "button", "aria-label": "Đóng", onclick: closeModal }, "✕")),
    h("div", { class: "modal-body" }, body),
    footer ? h("div", { class: "modal-foot" }, footer) : null,
  );
  modal.style.width = wide ? "min(920px, calc(100vw - 32px))" : "";
  if (!modal.open) modal.showModal();
}
function closeModal() { if (modal.open) modal.close(); }
modal.addEventListener("click", (e) => { if (e.target === modal) closeModal(); });

function confirmBox(title, message, okLabel = "Xoá") {
  return new Promise((resolve) => {
    let done = false;
    const finish = (v) => { if (!done) { done = true; resolve(v); closeModal(); } };
    openModal({
      title,
      body: h("p", {}, message),
      footer: [
        h("button", { class: "btn", type: "button", onclick: () => finish(false) }, "Huỷ"),
        h("button", { class: "btn danger solid", type: "button", onclick: () => finish(true) }, okLabel),
      ],
    });
    modal.addEventListener("close", () => finish(false), { once: true });
  });
}

/* ------------------------------------------------------------------ */
/* Đăng nhập                                                           */
/* ------------------------------------------------------------------ */
function showLogin() {
  closeModal();
  $("#app").hidden = true;
  $("#login").hidden = false;
  $("#login-form").password.focus();
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const err = $("#login-error");
  err.hidden = true;
  f.querySelector("button").disabled = true;
  try {
    await api("POST", "/api/login", { password: f.password.value });
    f.reset();
    await boot();
  } catch (ex) {
    err.textContent = ex.message;
    err.hidden = false;
  } finally {
    f.querySelector("button").disabled = false;
  }
});

$("#logout").addEventListener("click", async () => {
  try { await api("POST", "/api/logout", {}); } catch { /* bỏ qua */ }
  showLogin();
});

/* ------------------------------------------------------------------ */
/* Định tuyến                                                          */
/* ------------------------------------------------------------------ */
const views = {
  overview: viewOverview, pending: viewPending, reports: viewReports, employees: viewEmployees,
  rules: viewRules, imports: viewImports,
};

function route() {
  const name = (location.hash.match(/^#\/(\w+)/) || [])[1] || "overview";
  const fn = views[name] || viewOverview;
  for (const a of document.querySelectorAll("#tabs a")) a.classList.toggle("active", a.dataset.view === name);
  const root = $("#view");
  root.replaceChildren();
  fn(root).catch(fail);
}
window.addEventListener("hashchange", () => { if (!$("#app").hidden) route(); });

async function boot() {
  try {
    state.me = await api("GET", "/api/me");
  } catch (e) {
    if (e.status !== 401) fail(e);
    const hint = $("#login-hint");
    hint.textContent = "Đăng nhập để quản lý dữ liệu.";
    return;
  }
  $("#login").hidden = true;
  $("#app").hidden = false;
  try { await loadMeta(); } catch (e) { fail(e); }
  route();
}

/* ------------------------------------------------------------------ */
/* TỔNG QUAN                                                           */
/* ------------------------------------------------------------------ */
async function viewOverview(root) {
  const today = state.me.today;
  const [ov, st] = await Promise.all([
    loadMeta(),
    api("GET", "/api/stats" + qs({ date_from: addDays(today, -13), date_to: today })),
  ]);
  const lastImport = ov.recent_imports && ov.recent_imports[0];

  const card = (label, value, sub) =>
    h("div", { class: "panel panel-pad card" },
      h("div", { class: "label" }, label), h("div", { class: "value" + (String(value).length > 8 ? " sm" : "") }, value), sub ? h("div", { class: "sub" }, sub) : null);

  const subsBox = h("div", { class: "panel panel-pad" });
  const dateInput = h("input", { type: "date", value: today, max: today, class: "nowrap", onchange: () => loadSubs(dateInput.value) });

  async function loadSubs(day) {
    subsBox.replaceChildren(h("div", { class: "muted" }, "Đang tải…"));
    const s = await api("GET", "/api/submissions" + qs({ date: day }));
    const head = h("div", { class: "page-head" },
      h("h2", {}, "Tình hình nộp báo cáo"),
      h("div", { class: "actions" }, dateInput));
    const summary = h("p", { class: "muted" },
      `${s.submitted_count}/${s.employees_active} nhân viên đã nộp · ${s.total_reports} báo cáo`,
      s.is_work_day ? "" : " · không phải ngày làm việc");

    const person = (p, badge, extra) => h("li", {},
      h("div", { class: "who" }, h("div", {}, p.name || p.email), h("div", {}, [p.department, p.email].filter(Boolean).join(" · "))),
      h("div", { class: "nowrap" }, extra || null, badge));

    const submitted = h("ul", { class: "people" }, s.submitted.map((p) =>
      person(p, h("span", { class: "badge ok" }, p.first_sent_at ? fmtDateTime(p.first_sent_at).slice(11) : "Đã nộp"))));
    const missing = h("ul", { class: "people" }, s.missing.map((p) => person(p, h("span", { class: "badge danger" }, "Chưa nộp"))));
    const unknown = h("ul", { class: "people" }, s.senders_not_in_employee_list.map((p) =>
      person(p, h("span", { class: "badge warn" }, `${p.report_ids.length} báo cáo`),
        h("button", { class: "btn sm", type: "button", onclick: () => employeeForm({ email: p.email, name: p.name || "" }, () => loadSubs(day)) }, "+ Thêm NV"))));

    subsBox.replaceChildren(head, summary,
      h("div", { class: "grid two-col" },
        h("div", {}, h("h2", {}, `Chưa nộp (${s.missing_count})`),
          s.missing.length ? missing : h("p", { class: "muted" }, s.employees_active ? "Tất cả đã nộp 🎉" : "Chưa có danh sách nhân viên.")),
        h("div", {}, h("h2", {}, `Đã nộp (${s.submitted_count})`),
          s.submitted.length ? submitted : h("p", { class: "muted" }, "Chưa có ai."))),
      s.senders_not_in_employee_list.length
        ? h("div", {}, h("h2", { class: "mt" }, "Người gửi chưa có trong danh sách nhân viên"), unknown)
        : null);
  }

  // Biểu đồ 14 ngày
  const perDay = Object.fromEntries(st.per_day.map((d) => [d.report_date, d.reports]));
  const days = Array.from({ length: 14 }, (_, i) => addDays(today, i - 13));
  const max = Math.max(1, ...days.map((d) => perDay[d] || 0));
  const bars = h("div", { class: "bars" }, days.map((d) => {
    const n = perDay[d] || 0;
    const fill = h("div", { class: "fill" + (n ? "" : " zero"), title: `${fmtDate(d)}: ${n} báo cáo` });
    fill.style.height = `${Math.max(2, (n / max) * 100)}%`;
    const wd = WEEKDAYS[new Date(d + "T00:00:00").getDay()];
    return h("div", { class: "bar" }, h("span", { class: "n" }, n || ""), fill, h("span", { class: "d" }, h("span", { class: "wd" }, wd + " "), d.slice(8)));
  }));

  const mcpUrl = location.origin + (state.me.mcp_path || "/mcp");
  const urlInput = h("input", { value: mcpUrl, readOnly: true, onfocus: (e) => e.target.select() });

  root.append(
    h("div", { class: "page-head" }, h("h1", {}, "Tổng quan"),
      h("div", { class: "muted" }, `Hôm nay ${fmtDate(today)} · ${state.me.timezone}`)),
    h("div", { class: "grid cards" },
      h("a", { href: "#/pending", class: "card-link" },
        card("Chờ duyệt", ov.pending ?? 0, ov.pending ? "Claude sẽ lọc ở lượt chạy tới" : "Đã duyệt hết")),
      card("Email đã lưu", ov.total_reports ?? 0, ov.first_date ? `${fmtDate(ov.first_date)} → ${fmtDate(ov.last_date)}` : "Chưa có dữ liệu"),
      card("Đã loại bỏ", (ov.by_status && ov.by_status.discarded) ?? 0, `${ov.senders ?? 0} người gửi đã lưu`),
      card("Nhân viên theo dõi", ov.active_employees ?? 0, null)),
    h("div", { class: "grid two-col" },
      h("div", { class: "panel panel-pad" }, h("h2", {}, "Email đã lưu 14 ngày gần đây"), bars),
      h("div", { class: "panel panel-pad" }, h("h2", {}, "Theo nhóm"),
        ov.categories && ov.categories.length
          ? h("ul", { class: "cat-list" }, ov.categories.map((c) => h("li", {},
            h("a", { href: "#/reports", title: c.name, onclick: () => Object.assign(reportFilter, {
              category: c.code.startsWith("(") ? "" : c.code, status: "kept", offset: 0 }) }, catLabel(c.code)),
            h("span", { class: "badge" }, c.count))))
          : h("p", { class: "muted" }, "Chưa có email nào được lưu."))),
    h("div", { class: "spacer" }),
    subsBox,
    h("div", { class: "spacer" }),
    h("div", { class: "grid two-col" },
      h("div", { class: "panel panel-pad" },
        h("h2", {}, "Luồng hoạt động"),
        h("div", { class: "flow" }, h("span", { class: "step" }, "Gmail"), "→", h("span", { class: "step" }, "Apps Script"), "→",
          h("span", { class: "step" }, "Chờ duyệt"), "→", h("span", { class: "step" }, "Claude lọc"), "→",
          h("span", { class: "step" }, "Đã lưu / Đã loại")),
        h("p", { class: "muted small" },
          lastImport ? `Lần nhập gần nhất ${fmtDateTime(lastImport.at)}: +${lastImport.inserted} mới, ${lastImport.duplicate} trùng, ${lastImport.failed} lỗi. `
            : "Chưa có lần nhập nào. ",
          ov.latest_sent_at ? `Email mới nhất gửi lúc ${fmtDateTime(ov.latest_sent_at)}.` : "")),
      h("div", { class: "panel panel-pad" },
        h("h2", {}, "Kết nối Claude (MCP)"),
        h("p", { class: "muted" }, "Dùng URL này khi thêm Custom connector trên claude.ai / Claude Desktop, nối thêm ?key=<API key>."),
        h("div", { class: "copy-row" }, urlInput,
          h("button", { class: "btn", type: "button", onclick: async () => {
            try { await navigator.clipboard.writeText(mcpUrl); toast("Đã sao chép"); } catch { urlInput.select(); }
          } }, "Sao chép")))),
  );
  await loadSubs(today);
}

/* ------------------------------------------------------------------ */
/* BÁO CÁO                                                             */
/* ------------------------------------------------------------------ */
const reportFilter = { q: "", date_from: "", date_to: "", employee: "", source: "", status: "kept", category: "", offset: 0, limit: 50 };

async function viewReports(root) {
  const selected = new Set();
  const sources = (state.overview && state.overview.sources) || (await loadMeta()).sources || [];

  const f = reportFilter;
  const set = (k) => (e) => { f[k] = e.target.value; f.offset = 0; load(); };
  const toolbar = h("div", { class: "toolbar" },
    h("label", { class: "field grow" }, "Tìm nội dung (không cần dấu)",
      h("input", { type: "search", value: f.q, placeholder: "vd: hop dong, tre tien do…", oninput: debounce(set("q")) })),
    h("label", { class: "field" }, "Từ ngày", h("input", { type: "date", value: f.date_from, onchange: set("date_from") })),
    h("label", { class: "field" }, "Đến ngày", h("input", { type: "date", value: f.date_to, onchange: set("date_to") })),
    h("label", { class: "field grow" }, "Nhân viên / phòng ban",
      h("input", { type: "search", value: f.employee, placeholder: "tên, email, phòng ban", oninput: debounce(set("employee")) })),
    h("label", { class: "field" }, "Nhóm", catSelect(f.category, { onchange: set("category") }, true)),
    h("label", { class: "field" }, "Trạng thái",
      h("select", { onchange: set("status") },
        [["kept", "Đã lưu"], ["pending", "Chờ duyệt"], ["discarded", "Đã loại"], ["all", "Tất cả"]]
          .map(([v, l]) => h("option", { value: v, selected: v === f.status }, l)))),
    h("label", { class: "field" }, "Nguồn",
      h("select", { onchange: set("source") }, h("option", { value: "" }, "Tất cả"),
        sources.map((s) => h("option", { value: s, selected: s === f.source }, s)))),
    h("button", { class: "btn", type: "button", onclick: () => {
      Object.assign(f, { q: "", date_from: "", date_to: "", employee: "", source: "", status: "kept", category: "", offset: 0 });
      route();
    } }, "Xoá lọc"));

  const bulkbar = h("div", { class: "bulkbar", hidden: true });
  const tableBox = h("div", { class: "table-wrap" });
  const pager = h("div", { class: "pager" });

  root.append(
    h("div", { class: "page-head" }, h("h1", {}, "Email đã lưu"),
      h("div", { class: "actions" }, h("button", { class: "btn primary", type: "button", onclick: () => reportForm(null, load) }, "+ Thêm email"))),
    h("div", { class: "panel" }, toolbar, bulkbar, tableBox, pager));

  function renderBulk() {
    bulkbar.hidden = selected.size === 0;
    bulkbar.replaceChildren(
      h("span", {}, `Đã chọn ${selected.size} email`),
      h("button", { class: "btn sm", type: "button", onclick: async () => {
        if (!(await confirmBox("Loại bỏ email", `Đánh dấu ${selected.size} email là không liên quan? Nội dung sẽ bị xoá, chỉ giữ dấu vết để không nhập lại.`, "Loại bỏ"))) return;
        try {
          const r = await api("POST", "/api/reports/review", { decisions: [...selected].map((id) => ({ id, action: "discard", note: "Loại thủ công" })) });
          toast(`Đã loại ${r.discarded} email`);
          selected.clear();
          load();
        } catch (e) { fail(e); }
      } }, "Loại bỏ"),
      h("button", { class: "btn danger sm", type: "button", onclick: async () => {
        if (!(await confirmBox("Xoá email", `Xoá hẳn ${selected.size} email đã chọn khỏi CSDL? Không thể hoàn tác.`))) return;
        try {
          const r = await api("POST", "/api/reports/delete", { ids: [...selected] });
          toast(`Đã xoá ${r.deleted} email`);
          selected.clear();
          load();
        } catch (e) { fail(e); }
      } }, "Xoá đã chọn"),
      h("button", { class: "btn ghost sm", type: "button", onclick: () => { selected.clear(); load(); } }, "Bỏ chọn"));
  }

  async function load() {
    tableBox.replaceChildren(h("div", { class: "empty" }, "Đang tải…"));
    let data;
    try { data = await api("GET", "/api/reports" + qs(f)); } catch (e) { fail(e); tableBox.replaceChildren(); return; }
    renderBulk();
    if (!data.reports.length) {
      tableBox.replaceChildren(h("div", { class: "empty" }, "Không có email nào khớp bộ lọc."));
    } else {
      const allBox = h("input", { type: "checkbox", "aria-label": "Chọn tất cả",
        checked: data.reports.every((r) => selected.has(r.id)),
        onchange: (e) => {
          for (const r of data.reports) e.target.checked ? selected.add(r.id) : selected.delete(r.id);
          load();
        } });
      tableBox.replaceChildren(h("table", {},
        h("thead", {}, h("tr", {},
          h("th", { class: "check" }, allBox), h("th", {}, "Ngày"), h("th", {}, "Người gửi"), h("th", {}, "Tiêu đề / tóm tắt"),
          h("th", {}, "Nhóm"), h("th", {}, "Gửi lúc"), h("th", {}))),
        h("tbody", {}, data.reports.map((r) => h("tr", { class: "clickable", onclick: (e) => {
          if (e.target.closest("input,button")) return;
          reportDetail(r.id, load);
        } },
          h("td", { class: "check" }, h("input", { type: "checkbox", checked: selected.has(r.id), "aria-label": "Chọn",
            onchange: (e) => { e.target.checked ? selected.add(r.id) : selected.delete(r.id); renderBulk(); } })),
          h("td", { class: "nowrap" }, fmtDate(r.report_date)),
          h("td", { class: "emp" }, h("div", {}, r.employee_name),
            r.employee_name !== r.sender_email ? h("div", { class: "muted small" }, r.department || r.sender_email) : null),
          h("td", { class: "subject" }, h("div", { class: "title" }, r.subject || "(không tiêu đề)"),
            h("div", { class: "preview" }, r.summary || r.preview || (r.status === "discarded" ? `Đã loại${r.review_note ? ": " + r.review_note : ""}` : "")),
            tagChips(r.tags)),
          h("td", {},
            r.status !== "kept" ? h("span", { class: "badge " + STATUS_BADGE[r.status] }, STATUS_LABEL[r.status]) : null,
            r.category ? h("span", { class: "badge primary", title: catName(r.category) }, catLabel(r.category)) : null),
          h("td", { class: "nowrap muted" }, fmtDateTime(r.sent_at)),
          h("td", { class: "actions" },
            h("button", { class: "btn sm", type: "button", onclick: () => reportForm(r.id, load) }, "Sửa"),
            h("button", { class: "btn danger sm", type: "button", onclick: () => deleteReport(r, load) }, "Xoá")))))));
    }
    const from = data.total ? f.offset + 1 : 0;
    const to = f.offset + data.count;
    pager.replaceChildren(
      h("span", { class: "muted" }, `${from}–${to} / ${data.total} email`),
      h("div", { class: "btns" },
        h("select", { onchange: (e) => { f.limit = +e.target.value; f.offset = 0; load(); } },
          [20, 50, 100, 200].map((n) => h("option", { value: n, selected: n === f.limit }, `${n} / trang`))),
        h("button", { class: "btn sm", type: "button", disabled: f.offset === 0, onclick: () => { f.offset = Math.max(0, f.offset - f.limit); load(); } }, "‹ Trước"),
        h("button", { class: "btn sm", type: "button", disabled: to >= data.total, onclick: () => { f.offset += f.limit; load(); } }, "Sau ›")));
  }
  await load();
}

async function deleteReport(r, onDone) {
  if (!(await confirmBox("Xoá email", `Xoá hẳn email "${r.subject || "(không tiêu đề)"}" của ${r.employee_name} ngày ${fmtDate(r.report_date)}? Không thể hoàn tác.`))) return;
  try {
    await api("DELETE", `/api/reports/${r.id}`);
    toast("Đã xoá email");
    onDone && onDone();
  } catch (e) { fail(e); }
}

async function reportDetail(id, onChange) {
  let r;
  try { r = await api("GET", `/api/reports/${id}`); } catch (e) { return fail(e); }
  const row = (k, v) => (v ? [h("dt", {}, k), h("dd", {}, v)] : []);
  openModal({
    title: r.subject || "(không tiêu đề)",
    wide: true,
    body: [
      h("dl", { class: "meta" },
        row("Trạng thái", STATUS_LABEL[r.status]),
        row("Nhóm", r.category ? `${catLabel(r.category)} — ${catName(r.category)}` : null),
        row("Tóm tắt", r.summary),
        row("Tags", (r.tags || []).map((t) => "#" + t).join(" ")),
        row("Ghi chú duyệt", r.review_note),
        row("Người gửi", `${r.employee_name} <${r.sender_email}>`),
        row("Phòng ban", r.department),
        row("Ngày", fmtDate(r.report_date)),
        row("Gửi lúc", fmtDateTime(r.sent_at)),
        row("Người nhận", r.to_addrs),
        row("CC", r.cc_addrs),
        row("Đính kèm", (r.attachments || []).map((a) => a.name).join(", ")),
        row("Nhãn Gmail", r.labels),
        row("Nguồn", r.source),
        row("Mã", `#${r.id}`)),
      h("pre", { class: "body-text" }, r.body_text || "(trống)"),
    ],
    footer: [
      h("button", { class: "btn danger left", type: "button", onclick: () => deleteReport(r, onChange) }, "Xoá"),
      r.thread_id ? h("button", { class: "btn", type: "button", onclick: () => threadView(r.id) }, "Cả luồng") : null,
      h("button", { class: "btn", type: "button", onclick: closeModal }, "Đóng"),
      r.status !== "discarded" ? h("button", { class: "btn primary", type: "button", onclick: () => reportForm(r.id, onChange) }, "Sửa") : null,
    ],
  });
}

async function reportForm(id, onSaved) {
  let r = { report_date: state.me.today, sent_at: new Date().toISOString(), source: "manual" };
  if (id) {
    try { r = await api("GET", `/api/reports/${id}`); } catch (e) { return fail(e); }
  }
  const form = h("form", { class: "form-grid", id: "report-form" },
    h("label", {}, "Email người gửi *", h("input", { name: "sender_email", type: "email", required: true, value: r.sender_email || "" })),
    h("label", {}, "Tên người gửi", h("input", { name: "sender_name", value: r.sender_name || "" })),
    h("label", {}, "Ngày *", h("input", { name: "report_date", type: "date", required: true, value: r.report_date || "" })),
    h("label", {}, "Gửi lúc", h("input", { name: "sent_at", type: "datetime-local", value: toLocalInput(r.sent_at) })),
    h("label", { class: "full" }, "Tiêu đề", h("input", { name: "subject", value: r.subject || "" })),
    h("label", {}, "Nhóm", catSelect(r.category || "khac", { name: "category" })),
    h("label", {}, "Tags (cách nhau dấu phẩy)", h("input", { name: "tags", value: (r.tags || []).join(", ") })),
    h("label", { class: "full" }, "Tóm tắt", h("textarea", { name: "summary", rows: 2, value: r.summary || "" })),
    h("label", { class: "full" }, "Nội dung *", h("textarea", { name: "body_text", rows: 12, required: true, value: r.body_text || "" })),
    h("label", {}, "Người nhận", h("input", { name: "to_addrs", value: r.to_addrs || "" })),
    h("label", {}, "CC", h("input", { name: "cc_addrs", value: r.cc_addrs || "" })),
    h("label", {}, "Nguồn", h("input", { name: "source", value: r.source || "", placeholder: "manual" })));

  const save = h("button", { class: "btn primary", type: "submit", form: "report-form" }, id ? "Lưu thay đổi" : "Thêm email");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const v = Object.fromEntries(new FormData(form));
    const payload = { ...v, sent_at: fromLocalInput(v.sent_at), tags: v.tags.split(",").map((t) => t.trim()).filter(Boolean) };
    save.disabled = true;
    try {
      if (id) {
        await api("PUT", `/api/reports/${id}`, payload);
        toast("Đã lưu email");
      } else {
        payload.body = payload.body_text;
        await api("POST", "/api/reports", payload);
        toast("Đã thêm email");
      }
      closeModal();
      onSaved && onSaved();
    } catch (ex) { fail(ex); } finally { save.disabled = false; }
  });
  openModal({
    title: id ? `Sửa email #${id}` : "Thêm email",
    wide: true,
    body: form,
    footer: [h("button", { class: "btn", type: "button", onclick: closeModal }, "Huỷ"), save],
  });
}

/* ------------------------------------------------------------------ */
/* NHÂN VIÊN                                                           */
/* ------------------------------------------------------------------ */
const empFilter = { q: "", show_inactive: true };

async function viewEmployees(root) {
  const tableBox = h("div", { class: "table-wrap" });
  const countEl = h("span", { class: "muted" });
  const toolbar = h("div", { class: "toolbar" },
    h("label", { class: "field grow" }, "Tìm nhân viên",
      h("input", { type: "search", value: empFilter.q, placeholder: "tên, email, phòng ban",
        oninput: debounce((e) => { empFilter.q = e.target.value; load(); }) })),
    h("label", { class: "check-label field" },
      h("input", { type: "checkbox", checked: empFilter.show_inactive, onchange: (e) => { empFilter.show_inactive = e.target.checked; load(); } }),
      "Hiện cả người đã ngừng theo dõi"));

  root.append(
    h("div", { class: "page-head" }, h("h1", {}, "Nhân viên"),
      h("div", { class: "actions" },
        h("button", { class: "btn", type: "button", onclick: () => importEmployees(load) }, "Nhập CSV"),
        h("button", { class: "btn primary", type: "button", onclick: () => employeeForm(null, load) }, "+ Thêm nhân viên"))),
    h("p", { class: "muted" }, "Danh sách người phải nộp báo cáo hằng ngày — dùng để biết ai chưa nộp. Xoá nhân viên không xoá báo cáo đã lưu."),
    h("div", { class: "panel" }, toolbar, tableBox, h("div", { class: "pager" }, countEl)));

  async function load() {
    let list;
    try {
      list = await api("GET", "/api/employees" + qs({ q: empFilter.q, include_inactive: empFilter.show_inactive ? 1 : 0 }));
    } catch (e) { return fail(e); }
    countEl.textContent = `${list.length} nhân viên`;
    if (!list.length) {
      tableBox.replaceChildren(h("div", { class: "empty" }, empFilter.q ? "Không tìm thấy." : "Chưa có nhân viên. Bấm “+ Thêm nhân viên” hoặc “Nhập CSV”."));
      return;
    }
    tableBox.replaceChildren(h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Họ tên"), h("th", {}, "Email"), h("th", {}, "Phòng ban"),
        h("th", {}, "Báo cáo"), h("th", {}, "Gần nhất"), h("th", {}, "Trạng thái"), h("th", {}))),
      h("tbody", {}, list.map((e) => h("tr", {},
        h("td", {}, h("strong", {}, e.name)),
        h("td", {}, e.email),
        h("td", {}, e.department || h("span", { class: "muted" }, "—")),
        h("td", {}, h("a", { href: "#/reports", onclick: () => { Object.assign(reportFilter, { employee: e.email, offset: 0 }); } }, String(e.report_count))),
        h("td", { class: "nowrap" }, e.last_report_date ? fmtDate(e.last_report_date) : h("span", { class: "muted" }, "—")),
        h("td", {}, h("label", { class: "check-label" },
          h("input", { type: "checkbox", checked: !!e.active, onchange: async (ev) => {
            try {
              await api("PUT", `/api/employees/${e.id}`, { active: ev.target.checked });
              toast(ev.target.checked ? `Đang theo dõi ${e.name}` : `Ngừng theo dõi ${e.name}`);
              load();
            } catch (ex) { ev.target.checked = !ev.target.checked; fail(ex); }
          } }),
          h("span", { class: "badge " + (e.active ? "ok" : "") }, e.active ? "Theo dõi" : "Ngừng"))),
        h("td", { class: "actions" },
          h("button", { class: "btn sm", type: "button", onclick: () => employeeForm(e, load) }, "Sửa"),
          h("button", { class: "btn danger sm", type: "button", onclick: async () => {
            if (!(await confirmBox("Xoá nhân viên", `Xoá ${e.name} (${e.email}) khỏi danh sách? ${e.report_count} báo cáo đã lưu vẫn được giữ lại.`))) return;
            try { await api("DELETE", `/api/employees/${e.id}`); toast("Đã xoá nhân viên"); load(); } catch (ex) { fail(ex); }
          } }, "Xoá")))))));
  }
  await load();
}

function employeeForm(e, onSaved) {
  const isEdit = !!(e && e.id);
  const v = e || {};
  const form = h("form", { class: "form-grid", id: "emp-form" },
    h("label", {}, "Họ tên *", h("input", { name: "name", required: true, value: v.name || "" })),
    h("label", {}, "Email *", h("input", { name: "email", type: "email", required: true, value: v.email || "" })),
    h("label", { class: "full" }, "Phòng ban", h("input", { name: "department", value: v.department || "" })),
    h("label", { class: "check-label full" }, h("input", { name: "active", type: "checkbox", checked: v.active !== 0 && v.active !== false }),
      "Theo dõi việc nộp báo cáo"));
  const save = h("button", { class: "btn primary", type: "submit", form: "emp-form" }, isEdit ? "Lưu" : "Thêm");
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const el = (n) => form.elements.namedItem(n);
    const payload = { name: el("name").value, email: el("email").value, department: el("department").value, active: el("active").checked };
    save.disabled = true;
    try {
      if (isEdit) await api("PUT", `/api/employees/${e.id}`, payload);
      else await api("POST", "/api/employees", payload);
      toast(isEdit ? "Đã lưu nhân viên" : "Đã thêm nhân viên");
      closeModal();
      onSaved && onSaved();
    } catch (ex) { fail(ex); } finally { save.disabled = false; }
  });
  openModal({
    title: isEdit ? "Sửa nhân viên" : "Thêm nhân viên",
    body: form,
    footer: [h("button", { class: "btn", type: "button", onclick: closeModal }, "Huỷ"), save],
  });
  setTimeout(() => form.elements.namedItem("name").focus(), 50);
}

function importEmployees(onDone) {
  const ta = h("textarea", { rows: 10, placeholder: "email,name,department\nan.nguyen@congty.vn,Nguyễn Văn An,Kinh doanh" });
  const file = h("input", { type: "file", accept: ".csv,text/csv", onchange: async () => {
    if (file.files[0]) ta.value = await file.files[0].text();
  } });
  const result = h("div");
  const go = h("button", { class: "btn primary", type: "button", onclick: async () => {
    go.disabled = true;
    try {
      const r = await api("POST", "/api/employees/import", { csv: ta.value });
      toast(`Đã nhập ${r.imported} nhân viên`);
      result.replaceChildren(
        h("p", {}, `Đã nhập/cập nhật ${r.imported} nhân viên.`),
        r.errors.length ? h("ul", { class: "error" }, r.errors.map((x) => h("li", {}, `Dòng ${x.line}: ${x.error}`))) : null);
      onDone && onDone();
      if (!r.errors.length) closeModal();
    } catch (ex) { fail(ex); } finally { go.disabled = false; }
  } }, "Nhập");
  openModal({
    title: "Nhập nhân viên từ CSV",
    body: [
      h("p", { class: "muted" }, "Mỗi dòng: email,họ tên,phòng ban. Dòng tiêu đề là tuỳ chọn. Email đã có sẽ được cập nhật."),
      h("label", {}, "Chọn file CSV", file),
      h("div", { class: "spacer" }),
      h("label", {}, "hoặc dán nội dung", ta),
      result,
    ],
    footer: [h("button", { class: "btn", type: "button", onclick: closeModal }, "Đóng"), go],
  });
}

/* ------------------------------------------------------------------ */
/* NHẬT KÝ NHẬP                                                        */
/* ------------------------------------------------------------------ */
async function viewImports(root) {
  const data = await api("GET", "/api/imports?limit=200");
  root.append(
    h("div", { class: "page-head" }, h("h1", {}, "Nhật ký nhập dữ liệu"), h("span", { class: "muted" }, `${data.total} lần nhập`)),
    h("p", { class: "muted" }, "Mỗi lần Apps Script / Claude đẩy một lô email vào hệ thống."),
    h("div", { class: "panel table-wrap" },
      data.items.length
        ? h("table", {},
          h("thead", {}, h("tr", {}, h("th", {}, "Thời điểm"), h("th", {}, "Nguồn"), h("th", {}, "Nhận"),
            h("th", {}, "Mới"), h("th", {}, "Trùng"), h("th", {}, "Lỗi"))),
          h("tbody", {}, data.items.map((x) => h("tr", {},
            h("td", { class: "nowrap" }, fmtDateTime(x.at)),
            h("td", {}, x.source ? h("span", { class: "badge" }, x.source) : "—"),
            h("td", {}, x.received),
            h("td", {}, x.inserted ? h("span", { class: "badge ok" }, `+${x.inserted}`) : "0"),
            h("td", { class: "muted" }, x.duplicate),
            h("td", {}, x.failed ? h("span", { class: "badge danger" }, x.failed) : "0")))))
        : h("div", { class: "empty" }, "Chưa có lần nhập nào.")));
}

/* ------------------------------------------------------------------ */
/* LUỒNG HỘI THOẠI                                                     */
/* ------------------------------------------------------------------ */
async function threadView(id) {
  let t;
  try { t = await api("GET", `/api/reports/${id}/thread`); } catch (e) { return fail(e); }
  openModal({
    title: `Luồng hội thoại (${t.count} email)`,
    wide: true,
    body: t.emails.map((e) => h("div", { class: "mt" },
      h("div", {}, h("strong", {}, e.employee_name), h("span", { class: "muted" }, ` · ${fmtDateTime(e.sent_at)} · `),
        h("span", { class: "badge " + STATUS_BADGE[e.status] }, STATUS_LABEL[e.status])),
      h("div", { class: "muted small" }, e.subject),
      h("pre", { class: "body-text" }, e.body_text || "(trống)"))),
    footer: [h("button", { class: "btn", type: "button", onclick: closeModal }, "Đóng")],
  });
}

/* ------------------------------------------------------------------ */
/* CHỜ DUYỆT                                                           */
/* ------------------------------------------------------------------ */
async function viewPending(root) {
  const selected = new Set();
  const bulkCat = catSelect("khac", { "aria-label": "Nhóm khi giữ" });
  const bulkbar = h("div", { class: "bulkbar", hidden: true });
  const tableBox = h("div", { class: "table-wrap" });
  const countEl = h("span", { class: "muted" });

  root.append(
    h("div", { class: "page-head" }, h("h1", {}, "Chờ duyệt"),
      h("div", { class: "actions" }, h("button", { class: "btn", type: "button", onclick: () => load() }, "Tải lại"))),
    h("p", { class: "muted" },
      "Email do Apps Script lấy từ Gmail. Claude Desktop (tác vụ định kỳ, prompt “loc_email_moi”) sẽ tự giữ email công việc và loại email không liên quan theo ",
      h("a", { href: "#/rules" }, "quy tắc lọc"), ". Bạn cũng có thể duyệt tay ở đây."),
    h("div", { class: "panel" }, bulkbar, tableBox, h("div", { class: "pager" }, countEl)));

  async function decide(decisions) {
    try {
      const r = await api("POST", "/api/reports/review", { decisions });
      toast(`Giữ ${r.kept} · loại ${r.discarded}` + (r.error ? ` · ${r.error} lỗi` : ""), !!r.error);
      selected.clear();
      setPendingBadge(r.pending_remaining);
      load();
    } catch (e) { fail(e); }
  }

  function renderBulk() {
    bulkbar.hidden = selected.size === 0;
    bulkbar.replaceChildren(
      h("span", {}, `Đã chọn ${selected.size}`),
      bulkCat,
      h("button", { class: "btn sm", type: "button", onclick: () =>
        decide([...selected].map((id) => ({ id, action: "keep", category: bulkCat.value }))) }, "Giữ"),
      h("button", { class: "btn danger sm", type: "button", onclick: () =>
        decide([...selected].map((id) => ({ id, action: "discard", note: "Loại thủ công" }))) }, "Loại bỏ"));
  }

  async function load() {
    tableBox.replaceChildren(h("div", { class: "empty" }, "Đang tải…"));
    let data;
    try { data = await api("GET", "/api/reports" + qs({ status: "pending", limit: 200 })); } catch (e) { return fail(e); }
    setPendingBadge(data.total);
    countEl.textContent = data.total > data.count ? `Hiển thị ${data.count} / ${data.total} email chờ duyệt` : `${data.total} email chờ duyệt`;
    renderBulk();
    if (!data.reports.length) {
      tableBox.replaceChildren(h("div", { class: "empty" }, "Không còn email chờ duyệt 🎉"));
      return;
    }
    const allBox = h("input", { type: "checkbox", "aria-label": "Chọn tất cả",
      checked: data.reports.every((r) => selected.has(r.id)),
      onchange: (e) => { for (const r of data.reports) e.target.checked ? selected.add(r.id) : selected.delete(r.id); load(); } });
    tableBox.replaceChildren(h("table", {},
      h("thead", {}, h("tr", {}, h("th", { class: "check" }, allBox), h("th", {}, "Gửi lúc"), h("th", {}, "Người gửi"),
        h("th", {}, "Tiêu đề / nội dung"), h("th", {}))),
      h("tbody", {}, data.reports.map((r) => {
        const sel = catSelect("khac", { "aria-label": "Nhóm" });
        return h("tr", { class: "clickable", onclick: (e) => { if (!e.target.closest("input,button,select")) reportDetail(r.id, load); } },
          h("td", { class: "check" }, h("input", { type: "checkbox", checked: selected.has(r.id), "aria-label": "Chọn",
            onchange: (e) => { e.target.checked ? selected.add(r.id) : selected.delete(r.id); renderBulk(); } })),
          h("td", { class: "nowrap muted" }, fmtDateTime(r.sent_at)),
          h("td", { class: "emp" }, h("div", {}, r.employee_name),
            r.employee_name !== r.sender_email ? h("div", { class: "muted small" }, r.sender_email) : null),
          h("td", { class: "subject" }, h("div", { class: "title" }, r.subject || "(không tiêu đề)"), h("div", { class: "preview" }, r.preview)),
          h("td", {}, h("div", { class: "review-actions" }, sel,
            h("button", { class: "btn sm", type: "button", onclick: () => decide([{ id: r.id, action: "keep", category: sel.value }]) }, "Giữ"),
            h("button", { class: "btn danger sm", type: "button", onclick: () => decide([{ id: r.id, action: "discard", note: "Loại thủ công" }]) }, "Loại"))));
      }))));
  }
  await load();
}

/* ------------------------------------------------------------------ */
/* QUY TẮC LỌC                                                         */
/* ------------------------------------------------------------------ */
async function viewRules(root) {
  const data = await api("GET", "/api/rules");
  const rulesTa = h("textarea", { rows: 18, class: "mono", value: data.filter_rules });
  const catsTa = h("textarea", { rows: 12, class: "mono",
    value: data.categories.map((c) => `${c.code} | ${c.name}`).join("\n") });
  const save = h("button", { class: "btn primary", type: "button", onclick: async () => {
    const categories = catsTa.value.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => {
      const [code, ...rest] = l.split("|");
      return { code: code.trim(), name: rest.join("|").trim() || code.trim() };
    });
    save.disabled = true;
    try {
      const r = await api("PUT", "/api/rules", { filter_rules: rulesTa.value, categories });
      state.cats = r.categories;
      catsTa.value = r.categories.map((c) => `${c.code} | ${c.name}`).join("\n");
      toast("Đã lưu quy tắc lọc");
    } catch (e) { fail(e); } finally { save.disabled = false; }
  } }, "Lưu quy tắc");

  root.append(
    h("div", { class: "page-head" }, h("h1", {}, "Quy tắc lọc"),
      h("div", { class: "actions" },
        h("button", { class: "btn", type: "button", onclick: () => { rulesTa.value = data.default_rules; } }, "Dùng quy tắc mặc định"),
        save)),
    h("p", { class: "muted" }, "Claude đọc quy tắc này mỗi lần lọc email chờ duyệt (tool get_pending_emails). ",
      "Bạn cũng có thể nói với Claude: “từ nay giữ mọi email từ @doitac.vn” — Claude sẽ tự cập nhật bằng update_filter_rules."),
    h("div", { class: "grid two-col" },
      h("div", { class: "panel panel-pad" }, h("h2", {}, "Tiêu chí giữ / loại"), rulesTa),
      h("div", { class: "panel panel-pad" }, h("h2", {}, "Nhóm phân loại"),
        h("p", { class: "muted small" }, "Mỗi dòng: mã | mô tả. Mã viết không dấu, ví dụ: khach_hang | Khách hàng, đối tác. ",
          "Nhóm “bao_cao_ngay” dùng để kiểm tra ai đã nộp báo cáo."),
        catsTa)));
}

boot();
