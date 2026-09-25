const state = { view: "inbox", queue: "recent", reviewItems: [], selectedSource: null, units: [], searchEventId: null };

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const labels = {
  discovered: "待处理", transcribed: "已转写", triage_ready: "待审核", approved: "已批准",
  deferred: "稍后处理", rejected: "已拒绝", curated: "已沉淀", legacy_imported: "旧资料", error: "处理失败",
  queued: "等待中", running: "处理中", succeeded: "已完成", failed: "失败",
};

const viewMeta = {
  inbox: ["收件箱", "导入公开链接，处理过程会在本机继续运行。"],
  review: ["待审核", "先判断是否值得沉淀，再生成正式知识卡。"],
  knowledge: ["知识库", "可复用的方法，以及它们来自哪里。"],
  search: ["搜索", "从方法卡和保留的原始来源中寻找答案。"],
  settings: ["设置", "AI 增强可选，API Key 不会写入磁盘。"],
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.error || `请求失败 (${response.status})`);
    error.status = response.status;
    error.data = data;
    throw error;
  }
  return data;
}

function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
}

function notice(message, error = false) {
  const node = $("#notice");
  node.textContent = message;
  node.classList.toggle("error", error);
  node.hidden = false;
  clearTimeout(notice.timer);
  notice.timer = setTimeout(() => { node.hidden = true; }, 4200);
}

function badge(status) {
  const cls = ["succeeded", "curated", "approved"].includes(status) ? "success" :
    ["failed", "error", "rejected"].includes(status) ? "error" : "warning";
  return `<span class="badge ${cls}">${escapeHtml(labels[status] || status || "未知")}</span>`;
}

