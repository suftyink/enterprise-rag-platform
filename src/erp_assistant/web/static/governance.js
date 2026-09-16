"use strict";

import {
  api,
  byId,
  formatBytes,
  setBusy,
  showGlobal,
  state,
  statusParagraph,
} from "./core.js";

let documentRefreshTimer = null;

export function bindGovernanceWorkflows() {
  byId("upload-form").addEventListener("submit", uploadDocument);
  byId("refresh-documents").addEventListener("click", loadDocuments);
  byId("pilot-form").addEventListener("submit", managePilot);
  byId("confirm-dialog").addEventListener("close", completeLifecycleConfirmation);
  document.querySelectorAll("[data-pilot-action]").forEach((button) => {
    button.addEventListener("click", () => {
      state.pilotAction = button.dataset.pilotAction;
    });
  });
}

export function configureGovernance(session) {
  byId("upload-limits").textContent = [
    `支持 ${session.upload.formats.join("、")}`,
    `单个文件不超过 ${formatBytes(session.upload.maxFileSize)}。`,
  ].join("；");
  const select = byId("document-scope");
  select.replaceChildren(scopeOption("TENANT", session.tenantId, `当前租户 · ${session.tenantId}`));
  session.knowledgeScopes.forEach((item) => {
    if (item.scope !== "TENANT") {
      select.append(scopeOption(item.scope, item.scopeId, `${item.scope} · ${item.scopeId}`));
    }
  });
  if (
    session.roles.includes("SYSTEM_ADMIN")
    && !Array.from(select.options).some((option) => option.dataset.scope === "GLOBAL")
  ) {
    select.append(scopeOption("GLOBAL", "GLOBAL", "GLOBAL · 全局（高风险）"));
  }
}

function scopeOption(scope, scopeId, label) {
  const option = document.createElement("option");
  option.value = `${scope}:${scopeId}`;
  option.dataset.scope = scope;
  option.dataset.scopeId = scopeId;
  option.textContent = label;
  return option;
}

async function uploadDocument(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const file = byId("document-file").files[0];
  if (!file) return;
  if (file.size > state.session.upload.maxFileSize) {
    byId("upload-status").textContent = "文件超过服务端允许的大小限制。";
    return;
  }
  const selected = byId("document-scope").selectedOptions[0];
  const payload = new FormData();
  payload.append("file", file);
  payload.append("title", byId("document-title").value);
  payload.append("documentKey", byId("document-key").value);
  payload.append("scope", selected.dataset.scope);
  if (selected.dataset.scope === "SHARED") payload.append("scopeId", selected.dataset.scopeId);
  const button = byId("upload-button");
  setBusy(button, true, "上传处理中…");
  try {
    const result = await api(
      "/internal/api/documents",
      { method: "POST", body: payload },
      true,
      false,
    );
    byId("upload-status").textContent = [
      `已接收 ${result.title} v${result.version}`,
      `trace ${result.traceId}。`,
    ].join("，");
    form.reset();
    if (byId("document-scope").options.length) byId("document-scope").selectedIndex = 0;
    await loadDocuments();
  } catch (error) {
    byId("upload-status").textContent = `上传失败：${error.message}`;
  } finally {
    setBusy(button, false, "上传并开始处理");
  }
}

export async function loadDocuments() {
  if (!state.session) return;
  const list = byId("document-list");
  list.replaceChildren(statusParagraph("正在读取知识清单…"));
  try {
    const documents = await api("/internal/api/documents", { method: "GET" });
    list.replaceChildren();
    if (!documents.length) {
      list.append(statusParagraph("当前授权范围内还没有文档。"));
      return;
    }
    documents.forEach((item) => list.append(documentCard(item)));
    scheduleProgressRefresh(documents);
  } catch (error) {
    list.replaceChildren(statusParagraph(`知识清单读取失败：${error.message}`));
  }
}

function documentCard(item) {
  const article = document.createElement("article");
  article.className = "document-card";
  const header = document.createElement("header");
  const titleBlock = document.createElement("div");
  const title = document.createElement("h2");
  title.textContent = item.title;
  const key = document.createElement("small");
  key.textContent = item.documentKey;
  titleBlock.append(title, key);
  const status = document.createElement("span");
  status.className = "status-chip";
  status.dataset.tone = item.publicationStatus === "PUBLISHED" ? "success" : "warning";
  status.textContent = `${item.status} · ${item.publicationStatus}`;
  header.append(titleBlock, status);
  const meta = document.createElement("p");
  meta.className = "document-meta";
  meta.textContent = [
    `v${item.version}`,
    item.scope,
    `revision ${item.governanceRevision}`,
    item.mediaType,
    `parser ${item.parserVersion || "等待中"}`,
  ].join(" · ");
  article.append(header, meta);
  const progress = document.createElement("dl");
  progress.className = "document-progress";
  [
    ["处理阶段", item.phase || item.status],
    ["任务结果", item.jobStatus || "尚未创建任务"],
    ["处理尝试", item.attempt === null ? "—" : String(item.attempt)],
    ["分块数量", item.chunkCount === null ? "—" : String(item.chunkCount)],
    ["检索资格", item.retrievalEligible ? "可检索" : "不可检索"],
    ["切分版本", item.chunkingVersion || "等待中"],
  ].forEach(([label, value]) => {
    const term = document.createElement("dt");
    term.textContent = label;
    const description = document.createElement("dd");
    description.textContent = value;
    progress.append(term, description);
  });
  article.append(progress);
  if (item.warnings.length) {
    const warnings = document.createElement("p");
    warnings.className = "document-warnings";
    warnings.textContent = `解析警告：${item.warnings.join("；")}`;
    article.append(warnings);
  }
  if (item.errorCode || item.errorDetail) {
    const failure = document.createElement("p");
    failure.className = "document-error";
    failure.textContent = `处理失败：${item.errorCode || "UNKNOWN"} · ${item.errorDetail || "请重试或联系管理员"}`;
    article.append(failure);
  }
  const actions = document.createElement("div");
  actions.className = "document-actions";
  ["PUBLISH", "WITHDRAW", "ROLLBACK"].forEach((action) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = action === "PUBLISH" ? "secondary-button" : "danger-button";
    button.textContent = { PUBLISH: "发布", WITHDRAW: "撤回", ROLLBACK: "回滚" }[action];
    button.addEventListener("click", () => confirmLifecycle(item, action));
    actions.append(button);
  });
  article.append(actions);
  return article;
}

