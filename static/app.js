const state = { path: "", entries: [] };
const $ = (id) => document.getElementById(id);
const csrf = () => document.cookie.split("; ").find((x) => x.startsWith("fileweb_csrf="))?.split("=")[1] || "";
const api = async (url, options = {}) => {
  const opts = { ...options, headers: { ...(options.headers || {}) } };
  if (opts.body && !(opts.body instanceof FormData)) opts.headers["Content-Type"] = "application/json";
  if (opts.method && opts.method !== "GET") opts.headers["X-CSRF-Token"] = csrf();
  const response = await fetch(url, opts);
  const data = response.headers.get("content-type")?.includes("application/json") ? await response.json() : null;
  if (response.status === 401 && !location.pathname.startsWith("/login")) {
    location.href = `/login.html?next=${encodeURIComponent(location.pathname + location.search)}`;
  }
  if (!response.ok) {
    const error = new Error(data?.error || `请求失败 (${response.status})`);
    error.status = response.status;
    error.data = data;
    throw error;
  }
  return data;
};
const esc = (value) => String(value).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[c]));
const formatSize = (size) => size == null ? "—" : size < 1024 ? `${size} B` : size < 1024 ** 2 ? `${(size / 1024).toFixed(1)} KB` : `${(size / 1024 ** 2).toFixed(1)} MB`;
const formatTime = (seconds) => new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(seconds * 1000));
const pathUrl = (path) => encodeURIComponent(path);

function toast(message, error = false) {
  const node = $("toast");
  node.textContent = message;
  node.className = `toast show ${error ? "error" : ""}`;
  setTimeout(() => node.className = "toast", 2800);
}

function renderCrumbs() {
  const parts = state.path ? state.path.split("/") : [];
  const nodes = [`<button data-path="">根目录</button>`];
  parts.forEach((part, i) => nodes.push(`<span>/</span><button data-path="${esc(parts.slice(0, i + 1).join("/"))}">${esc(part)}</button>`));
  $("crumbs").innerHTML = nodes.join("");
  $("crumbs").querySelectorAll("button").forEach((button) => button.addEventListener("click", () => load(button.dataset.path)));
  $("locationLabel").textContent = state.path || "根目录";
  renderWorkdirOptions();
}

function renderRows() {
  const rows = state.entries.map((entry, index) => {
    const icon = entry.kind === "directory" ? "▰" : entry.preview === "html" ? "⌘" : entry.preview === "image" ? "▧" : "▤";
    const action = entry.kind === "directory"
      ? `<button class="row-action" data-open="${esc(entry.path)}">打开</button><button class="row-action" data-move="${esc(entry.path)}">移动</button><button class="row-action danger" data-delete="${esc(entry.path)}" data-kind="directory">删除</button>`
      : `<button class="row-action" data-preview="${esc(entry.path)}">查看</button><a class="row-action" href="/api/download?path=${pathUrl(entry.path)}">下载</a><button class="row-action" data-share="${esc(entry.path)}">分享</button><button class="row-action" data-move="${esc(entry.path)}">移动</button><button class="row-action danger" data-delete="${esc(entry.path)}" data-kind="file">删除</button>`;
    return `<tr style="animation-delay:${Math.min(index * 35, 280)}ms"><td><button class="name-cell" data-${entry.kind === "directory" ? "open" : "preview"}="${esc(entry.path)}"><span class="file-icon ${entry.kind}">${icon}</span><span>${esc(entry.name)}</span></button></td><td>${entry.kind === "directory" ? "文件夹" : esc(entry.preview === "html" ? "HTML 页面" : entry.mime || "文件")}</td><td>${formatSize(entry.size)}</td><td>${formatTime(entry.mtime)}</td><td class="actions">${action}</td></tr>`;
  }).join("");
  $("fileRows").innerHTML = rows;
  $("emptyState").hidden = state.entries.length !== 0;
  $("itemCount").textContent = `${state.entries.length} 项`;
  document.querySelectorAll("[data-open]").forEach((node) => node.addEventListener("click", () => load(node.dataset.open)));
  document.querySelectorAll("[data-preview]").forEach((node) => node.addEventListener("click", () => preview(node.dataset.preview)));
  document.querySelectorAll("[data-move]").forEach((node) => node.addEventListener("click", () => openMoveDialog(node.dataset.move)));
  document.querySelectorAll("[data-delete]").forEach((node) => node.addEventListener("click", () => deleteEntry(node.dataset.delete, node.dataset.kind)));
  document.querySelectorAll("[data-share]").forEach((node) => node.addEventListener("click", () => shareEntry(node.dataset.share)));
}

