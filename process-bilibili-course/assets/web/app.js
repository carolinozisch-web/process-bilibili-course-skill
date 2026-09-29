const state = { view: "inbox", queue: "recent", reviewItems: [], selectedSource: null, units: [], tree: [], searchEventId: null };

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const labels = {
  discovered: "待处理", transcribed: "已转写", triage_ready: "待审核", approved: "已批准",
  deferred: "稍后处理", rejected: "已拒绝", curated: "已沉淀", legacy_imported: "旧资料", error: "处理失败",
  queued: "等待中", running: "处理中", succeeded: "已完成", failed: "失败",
};

const viewMeta = {
  inbox: ["收件箱", "导入公开链接，处理过程会在本机继续运行。"],
  review: ["待审核", "先判断是否值得沉淀，再加入正式知识库。"],
  knowledge: ["知识地图", "沿主题结构理解知识，也可以查看每个知识点的出处。"],
  search: ["搜索", "从提炼好的知识点和保留的原始来源中寻找答案。"],
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

function legacyQuestions(item) {
  const evidence = item.evidence_json || [];
  return [1, 2, 3].map((index) => {
    const answer = item[`point_${index}`] || "";
    const matched = evidence.filter((row) => row.question_index === index || row.point === answer || row.point === index);
    return answer ? { question: `关键要点 ${index}`, answer, evidence: matched } : null;
  }).filter(Boolean);
}

function reviewQuestions(item) {
  return (item.key_questions || []).length ? item.key_questions : legacyQuestions(item);
}

function answerStatus(question) {
  return question.answer_status || (question.answer ? "answered" : "question_only");
}

function answerStatusBadge(question) {
  const status = answerStatus(question);
  const labels = {
    answered: ["可回答", "success"],
    partial: ["部分回答", "warning"],
    question_only: ["仅提出问题", "warning"],
  };
  const [label, tone] = labels[status] || labels.question_only;
  return `<span class="badge ${tone}">${label}</span>`;
}

function questionBlock(item, question, index) {
  const evidence = question.evidence || [];
  const status = answerStatus(question);
  const evidenceHtml = evidence.length ? evidence.map((row) => {
    const time = escapeHtml(row.time || "原文");
    const link = item.platform === "bilibili" && row.source_url_at
      ? `<a class="evidence-time" href="${escapeHtml(row.source_url_at)}" target="_blank" rel="noreferrer">${time}</a>`
      : `<span class="evidence-time">${time}</span>`;
    return `<div class="evidence-item">${link}<span>${escapeHtml(row.excerpt || "对应原文" )}</span></div>`;
  }).join("") : '<div class="empty">暂无时间证据。</div>';
  const response = status === "answered"
    ? `<p>${escapeHtml(question.answer)}</p>`
    : `<p class="answer-limited">${escapeHtml(question.status_note || (status === "partial" ? "原文只提供了部分方向，不能补全为完整答案。" : "原文提出了这个问题，但没有提供可验证答案。"))}${question.answer ? `<br>${escapeHtml(question.answer)}` : ""}</p>`;
  return `<article class="question-block"><div class="question-number">${index + 1}</div><div class="question-content"><div class="question-heading"><h3>${escapeHtml(question.question)}</h3>${answerStatusBadge(question)}</div>${response}<details class="question-evidence"><summary>查看原文依据（${evidence.length}）</summary><div class="evidence-list">${evidenceHtml}</div></details></div></article>`;
}

function questionEditor(item, questions) {
  if (!questions.length) return "";
  return `<details class="question-editor"><summary>编辑候选问题</summary><form id="questionForm">${questions.map((question, index) => `
    <fieldset class="question-edit-row"><legend>问题 ${index + 1}</legend>
      <label>问题<input data-question="${index}" value="${escapeHtml(question.question)}" maxlength="80" required></label>
      <label>原文回答<textarea data-answer="${index}" rows="3" maxlength="360" ${answerStatus(question) === "answered" ? "required" : ""}>${escapeHtml(question.answer)}</textarea></label>
      <p class="candidate-status">${answerStatusBadge(question)} ${escapeHtml(question.status_note || "")}</p>
      <label class="question-keep"><input type="checkbox" data-keep="${index}" checked> 保留这条候选内容</label>
    </fieldset>`).join("")}<div class="dialog-actions"><button class="button primary" type="submit">保存问答</button></div></form></details>`;
}

function curationSelection(item, questions) {
  const eligible = questions.map((question, index) => ({ question, index }))
    .filter(({ question }) => answerStatus(question) === "answered" && question.answer);
  if (item.status !== "approved") return "";
  if (!eligible.length) {
    return `<section class="curation-selection"><h3>选择入库内容</h3><div class="empty">这条内容没有可验证的完整答案，因此暂不生成知识点。你可以保留原始来源，或返回审核后再处理。</div></section>`;
  }
  return `<section class="curation-selection"><h3>选择入库内容</h3><p>只会把勾选的“可回答”内容沉淀为知识点；其余候选会继续保留在原始来源中。</p><form id="curateQuestionForm"><div class="curation-options">${eligible.map(({ question, index }) => `
    <label class="curation-option"><input type="checkbox" data-curate-index="${index}"><span><strong>${escapeHtml(question.question)}</strong><small>${escapeHtml(question.answer)}</small></span></label>`).join("")}</div><div class="dialog-actions"><button class="button primary" type="submit">沉淀选中内容</button></div></form></section>`;
}

function renderReviewDetail(item) {
  const questions = reviewQuestions(item);
  const duplicates = item.possible_duplicates || [];
  const canReview = ["triage_ready", "deferred", "legacy_imported", "approved"].includes(item.status);
  $("#reviewDetail").innerHTML = `
    <div class="detail-header"><div><h2>${escapeHtml(item.title || "未命名内容")}</h2><a href="${escapeHtml(item.canonical_url)}" target="_blank" rel="noreferrer">打开原视频</a></div>${badge(item.status)}</div>
    <div class="detail-actions">
      ${item.status === "legacy_imported" && !item.summary_50 ? '<button class="button secondary" id="triageButton">生成速览</button>' : ""}
      ${canReview ? '<button class="button primary" data-decision="approve">批准</button><button class="button secondary" data-decision="defer">稍后处理</button><button class="button danger-text" data-decision="reject">拒绝</button>' : ""}
    </div>
    <h3>这条内容回答了什么 ${item.ai_mode ? `<span class="badge">${item.ai_mode === "ai" ? "AI 增强" : "基础提取"}</span>` : ""}</h3>
    <div class="question-list">${questions.length ? questions.map((question, index) => questionBlock(item, question, index)).join("") : '<div class="empty">尚未提取到可审核的问题。</div>'}</div>
    ${canReview ? questionEditor(item, questions) : ""}
    ${curationSelection(item, questions)}
    <h3>可能重复</h3>${duplicates.length ? `<div class="tag-row">${duplicates.map((row) => `<span class="badge">${escapeHtml(row.title || "相似来源")}</span>`).join("")}</div>` : '<div class="empty">无疑似重复。</div>'}
    <h3>逐字稿节选</h3><div class="transcript">${escapeHtml(item.transcript_excerpt || "没有可显示的逐字稿")}</div>`;
  $$("[data-decision]").forEach((button) => button.addEventListener("click", () => reviewDecision(item.id, button.dataset.decision)));
  $("#triageButton")?.addEventListener("click", async () => {
    await api(`/api/items/${item.id}/triage`, { method: "POST", body: "{}" });
    notice("已加入速览任务");
    switchView("inbox");
  });
  $("#questionForm")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const updated = questions.flatMap((question, index) => {
      if (!form.querySelector(`[data-keep="${index}"]`).checked) return [];
      return [{
        question: form.querySelector(`[data-question="${index}"]`).value.trim(),
        answer: form.querySelector(`[data-answer="${index}"]`).value.trim(),
        evidence: question.evidence || [],
        answer_status: answerStatus(question),
        status_note: question.status_note || "",
      }];
    });
    try {
      await api(`/api/items/${item.id}/questions`, { method: "PUT", body: JSON.stringify({ questions: updated }) });
      notice("问答已更新");
      await selectReview(item.id);
    } catch (error) { notice(error.message, true); }
  });
  $("#curateQuestionForm")?.addEventListener("submit", (event) => curateSelectedQuestions(event, item, questions));
}

