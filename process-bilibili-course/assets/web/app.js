const state = { view: "inbox", queue: "recent", reviewItems: [], selectedSource: null, units: [], tree: [], searchEventId: null, organizationPlan: null };

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const labels = {
  discovered: "待处理", transcribed: "已转写", triage_ready: "待审核", approved: "已批准",
  deferred: "稍后处理", rejected: "已拒绝", curated: "已沉淀", legacy_imported: "旧资料", error: "处理失败",
  queued: "等待中", running: "处理中", succeeded: "已完成", failed: "失败",
};

const viewMeta = {
  inbox: ["收件箱", "导入公开链接，处理过程会在本机继续运行。"],
  review: ["审核与沉淀", "批准后继续生成知识节点；完成前不会从流程中消失。"],
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
      <td>${escapeHtml(job.phase === "waiting_for_model" ? "等待模型恢复" : job.phase)}</td>
      <td><div class="progress"><span style="width:${Number(job.progress || 0)}%"></span></div></td>
      <td>${job.phase === "waiting_for_model" ? '<span class="badge warning">自动重试中</span>' : badge(job.status)}</td>
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
    </button>`).join("") : `<div class="empty large">${state.queue === "approved" ? "没有等待沉淀的内容。" : "这个队列现在是空的。"}</div>`;
  $$(".review-item").forEach((button) => button.addEventListener("click", () => selectReview(Number(button.dataset.id))));
  if (state.reviewItems.length) await selectReview(state.reviewItems[0].id);
  else $("#reviewDetail").innerHTML = `<div class="empty large">${state.queue === "approved" ? "没有等待沉淀的内容。" : "这个队列现在是空的。"}</div>`;
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
  if ((item.key_questions || []).length) return item.key_questions;
  return item.ai_mode === "basic" ? [] : legacyQuestions(item);
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

function basicClueBlock(item, clue, index) {
  const time = escapeHtml(clue.time || "原文片段");
  const link = item.platform === "bilibili" && clue.source_url_at
    ? `<a class="evidence-time" href="${escapeHtml(clue.source_url_at)}" target="_blank" rel="noreferrer">${time}</a>`
    : `<span class="evidence-time">${time}</span>`;
  return `<article class="question-block basic-clue"><div class="question-number">${index + 1}</div><div class="question-content"><div class="question-heading"><h3>可能相关的原文片段</h3><span class="badge">基础线索</span></div><p>${link} ${escapeHtml(clue.excerpt)}</p></div></article>`;
}

function outlineBlock(item, section, index) {
  const evidence = section.evidence || [];
  const evidenceHtml = evidence.map((row) => {
    const time = escapeHtml(row.time || "原文");
    const link = item.platform === "bilibili" && row.source_url_at
      ? `<a class="evidence-time" href="${escapeHtml(row.source_url_at)}" target="_blank" rel="noreferrer">${time}</a>`
      : `<span class="evidence-time">${time}</span>`;
    return `<div class="evidence-item">${link}<span>${escapeHtml(row.excerpt || "对应原文")}</span></div>`;
  }).join("") || '<div class="empty">暂无时间证据。</div>';
  const points = (section.points || []).map((point) => `<li>${escapeHtml(point)}</li>`).join("");
  return `<article class="outline-block"><div class="question-number">${index + 1}</div><div class="question-content"><h3>${escapeHtml(section.title)}</h3><p>${escapeHtml(section.summary)}</p>${points ? `<ul class="outline-points">${points}</ul>` : ""}<details class="question-evidence"><summary>核对原文依据（${evidence.length}）</summary><div class="evidence-list">${evidenceHtml}</div></details></div></article>`;
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

function curationSelection(item) {
  if (!["approved", "curated"].includes(item.status)) return "";
  if ((item.units || []).length) {
    return `<section class="curation-selection"><h3>知识节点</h3><div class="empty">已从这条视频沉淀 ${item.units.length} 个知识节点，可在知识库中归入主题。</div></section>`;
  }
  const hasCompleteOutline = (item.video_outline?.sections || []).length > 0;
  if (!hasCompleteOutline) {
    return '<section class="curation-selection"><h3>沉淀知识</h3><div class="empty">当前只有基础原文线索，不能直接生成知识节点。请先重新提取完整结构，或人工补充知识点。</div></section>';
  }
  return `<section class="curation-selection"><h3>沉淀知识</h3><p>批准后可从这条视频的完整结构中提取知识节点，再将它们归入主题树。</p><button class="button primary" id="curateButton">生成知识节点</button></section>`;
}

function renderReviewDetail(item) {
  const outline = item.video_outline || {};
  const sections = outline.sections || [];
  const basicOnly = item.ai_mode === "basic" && !sections.length;
  const clues = basicOnly ? item.basic_clues || [] : [];
  const duplicates = item.possible_duplicates || [];
  const canReview = ["triage_ready", "deferred", "legacy_imported", "approved"].includes(item.status);
  const canRegenerate = ["triage_ready", "deferred", "legacy_imported", "approved"].includes(item.status);
  const regenerateLabel = item.status === "legacy_imported" && !item.summary_50 ? "生成速览" : "重新提取";
  $("#reviewDetail").innerHTML = `
    <div class="detail-header"><div><h2>${escapeHtml(item.title || "未命名内容")}</h2><a href="${escapeHtml(item.canonical_url)}" target="_blank" rel="noreferrer">打开原视频</a></div>${badge(item.status)}</div>
    <div class="detail-actions">
      ${canRegenerate ? `<button class="button secondary" id="triageButton">${regenerateLabel}</button>` : ""}
      ${canReview ? '<button class="button primary" data-decision="approve">批准</button><button class="button secondary" data-decision="defer">稍后处理</button><button class="button danger-text" data-decision="reject">拒绝</button>' : ""}
    </div>
    <h3>${basicOnly ? "基础原文线索" : "这条视频在讲什么"} ${item.ai_mode ? `<span class="badge">${item.ai_mode === "ai" ? "AI 增强" : "基础提取"}</span>` : ""}</h3>
    ${basicOnly ? '<p class="basic-notice">当前只保留了基础原文线索，尚未生成完整视频结构，因此不能直接沉淀知识节点。</p>' : ""}
    ${!basicOnly && outline.overview ? `<div class="video-overview">${escapeHtml(outline.overview)}</div>` : ""}
    <div class="question-list">${basicOnly ? (clues.length ? clues.map((clue, index) => basicClueBlock(item, clue, index)).join("") : '<div class="empty">没有定位到明显相关的原文片段。</div>') : (sections.length ? sections.map((section, index) => outlineBlock(item, section, index)).join("") : '<div class="empty">尚未提取到完整的视频结构。</div>')}</div>
    ${curationSelection(item)}
    <h3>可能重复</h3>${duplicates.length ? `<div class="tag-row">${duplicates.map((row) => `<span class="badge">${escapeHtml(row.title || "相似来源")}</span>`).join("")}</div>` : '<div class="empty">无疑似重复。</div>'}
    <h3>逐字稿节选</h3><div class="transcript">${escapeHtml(item.transcript_excerpt || "没有可显示的逐字稿")}</div>`;
  $$("[data-decision]").forEach((button) => button.addEventListener("click", () => reviewDecision(item.id, button.dataset.decision)));
  $("#triageButton")?.addEventListener("click", async () => {
    await api(`/api/items/${item.id}/triage`, { method: "POST", body: "{}" });
    notice("已加入重新提取任务");
    switchView("inbox");
  });
 $("#curateButton")?.addEventListener("click", async () => {
    try {
      const result = await api(`/api/items/${item.id}/curate`, { method: "POST", body: "{}" });
      notice(`已生成 ${result.unit_ids?.length || 0} 个知识节点`);
      await switchView("knowledge");
      loadDashboard();
    } catch (error) { notice(error.message, true); }
  });
}

async function reviewDecision(sourceId, decision) {
  await api(`/api/items/${sourceId}/review`, { method: "POST", body: JSON.stringify({ decision }) });
  if (decision === "approve") {
    state.queue = "approved";
    $$('[data-queue]').forEach((node) => node.classList.toggle("active", node.dataset.queue === "approved"));
    notice("已批准，下一步请生成知识节点");
    await loadReview();
  } else {
    notice({ defer: "已移到稍后处理", reject: "已记录拒绝决定" }[decision]);
    await loadReview();
  }
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

function flattenTopics(topics = state.tree, path = []) {
  return topics.flatMap((topic) => {
    if (topic.id === 0) return [];
    const current = [...path, topic];
    return [{ id: topic.id, label: current.map((item) => item.title).join(" › ") },
      ...flattenTopics(topic.children || [], current)];
  });
}

function topicOptions(includeRoot = false) {
  const root = includeRoot ? "<option value=\"\">作为根主题</option>" : "";
  return root + flattenTopics().map((topic) =>
    "<option value=\"" + topic.id + "\">" + escapeHtml(topic.label) + "</option>"
  ).join("");
}

const organizationTypeLabels = {
  action_playbook: "行动指南", topic_dossier: "专题档案", learning_map: "课程地图", reference: "资料索引",
};

function topicOptionMarkup() {
  return flattenTopics().map((topic) => `<option value="${topic.id}">${escapeHtml(topic.label)}</option>`).join("");
}

async function openOrganizationDialog() {
  const dialog = $("#organizationDialog");
  const sources = await api("/api/organization-sources");
  $("#organizationSource").innerHTML = sources.length
    ? sources.map((source) => `<option value="${source.id}">${escapeHtml(source.title)}（${source.unit_count} 个知识点）</option>`).join("")
    : "";
  $("#generateOrganizationButton").disabled = !sources.length;
  $("#organizationStatus").textContent = sources.length ? "" : "还没有可归档的视频。";
  $("#organizationProposal").hidden = true;
  $("#applyOrganizationButton").disabled = true;
  state.organizationPlan = null;
  dialog.showModal();
}

function syncProposalRootFields() {
  const existing = $("#proposalRootMode").value === "existing";
  $("#proposalExistingFields").hidden = !existing;
  $("#proposalNewFields").hidden = existing;
}

function branchText(branches) {
  return (branches || []).map((branch) => (branch.path || []).join(" > ")).filter(Boolean).join("\n");
}

function parseBranches() {
  const seen = new Set();
  return $("#proposalBranches").value.split(/\n/).map((line) => line.split(">").map((part) => part.trim()).filter(Boolean).slice(0, 3))
    .filter((path) => path.length && !seen.has(path.join("\u0000")) && (seen.add(path.join("\u0000")), true))
    .map((path) => ({ path, description: "" }));
}

function renderOrganizationProposal(plan, mode) {
  state.organizationPlan = plan;
  $("#organizationProposal").hidden = false;
  $("#applyOrganizationButton").disabled = false;
  $("#proposalType").textContent = organizationTypeLabels[plan.architecture_type] || "归档建议";
  $("#proposalReason").textContent = plan.reason || "这是一份待确认的结构建议。";
  $("#proposalRootMode").value = plan.root?.mode === "existing" ? "existing" : "new";
  $("#proposalExistingTopic").innerHTML = topicOptionMarkup();
  if (plan.root?.existing_topic_id) $("#proposalExistingTopic").value = String(plan.root.existing_topic_id);
  $("#proposalRootTitle").value = plan.root?.title || "";
  $("#proposalRootDescription").value = plan.root?.description || "";
  $("#proposalBranches").value = branchText(plan.branches);
  const pathsByUnit = new Map((plan.assignments || []).map((item) => [item.unit_id, (item.path || []).join(" > ") || "主题根目录"]));
  const units = (state.organizationSourceUnits || []);
  $("#proposalAssignments").innerHTML = `<h3>知识点去向</h3>${units.map((unit) => `<div><strong>${escapeHtml(unit.title)}</strong><span>${escapeHtml(pathsByUnit.get(unit.id) || "主题根目录")}</span></div>`).join("")}`;
  $("#organizationStatus").textContent = mode === "ai" ? "已根据视频结构生成建议。" : "AI 暂不可用，已按视频结构生成可编辑建议。";
  syncProposalRootFields();
}

async function generateOrganizationProposal() {
  const sourceId = Number($("#organizationSource").value);
  if (!sourceId) return;
  $("#generateOrganizationButton").disabled = true;
  $("#organizationStatus").textContent = "正在分析这条视频的整体结构...";
  try {
    const source = await api(`/api/items/${sourceId}`);
    state.organizationSourceUnits = source.units || [];
    const data = await api(`/api/items/${sourceId}/organization-proposal`, { method: "POST", body: "{}" });
    renderOrganizationProposal(data.plan, data.mode);
  } catch (error) {
    $("#organizationStatus").textContent = error.message;
  } finally {
    $("#generateOrganizationButton").disabled = false;
  }
}

function editedOrganizationPlan() {
  const plan = structuredClone(state.organizationPlan);
  plan.root = {
    mode: $("#proposalRootMode").value,
    title: $("#proposalRootTitle").value.trim(),
    description: $("#proposalRootDescription").value.trim(),
  };
  if (plan.root.mode === "existing") plan.root.existing_topic_id = Number($("#proposalExistingTopic").value);
  const branches = parseBranches();
  const validPaths = new Set(branches.map((branch) => branch.path.join("\u0000")));
  plan.branches = branches;
  plan.assignments = (plan.assignments || []).map((item) => ({
    ...item, path: validPaths.has((item.path || []).join("\u0000")) ? item.path : [],
  }));
  return plan;
}

async function applyOrganizationPlan(event) {
  event.preventDefault();
  if (!state.organizationPlan) return;
  const sourceId = Number($("#organizationSource").value);
  const plan = editedOrganizationPlan();
  if (plan.root.mode === "new" && !plan.root.title) {
    $("#organizationStatus").textContent = "请填写主题名称。";
    return;
  }
  $("#applyOrganizationButton").disabled = true;
  try {
    const result = await api(`/api/items/${sourceId}/organization`, { method: "POST", body: JSON.stringify({ plan }) });
    $("#organizationDialog").close();
    await loadUnits();
    selectTopic(result.root_id);
    notice(`已归入知识库：${result.unit_ids.length} 个知识点`);
  } catch (error) {
    $("#organizationStatus").textContent = error.message;
  } finally {
    $("#applyOrganizationButton").disabled = false;
  }
}

function syncTopicDialog() {
  const isNew = $("#topicMode").value === "new";
  $("#existingTopicFields").hidden = isNew;
  $("#newTopicFields").hidden = !isNew;
}

function openTopicDialog(unit = null) {
  $("#topicUnitId").value = unit?.id || "";
  $("#topicDialogTitle").textContent = unit ? "归入主题：" + unit.title : "新建主题";
  $("#existingTopicSelect").innerHTML = topicOptions();
  $("#newTopicParent").innerHTML = topicOptions(true);
  $("#newTopicTitle").value = "";
  $("#newTopicDescription").value = "";
  const hasExisting = flattenTopics().length > 0;
  $("#topicMode").value = unit && hasExisting ? "existing" : "new";
  $("#topicMode").disabled = !unit;
  syncTopicDialog();
  $("#topicDialog").showModal();
}

async function saveTopic(event) {
  event.preventDefault();
  const unitId = Number($("#topicUnitId").value) || null;
  let topicId;
  if ($("#topicMode").value === "new") {
    const title = $("#newTopicTitle").value.trim();
    if (!title) {
      notice("请填写主题名称", true);
      return;
    }
    const result = await api("/api/topics", { method: "POST", body: JSON.stringify({
      title, description: $("#newTopicDescription").value.trim(),
      parent_id: $("#newTopicParent").value || null,
    }) });
    topicId = result.id;
  } else {
    topicId = Number($("#existingTopicSelect").value);
    if (!topicId) {
      notice("请选择一个主题", true);
      return;
    }
  }
  if (unitId) {
    await api("/api/units/" + unitId + "/topic", {
      method: "PUT", body: JSON.stringify({ topic_id: topicId }),
    });
  }
  $("#topicDialog").close();
  await loadUnits();
  if (unitId) selectUnit(unitId, topicId);
  else selectTopic(topicId);
  notice(unitId ? "知识点已归入主题" : "主题已创建");
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

function inlineSourceLinks(unit) {
  const sources = (unit.sources || []).map((source) => {
    const time = source.segment_start != null ? ` · ${Math.floor(source.segment_start / 60)}:${String(Math.floor(source.segment_start % 60)).padStart(2, "0")}` : "";
    return `<a href="${escapeHtml(source.source_url_at || source.canonical_url)}" target="_blank" rel="noreferrer">${escapeHtml(source.title || "原始来源")}${time}</a>`;
  }).join("");
  return sources || '<span class="badge warning">尚未关联来源</span>';
}

function inlineUnit(unit, depth = 1) {
  const steps = (unit.steps || []).map((step) => `<li>${escapeHtml(step)}</li>`).join("");
  const constraints = (unit.constraints || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  const tags = (unit.keywords || []).map((tag) => `<span class="badge">${escapeHtml(tag)}</span>`).join("");
  const contentCount = (unit.steps || []).length + (unit.constraints || []).length;
  return `<details class="inline-knowledge-unit knowledge-depth-${Math.min(depth, 3)}" id="knowledge-unit-${unit.id}">
    <summary><span><strong>${escapeHtml(unit.title)}</strong>${contentCount ? `<span class="inline-expand-count">${contentCount}</span>` : ""}</span><span class="badge success">${escapeHtml(unit.method_type || "知识点")}</span></summary>
    <div class="inline-unit-body">
      ${steps ? `<h4>核心内容</h4><ol class="knowledge-steps">${steps}</ol>` : ""}
      ${constraints ? `<h4>限制与提醒</h4><ul class="knowledge-steps">${constraints}</ul>` : ""}
      ${tags ? `<div class="tag-row">${tags}</div>` : ""}
      <div class="inline-source"><h4>核对出处</h4><div class="source-links">${inlineSourceLinks(unit)}</div></div>
      <div class="form-row actions-left"><button class="button small secondary inline-assign" data-unit-id="${unit.id}" type="button">归入主题</button><button class="button small secondary inline-edit" data-unit-id="${unit.id}" type="button">编辑知识点</button></div>
    </div>
  </details>`;
}

function topicPageSection(topic, depth = 0) {
  const units = (topic.units || []).map((unit) => inlineUnit(unit, depth + 1)).join("");
  const children = (topic.children || []).map((child) => topicPageSection(child, depth + 1)).join("");
  return `<section class="topic-page-section depth-${Math.min(depth, 3)}">
    ${depth ? `<div class="topic-page-heading"><h3>${escapeHtml(topic.title)}</h3>${topic.description ? `<p>${escapeHtml(topic.description)}</p>` : ""}</div>` : ""}
    ${units}${children}
  </section>`;
}

function selectTopic(topicId) {
  const found = findTopic(topicId);
  if (!found) return;
  const { topic, path } = found;
  $$("#topicTree summary").forEach((node) => node.classList.toggle("active", Number(node.dataset.topicId) === topicId));
  $$(".tree-unit").forEach((node) => node.classList.remove("active"));
  $("#knowledgeDetail").innerHTML = `
    <div class="knowledge-heading"><h2>${escapeHtml(topic.title)}</h2><p>${escapeHtml(topic.description || "这个主题下的知识结构。")}</p></div>
    <div class="topic-page-content">${topicPageSection(topic)}</div>`;
  $("#knowledgeContext").innerHTML = `
    <div class="panel-label">当前位置</div><div class="knowledge-path">${pathHtml(path)}</div>
    <div class="context-stat"><strong>${topicUnitIds(topic).size}</strong><span>个知识点</span></div>
    <p>知识点在当前页面原地展开，便于横向对照和回看。</p>`;
  $$(".inline-edit").forEach((button) => button.addEventListener("click", () => {
    const unit = state.units.find((item) => item.id === Number(button.dataset.unitId));
    if (unit) openUnitDialog(unit, { sourceId: null, manual: false });
  }));
  $$(".inline-assign").forEach((button) => button.addEventListener("click", () => {
    const unit = state.units.find((item) => item.id === Number(button.dataset.unitId));
    if (unit) openTopicDialog(unit);
  }));
}

function selectUnit(unitId, topicId) {
  const found = findTopic(topicId);
  if (!found) return;
  selectTopic(topicId);
  const node = document.getElementById(`knowledge-unit-${unitId}`);
  if (!node) return;
  node.open = true;
  node.scrollIntoView({ behavior: "smooth", block: "center" });
  $$(".tree-unit").forEach((item) => item.classList.toggle("active",
    Number(item.dataset.unitId) === unitId && Number(item.dataset.topicId) === topicId));
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

async function runSearch(event, useAi = false) {
  event?.preventDefault();
  const query = $("#searchInput").value.trim();
  $("#searchAnswer").hidden = false;
  $("#searchAnswer").textContent = useAi ? "正在整理答案..." : "正在搜索...";
  try {
    const data = await api(`/api/search?q=${encodeURIComponent(query)}${useAi ? "&ai=1" : ""}`);
    state.searchEventId = data.event_id;
    const cards = data.results.filter((result) => result.kind === "knowledge_unit");
    const sources = data.results.filter((result) => result.kind === "source");
    const hasOutline = data.results.some((result) => (result.video_outline?.sections || []).length);
    const answerLabel = data.mode === "ai" ? "AI 综合回答" : cards.length
      ? "知识库直接答案" : hasOutline ? "视频结构摘录" : "知识库匹配";
    const fallback = data.ai_error ? `\n\nAI 增强暂不可用，已退回本地知识库。` : "";
    const aiAction = data.ai_available && !useAi
      ? `<button id="answerAiButton" class="button small secondary" type="button">AI 整理答案</button>` : "";
    $("#searchAnswer").innerHTML = `<span class="badge success">${answerLabel}</span><div class="answer-copy">${escapeHtml(data.answer + fallback)}</div>${aiAction}`;
    $("#answerAiButton")?.addEventListener("click", () => runSearch(null, true));
    const cardHtml = cards.map((result, index) => {
      const steps = (result.steps || []).length ? `<ol>${result.steps.map((step) => `<li>${escapeHtml(step)}</li>`).join("")}</ol>` : "";
      const constraints = (result.constraints || []).length ? `<p><strong>注意：</strong>${escapeHtml(result.constraints.join("；"))}</p>` : "";
      const links = (result.sources || []).map((source) => {
        const time = source.segment_start != null ? ` · ${Math.floor(source.segment_start / 60)}:${String(Math.floor(source.segment_start % 60)).padStart(2, "0")}` : "";
        return `<a href="${escapeHtml(source.source_url_at || source.canonical_url)}" target="_blank" rel="noreferrer">${escapeHtml(source.title || "原始来源")}${time}</a>`;
      }).join("");
      return `<article class="search-result method-result"><div class="result-meta"><span class="badge success">知识点 ${index + 1}</span></div><h3>${escapeHtml(result.title || "未命名知识点")}</h3><p>${escapeHtml(result.when_to_use || "")}</p>${steps}${constraints}${links ? `<details class="evidence-details"><summary>核对来源和时间点</summary><div class="source-links">${links}</div></details>` : ""}</article>`;
    }).join("");
    const outlineSources = sources.filter((result) => (result.video_outline?.sections || []).length);
    const outlineHtml = outlineSources.map((result) => {
      const outline = result.video_outline;
      const sections = outline.sections.map((section) => {
        const evidence = (section.evidence || []).map((row) => {
        const time = escapeHtml(row.time || "原文");
        const source = escapeHtml(row.source_url_at || result.source_url_at || result.canonical_url);
        const label = result.platform === "bilibili" && row.time
          ? `<a class="evidence-time" href="${source}" target="_blank" rel="noreferrer">${time}</a>`
          : `<span class="evidence-time">${time}</span>`;
        return `<div class="evidence-item">${label}<span>${escapeHtml(row.excerpt || "对应原文")}</span></div>`;
      }).join("");
        const points = (section.points || []).map((point) => `<li>${escapeHtml(point)}</li>`).join("");
        return `<section class="outline-search-section"><h3>${escapeHtml(section.title)}</h3><p>${escapeHtml(section.summary)}</p>${points ? `<ul>${points}</ul>` : ""}${evidence ? `<details class="evidence-details"><summary>核对原文依据</summary><div class="evidence-list">${evidence}</div></details>` : ""}</section>`;
      }).join("");
      return `<article class="search-result method-result"><div class="result-meta"><span class="badge ${result.status === "triage_ready" ? "warning" : "success"}">${result.status === "triage_ready" ? "待审核视频" : "视频结构"}</span></div><h3>${escapeHtml(result.title || "未命名来源")}</h3><p>${escapeHtml(outline.overview || "")}</p>${sections}</article>`;
    }).join("");
    const rawSources = sources.filter((result) => !(result.video_outline?.sections || []).length);
    const sourceHtml = rawSources.length ? `<details class="raw-results"><summary>查看原始视频匹配（${rawSources.length}）</summary><div>${rawSources.map((result) => `<article class="search-result source-result"><div class="result-meta"><span class="badge">原始来源</span></div><h3>${escapeHtml(result.title || "未命名来源")}</h3><p>${escapeHtml(result.summary_50 || "命中原始逐字稿")}</p><a href="${escapeHtml(result.source_url_at || result.canonical_url)}" target="_blank" rel="noreferrer">打开对应分集</a></article>`).join("")}</div></details>` : "";
    const hasDirectAnswer = cards.length || outlineSources.length;
    $("#searchResults").innerHTML = hasDirectAnswer ? `${cards.length ? `<h2 class="search-heading">提炼好的知识点</h2>${cardHtml}` : ""}${outlineHtml ? `<h2 class="search-heading">相关视频结构</h2>${outlineHtml}` : ""}${sourceHtml}<div class="feedback"><span>这个答案有帮助吗？</span><button class="button small secondary search-feedback" data-value="helpful">有帮助</button><button class="button small secondary search-feedback" data-value="not_helpful">没有</button></div>` : `<div class="empty large">知识库中没有直接答案，可以换一个更具体的问题。</div>${sourceHtml}`;
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
  $("#fallbackModel").value = settings.fallback_model || "";
  $("#apiKey").value = "";
  $("#rememberApiKey").checked = Boolean(settings.key_saved_on_device);
  const saved = settings.key_saved_on_device ? "，Key 已存入 Windows 凭据管理器" : "";
  $("#llmStatus").textContent = settings.configured ? `AI 增强已启用：${settings.model}${saved}` : "当前为基础模式。";
}

async function saveSettings(event) {
  event.preventDefault();
  const payload = {
    base_url: $("#baseUrl").value.trim(), model: $("#modelName").value.trim(),
    fallback_model: $("#fallbackModel").value.trim(),
    api_key: $("#apiKey").value.trim(), remember_api_key: $("#rememberApiKey").checked,
  };
  $("#llmStatus").textContent = "正在测试连接...";
  try {
    const result = await api("/api/settings/llm/test", { method: "POST", body: JSON.stringify(payload) });
    $("#apiKey").value = "";
    $("#llmStatus").textContent = result.ok ? `连接成功：${result.model}` : "服务有响应，但测试结果不符合预期。";
    notice(payload.remember_api_key ? "模型配置已保存到 Windows 凭据管理器" : "模型配置已在本次运行中启用");
    loadDashboard();
  } catch (error) {
    $("#llmStatus").textContent = error.message;
  }
}

async function clearKey() {
  await api("/api/settings/llm", { method: "PUT", body: JSON.stringify({ clear_api_key: true }) });
  notice("已移除本机保存的模型设置");
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
  $("#aiSearchButton").addEventListener("click", () => runSearch(null, true));
  $("#settingsForm").addEventListener("submit", saveSettings);
  $("#suggestOrganizationButton").addEventListener("click", () => openOrganizationDialog().catch((error) => notice(error.message, true)));
  $("#newTopicButton").addEventListener("click", () => openTopicDialog());
  $("#topicForm").addEventListener("submit", saveTopic);
  $("#topicMode").addEventListener("change", syncTopicDialog);
  $("#closeTopicDialog").addEventListener("click", () => $("#topicDialog").close());
  $("#cancelTopic").addEventListener("click", () => $("#topicDialog").close());
  $("#generateOrganizationButton").addEventListener("click", generateOrganizationProposal);
  $("#organizationForm").addEventListener("submit", applyOrganizationPlan);
  $("#proposalRootMode").addEventListener("change", syncProposalRootFields);
  $("#closeOrganizationDialog").addEventListener("click", () => $("#organizationDialog").close());
  $("#cancelOrganization").addEventListener("click", () => $("#organizationDialog").close());
  $("#clearKeyButton").addEventListener("click", clearKey);
  $("#refreshButton").addEventListener("click", refreshCurrent);
  $("#unitForm").addEventListener("submit", saveUnit);
  $("#closeUnitDialog").addEventListener("click", () => $("#unitDialog").close());
  $("#cancelUnit").addEventListener("click", () => $("#unitDialog").close());
}

bindEvents();
loadDashboard().catch((error) => notice(error.message, true));
setInterval(() => { if (state.view === "inbox") loadDashboard().catch(() => {}); }, 3000);