function confirmLifecycle(item, action) {
  state.pendingLifecycle = { document: item, action };
  const names = { PUBLISH: "发布", WITHDRAW: "撤回", ROLLBACK: "回滚" };
  byId("confirm-copy").textContent = [
    `确认${names[action]}“${item.title}” v${item.version}？`,
    `作用范围：${item.scope}，当前治理 revision：${item.governanceRevision}。`,
  ].join("");
  byId("confirm-reason").value = "";
  byId("confirm-dialog").showModal();
}

async function completeLifecycleConfirmation() {
  if (byId("confirm-dialog").returnValue !== "confirm" || !state.pendingLifecycle) return;
  const reason = byId("confirm-reason").value.trim();
  const pending = state.pendingLifecycle;
  state.pendingLifecycle = null;
  if (!reason) {
    showGlobal("治理操作必须填写原因。", "error");
    return;
  }
  try {
    const result = await api(`/internal/api/documents/${pending.document.documentId}/lifecycle`, {
      method: "POST",
      body: JSON.stringify({
        action: pending.action,
        reason,
        expectedRevision: pending.document.governanceRevision,
      }),
    });
    const names = { PUBLISH: "发布", WITHDRAW: "撤回", ROLLBACK: "回滚" };
    showGlobal(
      `${names[pending.action]}操作已完成并写入审计。trace ${result.traceId}`,
      "success",
    );
    await loadDocuments();
  } catch (error) {
    showGlobal(`治理操作失败：${error.message}`, "error");
  }
}

function scheduleProgressRefresh(documents) {
  if (documentRefreshTimer !== null) window.clearTimeout(documentRefreshTimer);
  const processing = documents.some((item) =>
    ["QUEUED", "PROCESSING", "PARSING", "INDEXING"].includes(item.jobStatus)
  );
  documentRefreshTimer = processing
    ? window.setTimeout(() => loadDocuments(), 2000)
    : null;
}

export async function loadPilotStatus() {
  try {
    const pilot = await api("/internal/api/pilot", { method: "GET" });
    const badge = byId("pilot-badge");
    badge.textContent = pilot.enabled ? "内部试点已开放" : "内部试点未开放";
    badge.dataset.tone = pilot.enabled ? "success" : "warning";
    renderPilotEvidence(pilot);
    if (!pilot.enabled) {
      showGlobal(`试点门禁：${pilot.reasonCode}。问答和治理暂不可用。`, "warning");
    }
  } catch (error) {
    showGlobal(`无法读取试点状态：${error.message}`, "error");
  }
}

function renderPilotEvidence(pilot) {
  const container = byId("pilot-evidence");
  container.replaceChildren();
  const list = document.createElement("dl");
  [
    ["门禁", pilot.enabled ? "已开放" : pilot.reasonCode],
    ["Readiness 报告", pilot.reportId || "—"],
    ["发布指纹", pilot.releaseFingerprint || "—"],
    ["证据时间", pilot.generatedAt ? new Date(pilot.generatedAt).toLocaleString() : "—"],
    ["批准人", pilot.approvedBy || "—"],
    ["回滚负责人", pilot.rollbackOwner || "—"],
  ].forEach(([label, value]) => {
    const term = document.createElement("dt");
    term.textContent = label;
    const description = document.createElement("dd");
    description.textContent = value;
    list.append(term, description);
  });
  container.append(list);
}

async function managePilot(event) {
  event.preventDefault();
  const data = new FormData(event.currentTarget);
  const enabled = state.pilotAction === "enable";
  try {
    const result = await api("/internal/api/pilot", {
      method: "POST",
      body: JSON.stringify({
        enabled,
        pilotScope: data.get("pilotScope"),
        capacity: data.get("capacity"),
        monitoringOwner: data.get("monitoringOwner"),
        rollbackOwner: data.get("rollbackOwner"),
        reason: data.get("reason"),
      }),
    });
    byId("pilot-form-status").textContent = enabled
      ? `试点已开放。trace ${result.traceId}`
      : `试点已关闭，知识数据未删除。trace ${result.traceId}`;
    await loadPilotStatus();
  } catch (error) {
    byId("pilot-form-status").textContent = `门禁变更失败：${error.message}`;
  }
}