async function reviewDecision(sourceId, decision) {
  await api(`/api/items/${sourceId}/review`, { method: "POST", body: JSON.stringify({ decision }) });
  notice({ approve: "已批准，可以提炼知识点", defer: "已移到稍后处理", reject: "已记录拒绝决定" }[decision]);
  if (decision === "approve") await selectReview(sourceId); else await loadReview();
  loadDashboard();
}

function secondsFromTime(value) {
  const parts = String(value || "").split(":").map(Number);
  if (!parts.length || parts.some((part) => !Number.isFinite(part))) return null;
  return parts.reduce((total, part) => total * 60 + part, 0);
}

async function curateSelectedQuestions(event, item, questions) {
  event.preventDefault();
  const selected = [...event.currentTarget.querySelectorAll("[data-curate-index]:checked")]
    .map((node) => questions[Number(node.dataset.curateIndex)]);
  if (!selected.length) {
    notice("请至少选择一条可回答内容", true);
    return;
  }
  const units = selected.map((question) => ({
    title: question.question,
    method_type: "",
    when_to_use: "",
    steps: [question.answer],
    constraints: [],
    common_questions: [],
    common_symptoms: [],
    keywords: item.keywords || [],
    topic_tags: item.topic_candidates || [],
    status: "approved",
    segment_start: secondsFromTime(question.evidence?.[0]?.time),
  }));
  try {
    await api(`/api/items/${item.id}/curate`, { method: "POST", body: JSON.stringify({ units }) });
    notice(`已沉淀 ${units.length} 条知识点`);
    await selectReview(item.id);
    loadDashboard();
  } catch (error) {
    notice(error.message, true);
  }
}

