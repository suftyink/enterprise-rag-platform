"use strict";

import { bindAnswerWorkflow } from "./answer.js";
import { api, byId, showGlobal, state } from "./core.js";
import {
  bindGovernanceWorkflows,
  configureGovernance,
  loadDocuments,
  loadPilotStatus,
} from "./governance.js";

document.addEventListener("DOMContentLoaded", () => {
  bindNavigation();
  bindAnswerWorkflow();
  bindGovernanceWorkflows();
  byId("logout-button").addEventListener("click", logout);
  bootstrap();
});

async function bootstrap() {
  try {
    const session = await api("/internal/api/session", { method: "GET" }, false);
    state.session = session;
    state.csrf = session.csrfToken;
    byId("session-user").textContent = `${session.subjectId} · ${session.tenantId}`;
    byId("logout-button").hidden = false;
    configureForRoles(session);
    await loadPilotStatus();
  } catch (error) {
    if (error.status === 401) {
      window.location.replace("/internal/auth/login");
      return;
    }
    showGlobal(error.message, "error");
  }
}

function configureForRoles(session) {
  const roles = new Set(session.roles);
  const isAdmin = roles.has("KNOWLEDGE_ADMIN") || roles.has("SYSTEM_ADMIN");
  const isSystemAdmin = roles.has("SYSTEM_ADMIN");
  byId("governance-nav").hidden = !isAdmin;
  byId("pilot-nav").hidden = !isSystemAdmin;
  configureGovernance(session);
  if (isAdmin) loadDocuments();
}

function bindNavigation() {
  document.querySelectorAll("[data-view]").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll("[data-view]").forEach((item) => {
        const active = item === button;
        item.classList.toggle("is-active", active);
        if (active) item.setAttribute("aria-current", "page");
        else item.removeAttribute("aria-current");
      });
      document.querySelectorAll(".view").forEach((view) => {
        const active = view.id === button.dataset.view;
        view.hidden = !active;
        view.classList.toggle("is-active", active);
      });
      byId("main-content").focus();
    });
  });
}

async function logout() {
  try {
    await api("/internal/api/logout", { method: "POST" });
  } finally {
    window.location.replace("/internal");
  }
}
