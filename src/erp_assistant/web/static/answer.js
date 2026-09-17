"use strict";

import { api, byId, setBusy, state } from "./core.js";

export function bindAnswerWorkflow() {
  byId("question-form").addEventListener("submit", askQuestion);
  byId("feedback-form").addEventListener("submit", submitFeedback);
  byId("citation-dialog")
    .querySelector("[data-close-dialog]")
    .addEventListener("click", () => byId("citation-dialog").close());
}

async function askQuestion(event) {
  event.preventDefault();
  const question = byId("question-input").value.trim();
  if (!question) return;
  setBusy(byId("ask-button"), true, "正在检索…");
  setAnswerState("正在核对可见知识、生成答案并绑定引用…", "loading");
  byId("answer-panel").hidden = true;
  clearCitations();
  try {
    const answer = await api("/internal/api/answers", {
      method: "POST",
      body: JSON.stringify({ question }),
    });
    state.answerTrace = answer.traceId;
    byId("answer-trace").textContent = answer.traceId;
    if (answer.decision === "ANSWERED") renderAnswer(answer);
    else if (answer.decision === "REFUSED") renderRefusal(answer.reason);
    else renderFailure({ status: 500, message: "回答服务返回异常结果，请重试。" });
  } catch (error) {
    renderFailure(error);
  } finally {
    setBusy(byId("ask-button"), false, "查询知识");
  }
}

function renderAnswer(answer) {
  byId("answer-state").hidden = true;
  byId("answer-panel").hidden = false;
  byId("answer-decision").textContent = "已引用回答";
  byId("answer-decision").dataset.tone = "success";
  const units = byId("answer-units");
  units.replaceChildren();
  state.citations = answer.citations;
  const citationNumbers = new Map(
    answer.citations.map((item, index) => [item.evidenceId, index + 1]),
  );
  answer.units.forEach((unit) => {
    const paragraph = document.createElement("p");
    paragraph.className = "answer-unit";
    paragraph.append(document.createTextNode(unit.text));
    unit.evidenceIds.forEach((evidenceId) => {
      const citation = answer.citations.find((item) => item.evidenceId === evidenceId);
      if (citation) paragraph.append(citationButton(citation, citationNumbers.get(evidenceId)));
    });
    units.append(paragraph);
  });
  renderCitationRail(answer.citations);
  const feedback = byId("feedback-form");
  feedback.reset();
  feedback.querySelectorAll("input, textarea, button").forEach((control) => {
    control.disabled = false;
  });
  feedback.hidden = false;
  byId("feedback-status").textContent = "";
}

function renderRefusal(reason) {
  byId("answer-panel").hidden = true;
  const copy = reason === "CONFLICTING_EVIDENCE"
    ? "现有资料相互冲突，系统无法给出可靠结论。请联系知识管理员核对版本。"
    : "当前活动知识版本不足以支持这个问题。你可以补充条件，或请知识管理员完善资料。";
  setAnswerState(`证据不足：${copy}`, "refusal");
}

function renderFailure(error) {
  let category = "输入未通过校验";
  let recovery = "请检查问题内容后重新提交。";
  if (error.status === 401) {
    category = "登录已失效";
    recovery = "请刷新页面并重新使用企业身份登录。";
  } else if (error.status === 403 || error.status === 404) {
    category = "无权访问";
    recovery = "请联系管理员确认你的知识范围。";
  } else if (error.status >= 500) {
    category = "临时系统故障";
    recovery = "可保留问题后再次提交。";
  }
  setAnswerState(`${category}：${error.message} ${recovery}`, "error");
}

function setAnswerState(message, tone) {
  const node = byId("answer-state");
  node.hidden = false;
  node.dataset.tone = tone;
  node.textContent = message;
}

function citationButton(citation, number) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "citation-marker";
  button.textContent = `[${number}]`;
  button.setAttribute("aria-label", `打开引用 ${number}：${citation.title}`);
  button.addEventListener("click", () => showCitation(citation, number));
  return button;
}

function renderCitationRail(citations) {
  byId("evidence-empty").hidden = citations.length > 0;
  const list = byId("citation-list");
  list.replaceChildren();
  citations.forEach((citation, index) => {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    const title = document.createElement("strong");
    title.textContent = `[${index + 1}] ${citation.title}`;
    const meta = document.createElement("small");
    meta.textContent = citationLocation(citation);
    button.append(title, meta);
    button.addEventListener("click", () => showCitation(citation, index + 1));
    item.append(button);
    list.append(item);
  });
}

function showCitation(citation, number) {
  const detail = byId("citation-detail");
  detail.replaceChildren();
  [
    ["编号", `[${number}] ${citation.evidenceId}`],
    ["资料", citation.title],
    ["文档版本", `v${citation.documentVersion}`],
    ["位置", citationLocation(citation)],
    ["锚点", citation.anchor || "—"],
    ["摘录", citation.excerpt],
  ].forEach(([label, value]) => {
    const term = document.createElement("dt");
    term.textContent = label;
    const description = document.createElement("dd");
    description.textContent = value;
    detail.append(term, description);
  });
  byId("citation-dialog").showModal();
}

function citationLocation(citation) {
  if (citation.pageStart) {
    return citation.pageEnd && citation.pageEnd !== citation.pageStart
      ? `第 ${citation.pageStart}–${citation.pageEnd} 页`
      : `第 ${citation.pageStart} 页`;
  }
  return citation.sectionTitle || citation.headingPath.join(" / ") || "文档内定位";
}

function clearCitations() {
  state.citations = [];
  byId("citation-list").replaceChildren();
  byId("evidence-empty").hidden = false;
}

async function submitFeedback(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const data = new FormData(form);
  const rating = data.get("rating");
  if (!rating || !state.answerTrace) return;
  const button = form.querySelector("button[type=submit]");
  setBusy(button, true, "正在提交…");
  try {
    await api(`/internal/api/answers/${state.answerTrace}/feedback`, {
      method: "POST",
      body: JSON.stringify({ rating, reason: data.get("reason") || null }),
    });
    byId("feedback-status").textContent = "反馈已记录，不会自动进入获批候选知识。";
    form.querySelectorAll("input, textarea, button").forEach((control) => {
      control.disabled = true;
    });
  } catch (error) {
    byId("feedback-status").textContent = `反馈未提交：${error.message}`;
    setBusy(button, false, "提交反馈");
  }
}