async function loadUnits() {
  [state.tree, state.units] = await Promise.all([api("/api/knowledge-tree"), api("/api/units")]);
  const hasKnowledge = state.units.length > 0;
  $("#unitsEmpty").hidden = hasKnowledge;
  $("#knowledgeLayout").hidden = !hasKnowledge;
  if (!hasKnowledge) return;
  renderTopicTree();
  selectTopic(state.tree[0].id);
}

function topicUnitIds(topic) {
  const ids = new Set((topic.units || []).map((unit) => unit.id));
  (topic.children || []).forEach((child) => topicUnitIds(child).forEach((id) => ids.add(id)));
  return ids;
}

function findTopic(topicId, topics = state.tree, path = []) {
  for (const topic of topics) {
    const nextPath = [...path, topic];
    if (topic.id === topicId) return { topic, path: nextPath };
    const found = findTopic(topicId, topic.children || [], nextPath);
    if (found) return found;
  }
  return null;
}

function topicBranch(topic, depth = 0) {
  const children = (topic.children || []).map((child) => topicBranch(child, depth + 1)).join("");
  const units = (topic.units || []).map((unit) => `
    <button class="tree-unit" data-unit-id="${unit.id}" data-topic-id="${topic.id}">
      <span>${escapeHtml(unit.title)}</span><span class="node-type">知识点</span>
    </button>`).join("");
  const count = topicUnitIds(topic).size;
  return `<details class="tree-branch depth-${depth}" ${depth < 2 ? "open" : ""}>
    <summary data-topic-id="${topic.id}"><span>${escapeHtml(topic.title)}</span><span class="tree-count">${count}</span></summary>
    <div class="tree-children">${children}${units}</div>
  </details>`;
}

function renderTopicTree() {
  $("#topicTree").innerHTML = state.tree.map((topic) => topicBranch(topic)).join("");
  $$("#topicTree summary").forEach((summary) => summary.addEventListener("click", () => {
    selectTopic(Number(summary.dataset.topicId));
  }));
  $$(".tree-unit").forEach((button) => button.addEventListener("click", () => {
    selectUnit(Number(button.dataset.unitId), Number(button.dataset.topicId));
  }));
}

function pathHtml(path) {
  return path.map((topic) => `<span>${escapeHtml(topic.title)}</span>`).join('<span class="path-separator">›</span>');
}