async function load(path = state.path) {
  try {
    const data = await api(`/api/list?path=${pathUrl(path)}`);
    state.path = data.path;
    state.entries = data.entries;
    renderCrumbs();
    renderRows();
  } catch (error) { toast(error.message, true); }
}

function preview(path) {
  const page = window.open(`/preview?path=${pathUrl(path)}`, "_blank", "noopener,noreferrer");
  if (!page) toast("浏览器阻止了新窗口，请允许弹出窗口", true);
}

function openMoveDialog(path) {
  $("moveSourceLabel").textContent = path;
  $("moveDestination").value = state.path;
  $("moveDialog").dataset.source = path;
  $("moveDialog").showModal();
  setTimeout(() => { $("moveDestination").focus(); $("moveDestination").select(); }, 0);
}

async function deleteEntry(path, kind) {
  const question = kind === "directory" ? `确认删除文件夹“${path}”及其中全部内容吗？此操作不可撤销。` : `确认删除文件“${path}”吗？此操作不可撤销。`;
  if (!window.confirm(question)) return;
  try {
    await api("/api/delete", { method: "DELETE", body: JSON.stringify({ path, recursive: kind === "directory" }) });
    toast("已删除");
    await load();
  } catch (error) { toast(error.message, true); }
}

async function shareEntry(path) {
  const rawDays = window.prompt("链接有效期（1-30 天）", "7");
  if (rawDays === null) return;
  const expiresDays = Number(rawDays);
  if (!Number.isInteger(expiresDays) || expiresDays < 1 || expiresDays > 30) return toast("有效期应为 1-30 天", true);
  try {
    const result = await api("/api/share", { method: "POST", body: JSON.stringify({ path, expires_days: expiresDays }) });
    const url = new URL(result.url, location.origin).href;
    try { await navigator.clipboard.writeText(url); toast("只读链接已复制"); }
    catch (_) { window.prompt("复制只读链接", url); }
  } catch (error) { toast(error.message, true); }
}

/* === FLOATING ASSISTANT === */
const orb = $("assistantOrb");
const panel = $("assistantPanel");
const scrim = $("assistantScrim");
let assistantOpen = false;
let restoreFocus = orb;

const assistantStorageKey = "fileweb-assistant-conversations";
const assistantState = { conversations: [], activeId: "", workdir: "", directories: [], jobs: new Map() };
const jobStatusLabels = { queued: "排队中", running: "执行中", succeeded: "已完成", failed: "执行失败", timeout: "已超时" };

