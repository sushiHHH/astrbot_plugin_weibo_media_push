let bridge = window.AstrBotPluginPage || null;

async function waitForBridge() {
  // 桥接脚本由 AstrBot 在服务页面时注入，可能需要稍等片刻
  if (bridge) return bridge;
  for (let i = 0; i < 50; i += 1) {
    await new Promise((resolve) => window.setTimeout(resolve, 100));
    if (window.AstrBotPluginPage) {
      bridge = window.AstrBotPluginPage;
      return bridge;
    }
  }
  return null;
}

const elements = {
  workspace: document.querySelector(".workspace"),
  runtimeStatus: document.getElementById("runtime-status"),
  refreshButton: document.getElementById("refresh-button"),
  errorNotice: document.getElementById("error-notice"),
  sessionList: document.getElementById("session-list"),
  sessionSearch: document.getElementById("session-search"),
  detailView: document.getElementById("detail-view"),
  intervalForm: document.getElementById("interval-form"),
  pollInterval: document.getElementById("poll-interval"),
  tabGroups: document.getElementById("tab-groups"),
  tabOthers: document.getElementById("tab-others"),
  toast: document.getElementById("toast"),
  removeDialog: document.getElementById("remove-dialog"),
  removeDialogText: document.getElementById("remove-dialog-text"),
  metrics: {
    groups: document.getElementById("metric-groups"),
    authors: document.getElementById("metric-authors"),
    subscriptions: document.getElementById("metric-subscriptions"),
    active: document.getElementById("metric-active"),
  },
};