function selectTopic(topicId) {
  const found = findTopic(topicId);
  if (!found) return;
  const { topic, path } = found;
  $$("#topicTree summary").forEach((node) => node.classList.toggle("active", Number(node.dataset.topicId) === topicId));
  $$(".tree-unit").forEach((node) => node.classList.remove("active"));
  const childHtml = (topic.children || []).map((child) => `
    <button class="knowledge-index-row topic-jump" data-topic-id="${child.id}">
      <span><strong>${escapeHtml(child.title)}</strong><small>${escapeHtml(child.description || "继续展开这个主题")}</small></span>
      <span>${topicUnitIds(child).size} 个知识点</span>
    </button>`).join("");
  const unitHtml = (topic.units || []).map((unit) => `
    <button class="knowledge-index-row unit-jump" data-unit-id="${unit.id}" data-topic-id="${topic.id}">
      <span><strong>${escapeHtml(unit.title)}</strong><small>${escapeHtml(unit.when_to_use || "查看知识点内容")}</small></span>
      <span>查看</span>
    </button>`).join("");
  $("#knowledgeDetail").innerHTML = `
    <div class="knowledge-heading"><span class="badge success">主题</span><h2>${escapeHtml(topic.title)}</h2><p>${escapeHtml(topic.description || "这个主题下的知识结构。")}</p></div>
    ${childHtml ? `<h3>子主题</h3><div class="knowledge-index">${childHtml}</div>` : ""}
    ${unitHtml ? `<h3>知识点</h3><div class="knowledge-index">${unitHtml}</div>` : ""}`;
  $("#knowledgeContext").innerHTML = `
    <div class="panel-label">当前位置</div><div class="knowledge-path">${pathHtml(path)}</div>
    <div class="context-stat"><strong>${topicUnitIds(topic).size}</strong><span>个知识点</span></div>
    <p>选择左侧知识点查看完整内容、相关知识和原始出处。</p>`;
  $$(".topic-jump").forEach((button) => button.addEventListener("click", () => selectTopic(Number(button.dataset.topicId))));
  $$(".unit-jump").forEach((button) => button.addEventListener("click", () => selectUnit(Number(button.dataset.unitId), Number(button.dataset.topicId))));
}