function makeId() {
  return globalThis.crypto?.randomUUID?.() || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function createConversation(path = state.path) {
  const conversation = { id: makeId(), title: "新对话", path: path || "", created: Date.now(), messages: [] };
  assistantState.conversations.unshift(conversation);
  assistantState.activeId = conversation.id;
  assistantState.workdir = conversation.path;
  saveAssistantState();
  renderConversationList();
  renderMessages();
  renderWorkdirOptions();
  return conversation;
}

function activeConversation() {
  return assistantState.conversations.find((item) => item.id === assistantState.activeId) || null;
}

function saveAssistantState() {
  try {
    localStorage.setItem(assistantStorageKey, JSON.stringify({ conversations: assistantState.conversations.slice(0, 30), activeId: assistantState.activeId, workdir: assistantState.workdir }));
  } catch (_) { /* Storage can be disabled by the browser. */ }
}

function loadAssistantState() {
  try {
    const saved = JSON.parse(localStorage.getItem(assistantStorageKey) || "null");
    if (saved && Array.isArray(saved.conversations)) {
      assistantState.conversations = saved.conversations.filter((item) => item && typeof item.id === "string").map((item) => ({
        id: item.id, title: typeof item.title === "string" ? item.title : "新对话", path: typeof item.path === "string" ? item.path : "", created: Number(item.created) || Date.now(),
        messages: Array.isArray(item.messages) ? item.messages.filter((message) => message && ["user", "assistant"].includes(message.role)).map((message) => ({ id: String(message.id || makeId()), role: message.role, content: String(message.content || ""), status: message.status || "done", created: Number(message.created) || Date.now() })) : [],
      }));
      assistantState.activeId = assistantState.conversations.some((item) => item.id === saved.activeId) ? saved.activeId : "";
      assistantState.workdir = typeof saved.workdir === "string" ? saved.workdir : "";
    }
  } catch (_) { /* Ignore malformed local UI state. */ }
  if (!assistantState.conversations.length) createConversation(state.path);
  if (!assistantState.activeId) assistantState.activeId = assistantState.conversations[0].id;
  assistantState.workdir = activeConversation()?.path ?? assistantState.workdir ?? "";
}

function renderConversationList() {
  const node = $("conversationList");
  if (!node) return;
  node.innerHTML = assistantState.conversations.map((conversation) => {
    const last = conversation.messages[conversation.messages.length - 1];
    const preview = last?.content?.replace(/\s+/g, " ").trim() || "开始一段新对话";
    return `<div class="conversation-item-wrap ${conversation.id === assistantState.activeId ? "active" : ""}"><button type="button" class="conversation-item" data-conversation="${esc(conversation.id)}"><strong>${esc(conversation.title || "新对话")}</strong><span>${esc(preview.slice(0, 38))}</span></button><span class="conversation-actions"><button type="button" data-rename-conversation="${esc(conversation.id)}" title="重命名对话" aria-label="重命名对话">✎</button><button type="button" data-delete-conversation="${esc(conversation.id)}" title="删除对话" aria-label="删除对话">×</button></span></div>`;
  }).join("");
  node.querySelectorAll("[data-conversation]").forEach((button) => button.addEventListener("click", () => switchConversation(button.dataset.conversation)));
  node.querySelectorAll("[data-rename-conversation]").forEach((button) => button.addEventListener("click", (event) => { event.stopPropagation(); renameConversation(button.dataset.renameConversation); }));
  node.querySelectorAll("[data-delete-conversation]").forEach((button) => button.addEventListener("click", (event) => { event.stopPropagation(); deleteConversation(button.dataset.deleteConversation); }));
}

function renameConversation(id) {
  const conversation = assistantState.conversations.find((item) => item.id === id);
  if (!conversation) return;
  const title = window.prompt("对话名称", conversation.title || "新对话")?.trim();
  if (!title) return;
  conversation.title = title.slice(0, 60);
  saveAssistantState();
  renderConversationList();
}

function deleteConversation(id) {
  const index = assistantState.conversations.findIndex((item) => item.id === id);
  if (index < 0 || assistantState.conversations.length === 1) return toast("至少保留一个对话", true);
  const conversation = assistantState.conversations[index];
  if (!window.confirm(`删除“${conversation.title || "新对话"}”？`)) return;
  assistantState.conversations.splice(index, 1);
  if (assistantState.activeId === id) {
    assistantState.activeId = assistantState.conversations[0].id;
    assistantState.workdir = activeConversation()?.path || "";
  }
  saveAssistantState();
  renderConversationList();
  renderMessages();
  renderWorkdirOptions();
}

function switchConversation(id) {
  const conversation = assistantState.conversations.find((item) => item.id === id);
  if (!conversation) return;
  assistantState.activeId = id;
  assistantState.workdir = conversation.path || "";
  saveAssistantState();
  renderConversationList();
  renderMessages();
  renderWorkdirOptions();
  $("agentPrompt").value = "";
  setTimeout(() => $("agentPrompt").focus(), 0);
}

function renderMessages() {
  const node = $("chatMessages");
  if (!node) return;
  const conversation = activeConversation();
  if (!conversation?.messages.length) {
    node.innerHTML = '<div class="chat-empty"><span class="chat-empty-icon">✦</span><strong>准备开始</strong><p>告诉助手你想在当前目录中完成什么。</p></div>';
    return;
  }
  node.innerHTML = conversation.messages.map((message) => {
    const status = message.role === "assistant" && message.status && message.status !== "done" ? `<span class="message-status">${esc(jobStatusLabels[message.status] || message.status)}</span>` : "";
    const content = message.content ? esc(message.content) : message.role === "assistant" ? "<span class=\"typing-indicator\"><i></i><i></i><i></i></span>" : "";
    return `<article class="chat-message ${message.role}" data-message="${esc(message.id)}"><div class="message-meta"><span>${message.role === "user" ? "你" : "文件助手"}</span>${status}</div><div class="message-body">${content}</div></article>`;
  }).join("");
  node.scrollTop = node.scrollHeight;
}

function updateMessage(conversationId, messageId, changes) {
  const conversation = assistantState.conversations.find((item) => item.id === conversationId);
  const message = conversation?.messages.find((item) => item.id === messageId);
  if (!message) return;
  Object.assign(message, changes);
  saveAssistantState();
  if (assistantState.activeId === conversationId) renderMessages();
  renderConversationList();
}

function renderWorkdirOptions() {
  const select = $("agentPathSelect");
  if (!select) return;
  const candidatePaths = new Set(["", state.path, assistantState.workdir]);
  assistantState.directories.forEach((directory) => candidatePaths.add(directory.path));
  state.entries.filter((entry) => entry.kind === "directory").forEach((entry) => candidatePaths.add(entry.path));
  const paths = [...candidatePaths].filter((path) => typeof path === "string").sort((a, b) => a.localeCompare(b, "zh-CN"));
  select.innerHTML = paths.map((path) => `<option value="${esc(path)}">${esc(path || "根目录")}</option>`).join("");
  if (!paths.includes(assistantState.workdir)) {
    assistantState.workdir = state.path || "";
    const conversation = activeConversation();
    if (conversation) conversation.path = assistantState.workdir;
    saveAssistantState();
  }
  select.value = assistantState.workdir;
}

async function refreshAssistantDirectories(path = "") {
  try {
    const data = await api(`/api/directories?path=${pathUrl(path)}&depth=3&limit=200`);
    if (Array.isArray(data.directories)) {
      assistantState.directories = data.directories;
      renderWorkdirOptions();
    }
  } catch (_) { /* Older servers can omit the directory tree endpoint. */ }
}

function setAssistantBusy(busy) {
  const button = $("runAgentBtn");
  button.disabled = busy;
  button.setAttribute("aria-busy", String(busy));
  button.innerHTML = busy ? "<span>◌</span> 执行中" : "<span>↑</span> 发送";
}

function applyJobUpdate(conversationId, messageId, payload) {
  const content = payload?.output ?? payload?.text;
  const delta = payload?.delta ?? payload?.chunk;
  const status = payload?.status;
  if (content !== undefined) updateMessage(conversationId, messageId, { content: String(content), status: status || "running" });
  else if (delta !== undefined) {
    const conversation = assistantState.conversations.find((item) => item.id === conversationId);
    const message = conversation?.messages.find((item) => item.id === messageId);
    updateMessage(conversationId, messageId, { content: `${message?.content || ""}${String(delta)}`, status: status || "running" });
  }
  else if (status) updateMessage(conversationId, messageId, { status });
}

async function consumeAgentStream(job, conversationId, messageId) {
  const response = await fetch(job.stream_url || `/api/agent/${job.id}/stream`, { credentials: "same-origin" });
  if (!response.ok || !response.body) throw new Error(`流式连接失败 (${response.status})`);
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let terminalStatus = "running";
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const lines = buffer.split(/\r?\n/);
    buffer = lines.pop() || "";
    lines.filter((line) => line.startsWith("data:")).forEach((line) => {
      const raw = line.slice(5).trim();
      if (!raw || raw === "[DONE]") return;
      try {
        const payload = JSON.parse(raw);
        terminalStatus = payload.status || terminalStatus;
        applyJobUpdate(conversationId, messageId, payload);
      } catch (_) { applyJobUpdate(conversationId, messageId, { delta: raw }); }
    });
    if (done) break;
  }
  return terminalStatus;
}

