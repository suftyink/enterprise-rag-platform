"use strict";

export const state = {
  session: null,
  csrf: "",
  answerTrace: null,
  citations: [],
  pendingLifecycle: null,
  pilotAction: "enable",
  releaseAction: "create",
  activeReleaseId: null,
};

export const byId = (id) => document.getElementById(id);

export async function api(url, options = {}, includeCsrf = true, jsonContent = true) {
  const headers = new Headers(options.headers || {});
  if (includeCsrf && state.csrf) headers.set("X-CSRF-Token", state.csrf);
  if (jsonContent && options.body && !(options.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(url, { ...options, headers, credentials: "same-origin" });
  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    let code = "HTTP_ERROR";
    try {
      const body = await response.json();
      message = body.detail?.message || message;
      code = body.detail?.code || code;
    } catch (_) {
      // The status code remains the recovery signal for a non-JSON response.
    }
    const error = new Error(message);
    error.status = response.status;
    error.code = code;
    throw error;
  }
  if (response.status === 204) return null;
  const payload = await response.json();
  const traceId = response.headers.get("X-Trace-ID");
  if (traceId && payload && !Array.isArray(payload) && !payload.traceId) {
    payload.traceId = traceId;
  }
  return payload;
}

export function setBusy(button, busy, label) {
  button.disabled = busy;
  button.textContent = label;
  button.setAttribute("aria-busy", String(busy));
}

export function showGlobal(message, tone) {
  const node = byId("global-status");
  node.textContent = message;
  node.dataset.tone = tone;
}

export function statusParagraph(message) {
  const paragraph = document.createElement("p");
  paragraph.textContent = message;
  return paragraph;
}

export function formatBytes(value) {
  if (value >= 1024 * 1024) return `${Math.round(value / 1024 / 1024)} MB`;
  return `${Math.round(value / 1024)} KB`;
}