function selectUnit(unitId, topicId) {
  const unit = state.units.find((item) => item.id === unitId);
  const found = findTopic(topicId);
  if (!unit || !found) return;
  $$("#topicTree summary").forEach((node) => node.classList.remove("active"));
  $$(".tree-unit").forEach((node) => node.classList.toggle("active",
    Number(node.dataset.unitId) === unitId && Number(node.dataset.topicId) === topicId));
  const steps = (unit.steps || []).map((step) => `<li>${escapeHtml(step)}</li>`).join("");
  const constraints = (unit.constraints || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  $("#knowledgeDetail").innerHTML = `
    <div class="knowledge-heading"><span class="badge success">${escapeHtml(unit.method_type || "知识点")}</span><h2>${escapeHtml(unit.title)}</h2><p>${escapeHtml(unit.when_to_use || "未填写适用场景")}</p></div>
    ${steps ? `<h3>核心内容</h3><ol class="knowledge-steps">${steps}</ol>` : ""}
    ${constraints ? `<h3>限制与提醒</h3><ul class="knowledge-steps">${constraints}</ul>` : ""}
    ${(unit.keywords || []).length ? `<div class="tag-row">${unit.keywords.map((tag) => `<span class="badge">${escapeHtml(tag)}</span>`).join("")}</div>` : ""}
    <button class="button small secondary" id="editSelectedUnit">编辑知识点</button>`;
  const siblings = (found.topic.units || []).filter((item) => item.id !== unitId);
  const sources = (unit.sources || []).map((source) => {
    const time = source.segment_start != null ? ` · ${Math.floor(source.segment_start / 60)}:${String(Math.floor(source.segment_start % 60)).padStart(2, "0")}` : "";
    return `<a href="${escapeHtml(source.source_url_at || source.canonical_url)}" target="_blank" rel="noreferrer">${escapeHtml(source.title || "原始来源")}${time}</a>`;
  }).join("");
  $("#knowledgeContext").innerHTML = `
    <div class="panel-label">所属主题</div><div class="knowledge-path">${pathHtml(found.path)}</div>
    ${siblings.length ? `<h3>同一主题</h3><div class="related-list">${siblings.map((item) => `<button class="related-unit" data-unit-id="${item.id}">${escapeHtml(item.title)}</button>`).join("")}</div>` : ""}
    <h3>核对出处</h3><div class="source-links">${sources || '<span class="badge warning">尚未关联来源</span>'}</div>`;
  $("#editSelectedUnit").addEventListener("click", () => openUnitDialog(unit, { sourceId: null, manual: false }));
  $$(".related-unit").forEach((button) => button.addEventListener("click", () => selectUnit(Number(button.dataset.unitId), topicId)));
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
  $("#unitDialogTitle").textContent = context.manual ? "人工确认知识点" : "编辑知识点";
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
    notice("知识点已保存并关联原始来源");
  } else {
    await api(`/api/units/${$("#unitId").value}`, { method: "PUT", body: JSON.stringify(payload) });
    notice("知识点已更新");
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
    const primaryQuestion = data.results[0]?.matched_question;
    const answerLabel = data.mode === "ai" ? "AI 综合回答" : primaryQuestion
      ? (data.results[0].status === "triage_ready" ? "待审核问答" : "视频问答")
      : "知识库直接答案";
    const fallback = data.ai_error ? `\n\nAI 增强暂不可用，已退回本地知识库。` : "";
    $("#searchAnswer").innerHTML = `<span class="badge success">${answerLabel}</span><div class="answer-copy">${escapeHtml(data.answer + fallback)}</div>`;
    const cards = data.results.filter((result) => result.kind === "knowledge_unit");
    const sources = data.results.filter((result) => result.kind === "source");
    const cardHtml = cards.map((result, index) => {
      const steps = (result.steps || []).length ? `<ol>${result.steps.map((step) => `<li>${escapeHtml(step)}</li>`).join("")}</ol>` : "";
      const constraints = (result.constraints || []).length ? `<p><strong>注意：</strong>${escapeHtml(result.constraints.join("；"))}</p>` : "";
      const links = (result.sources || []).map((source) => {
        const time = source.segment_start != null ? ` · ${Math.floor(source.segment_start / 60)}:${String(Math.floor(source.segment_start % 60)).padStart(2, "0")}` : "";
        return `<a href="${escapeHtml(source.source_url_at || source.canonical_url)}" target="_blank" rel="noreferrer">${escapeHtml(source.title || "原始来源")}${time}</a>`;
      }).join("");
      return `<article class="search-result method-result"><div class="result-meta"><span class="badge success">知识点 ${index + 1}</span></div><h3>${escapeHtml(result.title || "未命名知识点")}</h3><p>${escapeHtml(result.when_to_use || "")}</p>${steps}${constraints}${links ? `<details class="evidence-details"><summary>核对来源和时间点</summary><div class="source-links">${links}</div></details>` : ""}</article>`;
    }).join("");
    const questionSources = sources.filter((result) => result.matched_question);
    const questionHtml = questionSources.map((result) => {
      const question = result.matched_question;
      const evidence = (question.evidence || []).map((row) => {
        const time = escapeHtml(row.time || "原文");
        const source = escapeHtml(row.source_url_at || result.source_url_at || result.canonical_url);
        const label = result.platform === "bilibili" && row.time
          ? `<a class="evidence-time" href="${source}" target="_blank" rel="noreferrer">${time}</a>`
          : `<span class="evidence-time">${time}</span>`;
        return `<div class="evidence-item">${label}<span>${escapeHtml(row.excerpt || "对应原文")}</span></div>`;
      }).join("");
      return `<article class="search-result method-result"><div class="result-meta"><span class="badge ${result.status === "triage_ready" ? "warning" : "success"}">${result.status === "triage_ready" ? "待审核问答" : "视频问答"}</span></div><h3>${escapeHtml(question.question)}</h3><p>${escapeHtml(question.answer)}</p>${evidence ? `<details class="evidence-details"><summary>核对原文依据</summary><div class="evidence-list">${evidence}</div></details>` : ""}</article>`;
    }).join("");
    const rawSources = sources.filter((result) => !result.matched_question);
    const sourceHtml = rawSources.length ? `<details class="raw-results"><summary>查看原始视频匹配（${rawSources.length}）</summary><div>${rawSources.map((result) => `<article class="search-result source-result"><div class="result-meta"><span class="badge">原始来源</span></div><h3>${escapeHtml(result.title || "未命名来源")}</h3><p>${escapeHtml(result.summary_50 || "命中原始逐字稿")}</p><a href="${escapeHtml(result.source_url_at || result.canonical_url)}" target="_blank" rel="noreferrer">打开对应分集</a></article>`).join("")}</div></details>` : "";
    const hasDirectAnswer = cards.length || questionSources.length;
    const cardHeading = questionHtml ? "相关的已沉淀知识点" : "提炼好的知识点";
    $("#searchResults").innerHTML = hasDirectAnswer ? `${questionHtml ? `<h2 class="search-heading">直接问答</h2>${questionHtml}` : ""}${cards.length ? `<h2 class="search-heading">${cardHeading}</h2>${cardHtml}` : ""}${sourceHtml}<div class="feedback"><span>这个答案有帮助吗？</span><button class="button small secondary search-feedback" data-value="helpful">有帮助</button><button class="button small secondary search-feedback" data-value="not_helpful">没有</button></div>` : `<div class="empty large">知识库中没有直接答案，可以换一个更具体的问题。</div>${sourceHtml}`;
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