function formatDate(value) {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

async function switchView(view) {
  state.view = view;
  window.scrollTo({ top: 0, behavior: "auto" });
  $$(".view").forEach((node) => node.classList.toggle("active", node.id === `view-${view}`));
  $$(".nav-button").forEach((node) => node.classList.toggle("active", node.dataset.view === view));
  $("#pageTitle").textContent = viewMeta[view][0];
  $("#pageSubtitle").textContent = viewMeta[view][1];
  if (view === "review") await loadReview();
  if (view === "knowledge") await loadUnits();
  if (view === "settings") await loadSettings();
}

async function loadDashboard() {
  const [dashboard, jobs, sources, settings] = await Promise.all([
    api("/api/dashboard"), api("/api/jobs"), api("/api/items"), api("/api/settings/llm"),
  ]);
  $("#metricSources").textContent = dashboard.sources;
  $("#metricPending").textContent = dashboard.pending_review;
  $("#metricUnits").textContent = dashboard.knowledge_units;
  $("#metricFailed").textContent = dashboard.failed;
  $("#navJobs").textContent = jobs.filter((job) => ["queued", "running"].includes(job.status)).length;
  $("#navReview").textContent = dashboard.pending_review;
  $("#navUnits").textContent = dashboard.knowledge_units;
  $("#serviceDot").classList.toggle("online", settings.configured);
  $("#serviceText").textContent = settings.configured ? `AI：${settings.model}` : "基础模式";
  renderJobs(jobs);
  renderSources(sources.slice(0, 30));
}

function renderJobs(jobs) {
  $("#jobsEmpty").hidden = jobs.length > 0;
  $("#jobsTable").innerHTML = jobs.map((job) => `
    <tr>
      <td><strong>${escapeHtml(job.title || job.canonical_url)}</strong><br><small>${escapeHtml(job.message || job.error_message || "")}</small></td>
      <td>${escapeHtml(job.phase)}</td>
      <td><div class="progress"><span style="width:${Number(job.progress || 0)}%"></span></div></td>
      <td>${badge(job.status)}</td>
      <td>${job.status === "failed" ? `<button class="button small secondary retry-job" data-id="${job.id}">重试</button>` : ""}</td>
    </tr>`).join("");
  $$(".retry-job").forEach((button) => button.addEventListener("click", async () => {
    await api(`/api/jobs/${button.dataset.id}/retry`, { method: "POST", body: "{}" });
    notice("任务已重新排队");
    loadDashboard();
  }));
}

function renderSources(sources) {
  $("#sourcesEmpty").hidden = sources.length > 0;
  $("#sourcesTable").innerHTML = sources.map((source) => `
    <tr><td>${escapeHtml(source.title || source.canonical_url)}</td><td>${escapeHtml(source.platform)}</td>
    <td>${badge(source.status)}</td><td>${formatDate(source.imported_at)}</td>
    <td>${["discovered", "error"].includes(source.status) ? `<button class="button small secondary start-source" data-id="${source.id}">开始处理</button>` : ""}</td></tr>`).join("");
  $$(".start-source").forEach((button) => button.addEventListener("click", async () => {
    await api(`/api/items/${button.dataset.id}/transcribe`, { method: "POST", body: "{}" });
    notice("已加入处理队列");
    loadDashboard();
  }));
}

async function loadReview() {
  const endpoint = state.queue === "legacy" ? "/api/items?status=legacy_imported" : `/api/items?queue=${state.queue}`;
  state.reviewItems = await api(endpoint);
  const list = $("#reviewList");
  list.innerHTML = state.reviewItems.length ? state.reviewItems.map((item) => `
    <button class="review-item" data-id="${item.id}">
      <strong>${escapeHtml(item.title || item.canonical_url)}</strong>
      <p>${escapeHtml(item.summary_50 || (item.status === "legacy_imported" ? "旧资料尚未重新审核" : "等待生成速览"))}</p>
      <span class="review-meta"><span>${escapeHtml(item.platform)}</span><span>${formatDate(item.saved_at || item.imported_at)}</span></span>
    </button>`).join("") : '<div class="empty large">这个队列现在是空的。</div>';
  $$(".review-item").forEach((button) => button.addEventListener("click", () => selectReview(Number(button.dataset.id))));
  if (state.reviewItems.length) await selectReview(state.reviewItems[0].id);
  else $("#reviewDetail").innerHTML = '<div class="empty large">这个队列现在是空的。</div>';
}

async function selectReview(sourceId) {
  state.selectedSource = await api(`/api/items/${sourceId}`);
  $$(".review-item").forEach((node) => node.classList.toggle("active", Number(node.dataset.id) === sourceId));
  renderReviewDetail(state.selectedSource);
}

function renderReviewDetail(item) {
  const evidence = item.evidence_json || [];
  const duplicates = item.possible_duplicates || [];
  const canReview = ["triage_ready", "deferred", "legacy_imported", "approved"].includes(item.status);
  $("#reviewDetail").innerHTML = `
    <div class="detail-header"><div><h2>${escapeHtml(item.title || "未命名内容")}</h2><a href="${escapeHtml(item.canonical_url)}" target="_blank" rel="noreferrer">打开原视频</a></div>${badge(item.status)}</div>
    <div class="detail-actions">
      ${item.status === "legacy_imported" && !item.summary_50 ? '<button class="button secondary" id="triageButton">生成速览</button>' : ""}
      ${canReview ? '<button class="button primary" data-decision="approve">批准</button><button class="button secondary" data-decision="defer">稍后处理</button><button class="button danger-text" data-decision="reject">拒绝</button>' : ""}
      ${item.status === "approved" ? '<button class="button primary" id="curateButton">提炼方法卡</button>' : ""}
    </div>
    <h3>三点速览 ${item.ai_mode ? `<span class="badge">${item.ai_mode === "ai" ? "AI 增强" : "基础模式"}</span>` : ""}</h3>
    <div class="summary-box">${escapeHtml(item.summary_50 || "尚未生成速览")}</div>
    <h3>原文证据</h3>
    <div class="evidence-list">${evidence.length ? evidence.map((row) => `<div class="evidence-item"><span class="evidence-time">${escapeHtml(row.time || "--:--")}</span><span>${escapeHtml(row.excerpt || row.point || "")}</span></div>`).join("") : '<div class="empty">暂无时间证据。</div>'}</div>
    ${duplicates.length ? `<h3>可能重复</h3><div class="tag-row">${duplicates.map((row) => `<span class="badge">${escapeHtml(row.title || "相似来源")}</span>`).join("")}</div>` : ""}
    <h3>逐字稿节选</h3><div class="transcript">${escapeHtml(item.transcript_excerpt || "没有可显示的逐字稿")}</div>`;
  $$("[data-decision]").forEach((button) => button.addEventListener("click", () => reviewDecision(item.id, button.dataset.decision)));
  $("#triageButton")?.addEventListener("click", async () => {
    await api(`/api/items/${item.id}/triage`, { method: "POST", body: "{}" });
    notice("已加入速览任务");
    switchView("inbox");
  });
  $("#curateButton")?.addEventListener("click", () => beginCuration(item));
}

async function reviewDecision(sourceId, decision) {
  await api(`/api/items/${sourceId}/review`, { method: "POST", body: JSON.stringify({ decision }) });
  notice({ approve: "已批准，可以提炼方法卡", defer: "已移到稍后处理", reject: "已记录拒绝决定" }[decision]);
  if (decision === "approve") await selectReview(sourceId); else await loadReview();
  loadDashboard();
}

async function beginCuration(item) {
  try {
    const result = await api(`/api/items/${item.id}/curate`, { method: "POST", body: "{}" });
    if (result.job_id) {
      notice("已加入方法卡提炼任务");
      switchView("inbox");
    }
  } catch (error) {
    if (error.status === 409 && error.data.manual_required) {
      openUnitDialog(error.data.prefill, { sourceId: item.id, manual: true });
    } else {
      notice(error.message, true);
    }
  }
}

async function loadUnits() {
  state.units = await api("/api/units");
  $("#unitsEmpty").hidden = state.units.length > 0;
  $("#unitGrid").innerHTML = state.units.map((unit) => `
    <article class="unit-card">
      <div><h3>${escapeHtml(unit.title)}</h3><span class="badge success">${escapeHtml(unit.method_type || "方法")}</span></div>
      <p>${escapeHtml(unit.when_to_use || "未填写适用场景")}</p>
      <ol>${(unit.steps || []).map((step) => `<li>${escapeHtml(step)}</li>`).join("")}</ol>
      <div class="tag-row">${(unit.keywords || []).map((tag) => `<span class="badge">${escapeHtml(tag)}</span>`).join("")}</div>
      <div class="source-links">${(unit.sources || []).map((source) => `<a href="${escapeHtml(source.source_url_at || source.canonical_url)}" target="_blank" rel="noreferrer">${escapeHtml(source.title || "原始来源")}${source.segment_start != null ? ` · ${Math.floor(source.segment_start / 60)}:${String(Math.floor(source.segment_start % 60)).padStart(2, "0")}` : ""}</a>`).join("") || '<span class="badge warning">尚未关联来源</span>'}</div>
      <button class="button small secondary edit-unit" data-id="${unit.id}">编辑</button>
    </article>`).join("");
  $$(".edit-unit").forEach((button) => button.addEventListener("click", () => {
    const unit = state.units.find((item) => item.id === Number(button.dataset.id));
    openUnitDialog(unit, { sourceId: null, manual: false });
  }));
}

function openUnitDialog(unit, context) {
  const dialog = $("#unitDialog");
  dialog.dataset.sourceId = context.sourceId || "";
  dialog.dataset.manual = context.manual ? "true" : "false";
  $("#unitId").value = unit.id || "";
  $("#unitTitle").value = unit.title || "";
  $("#unitWhen").value = unit.when_to_use || "";
  $("#unitSteps").value = (unit.steps || []).join("\n");
  $("#unitConstraints").value = (unit.constraints || []).join("\n");
  $("#unitKeywords").value = (unit.keywords || []).join("，");
  $("#unitDialogTitle").textContent = context.manual ? "人工确认方法卡" : "编辑方法卡";
  dialog.showModal();
}

function unitPayload() {
  const lines = (selector) => $(selector).value.split(/\n/).map((item) => item.trim()).filter(Boolean);
  return {
    title: $("#unitTitle").value.trim(), when_to_use: $("#unitWhen").value.trim(),
    steps: lines("#unitSteps"), constraints: lines("#unitConstraints"),
    keywords: $("#unitKeywords").value.split(/[，,]/).map((item) => item.trim()).filter(Boolean),
    common_questions: [], common_symptoms: [], topic_tags: [], status: "approved",
  };
}

async function saveUnit(event) {
  event.preventDefault();
  const dialog = $("#unitDialog");
  const payload = unitPayload();
  if (dialog.dataset.manual === "true") {
    await api(`/api/items/${dialog.dataset.sourceId}/curate`, { method: "POST", body: JSON.stringify({ units: [payload] }) });
    notice("方法卡已保存并关联原始来源");
  } else {
    await api(`/api/units/${$("#unitId").value}`, { method: "PUT", body: JSON.stringify(payload) });
    notice("方法卡已更新");
  }
  dialog.close();
  await switchView("knowledge");
  loadDashboard();
}

async function runSearch(event) {
  event.preventDefault();
  const query = $("#searchInput").value.trim();
  $("#searchAnswer").hidden = false;
  $("#searchAnswer").textContent = "正在搜索...";
  try {
    const data = await api(`/api/search?q=${encodeURIComponent(query)}`);
    state.searchEventId = data.event_id;
    $("#searchAnswer").textContent = data.answer + (data.ai_error ? `\n\nAI 增强暂不可用，已退回全文检索。` : "");
    $("#searchResults").innerHTML = data.results.length ? data.results.map((result) => {
      const description = result.kind === "knowledge_unit" ? result.when_to_use : result.summary_50;
      const link = result.kind === "source" ? `<a href="${escapeHtml(result.canonical_url)}" target="_blank" rel="noreferrer">打开原视频</a>` :
        (result.sources || []).slice(0, 2).map((source) => `<a href="${escapeHtml(source.source_url_at || source.canonical_url)}" target="_blank" rel="noreferrer">${escapeHtml(source.title || "查看来源")}</a>`).join(" · ");
      return `<article class="search-result"><h3>${escapeHtml(result.title || "未命名来源")}</h3><p>${escapeHtml(description || "命中原始逐字稿")}</p>${link}</article>`;
    }).join("") + `<div class="feedback"><span>这些结果有帮助吗？</span><button class="button small secondary search-feedback" data-value="helpful">有帮助</button><button class="button small secondary search-feedback" data-value="not_helpful">没有</button></div>` : '<div class="empty large">没有找到直接答案，可以换一个更具体的关键词。</div>';
    $$(".search-feedback").forEach((button) => button.addEventListener("click", async () => {
      await api("/api/search-feedback", { method: "POST", body: JSON.stringify({ event_id: state.searchEventId, feedback: button.dataset.value }) });
      notice("已记录反馈");
    }));
  } catch (error) {
    $("#searchAnswer").textContent = error.message;
  }
}

async function loadSettings() {
  const settings = await api("/api/settings/llm");
  $("#baseUrl").value = settings.base_url || "";
  $("#modelName").value = settings.model || "";
  $("#apiKey").value = "";
  $("#llmStatus").textContent = settings.configured ? `AI 增强已启用：${settings.model}` : "当前为基础模式。";
}

async function saveSettings(event) {
  event.preventDefault();
  const payload = { base_url: $("#baseUrl").value.trim(), model: $("#modelName").value.trim(), api_key: $("#apiKey").value.trim() };
  $("#llmStatus").textContent = "正在测试连接...";
  try {
    const result = await api("/api/settings/llm/test", { method: "POST", body: JSON.stringify(payload) });
    $("#apiKey").value = "";
    $("#llmStatus").textContent = result.ok ? `连接成功：${result.model}` : "服务有响应，但测试结果不符合预期。";
    notice("模型配置已在本次运行中启用");
    loadDashboard();
  } catch (error) {
    $("#llmStatus").textContent = error.message;
  }
}

async function clearKey() {
  await api("/api/settings/llm", { method: "PUT", body: JSON.stringify({ clear_api_key: true }) });
  notice("API Key 已从内存中清除");
  loadSettings();
  loadDashboard();
}

async function refreshCurrent() {
  await loadDashboard();
  if (state.view === "review") await loadReview();
  if (state.view === "knowledge") await loadUnits();
  if (state.view === "settings") await loadSettings();
}

function bindEvents() {
  $$(".nav-button").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
  $$("[data-queue]").forEach((button) => button.addEventListener("click", async () => {
    state.queue = button.dataset.queue;
    $$("[data-queue]").forEach((node) => node.classList.toggle("active", node === button));
    await loadReview();
  }));
  $("#importForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const result = await api("/api/import", { method: "POST", body: JSON.stringify({ text: $("#importText").value, transcribe: $("#startTranscribe").checked }) });
      $("#importText").value = "";
      notice(`已导入 ${result.source_ids.length} 条内容`);
      loadDashboard();
    } catch (error) { notice(error.message, true); }
  });
  $("#searchForm").addEventListener("submit", runSearch);
  $("#settingsForm").addEventListener("submit", saveSettings);
  $("#clearKeyButton").addEventListener("click", clearKey);
  $("#refreshButton").addEventListener("click", refreshCurrent);
  $("#unitForm").addEventListener("submit", saveUnit);
  $("#closeUnitDialog").addEventListener("click", () => $("#unitDialog").close());
  $("#cancelUnit").addEventListener("click", () => $("#unitDialog").close());
}

bindEvents();
loadDashboard().catch((error) => notice(error.message, true));
setInterval(() => { if (state.view === "inbox") loadDashboard().catch(() => {}); }, 3000);