async function pollAgent(id, conversationId, messageId) {
  const job = await api(`/api/agent/${id}`);
  applyJobUpdate(conversationId, messageId, job);
  if (["queued", "running"].includes(job.status)) return setTimeout(() => pollAgent(id, conversationId, messageId).catch((error) => finishAgent(conversationId, messageId, error)), 650);
  finishAgent(conversationId, messageId, null, job.status);
}

function finishAgent(conversationId, messageId, error = null, status = "succeeded") {
  const job = [...assistantState.jobs.values()].find((item) => item.conversationId === conversationId && item.messageId === messageId);
  if (job) assistantState.jobs.delete(job.id);
  if (error) {
    updateMessage(conversationId, messageId, { status: "failed", content: `请求失败：${error.message}` });
    toast(error.message, true);
  } else {
    updateMessage(conversationId, messageId, { status: status || "succeeded" });
    if (status === "succeeded") toast("助手任务已完成");
  }
  setAssistantBusy(assistantState.jobs.size > 0);
}

function setAssistantOpen(open, focusInput = true) {
  assistantOpen = open;
  panel.classList.toggle("open", open);
  scrim.classList.toggle("open", open);
  panel.setAttribute("aria-hidden", String(!open));
  scrim.setAttribute("aria-hidden", String(!open));
  orb.setAttribute("aria-expanded", String(open));
  if (open) {
    panel.removeAttribute("inert");
    restoreFocus = document.activeElement || orb;
    if (focusInput) setTimeout(() => $("agentPrompt").focus(), 80);
  } else {
    panel.setAttribute("inert", "");
    (restoreFocus?.isConnected ? restoreFocus : orb)?.focus?.();
  }
}

