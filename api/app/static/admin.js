const state = {
  selectedMailboxId: null,
  selectedMessageId: null,
  selectedPolicyId: null,
  selectedDomainId: null,
  runtimeConfig: {},
  deploymentConfig: {},
  policyItems: [],
  domainItems: [],
  mailboxItemsCache: [],
  mailboxPage: 1,
  mailboxPageSize: 50,
  mailboxTotal: 0,
  messageItemsCache: [],
  messagePage: 1,
  messagePageSize: 25,
  messageTotal: 0,
  policyPage: 1,
  policyPageSize: 200,
  policyTotal: 0,
  domainListRequestSeq: 0,
  requestControllers: {},
  monitorEvents: [],
  monitorEventIds: new Set(),
  monitorCursor: 0,
  monitorSource: null,
  monitorReconnectTimer: null,
  mailboxListRequestSeq: 0,
  mailboxDetailRequestSeq: 0,
  messageDetailRequestSeq: 0,
  messageListRequestSeq: 0,
  policyListRequestSeq: 0,
  searchDebounceTimer: null,
  messageSearchDebounceTimer: null,
  dateTimeFormatter: new Intl.DateTimeFormat(undefined, {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }),
  numberFormatter: new Intl.NumberFormat(),
};

const fieldLabels = {
  mailbox_default_ttl_minutes: "默认 TTL（分钟）",
  mailbox_min_ttl_minutes: "最小 TTL（分钟）",
  mailbox_max_ttl_minutes: "最大 TTL（分钟）",
  mailbox_local_part_min_length: "邮箱前缀最小长度",
  mailbox_local_part_max_length: "邮箱前缀最大长度",
  mailbox_subdomain_min_length: "邮箱子域最小长度",
  mailbox_subdomain_max_length: "邮箱子域最大长度",
  create_rate_limit_count: "创建邮箱次数",
  create_rate_limit_window_seconds: "创建限流窗口（秒）",
  inbox_rate_limit_count: "收件箱读取次数",
  inbox_rate_limit_window_seconds: "收件箱限流窗口（秒）",
  message_size_limit_bytes: "单封邮件最大字节数",
  max_text_body_chars: "纯文本正文最大字符数",
  max_html_body_chars: "HTML 正文最大字符数",
  max_attachments_per_message: "单封邮件最大附件数",
  purge_grace_minutes: "清理宽限期（分钟）",
  access_event_retention_days: "访问日志保留天数",
  cleanup_batch_size: "单次清理批量数",
  domain_monitor_loop_seconds: "域名监控循环间隔（秒）",
  domain_verify_pending_interval_seconds: "待验证域名检查间隔（秒）",
  domain_verify_active_interval_seconds: "已启用域名检查间隔（秒）",
  domain_verify_disabled_interval_seconds: "已停用域名重试间隔（秒）",
  domain_verify_failure_threshold: "域名停用失败阈值",
};

const mailboxStatusLabels = {
  active: "使用中",
  disabled: "已禁用",
  expired: "已过期",
};

const policyScopeLabels = {
  recipient_base_domain: "收件基础域",
  sender_domain: "发件域",
};

const policyActionLabels = {
  allow: "允许投递",
  reject: "拒绝接收",
  discard: "静默丢弃",
};

const policyStatusLabels = {
  active: "启用",
  disabled: "停用",
};

const domainStatusLabels = {
  pending: "待验证",
  active: "已启用",
  disabled: "已停用",
};

const byId = (id) => document.getElementById(id);
let toastSerial = 0;

function activeViewId() {
  return document.querySelector(".view-panel.active-view")?.id || "overviewView";
}

function switchView(viewId) {
  document.querySelectorAll(".view-panel").forEach((node) => {
    const active = node.id === viewId;
    node.classList.toggle("hidden", !active);
    node.classList.toggle("active-view", active);
  });
  document.querySelectorAll(".nav-tab").forEach((node) => {
    node.classList.toggle("is-active", node.dataset.view === viewId);
  });

  if (viewId === "monitorView") {
    refreshMonitorSnapshotIfNeeded()
      .catch(() => {})
      .finally(() => connectMonitorStream());
  } else {
    disconnectMonitorStream("切换到其他页面，已暂停实时连接");
  }
}

function syncSelectedMailboxRows() {
  document.querySelectorAll(".mailbox-row[data-mailbox-id]").forEach((node) => {
    node.classList.toggle("is-selected", node.dataset.mailboxId === state.selectedMailboxId);
  });
  document.querySelectorAll(".rail-card[data-mailbox-id]").forEach((node) => {
    node.classList.toggle("is-active", node.dataset.mailboxId === state.selectedMailboxId);
  });
}