const state = {
  overview: null,
  view: "groups",
  selectedUmo: null,
  search: "",
  loading: true,
  saving: false,
  toastTimer: null,
  removeTarget: null,
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function showToast(message, isError = false) {
  window.clearTimeout(state.toastTimer);
  elements.toast.textContent = message;
  elements.toast.classList.toggle("is-error", isError);
  elements.toast.classList.remove("is-hidden");
  state.toastTimer = window.setTimeout(() => {
    elements.toast.classList.add("is-hidden");
  }, 3000);
}

function setError(message) {
  elements.errorNotice.textContent = message || "";
  elements.errorNotice.classList.toggle("is-hidden", !message);
}

function sessions() {
  if (!state.overview) return [];
  return state.view === "groups"
    ? state.overview.groups
    : state.overview.other_sessions;
}

function filteredSessions() {
  const query = state.search.trim().toLowerCase();
  const list = sessions();
  if (!query) return list;
  return list.filter((session) => {
    const name = (session.group_name || session.session_name || "").toLowerCase();
    const id = (session.group_id || session.session_id || "").toLowerCase();
    return name.includes(query) || id.includes(query);
  });
}

function selectedSession() {
  return filteredSessions().find((session) => session.umo === state.selectedUmo) || null;
}

function ensureSelection() {
  if (selectedSession()) return;
  const list = filteredSessions();
  state.selectedUmo = list.length ? list[0].umo : null;
}

function renderRuntime() {
  if (!state.overview) return;
  const { polling, cookie } = state.overview;
  const ready = polling.running;
  elements.runtimeStatus.classList.toggle("is-ready", ready);
  elements.runtimeStatus.classList.toggle("is-error", !ready);
  const cookieText = cookie.configured ? "Cookie ✓" : "Cookie ✗";
  elements.runtimeStatus.lastElementChild.textContent = ready
    ? `轮询中 · ${cookieText}`
    : `已停止 · ${cookieText}`;

  for (const [key, target] of Object.entries(elements.metrics)) {
    target.textContent = state.overview.totals[key] ?? 0;
  }
  if (document.activeElement !== elements.pollInterval) {
    elements.pollInterval.value = polling.interval_minutes;
  }

  const failedSources = (state.overview.group_sources || []).filter((s) => !s.available);
  if (failedSources.length) {
    setError(
      `${failedSources.length} 个 QQ 实例暂时无法读取群列表；已有订阅仍可管理。`,
    );
  } else {
    setError("");
  }
}

function renderSessionList() {
  const list = filteredSessions();
  elements.sessionSearch.placeholder =
    state.view === "groups" ? "搜索群名或群号" : "搜索会话";

  if (!list.length) {
    const title = state.search
      ? "没有匹配结果"
      : state.view === "groups"
        ? "暂无群聊"
        : "暂无其他会话";
    elements.sessionList.innerHTML = `
      <div class="list-empty">
        <strong>${title}</strong>
        <span>${state.search ? "请尝试其他关键词" : "当前没有可管理的会话"}</span>
      </div>
    `;
    return;
  }

  elements.sessionList.innerHTML = list
    .map((session) => {
      const isGroup = state.view === "groups";
      const name = isGroup ? session.group_name : session.session_name;
      const id = isGroup ? session.group_id : session.session_id;
      const unavailable = isGroup && !session.available;
      return `
        <button
          class="session-item ${session.umo === state.selectedUmo ? "is-active" : ""}"
          type="button"
          data-umo="${escapeHtml(session.umo)}"
        >
          <span class="session-copy">
            <span class="session-name">${escapeHtml(name)}</span>
            <span class="session-meta">
              ${escapeHtml(id)}${unavailable ? ' · <span class="availability-mark">未连接</span>' : ""}
            </span>
          </span>
          <span class="session-count">${session.subscriptions.length}</span>
        </button>
      `;
    })
    .join("");
}

function subscriptionRow(session, subscription) {
  return `
    <div class="subscription-row" data-uid="${escapeHtml(subscription.uid)}">
      <div class="author-cell">
        <span class="author-name">${escapeHtml(subscription.screen_name)}</span>
        <span class="author-handle">${escapeHtml(subscription.uid)}</span>
        <span class="pushed-badge">已推送 ${subscription.pushed ?? 0} 条</span>
      </div>
      <label class="switch-field">
        <input
          type="checkbox"
          data-subscription-field="enabled"
          data-umo="${escapeHtml(session.umo)}"
          data-uid="${escapeHtml(subscription.uid)}"
          ${subscription.enabled ? "checked" : ""}
        />
        <span>推送</span>
      </label>
      <button
        class="remove-button"
        type="button"
        data-remove-subscription
        data-umo="${escapeHtml(session.umo)}"
        data-uid="${escapeHtml(subscription.uid)}"
        data-screen-name="${escapeHtml(subscription.screen_name)}"
        title="移除订阅"
        aria-label="移除 ${escapeHtml(subscription.uid)}"
      >×</button>
    </div>
  `;
}

function renderDetail() {
  const session = selectedSession();
  if (!session) {
    elements.detailView.innerHTML = `
      <div class="detail-empty">
        <strong>未选择会话</strong>
        <span>从列表中选择一个会话</span>
      </div>
    `;
    return;
  }

  const isGroup = state.view === "groups";
  const name = isGroup ? session.group_name : session.session_name;
  const id = isGroup ? session.group_id : session.session_id;
  const allEnabled =
    session.subscriptions.length > 0 &&
    session.subscriptions.every((item) => item.enabled);
  const groupSwitch =
    isGroup && session.subscriptions.length
      ? `
      <label class="group-switch">
        <input
          id="group-status"
          type="checkbox"
          data-umo="${escapeHtml(session.umo)}"
          ${allEnabled ? "checked" : ""}
        />
        <span>全部推送</span>
      </label>
    `
      : "";

  let addSection = "";
  if (isGroup) {
    addSection = session.available
      ? `
        <section class="add-section">
          <div class="section-title">新增订阅</div>
          <form class="add-form" id="add-form" data-umo="${escapeHtml(session.umo)}">
            <input
              name="uid"
              type="text"
              maxlength="100"
              autocomplete="off"
              placeholder="微博 UID、主页链接或个性域名，例如 weibo.com/AoiMomoko813"
              aria-label="微博 UID"
              required
            />
            <button class="primary-button" type="submit">添加订阅</button>
          </form>
        </section>
      `
      : `
        <section class="add-section">
          <p class="unavailable-note">机器人当前未连接到这个群，暂不能新增订阅。</p>
        </section>
      `;
  }

  const rows = session.subscriptions.length
    ? session.subscriptions.map((item) => subscriptionRow(session, item)).join("")
    : `
      <div class="detail-empty">
        <strong>暂无订阅</strong>
        <span>${isGroup ? "可以在上方添加博主 UID" : "这个会话还没有微博订阅"}</span>
      </div>
    `;

  elements.detailView.innerHTML = `
    <header class="detail-header">
      <div class="detail-heading">
        <h2>${escapeHtml(name)}</h2>
        <p>${escapeHtml(id)} · ${escapeHtml(session.platform_id)}</p>
      </div>
      ${groupSwitch}
    </header>
    ${addSection}
    <section class="subscription-section">
      <div class="subscription-heading">
        <div class="section-title">订阅名单</div>
        <span>${session.subscriptions.length} 项</span>
      </div>
      <div class="subscription-list">${rows}</div>
    </section>
  `;
}

function render() {
  if (!state.overview) return;
  ensureSelection();
  renderRuntime();
  renderSessionList();
  renderDetail();
  elements.workspace.setAttribute("aria-busy", "false");
}

async function loadOverview({ announce = false } = {}) {
  if (!bridge) {
    setError(
      "当前页面未运行在 AstrBot Dashboard 中。请从 AstrBot 面板的插件页面入口打开。",
    );
    return;
  }
  state.loading = true;
  elements.refreshButton.classList.add("is-loading");
  try {
    state.overview = await bridge.apiGet("overview");
    render();
    if (announce) showToast("已刷新");
  } catch (error) {
    setError(`读取数据失败: ${error?.message || error}`);
  } finally {
    state.loading = false;
    elements.refreshButton.classList.remove("is-loading");
  }
}

async function post(endpoint, payload, successMessage) {
  if (state.saving) {
    showToast("正在处理上一步操作，请稍候...", true);
    return;
  }
  state.saving = true;
  document.body.classList.add("is-saving");
  try {
    await bridge.apiPost(endpoint, payload);
    showToast(successMessage);
    await loadOverview();
  } catch (error) {
    let message = error?.message || String(error);
    try {
      const parsed = JSON.parse(message);
      message = parsed.message || parsed.detail || message;
    } catch {
      /* 保持原样 */
    }
    showToast(`操作失败: ${message}`, true);
  } finally {
    state.saving = false;
    document.body.classList.remove("is-saving");
  }
}

function bindEvents() {
  elements.refreshButton.addEventListener("click", () => loadOverview({ announce: true }));
  elements.sessionSearch.addEventListener("input", (event) => {
    state.search = event.target.value;
    ensureSelection();
    renderSessionList();
    renderDetail();
  });

  elements.tabGroups.addEventListener("click", () => {
    state.view = "groups";
    elements.tabGroups.classList.add("is-active");
    elements.tabOthers.classList.remove("is-active");
    state.selectedUmo = null;
    ensureSelection();
    renderSessionList();
    renderDetail();
  });
  elements.tabOthers.addEventListener("click", () => {
    state.view = "others";
    elements.tabOthers.classList.add("is-active");
    elements.tabGroups.classList.remove("is-active");
    state.selectedUmo = null;
    ensureSelection();
    renderSessionList();
    renderDetail();
  });

  elements.intervalForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const minutes = Number(elements.pollInterval.value);
    if (!Number.isInteger(minutes) || minutes < 1) {
      showToast("轮询间隔必须是不少于 1 的整数", true);
      return;
    }
    post("settings/poll-interval", { minutes }, "轮询间隔已保存");
  });

  elements.sessionList.addEventListener("click", (event) => {
    const item = event.target.closest(".session-item");
    if (!item) return;
    state.selectedUmo = item.dataset.umo;
    renderSessionList();
    renderDetail();
  });

  elements.detailView.addEventListener("submit", (event) => {
    const form = event.target.closest("#add-form");
    if (!form) return;
    event.preventDefault();
    const umo = form.dataset.umo;
    const uid = form.elements.uid.value.trim();
    if (!uid) return;
    showToast("正在添加订阅...");
    post("subscriptions/add", { umo, uid }, "订阅已添加");
  });

  elements.detailView.addEventListener("change", (event) => {
    const field = event.target.dataset.subscriptionField;
    if (field === "enabled") {
      post(
        "subscriptions/update",
        {
          umo: event.target.dataset.umo,
          uid: event.target.dataset.uid,
          enabled: event.target.checked,
        },
        "已更新",
      );
      return;
    }
    const groupStatus = event.target.closest("#group-status");
    if (groupStatus) {
      post(
        "groups/status",
        { umo: groupStatus.dataset.umo, enabled: groupStatus.checked },
        "已更新",
      );
    }
  });

  elements.detailView.addEventListener("click", (event) => {
    const button = event.target.closest("[data-remove-subscription]");
    if (!button) return;
    state.removeTarget = {
      umo: button.dataset.umo,
      uid: button.dataset.uid,
      name: button.dataset.screenName || button.dataset.uid,
    };
    elements.removeDialogText.textContent = `确定移除微博订阅 ${state.removeTarget.name}（${state.removeTarget.uid}）吗？`;
    elements.removeDialog.showModal();
  });

  elements.removeDialog.addEventListener("close", () => {
    if (
      elements.removeDialog.returnValue !== "confirm" ||
      !state.removeTarget
    ) {
      state.removeTarget = null;
      return;
    }
    const target = state.removeTarget;
    state.removeTarget = null;
    showToast("正在移除...");
    post("subscriptions/remove", { umo: target.umo, uid: target.uid }, "已移除订阅");
  });
}

async function main() {
  bindEvents();
  bridge = await waitForBridge();
  if (!bridge) {
    setError(
      "当前页面未运行在 AstrBot Dashboard 中。\n请从 AstrBot 面板 → 插件 → 微博媒体推送 → 订阅管理 进入本页面。",
    );
    elements.workspace.setAttribute("aria-busy", "false");
    return;
  }
  await bridge.ready();
  document.title = bridge.t?.("pages.subscriptions.title", "微博订阅管理");
  bridge.onContext?.((nextContext) => {
    const current = nextContext || bridge.getContext?.();
    if (current?.plugin_name) {
      elements.detailView.setAttribute("data-plugin", current.plugin_name);
    }
  });
  await loadOverview();
}

main();