function trapAssistantFocus(event) {
  if (!assistantOpen || event.key !== "Tab") return;
  const focusable = [...panel.querySelectorAll("button, textarea, input, [href]")].filter((node) => !node.disabled);
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
}

function clampOrb(left, top) {
  const margin = 14;
  const maxLeft = Math.max(margin, window.innerWidth - orb.offsetWidth - margin);
  const minTop = ($("topbar")?.offsetHeight || 69) + 8;
  const maxTop = Math.max(minTop, window.innerHeight - orb.offsetHeight - margin);
  return { left: Math.min(Math.max(margin, left), maxLeft), top: Math.min(Math.max(minTop, top), maxTop) };
}

function positionOrb(left, top, persist = false) {
  const position = clampOrb(left, top);
  orb.style.left = `${position.left}px`;
  orb.style.top = `${position.top}px`;
  orb.style.right = "auto";
  orb.style.bottom = "auto";
  if (persist) localStorage.setItem("fileweb-assistant-position", JSON.stringify(position));
}

function restoreOrbPosition() {
  try {
    const saved = JSON.parse(localStorage.getItem("fileweb-assistant-position") || "null");
    if (saved && Number.isFinite(saved.left) && Number.isFinite(saved.top)) positionOrb(saved.left, saved.top);
  } catch (_) { /* Ignore malformed local UI state. */ }
}