function syncSelectedMessageRows() {
  document.querySelectorAll(".message-row[data-message-id]").forEach((node) => {
    node.classList.toggle("is-selected", node.dataset.messageId === state.selectedMessageId);
  });
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function fmtDate(value) {
  return value ? state.dateTimeFormatter.format(new Date(value)) : "-";
}

function fmtNumber(value) {
  return state.numberFormatter.format(Number(value || 0));
}

function fmtBytes(bytes) {
  if (bytes == null) return "-";
  const units = ["B", "KB", "MB", "GB"];
  let value = Number(bytes);
  let idx = 0;
  while (value >= 1024 && idx < units.length - 1) {
    value /= 1024;
    idx += 1;
  }
  return `${value.toFixed(value >= 100 || idx === 0 ? 0 : 1)} ${units[idx]}`;
}

function labelFor(mapping, value) {
  const key = String(value ?? "").trim();
  return mapping[key] || key || "-";
}

function showStatus(message, tone = "info") {
  const node = byId("appStatus");
  if (!node) return;
  if (!message) {
    node.className = "status-banner hidden";
    node.textContent = "";
    return;
  }
  node.className = `status-banner ${tone}`;
  node.textContent = message;
}

function showToast(message, tone = "success", duration = 3200) {
  const stack = byId("toastStack");
  if (!stack) return;
  const toast = document.createElement("div");
  toast.className = `toast ${tone}`;
  toast.id = `toast-${++toastSerial}`;
  toast.textContent = message;
  stack.appendChild(toast);
  requestAnimationFrame(() => toast.classList.add("show"));
  setTimeout(() => {
    toast.classList.remove("show");
    setTimeout(() => toast.remove(), 180);
  }, duration);
}

function setButtonBusy(target, busy, busyText) {
  const button = typeof target === "string" ? byId(target) : target;
  if (!button) return;
  if (!button.dataset.label) {
    button.dataset.label = button.textContent;
  }
  if (busy) {
    button.dataset.wasDisabled = button.disabled ? "true" : "false";
  }
  button.disabled = !!busy;
  button.textContent = busy ? busyText : button.dataset.label;
}

function restoreButtonLabel(target) {
  const button = typeof target === "string" ? byId(target) : target;
  if (button?.dataset.label) {
    button.textContent = button.dataset.label;
  }
  if (button?.dataset.wasDisabled) {
    button.disabled = button.dataset.wasDisabled === "true";
    delete button.dataset.wasDisabled;
  }
}

function isAbortError(error) {
  return error?.name === "AbortError";
}

function createManagedRequestController(key) {
  if (state.requestControllers[key]) {
    state.requestControllers[key].abort();
  }
  const controller = new AbortController();
  state.requestControllers[key] = controller;
  return controller;
}

function cancelPendingRequests(excludeKeys = []) {
  const keep = new Set(excludeKeys);
  Object.entries(state.requestControllers).forEach(([key, controller]) => {
    if (keep.has(key)) return;
    controller.abort();
    delete state.requestControllers[key];
  });
}

async function fetchJson(url, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(url, {
    credentials: "same-origin",
    cache: "no-store",
    ...options,
    headers,
  });

  if (response.status === 401 && url !== "/api/v1/admin/login") {
    showLogin();
    throw new Error("未登录或会话已失效");
  }

  if (!response.ok) {
    let detail = response.statusText;
    try {
      const payload = await response.json();
      detail =
        typeof payload.detail === "string"
          ? payload.detail
          : JSON.stringify(payload.detail ?? payload);
    } catch (_error) {}
    throw new Error(detail);
  }

  const contentType = response.headers.get("content-type") || "";
  return contentType.includes("application/json")
    ? response.json()
    : response;
}

async function fetchJsonManaged(key, url, options = {}) {
  const controller = createManagedRequestController(key);
  try {
    return await fetchJson(url, { ...options, signal: controller.signal });
  } finally {
    if (state.requestControllers[key] === controller) {
      delete state.requestControllers[key];
    }
  }
}

async function copyText(value, successMessage) {
  try {
    await navigator.clipboard.writeText(String(value || ""));
    showToast(successMessage, "success");
  } catch (_error) {
    showToast("复制失败，请手动复制。", "error");
  }
}

function showLogin() {
  cancelPendingRequests();
  disconnectMonitorStream();
  if (state.searchDebounceTimer) clearTimeout(state.searchDebounceTimer);
  if (state.messageSearchDebounceTimer) clearTimeout(state.messageSearchDebounceTimer);
  state.selectedMailboxId = null;
  state.selectedMessageId = null;
  state.selectedPolicyId = null;
  state.selectedDomainId = null;
  const copyBtn = byId("copySelectedAddressButton");
  if (copyBtn) {
    copyBtn.disabled = true;
    delete copyBtn.dataset.address;
  }
  byId("loginView")?.classList.remove("hidden");
  byId("appView")?.classList.add("hidden");
  showStatus("");
}

function showApp() {
  byId("loginView")?.classList.add("hidden");
  byId("appView")?.classList.remove("hidden");
  if (!document.querySelector(".nav-tab.is-active")) {
    switchView("overviewView");
  }
}

function statusPill(status) {
  const className =
    status === "disabled"
      ? "pill disabled"
      : status === "expired"
      ? "pill expired"
      : "pill";
  return `<span class="${className}">${escapeHtml(
    labelFor(mailboxStatusLabels, status)
  )}</span>`;
}

function domainStatusPill(status) {
  const className =
    status === "disabled"
      ? "pill disabled"
      : status === "pending"
      ? "pill expired"
      : "pill";
  return `<span class="${className}">${escapeHtml(
    labelFor(domainStatusLabels, status)
  )}</span>`;
}

function renderStats(stats) {
  const pairs = [
    ["活跃邮箱", stats.active_mailboxes],
    ["邮箱总数", stats.total_mailboxes],
    ["禁用邮箱", stats.disabled_mailboxes],
    ["过期邮箱", stats.expired_mailboxes],
    ["启用域名", stats.active_domains],
    ["待验证域名", stats.pending_domains],
    ["停用域名", stats.disabled_domains],
    ["邮件总数", stats.total_messages],
    ["24h 邮件", stats.messages_last_24h],
    ["附件总数", stats.total_attachments],
    ["24h 访问事件", stats.total_access_events],
  ];
  byId("statsGrid").innerHTML = pairs
    .map(
      ([label, value]) => `
      <div class="stat">
        <div class="label">${escapeHtml(label)}</div>
        <div class="value">${escapeHtml(fmtNumber(value ?? 0))}</div>
      </div>
    `
    )
    .join("");
}

function renderRecentMailboxes(items) {
  byId("recentMailboxRail").innerHTML =
    items
      .map(
        (item) => `
      <button type="button" class="rail-card ${state.selectedMailboxId === item.id ? "is-active" : ""}" data-mailbox-id="${escapeHtml(item.id)}">
        <div class="title">${escapeHtml(item.address)}</div>
        <div class="meta">
          状态：${escapeHtml(labelFor(mailboxStatusLabels, item.status))}<br>
          创建：${escapeHtml(fmtDate(item.created_at))}<br>
          过期：${escapeHtml(fmtDate(item.expires_at))}
        </div>
      </button>
    `
      )
      .join("") || `<div class="empty-box">还没有可展示的邮箱记录。</div>`;
  syncSelectedMailboxRows();
}

function renderMailboxCreateResult(payload) {
  byId("mailboxCreateResult").classList.remove("hidden");
  byId("mailboxCreateResult").innerHTML = [
    ["邮箱地址", payload.address],
    ["访问 Token", payload.token],
    ["创建时间", fmtDate(payload.created_at)],
    ["过期时间", fmtDate(payload.expires_at)],
    ["收件箱接口", payload.list_messages_url],
  ]
    .map(
      ([k, v]) => `
      <div class="kv">
        <div class="k">${escapeHtml(k)}</div>
        <div class="v">${escapeHtml(v)}</div>
      </div>
    `
    )
    .join("");
}

function clearMailboxCreateResult() {
  byId("mailboxCreateResult").classList.add("hidden");
  byId("mailboxCreateResult").innerHTML = "";
}

function renderMailboxCreateMeta(runtime, deployment) {
  state.runtimeConfig = runtime || {};
  state.deploymentConfig = deployment || {};
  const domains = state.deploymentConfig.base_domains || [];
  byId("mailboxCreateMeta").textContent =
    `允许基础域名：${domains.join(", ") || "-"} · TTL 范围：` +
    `${state.runtimeConfig.mailbox_min_ttl_minutes ?? "-"} - ${state.runtimeConfig.mailbox_max_ttl_minutes ?? "-"} 分钟 · ` +
    `未传 domain/address 时会随机选择一个 active 基础域名，并按长度范围生成前缀与子域`;
}

function renderMailboxTable(items) {
  state.mailboxItemsCache = items || [];
  const totalPages = Math.max(
    1,
    Math.ceil((state.mailboxTotal || 0) / state.mailboxPageSize)
  );
  byId("mailboxResultMeta").textContent = state.mailboxItemsCache.length
    ? `当前显示 ${state.mailboxItemsCache.length} / ${state.mailboxTotal} 个邮箱`
    : "当前没有匹配邮箱";

  if (byId("mailboxPageMeta")) {
    byId("mailboxPageMeta").textContent = `第 ${state.mailboxPage} / ${totalPages} 页`;
  }
  if (byId("mailboxPrevPageButton")) {
    byId("mailboxPrevPageButton").disabled = state.mailboxPage <= 1;
  }
  if (byId("mailboxNextPageButton")) {
    byId("mailboxNextPageButton").disabled = state.mailboxPage >= totalPages;
  }

  byId("mailboxTable").innerHTML =
    items
      .map(
        (item) => `
      <tr data-mailbox-id="${item.id}" class="mailbox-row ${state.selectedMailboxId === item.id ? "is-selected" : ""}" style="cursor:pointer">
        <td>${escapeHtml(item.address)}</td>
        <td>${statusPill(item.status)}</td>
        <td>${escapeHtml(fmtNumber(item.message_count ?? 0))}</td>
        <td>${escapeHtml(fmtDate(item.created_at))}</td>
        <td>${escapeHtml(fmtDate(item.expires_at))}</td>
      </tr>
    `
      )
      .join("") || `<tr><td colspan="5" class="subtle">没有匹配的邮箱</td></tr>`;
  syncSelectedMailboxRows();
}

function renderMailboxDetail(payload) {
  if (!payload || !payload.mailbox) {
    byId("mailboxDetail").classList.add("empty-box");
    byId("mailboxDetail").textContent = "没有可用的邮箱详情。";
    byId("mailboxMessages").innerHTML =
      `<tr><td colspan="4" class="subtle">暂无消息</td></tr>`;
    return;
  }

  const mailbox = payload.mailbox;
  state.selectedMailboxId = mailbox.id;

  const copyBtn = byId("copySelectedAddressButton");
  if (copyBtn) {
    copyBtn.disabled = false;
    copyBtn.dataset.address = mailbox.address;
  }

  byId("disableMailboxButton").disabled = mailbox.status !== "active";
  byId("mailboxDetail").classList.remove("empty-box");
  byId("mailboxDetail").innerHTML = [
    ["地址", mailbox.address],
    ["状态", labelFor(mailboxStatusLabels, mailbox.status)],
    ["基础域名", mailbox.base_domain],
    ["子域", mailbox.subdomain || "-"],
    ["前缀", mailbox.local_part],
    ["创建时间", fmtDate(mailbox.created_at)],
    ["过期时间", fmtDate(mailbox.expires_at)],
    ["最近访问", fmtDate(mailbox.last_accessed_at)],
    ["创建 IP", mailbox.created_ip || "-"],
    ["最近访问 IP", mailbox.last_access_ip || "-"],
  ]
    .map(
      ([k, v]) => `
      <div class="kv">
        <div class="k">${escapeHtml(k)}</div>
        <div class="v">${escapeHtml(v)}</div>
      </div>
    `
    )
    .join("");

  byId("mailboxMessages").innerHTML =
    (payload.messages || [])
      .map(
        (message) => `
      <tr data-message-id="${message.id}" class="message-row ${state.selectedMessageId === message.id ? "is-selected" : ""}" style="cursor:pointer">
        <td>${escapeHtml(message.subject || "(无主题)")}</td>
        <td>${escapeHtml(message.from_header || "-")}</td>
        <td>${escapeHtml(fmtDate(message.received_at))}</td>
        <td>${escapeHtml(fmtBytes(message.size_bytes))}</td>
      </tr>
    `
      )
      .join("") || `<tr><td colspan="4" class="subtle">这个邮箱还没有收到邮件</td></tr>`;
  syncSelectedMailboxRows();
  syncSelectedMessageRows();
}

function renderMessageTable(items) {
  state.messageItemsCache = items || [];
  const totalPages = Math.max(1, Math.ceil((state.messageTotal || 0) / state.messagePageSize));
  const keyword = byId("recentMessageSearch")?.value?.trim() || "";
  byId("messageResultMeta").textContent = state.messageItemsCache.length
    ? `当前显示 ${state.messageItemsCache.length} / ${state.messageTotal} 条消息${keyword ? ` · 关键词：${keyword}` : ""}`
    : keyword
      ? `没有匹配“${keyword}”的消息`
      : "当前还没有消息记录";

  if (byId("messagePageMeta")) {
    byId("messagePageMeta").textContent = `第 ${state.messagePage} / ${totalPages} 页`;
  }
  if (byId("messagePrevPageButton")) {
    byId("messagePrevPageButton").disabled = state.messagePage <= 1;
  }
  if (byId("messageNextPageButton")) {
    byId("messageNextPageButton").disabled = state.messagePage >= totalPages;
  }

  byId("recentMessages").innerHTML =
    state.messageItemsCache
      .map(
        (item) => `
      <tr data-message-id="${item.id}" class="message-row ${state.selectedMessageId === item.id ? "is-selected" : ""}" style="cursor:pointer">
        <td>${escapeHtml(item.mailbox_address)}</td>
        <td>${escapeHtml(item.subject || "(无主题)")}</td>
        <td>${escapeHtml(item.from_header || "-")}</td>
        <td>${escapeHtml(fmtDate(item.received_at))}</td>
      </tr>
    `
      )
      .join("") || `<tr><td colspan="4" class="subtle">没有匹配的消息</td></tr>`;
  syncSelectedMessageRows();
}

function renderMessageDetail(payload) {
  if (!payload || !payload.message) {
    byId("messageDetailBox").classList.add("empty-box");
    byId("messageDetailBox").textContent = "没有可用的消息详情。";
    syncSelectedMessageRows();
    return;
  }

  const message = payload.message;
  const attachments = payload.attachments || [];
  state.selectedMessageId = message.id;

  const text = String(message.text_body || "").trim();
  const html = String(message.html_body || "").trim();

  byId("messageDetailBox").classList.remove("empty-box");
  byId("messageMetaActions").innerHTML = `
    <div class="inline-actions">
      <button type="button" class="secondary" id="copyMessageSubjectButton">复制主题</button>
      <button type="button" class="secondary" id="openMessageMailboxButton" data-mailbox-id="${escapeHtml(message.mailbox_id)}">定位邮箱</button>
      <a class="text-link" href="/api/v1/admin/messages/${message.id}/raw" target="_blank" rel="noopener noreferrer">打开原始邮件</a>
    </div>
  `;

  byId("messageDetailBox").innerHTML = `
    <div class="kv-grid">
      <div class="kv"><div class="k">邮箱</div><div class="v">${escapeHtml(message.mailbox_address)}</div></div>
      <div class="kv"><div class="k">主题</div><div class="v">${escapeHtml(message.subject || "(无主题)")}</div></div>
      <div class="kv"><div class="k">发件人</div><div class="v">${escapeHtml(message.from_header || message.envelope_from || "-")}</div></div>
      <div class="kv"><div class="k">收件人</div><div class="v">${escapeHtml(message.envelope_to || "-")}</div></div>
      <div class="kv"><div class="k">接收时间</div><div class="v">${escapeHtml(fmtDate(message.received_at))}</div></div>
      <div class="kv"><div class="k">大小</div><div class="v">${escapeHtml(fmtBytes(message.size_bytes))}</div></div>
    </div>
    <div class="panel-subhead"><h3>正文文本</h3></div>
    <div class="message-body">${escapeHtml(text || "(无可提取文本)")}</div>
    <div class="panel-subhead"><h3>HTML 预览</h3></div>
    ${
      html
        ? `<iframe class="message-preview" sandbox="allow-same-origin" srcdoc="${escapeHtml(html)}"></iframe>`
        : `<div class="empty-box">当前邮件没有 HTML 正文，可查看文本正文或原始邮件</div>`
    }
    <div class="panel-subhead"><h3>附件</h3></div>
    <div class="kv-grid">
      ${
        attachments.length
          ? attachments
              .map(
                (item) => `
            <div class="kv">
              <div class="k">${escapeHtml(item.filename || "未命名附件")}</div>
              <div class="v">
                ${escapeHtml(item.content_type || "-")} · ${escapeHtml(fmtBytes(item.size_bytes))}
                <br>
                <a href="/api/v1/admin/messages/${message.id}/attachments/${item.id}" target="_blank" rel="noopener noreferrer">下载</a>
              </div>
            </div>
          `
              )
              .join("")
          : `<div class="empty-box">无附件</div>`
      }
    </div>
  `;
  syncSelectedMessageRows();
}

function renderLogItem(item, type) {
  return `
    <div class="log-item">
      <div class="meta">
        <strong>${escapeHtml(item.action)}</strong>
        · ${escapeHtml(fmtDate(item.created_at))}
        ${item.ip ? ` · ${escapeHtml(item.ip)}` : ""}
        ${type === "audit" ? ` · ${escapeHtml(item.admin_username)}` : ""}
      </div>
      <pre>${escapeHtml(JSON.stringify(item.metadata || {}, null, 2))}</pre>
    </div>
  `;
}

function renderMonitorItem(item) {
  return `
    <div class="log-item" data-monitor-id="${escapeHtml(item.id)}">
      <div class="meta">
        <strong>${escapeHtml(item.event_type)}</strong>
        · ${escapeHtml(item.source || "-")}
        · ${escapeHtml(item.level || "info")}
        · ${escapeHtml(fmtDate(item.created_at))}
        ${item.address ? ` · ${escapeHtml(item.address)}` : ""}
      </div>
      <div style="font-weight:600;margin-bottom:8px">${escapeHtml(item.summary || "-")}</div>
      <pre>${escapeHtml(JSON.stringify(item.payload || {}, null, 2))}</pre>
    </div>
  `;
}

function renderLogList(targetId, items, type) {
  byId(targetId).innerHTML =
    items.map((item) => renderLogItem(item, type)).join("") ||
    `<div class="empty-box">暂无记录</div>`;
}

function scrollToTopSmooth() {
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function renderMonitorList(items) {
  state.monitorEventIds = new Set(items.map((item) => Number(item.id) || 0));
  byId("monitorList").innerHTML =
    items.map((item) => renderMonitorItem(item)).join("") ||
    `<div class="empty-box">暂时还没有实时事件</div>`;
}

function renderConfig(runtime, deployment) {
  state.runtimeConfig = runtime || {};
  state.deploymentConfig = deployment || {};
  if (!Object.keys(state.runtimeConfig).length) {
    byId("configForm").innerHTML = `<div class="empty-box">暂无可编辑配置</div>`;
    byId("deploymentConfig").textContent = JSON.stringify(state.deploymentConfig, null, 2);
    if (byId("domainCreateMeta")) {
      byId("domainCreateMeta").textContent = `当前期望 MX 主机：${state.deploymentConfig.smtp_hostname || "-"} · 系统会同时检查根域 MX 与通配子域 MX。`;
    }
    return;
  }

  byId("configForm").innerHTML = Object.entries(state.runtimeConfig)
    .map(
      ([key, value]) => `
      <label>
        <span>${escapeHtml(fieldLabels[key] || key)}</span>
        <input data-config-key="${escapeHtml(key)}" type="number" value="${escapeHtml(value)}">
      </label>
    `
    )
    .join("");

  byId("deploymentConfig").textContent = JSON.stringify(state.deploymentConfig, null, 2);
  renderMailboxCreateMeta(state.runtimeConfig, state.deploymentConfig);
  if (byId("domainCreateMeta")) {
    byId("domainCreateMeta").textContent = `当前期望 MX 主机：${state.deploymentConfig.smtp_hostname || "-"} · 系统会同时检查根域 MX 与通配子域 MX。`;
  }
}

function renderDomainTable(items) {
  state.domainItems = items || [];
  const metaNode = byId("domainResultMeta");
  if (metaNode) {
    metaNode.textContent = state.domainItems.length
      ? `当前显示 ${state.domainItems.length} 个域名；仅 active 状态可用于新建邮箱与 SMTP 收件。`
      : "还没有基础域名，添加后系统会自动开始 MX 校验。";
  }

  byId("domainTable").innerHTML =
    state.domainItems.length
      ? state.domainItems
          .map(
            (item) => `
      <tr data-domain-id="${escapeHtml(item.id)}">
        <td>${escapeHtml(item.domain)}</td>
        <td>${domainStatusPill(item.status)}</td>
        <td>${escapeHtml(item.expected_mx_host || "-")}</td>
        <td>${escapeHtml(fmtDate(item.last_checked_at))}</td>
        <td>${escapeHtml(fmtNumber(item.failure_count ?? 0))}</td>
        <td title="${escapeHtml(item.last_error || "")}">${escapeHtml(item.last_error || "-")}</td>
        <td>
          <button type="button" class="secondary" data-domain-action="recheck" data-domain-id="${escapeHtml(item.id)}">重检</button>
        </td>
      </tr>`
          )
          .join("")
      : `<tr><td colspan="7" class="empty-cell">暂无域名记录</td></tr>`;
}

async function loadDomains() {
  const requestSeq = ++state.domainListRequestSeq;
  const params = new URLSearchParams({
    limit: "200",
    offset: "0",
    status: byId("domainStatusFilter")?.value || "all",
  });
  try {
    const data = await fetchJsonManaged("domains", `/api/v1/admin/domains?${params.toString()}`);
    if (requestSeq !== state.domainListRequestSeq) return [];
    const items = data.items || [];
    renderDomainTable(items);
    return items;
  } catch (error) {
    if (isAbortError(error)) return [];
    const metaNode = byId("domainResultMeta");
    if (metaNode) metaNode.textContent = error.message || "加载域名列表失败。";
    throw error;
  }
}

async function createManagedDomain() {
  const domain = byId("domainCreateInput").value.trim();
  const note = byId("domainNoteInput").value.trim();
  if (!domain) {
    showToast("请先输入基础域名。", "error");
    return;
  }

  setButtonBusy("createDomainButton", true, "添加中...");
  try {
    await fetchJson("/api/v1/admin/domains", {
      method: "POST",
      body: JSON.stringify({
        domain,
        note: note || null,
      }),
    });
    byId("domainCreateInput").value = "";
    byId("domainNoteInput").value = "";
    await Promise.all([loadDomains(), loadOverview(), refreshMonitorSnapshotIfNeeded(), loadEvents()]);
    showToast("域名已加入验证队列。", "success");
  } catch (error) {
    showToast(error.message || "添加域名失败。", "error");
  } finally {
    restoreButtonLabel("createDomainButton");
  }
}

async function recheckManagedDomain(domainId) {
  if (!domainId) return;
  try {
    await fetchJson(`/api/v1/admin/domains/${domainId}/recheck`, { method: "POST" });
    await Promise.all([loadDomains(), refreshMonitorSnapshotIfNeeded(), loadEvents()]);
    showToast("域名已重新加入校验队列。", "success");
  } catch (error) {
    showToast(error.message || "发起重检失败。", "error");
  }
}

function resetPolicyForm() {
  state.selectedPolicyId = null;
  byId("policyScopeInput").value = "recipient_base_domain";
  byId("policyPatternInput").value = "";
  byId("policyActionInput").value = "allow";
  byId("policyStatusInput").value = "active";
  byId("policyPriorityInput").value = "100";
  byId("policyNoteInput").value = "";
  byId("policyDeleteButton").disabled = true;
  byId("policyEditorMeta").textContent = "新建一条策略，或点击表格中的策略继续编辑。";
}

function fillPolicyForm(item) {
  state.selectedPolicyId = item.id;
  byId("policyScopeInput").value = item.scope;
  byId("policyPatternInput").value = item.pattern;
  byId("policyActionInput").value = item.action;
  byId("policyStatusInput").value = item.status;
  byId("policyPriorityInput").value = String(item.priority ?? 100);
  byId("policyNoteInput").value = item.note || "";
  byId("policyDeleteButton").disabled = false;
  byId("policyEditorMeta").textContent = `正在编辑策略 ${item.id}`;
}

function renderPolicyTable(items) {
  const keyword = byId("policySearchInput")?.value?.trim().toLowerCase() || "";
  state.policyItems = items || [];
  const filteredItems = state.policyItems.filter(
    (item) =>
      !keyword ||
      String(item.pattern || "").toLowerCase().includes(keyword) ||
      String(item.note || "").toLowerCase().includes(keyword)
  );

  const totalPages = Math.max(1, Math.ceil((state.policyTotal || 0) / state.policyPageSize));
  if (byId("policyPageMeta")) byId("policyPageMeta").textContent = `第 ${state.policyPage} / ${totalPages} 页`;
  if (byId("policyPrevPageButton")) byId("policyPrevPageButton").disabled = state.policyPage <= 1;
  if (byId("policyNextPageButton")) byId("policyNextPageButton").disabled = state.policyPage >= totalPages;

  byId("policyTable").innerHTML =
    filteredItems
      .map(
        (item) => `
      <tr data-policy-id="${item.id}" class="policy-row ${state.selectedPolicyId === item.id ? "is-selected" : ""}" style="cursor:pointer">
        <td>${escapeHtml(labelFor(policyScopeLabels, item.scope))}</td>
        <td>${escapeHtml(item.pattern)}</td>
        <td>${escapeHtml(labelFor(policyActionLabels, item.action))}</td>
        <td>${escapeHtml(labelFor(policyStatusLabels, item.status))}</td>
        <td>${escapeHtml(fmtNumber(item.priority))}</td>
        <td>${escapeHtml(fmtNumber(item.match_count ?? 0))}</td>
        <td>${escapeHtml(fmtDate(item.last_matched_at))}</td>
      </tr>
    `
      )
      .join("") || `<tr><td colspan="7" class="subtle">还没有策略记录</td></tr>`;
}

function setMonitorStatus(text) {
  const el = byId("monitorStatus");
  if (el) el.textContent = text;
}

function clearMonitorReconnect() {
  if (!state.monitorReconnectTimer) return;
  clearTimeout(state.monitorReconnectTimer);
  state.monitorReconnectTimer = null;
}

function disconnectMonitorStream(statusText = "未连接") {
  clearMonitorReconnect();
  if (state.monitorSource) {
    state.monitorSource.close();
    state.monitorSource = null;
  }
  setMonitorStatus(statusText);
}

function isMonitorStreaming() {
  return !!state.monitorSource && !document.hidden && !byId("appView").classList.contains("hidden") && activeViewId() === "monitorView";
}

async function loadMonitorSnapshot() {
  try {
    const data = await fetchJsonManaged("monitorSnapshot", "/api/v1/admin/monitor/events?limit=60");
    state.monitorEvents = data.items || [];
    state.monitorCursor = state.monitorEvents.reduce((maxId, item) => Math.max(maxId, Number(item.id) || 0), 0);
    renderMonitorList(state.monitorEvents);
    setMonitorStatus("已载入快照");
    return state.monitorEvents;
  } catch (error) {
    if (isAbortError(error)) return state.monitorEvents;
    setMonitorStatus("加载失败");
    showToast(error.message || "加载实时监控快照失败。", "error");
    throw error;
  }
}

async function refreshMonitorSnapshotIfNeeded() {
  return isMonitorStreaming() ? state.monitorEvents : loadMonitorSnapshot();
}

function prependMonitorEvent(item) {
  const itemId = Number(item.id) || 0;
  state.monitorCursor = Math.max(state.monitorCursor || 0, itemId);

  if (state.monitorEventIds.has(itemId)) {
    state.monitorEvents = [item, ...state.monitorEvents.filter((existing) => Number(existing.id) !== itemId)].slice(0, 100);
    renderMonitorList(state.monitorEvents);
    return;
  }

  state.monitorEvents = [item, ...state.monitorEvents].slice(0, 100);
  state.monitorEventIds.add(itemId);

  const list = byId("monitorList");
  if (!list || !list.children.length || list.querySelector(".empty-box")) {
    renderMonitorList(state.monitorEvents);
    return;
  }

  list.insertAdjacentHTML("afterbegin", renderMonitorItem(item));
  while (list.children.length > 100) {
    const removed = list.lastElementChild;
    const removedId = Number(removed?.dataset?.monitorId || 0);
    if (removedId) state.monitorEventIds.delete(removedId);
    removed?.remove();
  }
}

function connectMonitorStream() {
  clearMonitorReconnect();
  if (byId("appView").classList.contains("hidden") || document.hidden || activeViewId() !== "monitorView") return;
  if (!window.EventSource) {
    setMonitorStatus("当前浏览器不支持实时流");
    return;
  }

  if (state.monitorSource) state.monitorSource.close();

  setMonitorStatus("连接中...");
  const source = new EventSource(`/api/v1/admin/monitor/stream?after_id=${encodeURIComponent(state.monitorCursor || 0)}`);
  state.monitorSource = source;

  source.onopen = () => setMonitorStatus("实时连接中");

  source.addEventListener("system_event", (event) => {
    try {
      const item = JSON.parse(event.data);
      prependMonitorEvent(item);
      setMonitorStatus("实时连接中");
    } catch (_error) {}
  });

  source.addEventListener("heartbeat", () => {
    if (state.monitorSource === source) setMonitorStatus("实时连接中");
  });

  source.onerror = () => {
    if (state.monitorSource !== source) return;
    source.close();
    state.monitorSource = null;

    if (document.hidden || byId("appView").classList.contains("hidden")) {
      setMonitorStatus("页面在后台，已暂停实时连接");
      return;
    }

    setMonitorStatus("重连中...");
    clearMonitorReconnect();
    state.monitorReconnectTimer = setTimeout(() => {
      state.monitorReconnectTimer = null;
      connectMonitorStream();
    }, 3000);
  };
}

async function loadOverview() {
  showStatus("正在加载系统概览…", "info");
  try {
    const data = await fetchJsonManaged("overview", "/api/v1/admin/overview");
    renderStats(data.stats || {});
    renderRecentMailboxes(data.recent_mailboxes || []);
    renderConfig(data.runtime_config || data.runtime || {}, data.deployment || {});
    byId("recentMailboxMeta").textContent =
      (data.recent_mailboxes || []).length
        ? `最近创建的 ${(data.recent_mailboxes || []).length} 个邮箱，可直接跳转查看详情。`
        : "还没有创建记录，创建邮箱后会显示在这里。";
    byId("brandMeta").textContent = `Web/API 域名：${data.deployment?.web_hostname || "-"} · SMTP 主机：${data.deployment?.smtp_hostname || "-"}`;
    byId("overviewTime").textContent = `最近刷新：${fmtDate(new Date())}`;
    showStatus("系统概览与运行状态已同步。", "success");
    const btn = byId("refreshAllButton");
    if (btn) btn.removeAttribute("aria-busy");
    return data;
  } catch (error) {
    if (isAbortError(error)) return null;
    const btn = byId("refreshAllButton");
    if (btn) btn.removeAttribute("aria-busy");
    showStatus(error.message || "加载系统概览失败。", "error");
    throw error;
  }
}

async function loadMessages() {
  const requestSeq = ++state.messageListRequestSeq;
  const params = new URLSearchParams({
    limit: String(state.messagePageSize),
    offset: String((state.messagePage - 1) * state.messagePageSize),
    q: byId("recentMessageSearch").value.trim(),
  });

  try {
    const data = await fetchJsonManaged("messages", `/api/v1/admin/messages?${params.toString()}`);
    if (requestSeq !== state.messageListRequestSeq) return [];
    state.messageTotal = Number(data.total || 0);
    const items = data.items || [];
    const totalPages = Math.max(1, Math.ceil(state.messageTotal / state.messagePageSize));
    if (!items.length && state.messagePage > totalPages) {
      state.messagePage = totalPages;
      return loadMessages();
    }
    renderMessageTable(items);
    return items;
  } catch (error) {
    if (isAbortError(error)) return [];
    showToast(error.message || "加载消息列表失败。", "error");
    throw error;
  }
}

async function loadMailboxes() {
  const requestSeq = ++state.mailboxListRequestSeq;
  const params = new URLSearchParams({
    limit: String(state.mailboxPageSize),
    offset: String((state.mailboxPage - 1) * state.mailboxPageSize),
    status: byId("mailboxStatus").value,
    q: byId("mailboxSearch").value.trim(),
  });

  try {
    const data = await fetchJsonManaged("mailboxes", `/api/v1/admin/mailboxes?${params.toString()}`);
    if (requestSeq !== state.mailboxListRequestSeq) return [];
    state.mailboxTotal = Number(data.total || 0);
    const items = data.items || [];
    const totalPages = Math.max(1, Math.ceil(state.mailboxTotal / state.mailboxPageSize));
    if (!items.length && state.mailboxPage > totalPages) {
      state.mailboxPage = totalPages;
      return loadMailboxes();
    }
    renderMailboxTable(items);
    return items;
  } catch (error) {
    if (isAbortError(error)) return [];
    showToast(error.message || "加载邮箱列表失败。", "error");
    throw error;
  }
}

async function loadMailboxDetail(mailboxId) {
  const requestSeq = ++state.mailboxDetailRequestSeq;
  state.selectedMailboxId = mailboxId;
  syncSelectedMailboxRows();
  byId("mailboxDetail").textContent = "正在加载邮箱详情…";
  byId("mailboxDetail").classList.add("empty-box");

  try {
    const data = await fetchJsonManaged("mailboxDetail", `/api/v1/admin/mailboxes/${mailboxId}`);
    if (requestSeq !== state.mailboxDetailRequestSeq) return null;
    renderMailboxDetail(data);
    return data;
  } catch (error) {
    if (isAbortError(error)) return null;
    byId("mailboxDetail").textContent = "加载邮箱详情失败，请重试。";
    showToast(error.message || "加载邮箱详情失败。", "error");
    throw error;
  }
}

async function loadMessageDetail(messageId) {
  const requestSeq = ++state.messageDetailRequestSeq;
  state.selectedMessageId = messageId;
  syncSelectedMessageRows();
  byId("messageDetailBox").textContent = "正在加载消息详情…";
  byId("messageDetailBox").classList.add("empty-box");

  try {
    const data = await fetchJsonManaged("messageDetail", `/api/v1/admin/messages/${messageId}`);
    if (requestSeq !== state.messageDetailRequestSeq) return null;
    renderMessageDetail(data);
    return data;
  } catch (error) {
    if (isAbortError(error)) return null;
    byId("messageDetailBox").textContent = "加载消息详情失败，请重试。";
    showToast(error.message || "加载消息详情失败。", "error");
    throw error;
  }
}

async function loadEvents() {
  try {
    const [events, audit] = await Promise.all([
      fetchJsonManaged("accessEvents", "/api/v1/admin/events?limit=50"),
      fetchJsonManaged("auditEvents", "/api/v1/admin/audit?limit=50"),
    ]);
    renderLogList("eventsList", events.items || [], "events");
    renderLogList("auditList", audit.items || [], "audit");
  } catch (error) {
    if (isAbortError(error)) return;
    showToast(error.message || "加载日志列表失败。", "error");
    throw error;
  }
}

async function loadPolicies() {
  const requestSeq = ++state.policyListRequestSeq;
  const params = new URLSearchParams({
    limit: String(state.policyPageSize),
    offset: String((state.policyPage - 1) * state.policyPageSize),
    status: byId("policyStatusFilter").value,
  });

  const scope = byId("policyScopeFilter").value;
  if (scope) params.set("scope", scope);

  try {
    const data = await fetchJsonManaged("policies", `/api/v1/admin/policies?${params.toString()}`);
    if (requestSeq !== state.policyListRequestSeq) return [];
    state.policyTotal = Number(data.total || 0);
    const items = data.items || [];
    if (state.selectedPolicyId && !items.some((item) => item.id === state.selectedPolicyId)) {
      resetPolicyForm();
    }
    renderPolicyTable(items);
    return items;
  } catch (error) {
    if (isAbortError(error)) return [];
    showToast(error.message || "加载域名策略失败。", "error");
    throw error;
  }
}

async function saveRuntimeConfig() {
  const payload = {};
  document.querySelectorAll("[data-config-key]").forEach((input) => {
    payload[input.dataset.configKey] = Number(input.value);
  });

  setButtonBusy("saveConfigButton", true, "保存中...");
  try {
    const data = await fetchJson("/api/v1/admin/config", {
      method: "PUT",
      body: JSON.stringify(payload),
    });
    renderConfig(data.runtime || {}, data.deployment || {});
    showToast("运行时配置已保存并立即生效。", "success");
  } catch (error) {
    showToast(error.message || "保存运行时配置失败。", "error");
  } finally {
    restoreButtonLabel("saveConfigButton");
  }
}

async function createAdminMailbox() {
  const address = byId("mailboxCreateAddress").value.trim();
  const ttlValue = byId("mailboxCreateTtl").value.trim();
  const payload = {};

  if (address) payload.address = address;
  if (ttlValue) {
    const ttl = Number(ttlValue);
    if (!Number.isInteger(ttl) || ttl < 1) {
      showToast("TTL 必须是大于 0 的整数分钟数。", "error");
      return;
    }
    payload.ttl_minutes = ttl;
  }

  setButtonBusy("createMailboxButton", true, "创建中...");
  try {
    const data = await fetchJson("/api/v1/admin/mailboxes", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    renderMailboxCreateResult(data);
    byId("mailboxCreateAddress").value = data.address;
    byId("mailboxSearch").value = data.address;
    byId("mailboxStatus").value = "all";
    state.mailboxPage = 1;

    await Promise.all([loadOverview(), loadEvents(), refreshMonitorSnapshotIfNeeded()]);
    const items = await loadMailboxes();
    const createdMailbox = items.find(
      (item) => String(item.address || "").toLowerCase() === data.address.toLowerCase()
    );
    if (createdMailbox) await loadMailboxDetail(createdMailbox.id);

    showToast(`邮箱 ${data.address} 创建成功。`, "success");
  } catch (error) {
    showToast(error.message || "创建邮箱失败。", "error", 4200);
  } finally {
    restoreButtonLabel("createMailboxButton");
  }
}

async function disableSelectedMailbox() {
  if (!state.selectedMailboxId) return;
  if (!confirm("确定禁用这个邮箱吗？禁用后将立即拒收新邮件。")) return;

  setButtonBusy("disableMailboxButton", true, "禁用中...");
  try {
    await fetchJson(`/api/v1/admin/mailboxes/${state.selectedMailboxId}/disable`, {
      method: "POST",
    });
    await Promise.all([loadMailboxes(), loadEvents(), refreshMonitorSnapshotIfNeeded()]);
    await loadMailboxDetail(state.selectedMailboxId);
    showToast("邮箱已禁用，后续新邮件会被拒收。", "info");
  } catch (error) {
    showToast(error.message || "禁用邮箱失败。", "error");
  } finally {
    restoreButtonLabel("disableMailboxButton");
  }
}

async function savePolicy() {
  const priority = Number(byId("policyPriorityInput").value || 100);
  if (!Number.isInteger(priority) || priority < 0 || priority > 100000) {
    throw new Error("优先级必须是 0 到 100000 之间的整数。");
  }

  const payload = {
    scope: byId("policyScopeInput").value,
    pattern: byId("policyPatternInput").value.trim(),
    action: byId("policyActionInput").value,
    status: byId("policyStatusInput").value,
    priority,
    note: byId("policyNoteInput").value.trim() || null,
  };

  if (!payload.pattern) {
    showToast("匹配规则不能为空。", "error");
    return;
  }

  setButtonBusy("policySaveButton", true, "保存中...");
  try {
    if (state.selectedPolicyId) {
      await fetchJson(`/api/v1/admin/policies/${state.selectedPolicyId}`, {
        method: "PUT",
        body: JSON.stringify(payload),
      });
    } else {
      await fetchJson("/api/v1/admin/policies", {
        method: "POST",
        body: JSON.stringify(payload),
      });
    }

    await Promise.all([loadPolicies(), refreshMonitorSnapshotIfNeeded(), loadEvents()]);
    showToast("域名策略已保存。", "success");
  } catch (error) {
    showToast(error.message || "保存域名策略失败。", "error");
  } finally {
    restoreButtonLabel("policySaveButton");
  }
}

async function deleteSelectedPolicy() {
  if (!state.selectedPolicyId) return;
  if (!confirm("确定删除这条域名策略吗？")) return;

  setButtonBusy("policyDeleteButton", true, "删除中...");
  try {
    await fetchJson(`/api/v1/admin/policies/${state.selectedPolicyId}`, {
      method: "DELETE",
    });
    resetPolicyForm();
    await Promise.all([loadPolicies(), refreshMonitorSnapshotIfNeeded(), loadEvents()]);
    showToast("域名策略已删除。", "info");
  } catch (error) {
    showToast(error.message || "删除域名策略失败。", "error");
  } finally {
    restoreButtonLabel("policyDeleteButton");
  }
}

async function refreshAll({ notify = true } = {}) {
  showStatus("正在刷新管理台数据…", "info");
  const btn = byId("refreshAllButton");
  if (btn) btn.setAttribute("aria-busy", "true");

  try {
    await Promise.all([
      loadOverview(),
      loadMessages(),
      loadMailboxes(),
      loadDomains(),
      loadEvents(),
      activeViewId() === "monitorView" ? loadMonitorSnapshot() : Promise.resolve(state.monitorEvents),
      loadPolicies(),
    ]);
    if (notify) showToast("管理台数据已刷新。", "success");
  } catch (error) {
    if (btn) btn.removeAttribute("aria-busy");
    showStatus(error.message || "刷新失败，请稍后重试。", "error");
    throw error;
  }
}

async function tryRestoreSession() {
  try {
    await fetchJson("/api/v1/admin/session");
    showApp();
    resetPolicyForm();
    try {
      await refreshAll({ notify: false });
    } catch (_error) {
      showStatus("会话已恢复，但部分数据加载失败，可稍后手动刷新。", "warning");
    }
    connectMonitorStream();
  } catch (_error) {
    showLogin();
  }
}

document.addEventListener("visibilitychange", () => {
  if (byId("appView").classList.contains("hidden")) return;
  if (document.hidden) {
    disconnectMonitorStream("页面在后台，已暂停实时连接");
    return;
  }
  refreshMonitorSnapshotIfNeeded()
    .catch(() => {})
    .finally(() => connectMonitorStream());
});

window.addEventListener("beforeunload", () => {
  cancelPendingRequests();
  disconnectMonitorStream();
});

byId("loginButton").addEventListener("click", async () => {
  const username = byId("loginUsername").value.trim();
  const password = byId("loginPassword").value;
  byId("loginError").textContent = "";

  setButtonBusy("loginButton", true, "登录中...");
  try {
    await fetchJson("/api/v1/admin/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    byId("loginPassword").value = "";
    showApp();
    resetPolicyForm();
    try {
      await refreshAll({ notify: false });
    } catch (_error) {
      showStatus("登录成功，但部分数据加载失败，可稍后手动刷新。", "warning");
    }
    connectMonitorStream();
  } catch (error) {
    byId("loginError").textContent = error.message || "登录失败";
  } finally {
    restoreButtonLabel("loginButton");
  }
});

byId("logoutButton").addEventListener("click", async () => {
  try {
    await fetchJson("/api/v1/admin/logout", { method: "POST" });
  } finally {
    showLogin();
  }
});

byId("refreshAllButton").addEventListener("click", () => refreshAll().catch(() => {}));
byId("mailboxSearchButton").addEventListener("click", () => {
  state.mailboxPage = 1;
  loadMailboxes().catch(() => {});
});
byId("mailboxStatus").addEventListener("change", () => {
  state.mailboxPage = 1;
  loadMailboxes().catch(() => {});
});
byId("mailboxSearch").addEventListener("input", () => {
  if (state.searchDebounceTimer) clearTimeout(state.searchDebounceTimer);
  state.searchDebounceTimer = setTimeout(() => {
    state.searchDebounceTimer = null;
    state.mailboxPage = 1;
    loadMailboxes().catch(() => {});
  }, 260);
});
byId("mailboxSearch").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    if (state.searchDebounceTimer) clearTimeout(state.searchDebounceTimer);
    state.mailboxPage = 1;
    loadMailboxes().catch(() => {});
  }
});

byId("mailboxPrevPageButton")?.addEventListener("click", () => {
  if (state.mailboxPage > 1) {
    state.mailboxPage -= 1;
    loadMailboxes().catch(() => {});
  }
});
byId("mailboxNextPageButton")?.addEventListener("click", () => {
  state.mailboxPage += 1;
  loadMailboxes().catch(() => {});
});

byId("messageSearchButton")?.addEventListener("click", () => {
  if (state.messageSearchDebounceTimer) clearTimeout(state.messageSearchDebounceTimer);
  state.messagePage = 1;
  loadMessages().catch(() => {});
});
byId("messagePrevPageButton")?.addEventListener("click", () => {
  if (state.messagePage > 1) {
    state.messagePage -= 1;
    loadMessages().catch(() => {});
  }
});
byId("messageNextPageButton")?.addEventListener("click", () => {
  state.messagePage += 1;
  loadMessages().catch(() => {});
});

byId("policyPrevPageButton")?.addEventListener("click", () => {
  if (state.policyPage > 1) {
    state.policyPage -= 1;
    loadPolicies().catch(() => {});
  }
});
byId("policyNextPageButton")?.addEventListener("click", () => {
  state.policyPage += 1;
  loadPolicies().catch(() => {});
});

byId("mailboxTable").addEventListener("click", (event) => {
  const row = event.target.closest(".mailbox-row[data-mailbox-id]");
  if (row) loadMailboxDetail(row.dataset.mailboxId).catch(() => {});
});

byId("recentMailboxRail").addEventListener("click", (event) => {
  const node = event.target.closest("[data-mailbox-id]");
  if (node) loadMailboxDetail(node.dataset.mailboxId).catch(() => {});
});

byId("mailboxMessages").addEventListener("click", (event) => {
  const row = event.target.closest(".message-row[data-message-id]");
  if (row) loadMessageDetail(row.dataset.messageId).catch(() => {});
});

byId("recentMessages").addEventListener("click", (event) => {
  const row = event.target.closest(".message-row[data-message-id]");
  if (row) loadMessageDetail(row.dataset.messageId).catch(() => {});
});

byId("createMailboxButton").addEventListener("click", createAdminMailbox);
byId("saveConfigButton").addEventListener("click", saveRuntimeConfig);
byId("disableMailboxButton").addEventListener("click", disableSelectedMailbox);
byId("createDomainButton")?.addEventListener("click", createManagedDomain);
byId("domainRefreshButton")?.addEventListener("click", () => loadDomains().catch(() => {}));
byId("domainStatusFilter")?.addEventListener("change", () => loadDomains().catch(() => {}));
byId("domainTable")?.addEventListener("click", (event) => {
  const button = event.target.closest("[data-domain-action='recheck'][data-domain-id]");
  if (!button) return;
  recheckManagedDomain(button.dataset.domainId).catch(() => {});
});

byId("policyRefreshButton").addEventListener("click", () => {
  state.policyPage = 1;
  loadPolicies().catch(() => {});
});
byId("policyStatusFilter").addEventListener("change", () => {
  state.policyPage = 1;
  loadPolicies().catch(() => {});
});
byId("policyScopeFilter").addEventListener("change", () => {
  state.policyPage = 1;
  loadPolicies().catch(() => {});
});
byId("policySearchInput")?.addEventListener("input", () => renderPolicyTable(state.policyItems));

byId("policyTable").addEventListener("click", (event) => {
  const row = event.target.closest(".policy-row[data-policy-id]");
  if (!row) return;
  const item = state.policyItems.find((entry) => entry.id === row.dataset.policyId);
  if (!item) return;
  fillPolicyForm(item);
  renderPolicyTable(state.policyItems);
});

byId("policySaveButton").addEventListener("click", () =>
  savePolicy().catch((error) => showToast(error.message || "保存策略失败。", "error"))
);
byId("policyResetButton").addEventListener("click", resetPolicyForm);
byId("policyDeleteButton").addEventListener("click", deleteSelectedPolicy);
byId("monitorReconnectButton").addEventListener("click", connectMonitorStream);

byId("copySelectedAddressButton")?.addEventListener("click", () => {
  const address = byId("copySelectedAddressButton").dataset.address;
  if (address) copyText(address, "邮箱地址已复制。");
});

document.addEventListener("click", (event) => {
  const navTab = event.target.closest(".nav-tab[data-view]");
  if (navTab) {
    switchView(navTab.dataset.view);
    return;
  }
  if (event.target?.id === "copyMessageSubjectButton") {
    const subjectNode = document.querySelector("#messageDetailBox .kv:nth-child(2) .v");
    if (subjectNode) copyText(subjectNode.textContent, "邮件主题已复制。");
  }
  if (event.target?.id === "openMessageMailboxButton") {
    const mailboxId = event.target.dataset.mailboxId;
    if (mailboxId) {
      switchView("mailboxesView");
      loadMailboxDetail(mailboxId).catch(() => {});
    }
  }
});

if (byId("recentMessageSearch")) {
  byId("recentMessageSearch").addEventListener("input", () => {
    if (state.messageSearchDebounceTimer) clearTimeout(state.messageSearchDebounceTimer);
    state.messageSearchDebounceTimer = setTimeout(() => {
      state.messageSearchDebounceTimer = null;
      state.messagePage = 1;
      loadMessages().catch(() => {});
    }, 260);
  });
  byId("recentMessageSearch").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      if (state.messageSearchDebounceTimer) clearTimeout(state.messageSearchDebounceTimer);
      state.messagePage = 1;
      loadMessages().catch(() => {});
    }
    if (event.key === "Escape") {
      event.target.value = "";
      if (state.messageSearchDebounceTimer) clearTimeout(state.messageSearchDebounceTimer);
      state.messagePage = 1;
      loadMessages().catch(() => {});
    }
  });
}

byId("appStatus")?.addEventListener("click", scrollToTopSmooth);

["mailboxCreateAddress", "mailboxCreateTtl", "domainCreateInput", "domainNoteInput", "loginUsername", "loginPassword"].forEach((id) => {
  const node = byId(id);
  if (!node) return;
  node.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    if (id.startsWith("login")) {
      byId("loginButton").click();
    } else if (id.startsWith("domain")) {
      createManagedDomain();
    } else {
      createAdminMailbox();
    }
  });
});

tryRestoreSession();