let orbDrag = null;
let suppressOrbClick = false;
function cancelOrbDrag() {
  if (!orbDrag) return;
  orb.classList.remove("dragging");
  try { orb.releasePointerCapture?.(orbDrag.pointerId); } catch (_) { /* Pointer capture may already be released. */ }
  orbDrag = null;
}
orb.addEventListener("pointerdown", (event) => {
  if (event.pointerType === "mouse" && event.button !== 0) return;
  const rect = orb.getBoundingClientRect();
  orb.setPointerCapture(event.pointerId);
  orbDrag = { pointerId: event.pointerId, startX: event.clientX, startY: event.clientY, left: rect.left, top: rect.top, moved: false };
  orb.classList.add("dragging");
});
orb.addEventListener("pointermove", (event) => {
  if (!orbDrag) return;
  const dx = event.clientX - orbDrag.startX;
  const dy = event.clientY - orbDrag.startY;
  if (Math.hypot(dx, dy) > 6) orbDrag.moved = true;
  if (orbDrag.moved) positionOrb(orbDrag.left + dx, orbDrag.top + dy);
});
orb.addEventListener("pointerup", (event) => {
  if (!orbDrag) return;
  if (orbDrag.moved) { positionOrb(orb.offsetLeft, orb.offsetTop, true); suppressOrbClick = true; setTimeout(() => suppressOrbClick = false, 0); }
  orb.classList.remove("dragging");
  try { orb.releasePointerCapture?.(event.pointerId); } catch (_) { /* Pointer capture may already be released. */ }
  orbDrag = null;
});
orb.addEventListener("pointercancel", cancelOrbDrag);
orb.addEventListener("lostpointercapture", cancelOrbDrag);
orb.addEventListener("click", () => { if (!suppressOrbClick) setAssistantOpen(!assistantOpen); });
$("minimizeAssistant").addEventListener("click", () => setAssistantOpen(false));
scrim.addEventListener("click", () => setAssistantOpen(false));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && assistantOpen) setAssistantOpen(false);
  trapAssistantFocus(event);
});
window.addEventListener("resize", () => { if (orb.style.left) positionOrb(orb.offsetLeft, orb.offsetTop, true); });
restoreOrbPosition();
panel.setAttribute("inert", "");

$("refreshBtn").addEventListener("click", () => load());
$("newFolderBtn").addEventListener("click", () => { $("folderName").value = ""; $("folderDialog").showModal(); setTimeout(() => $("folderName").focus(), 0); });
$("folderForm").addEventListener("submit", async (event) => { event.preventDefault(); try { await api("/api/mkdir", { method: "POST", body: JSON.stringify({ path: state.path, name: $("folderName").value.trim() }) }); $("folderDialog").close(); toast("文件夹已创建"); load(); } catch (error) { toast(error.message, true); } });
async function uploadFiles(files, overwrite = false) {
  const form = new FormData();
  [...files].forEach((file) => form.append("files", file, file.name));
  return api(`/api/upload?path=${pathUrl(state.path)}${overwrite ? "&overwrite=1" : ""}`, { method: "POST", body: form });
}
$("uploadInput").onchange = async (event) => {
  if (!event.target.files.length) return;
  const files = [...event.target.files];
  try {
    await uploadFiles(files);
    toast("文件上传完成");
    await load();
  } catch (error) {
    if (error.status === 409 && error.data?.conflicts?.length) {
      const names = error.data.conflicts.join("、");
      if (window.confirm(`以下文件已存在：${names}\n确认覆盖这些文件吗？`)) {
        try { await uploadFiles(files, true); toast("文件已覆盖并上传"); await load(); }
        catch (retryError) { toast(retryError.message, true); }
      }
    } else toast(error.message, true);
  } finally { event.target.value = ""; }
};
$("moveForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const source = $("moveDialog").dataset.source;
  const destination = $("moveDestination").value.trim().replace(/^\/+|\/+$/g, "");
  try {
    await api("/api/move", { method: "POST", body: JSON.stringify({ path: source, destination }) });
    $("moveDialog").close();
    toast("已移动");
    await load();
  } catch (error) { toast(error.message, true); }
});
$("closePreview").addEventListener("click", () => $("previewDialog").close());
$("assistantForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const prompt = $("agentPrompt").value.trim();
  const conversation = activeConversation();
  if (!prompt || !conversation) return toast("请先描述任务", true);
  const selectedPath = assistantState.workdir || state.path || "";
  conversation.path = selectedPath;
  if (conversation.title === "新对话") conversation.title = prompt.replace(/\s+/g, " ").slice(0, 32);
  const userMessage = { id: makeId(), role: "user", content: prompt, status: "done", created: Date.now() };
  const assistantMessage = { id: makeId(), role: "assistant", content: "", status: "queued", created: Date.now() };
  conversation.messages.push(userMessage, assistantMessage);
  $("agentPrompt").value = "";
  saveAssistantState();
  renderConversationList();
  renderMessages();
  setAssistantBusy(true);
  const history = [];
  let historyChars = 0;
  for (const message of conversation.messages.slice(0, -2).filter((item) => item.content.trim()).slice(-20).reverse()) {
    const content = message.content.slice(-6000);
    if (historyChars + content.length > 30000) break;
    history.unshift({ role: message.role, content });
    historyChars += content.length;
  }
  try {
    const job = await api("/api/agent", { method: "POST", body: JSON.stringify({ prompt, path: selectedPath, allow_write: $("allowWrite").checked, conversation_id: conversation.id, history }) });
    assistantState.jobs.set(job.id, { id: job.id, conversationId: conversation.id, messageId: assistantMessage.id });
    consumeAgentStream(job, conversation.id, assistantMessage.id)
      .then((status) => finishAgent(conversation.id, assistantMessage.id, null, status === "running" ? "succeeded" : status))
      .catch(() => pollAgent(job.id, conversation.id, assistantMessage.id).catch((error) => finishAgent(conversation.id, assistantMessage.id, error)));
  } catch (error) { finishAgent(conversation.id, assistantMessage.id, error); }
});
$("agentPrompt").addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); $("assistantForm").requestSubmit(); } });
$("newConversationBtn").addEventListener("click", () => createConversation(assistantState.workdir || state.path));
$("sidebarNewConversation").addEventListener("click", () => createConversation(assistantState.workdir || state.path));
$("agentPathSelect").addEventListener("change", (event) => { assistantState.workdir = event.target.value; const conversation = activeConversation(); if (conversation) conversation.path = assistantState.workdir; saveAssistantState(); });
$("useCurrentPathBtn").addEventListener("click", () => { assistantState.workdir = state.path || ""; const conversation = activeConversation(); if (conversation) conversation.path = assistantState.workdir; renderWorkdirOptions(); saveAssistantState(); });
async function loadProfile() {
  try {
    const profile = await api("/api/me");
    $("userName").textContent = profile.username;
    $("profileName").textContent = profile.username;
    $("profileInitial").textContent = profile.username.slice(0, 1).toUpperCase();
    $("userInitial").textContent = profile.username.slice(0, 1).toUpperCase();
    [$("userAvatar"), $("profileAvatar")].forEach((image) => {
      if (!image) return;
      if (profile.avatar) { image.src = `${profile.avatar}?v=${Date.now()}`; image.hidden = false; }
      else image.hidden = true;
    });
    $("userInitial").hidden = Boolean(profile.avatar);
  } catch (_) { /* The page can still operate with the fallback identity. */ }
}
$("userMenuBtn").addEventListener("click", () => { $("profileDialog").showModal(); loadProfile(); });
$("avatarInput").addEventListener("change", async (event) => {
  const file = event.target.files?.[0];
  if (!file) return;
  const form = new FormData();
  form.append("avatar", file, file.name);
  try { await api("/api/avatar", { method: "POST", body: form }); toast("头像已更新"); await loadProfile(); }
  catch (error) { toast(error.message, true); }
  event.target.value = "";
});
$("profileForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const current = $("currentPassword").value;
  const next = $("newPassword").value;
  if (!current && !next && !$("confirmPassword").value) { $("profileDialog").close(); return; }
  if (next !== $("confirmPassword").value) return toast("两次输入的新密码不一致", true);
  try {
    await api("/api/password", { method: "POST", body: JSON.stringify({ current_password: current, new_password: next }) });
    $("currentPassword").value = $("newPassword").value = $("confirmPassword").value = "";
    $("profileDialog").close();
    toast("密码已更新，请使用新密码登录");
  } catch (error) { toast(error.message, true); }
});
$("logoutBtn").addEventListener("click", async () => { try { await api("/api/logout", { method: "POST", body: "{}" }); location.href = "/login.html"; } catch (error) { toast(error.message, true); } });
loadAssistantState();
renderConversationList();
renderMessages();
renderWorkdirOptions();
refreshAssistantDirectories();
load();
loadProfile();
