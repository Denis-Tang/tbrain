/* Tbrain desktop UI: overview / projects / agents / settings. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const PAGES = { overview: "总观", projects: "项目配置", agents: "Agent 管理", settings: "设置", setup: "首次设置" };
  const PROMPT_SECTIONS = ["最终目标", "硬规则", "当前阶段", "关键路径和命令"];
  const STATUS_CLASS = { 进行中: "run", 受阻: "block", 待验证: "verify", 已完成: "done", 已放弃: "abandon", 已中断: "interrupt" };
  const AGENTS = { "claude-code": "Claude Code", codex: "Codex", dsh: "DSH", pi: "pi", "dsh-native": "DSH" };
  const HOSTS = { "claude-code": "Claude Code", codex: "Codex", dsh: "DSH", pi: "pi", install: "安装", processes: "进程" };
  const CONNECTABLE = ["claude-code", "codex", "dsh", "pi"];
  const APPEARANCES = ["system", "light", "dark"];
  const OFFLINE = "未连接桌面程序";
  const ICONS = {
    folder: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7V5h6l3 3h9v12H3Z"/></svg>',
    chevron: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="m4 6 4 4 4-4"/></svg>',
  };

  function storedAppearance() {
    try {
      const value = localStorage.getItem("appearance");
      return APPEARANCES.includes(value) ? value : "system";
    } catch (error) {
      return "system";
    }
  }

  const state = {
    page: "overview",
    filter: "全部",
    sessions: [],
    sessionsLoaded: false,
    sessionsRequest: null,
    projects: [],
    projectsLoaded: false,
    projectView: "",
    timezones: [],
    settings: null,
    status: null,
    doctor: null,
    detail: null,
    promptLoaded: false,
    promptDirty: false,
    appearance: storedAppearance(),
    editing: { original: "", paths: [] },
    deleting: "",
    update: null,
    savedTimer: 0,
    toastTimer: 0,
    lastFocus: null,
    booted: false,
    setup: null,
  };

  function hasBridge() {
    return !!window.desktopAvailable;
  }

  function withTimeout(promise, milliseconds) {
    return new Promise((resolve, reject) => {
      const timer = window.setTimeout(() => reject(new Error("请求超时")), milliseconds);
      promise.then(
        (value) => {
          window.clearTimeout(timer);
          resolve(value);
        },
        (error) => {
          window.clearTimeout(timer);
          reject(error);
        }
      );
    });
  }

  async function call(method, ...args) {
    if (!hasBridge()) {
      const error = new Error("bridge-unavailable");
      error.bridge = true;
      throw error;
    }
    const result = await withTimeout(window.desktop[method](...args), 60000);
    if (result && typeof result === "object" && !Array.isArray(result) && result.error) throw new Error(String(result.error));
    return result == null ? {} : result;
  }

  function errorText(error) {
    return error && error.message ? String(error.message) : String(error || "操作失败");
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function icon(className, markup) {
    const node = el("span", className);
    node.innerHTML = markup;
    return node;
  }

  function emptyState(text) {
    return el("div", "empty-state", text);
  }

  function projectLabel(project) {
    const value = typeof project === "string" ? project.trim() : "";
    return value || "独立";
  }

  function agentLabel(agent) {
    const value = typeof agent === "string" ? agent.trim() : "";
    return AGENTS[value] || value;
  }

  function statusClass(status) {
    return STATUS_CLASS[status] || "";
  }

  function statusChip(status) {
    const chip = el("span", `status-chip ${statusClass(status)}`.trim());
    chip.textContent = status || "";
    return chip;
  }

  function formatTime(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    const pad = (number) => String(number).padStart(2, "0");
    const clock = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
    const now = new Date();
    if (date.getFullYear() === now.getFullYear()) return `${date.getMonth() + 1}-${pad(date.getDate())} ${clock}`;
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${clock}`;
  }

  function toast(message, isError = false) {
    const node = $("toast");
    if (!node) return;
    node.textContent = String(message || "");
    node.classList.toggle("error", !!isError);
    node.hidden = false;
    clearTimeout(state.toastTimer);
    state.toastTimer = window.setTimeout(() => {
      node.hidden = true;
    }, isError ? 6000 : 3200);
  }

  function setBusy(button, busy) {
    if (!button) return;
    button.disabled = busy;
    if (busy) button.dataset.busy = "true";
    else delete button.dataset.busy;
  }

  async function withBusy(button, work) {
    setBusy(button, true);
    try {
      return await work();
    } catch (error) {
      if (!(error && error.bridge)) toast(errorText(error), true);
      return undefined;
    } finally {
      setBusy(button, false);
    }
  }

  function applyTheme(persist) {
    const preference = state.appearance;
    const dark = preference === "dark" || (preference === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    document.querySelectorAll("[data-appearance]").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.appearance === preference));
    });
    if (persist) {
      try {
        localStorage.setItem("appearance", preference);
      } catch (error) {
        /* Storage is optional. */
      }
    }
    if (window.desktopBridge) window.desktopBridge.send(`theme:${dark ? "dark" : "light"}`);
  }

  function updateStatusUI(status) {
    const ready = !!(status && status.ready);
    $("ready-dot").classList.toggle("ready", ready);
    $("ready-label").textContent = status ? (ready ? "已就绪" : "未就绪") : "未连接";
    const version = status && status.version ? `v${status.version}` : "v—";
    $("app-version").textContent = version;
    $("version-value").textContent = version;
    if (status && Array.isArray(status.timezones)) state.timezones = status.timezones;
  }

  function goTo(page) {
    if (state.setup && state.setup.required && page !== "setup") page = "setup";
    if (!PAGES[page]) return;
    state.page = page;
    document.querySelector("main").scrollTop = 0;
    document.querySelectorAll(".nav-item").forEach((item) => {
      const active = item.dataset.page === page;
      item.classList.toggle("active", active);
      if (active) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    });
    document.querySelectorAll(".page").forEach((section) => section.classList.toggle("active", section.id === page));
    $("page-title").textContent = PAGES[page];
    if (page === "overview") loadOverview();
    else if (page === "projects") loadProjects();
    else if (page === "settings") loadSettings();
    else if (page === "agents") loadDoctor();
  }

  async function loadSetup() {
    state.setup = await call("setup_info");
    if (!$("setup-vault").value) $("setup-vault").value = state.setup.vault_path;
    $("setup-install-path").textContent = `程序安装位置：${state.setup.install_root}`;
    $("setup-result").hidden = true;
    $("setup-form").hidden = false;
    const list = $("setup-hosts");
    const selected = [...list.querySelectorAll("input:checked")].map(input => input.value);
    list.replaceChildren();
    for (const host of CONNECTABLE) {
      const available = state.setup.hosts[host];
      const row = el("label", "setup-host");
      const check = document.createElement("input");
      check.type = "checkbox";
      check.value = host;
      check.setAttribute("aria-label", HOSTS[host]);
      check.disabled = !available;
      check.checked = available && selected.includes(host);
      row.append(check, el("span", "host-name", HOSTS[host]), el("span", "host-detail", available ? "可接入" : "未检测到，安装后刷新"));
      list.append(row);
    }
    goTo("setup");
  }

  /* ---------- overview ---------- */

  async function loadOverview() {
    await Promise.all([loadSessions(), loadGlobalPrompt()]);
  }

  async function loadSessions(force = false) {
    if (!hasBridge()) { renderSessions(); return; }
    if (state.sessionsLoaded && !force) { renderSessions(); return; }
    if (!state.sessionsRequest) {
      state.sessionsRequest = call("sessions", "all").then((result) => {
        state.sessions = Array.isArray(result) ? result : result.sessions || [];
        state.sessionsLoaded = true;
        state.projectsLoaded = false;
      }).finally(() => { state.sessionsRequest = null; });
    }
    try { await state.sessionsRequest; }
    catch (error) { if (!error.bridge) toast(errorText(error), true); }
    renderSessions();
  }

  function appendSessionBatch(container, rows) {
    let shown = 0;
    const more = el("button", "button quiet load-more", "加载更多");
    more.type = "button";
    const append = () => {
      more.remove();
      const fragment = document.createDocumentFragment();
      for (const row of rows.slice(shown, shown + 30)) fragment.append(sessionRow(row));
      shown += 30;
      container.append(fragment);
      if (shown < rows.length) {
        more.textContent = `加载更多 · 还有 ${rows.length - shown} 个`;
        container.append(more);
      }
    };
    more.addEventListener("click", append);
    append();
  }

  function renderSessions() {
    const list = $("session-list");
    list.replaceChildren();
    $("filter-label").textContent = state.filter;
    for (const button of document.querySelectorAll("[data-filter]")) {
      const count = state.sessions.filter(row => button.dataset.filter === "全部" || row.status === button.dataset.filter).length;
      button.querySelector(".filter-count").textContent = count;
    }
    if (!hasBridge()) { list.append(emptyState(OFFLINE)); return; }
    const rows = state.sessions.filter(row => state.filter === "全部" || row.status === state.filter);
    $("session-count").textContent = `${rows.length} 个会话`;
    if (!rows.length) {
      list.append(emptyState(state.sessionsLoaded ? "暂无会话" : "正在读取"));
      return;
    }
    if (state.filter !== "全部") { appendSessionBatch(list, rows); return; }
    const groups = new Map();
    for (const row of rows) {
      const name = projectLabel(row.project);
      if (!groups.has(name)) groups.set(name, []);
      groups.get(name).push(row);
    }
    for (const [name, sessions] of [...groups].sort((a, b) => a[0].localeCompare(b[0], "zh-CN", {numeric: true}))) {
      const group = el("details", "session-group");
      const head = el("summary", "session-group-heading");
      head.append(icon("chevron", ICONS.chevron), icon("group-folder", ICONS.folder), el("span", "group-name", name), el("span", "count", `${sessions.length} 个会话`));
      const body = el("div", "session-group-body");
      group.append(head, body);
      group.addEventListener("toggle", () => {
        if (group.open && !body.childElementCount) appendSessionBatch(body, sessions);
      });
      list.append(group);
    }
  }

  function sessionRow(session) {
    const row = el("button", "session-row");
    row.type = "button";
    const name = session && session.name ? String(session.name) : "未命名";
    const main = el("span", "session-main");
    const titleLine = el("span", "session-title-line");
    titleLine.append(el("span", "session-name", name));
    main.append(titleLine);
    const meta = el("span", "session-meta");
    meta.append(el("span", "session-project", projectLabel(session && session.project)));
    meta.append(el("span", "sep"));
    meta.append(el("span", "session-agent", agentLabel(session && session.agent)));
    main.append(meta);
    const nextStep = session && session.next_step ? String(session.next_step).trim() : "";
    if (nextStep) {
      const next = el("span", "session-next");
      next.append(el("span", "arrow", "→"));
      next.append(document.createTextNode(nextStep));
      next.title = nextStep;
      main.append(next);
    }
    const side = el("span", "session-side");
    side.append(statusChip(session && session.status));
    side.append(el("span", "session-time", formatTime(session && session.updated)));
    row.append(main, side);
    row.setAttribute("aria-label", `${name}，${projectLabel(session && session.project)}，${(session && session.status) || "未标注"}`);
    row.addEventListener("click", () => openSession(session || {}));
    return row;
  }

  function openSession(session) {
    state.detail = {
      path: typeof session.path === "string" ? session.path : "",
      name: session.name || "",
      project: session.project || "",
      status: session.status || "",
      agent: session.agent || "",
      updated: session.updated || "",
      markdown: "",
    };
    $("rename-row").hidden = true;
    renderDrawer(true);
    openDrawer();
    if (!state.detail.path) return;
    const path = state.detail.path;
    call("session_detail", path)
      .then((detail) => {
        if (!state.detail || state.detail.path !== path) return;
        const value = detail || {};
        state.detail = { ...state.detail, ...value, path: value.path || path };
        renderDrawer(false);
      })
      .catch((error) => {
        if (!error.bridge) toast(errorText(error), true);
      });
  }

  function renderDrawer(loading) {
    const detail = state.detail;
    if (!detail) return;
    const chip = $("detail-status");
    chip.className = `status-chip ${statusClass(detail.status)}`.trim();
    chip.textContent = detail.status || "";
    $("detail-name").textContent = detail.name || "未命名";
    $("detail-meta").textContent = [projectLabel(detail.project), agentLabel(detail.agent), formatTime(detail.updated)].filter(Boolean).join(" · ");
    $("rename-session").disabled = !detail.path;
    $("open-session-file").disabled = !detail.path;
    const body = $("detail-body");
    body.replaceChildren();
    if (loading) body.append(emptyState("正在读取"));
    else body.innerHTML = renderMarkdown(detail.markdown);
  }

  function openDrawer() {
    state.lastFocus = document.activeElement;
    $("drawer-scrim").classList.add("open");
    $("session-drawer").classList.add("open");
    $("session-drawer").setAttribute("aria-hidden", "false");
    $("session-drawer").inert = false;
    document.querySelector("main").inert = true;
    document.querySelector("aside").inert = true;
    $("close-drawer").focus({ preventScroll: true });
  }

  function closeDrawer() {
    const drawer = $("session-drawer");
    if (!drawer.classList.contains("open")) return;
    drawer.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true");
    drawer.inert = true;
    $("drawer-scrim").classList.remove("open");
    document.querySelector("main").inert = false;
    document.querySelector("aside").inert = false;
    $("rename-row").hidden = true;
    if (state.lastFocus && state.lastFocus.isConnected) state.lastFocus.focus({ preventScroll: true });
    state.lastFocus = null;
  }

  function beginRename() {
    if (!state.detail || !state.detail.path) return;
    $("rename-row").hidden = false;
    const input = $("rename-input");
    input.value = state.detail.name || "";
    input.focus({ preventScroll: true });
    input.select();
  }

  async function commitRename() {
    if (!state.detail || !state.detail.path) return;
    const name = $("rename-input").value.trim();
    if (!name) {
      $("rename-input").focus();
      return;
    }
    const path = state.detail.path;
    await withBusy($("rename-save"), async () => {
      const result = await call("rename_session", path, name);
      if (state.detail && state.detail.path === path) {
        state.detail.name = result.name || name;
        state.detail.path = result.path || path;
      }
      $("rename-row").hidden = true;
      renderDrawer(false);
      await loadSessions(true);
      toast("已重命名");
    });
  }

  async function openSessionFile() {
    if (!state.detail || !state.detail.path) return;
    await withBusy($("open-session-file"), async () => {
      await call("open_document", state.detail.path);
    });
  }

  function flashSaved(text) {
    const hint = $("prompt-saved");
    hint.textContent = text;
    hint.classList.add("show");
    clearTimeout(state.savedTimer);
    state.savedTimer = window.setTimeout(() => {
      hint.classList.remove("show");
      window.setTimeout(() => {
        if (!hint.classList.contains("show")) hint.textContent = "";
      }, 240);
    }, 2200);
  }

  async function loadGlobalPrompt() {
    if (!hasBridge() || state.promptDirty) return;
    try {
      const result = await call("global_prompt");
      const markdown = typeof result.markdown === "string" ? result.markdown : typeof result.text === "string" ? result.text : "";
      if (!state.promptDirty) {
        $("global-prompt").value = markdown;
        state.promptLoaded = true;
      }
    } catch (error) {
      if (!error.bridge) toast(errorText(error), true);
    }
  }

  async function saveGlobalPrompt() {
    if (!hasBridge()) return;
    await withBusy($("save-prompt"), async () => {
      await call("save_global_prompt", $("global-prompt").value);
      state.promptLoaded = true;
      state.promptDirty = false;
      flashSaved("已保存");
    });
  }

  function setPromptOpen(open) {
    $("prompt-card").classList.toggle("open", open);
    $("toggle-prompt").setAttribute("aria-expanded", String(open));
    $("prompt-body").inert = !open;
    if (open) loadGlobalPrompt();
  }

  /* ---------- projects ---------- */

  async function loadProjects(force = false) {
    if (force) state.sessionsLoaded = false;
    if (state.projectsLoaded && !force) { renderProjects(); return; }
    if (!hasBridge()) {
      state.projects = [];
      renderProjects();
      return;
    }
    try {
      const result = await call("projects");
      state.projects = Array.isArray(result) ? result : Array.isArray(result.projects) ? result.projects : [];
      state.projectsLoaded = true;
    } catch (error) {
      state.projects = [];
      if (!error.bridge) toast(errorText(error), true);
    }
    renderProjects();
  }

  function descendants(name) {
    const result = new Set();
    const queue = [name];
    while (queue.length) {
      const current = queue.shift();
      for (const item of state.projects) {
        if (!item || !item.name || item.parent !== current || result.has(item.name)) continue;
        result.add(item.name);
        queue.push(item.name);
      }
    }
    return result;
  }

  function renderProjects() {
    const list = $("project-list");
    const location = $("project-location");
    list.replaceChildren();
    location.replaceChildren();
    const parent = state.projects.find(project => project.name === state.projectView);
    const projects = parent ? state.projects.filter(project => project.parent === parent.name)
      : state.projects.filter(project => !project.parent || !state.projects.some(item => item.name === project.parent));
    if (state.page === "projects") $("page-title").textContent = parent ? parent.name : PAGES.projects;
    if (parent) {
      const back = el("button", "button quiet project-back", "‹ 全部项目");
      back.type = "button";
      back.addEventListener("click", () => { state.projectView = ""; renderProjects(); document.querySelector("main").scrollTop = 0; });
      location.append(back);
      list.append(projectCard(parent, true));
      const heading = el("div", "section-heading");
      heading.append(el("h2", "", "子项目"), el("span", "count", `${projects.length} 个`));
      list.append(heading);
    } else {
      state.projectView = "";
      location.append(el("span", "count", `${projects.length} 个主项目 · ${state.projects.length} 个项目`));
    }
    if (!hasBridge()) { list.append(emptyState(OFFLINE)); return; }
    if (!projects.length) list.append(emptyState(parent ? "暂无子项目" : "暂无项目"));
    for (const project of projects) list.append(projectCard(project));
    list.dataset.view = parent ? "detail" : "root";
  }

  function projectCard(project, detailed = false) {
    const card = el("article", "project-card");
    card.dataset.project = project.name;
    if (!project.parent && !detailed) card.classList.add("drilldown");
    const head = el("div", "project-head");
    const toggle = el("button", "project-toggle");
    toggle.type = "button";
    if (project.parent || detailed) toggle.setAttribute("aria-expanded", "false");
    const info = el("span", "project-info");
    const nameLine = el("span", "project-name-line");
    nameLine.append(el("span", "project-name", project.name || "未命名"));
    if (project.parent) nameLine.append(el("span", "tag child", "子项目"));
    const children = state.projects.filter(item => item.parent === project.name);
    if (children.length) nameLine.append(el("span", "tag parent", `${children.length} 个子项目`));
    const paths = Array.isArray(project.paths) ? project.paths.filter((item) => typeof item === "string" && item.trim()) : [];
    const pathText = paths.length ? (paths.length > 1 ? `${paths[0]} 等 ${paths.length} 个路径` : paths[0]) : "暂无路径";
    const meta = el("span", "project-meta", `${Number(project.sessions) || 0} 个会话 · ${pathText}`);
    info.append(nameLine, meta);
    if (children.length && !detailed) info.append(el("span", "project-children", children.map(item => item.name).join(" · ")));
    toggle.append(icon("folder-icon", ICONS.folder), info, icon("chevron project-chevron", ICONS.chevron));
    const actions = el("div", "project-actions");
    const edit = el("button", "button secondary small", "编辑");
    edit.type = "button";
    edit.addEventListener("click", () => openProjectEditor(project));
    const remove = el("button", "button quiet small", "删除");
    remove.type = "button";
    remove.addEventListener("click", () => askDelete(project));
    actions.append(edit, remove);
    head.append(toggle, actions);

    const details = el("div", "project-details");
    details.inert = true;
    const inner = el("div", "details-inner");
    const body = el("div", "project-body");
    const pathSection = el("div");
    pathSection.append(el("div", "field-label", "工作目录"));
    const pathList = el("div", "path-list");
    if (paths.length) {
      for (const path of paths) {
        const row = el("div", "path-row");
        row.append(el("span", "", path));
        pathList.append(row);
      }
    } else {
      pathList.append(el("p", "field-empty", "暂无路径"));
    }
    pathSection.append(pathList);
    const promptSection = el("div");
    promptSection.append(el("div", "field-label", "项目提示词"));
    const promptContainer = el("div", "prompt-editor");
    promptSection.append(promptContainer);
    body.append(pathSection, promptSection);
    inner.append(body);
    details.append(inner);
    card.append(head, details);

    toggle.addEventListener("click", () => {
      if (!project.parent && !detailed) { state.projectView = project.name; renderProjects(); document.querySelector("main").scrollTop = 0; return; }
      const open = !card.classList.contains("open");
      card.classList.toggle("open", open);
      details.inert = !open;
      toggle.setAttribute("aria-expanded", String(open));
      if (open && !promptContainer.dataset.loaded) loadProjectPrompt(project, promptContainer);
    });
    return card;
  }

  async function loadProjectPrompt(project, container) {
    container.dataset.loaded = "true";
    container.replaceChildren(emptyState("正在读取"));
    try {
      const result = await call("project_prompt", project.name);
      renderPromptEditor(project, container, result || {});
    } catch (error) {
      delete container.dataset.loaded;
      container.replaceChildren();
      const retry = el("button", "button secondary small", "重试");
      retry.type = "button";
      retry.addEventListener("click", () => loadProjectPrompt(project, container));
      container.append(el("p", "field-empty", "未读取"), retry);
      if (!error.bridge) toast(errorText(error), true);
    }
  }

  function renderPromptEditor(project, container, result) {
    const sections = result.sections && typeof result.sections === "object" ? result.sections : {};
    container.replaceChildren();
    const editor = el("div", "prompt-editor");
    const inputs = {};
    PROMPT_SECTIONS.forEach((section) => {
      const field = el("div", "prompt-field");
      const label = el("span", "prompt-label", section);
      const textarea = el("textarea");
      textarea.rows = section === "关键路径和命令" ? 4 : 3;
      textarea.setAttribute("aria-label", section);
      textarea.value = typeof sections[section] === "string" ? sections[section] : "";
      field.append(label, textarea);
      editor.append(field);
      inputs[section] = textarea;
    });
    const userField = el("div", "prompt-field");
    userField.append(el("label", "", "用户补充"));
    const user = el("div", "prompt-user");
    user.textContent = typeof result.user === "string" && result.user.trim() ? result.user.trim() : "暂无";
    userField.append(user);
    const rejected = el("div", "rejected");
    rejected.hidden = true;
    const footer = el("div", "project-footer");
    const save = el("button", "button primary", "保存");
    save.type = "button";
    save.addEventListener("click", () => withBusy(save, async () => {
      const payload = {};
      for (const section of PROMPT_SECTIONS) payload[section] = inputs[section].value;
      const saved = await call("project_prompt_save", project.name, payload);
      renderRejected(rejected, saved.rejected);
      toast("已保存");
    }));
    footer.append(save);
    editor.append(userField, rejected, footer);
    container.append(editor);
  }

  function renderRejected(container, rejected) {
    const items = Array.isArray(rejected) ? rejected : rejected ? [rejected] : [];
    container.replaceChildren();
    container.hidden = items.length === 0;
    if (!items.length) return;
    container.append(el("strong", "", "被拒条目"));
    const list = el("ul");
    for (const item of items) {
      const text = rejectedText(item);
      if (text) list.append(el("li", "", text));
    }
    container.append(list);
  }

  function rejectedText(item) {
    if (item === null || item === undefined) return "";
    if (typeof item === "string") return item;
    if (typeof item === "number" || typeof item === "boolean") return String(item);
    const parts = [];
    for (const key of ["section", "field", "reason", "message", "error", "value"]) {
      if (item[key] !== undefined && item[key] !== null && item[key] !== "") parts.push(String(item[key]));
    }
    if (parts.length) return parts.join(" · ");
    try {
      return JSON.stringify(item);
    } catch (error) {
      return String(item);
    }
  }

  function openProjectEditor(project) {
    state.editing = {
      original: project ? project.name : "",
      paths: project && Array.isArray(project.paths) ? [...project.paths] : [],
    };
    $("project-dialog-title").textContent = project ? "编辑项目" : "新增项目";
    $("project-name").value = project ? project.name : "";
    renderEditorPaths();
    const parent = $("project-parent");
    parent.replaceChildren();
    parent.append(new Option("无", ""));
    const blocked = project ? descendants(project.name) : new Set();
    for (const item of state.projects) {
      if (!item || !item.name) continue;
      if (project && (item.name === project.name || blocked.has(item.name))) continue;
      parent.append(new Option(item.name, item.name));
    }
    parent.value = project && project.parent ? project.parent : "";
    $("project-allow-child").checked = !!(project && project.allow_child);
    $("save-project").textContent = project ? "保存" : "创建";
    $("project-dialog").showModal();
    $("project-name").focus();
  }

  function renderEditorPaths() {
    const container = $("project-paths");
    container.replaceChildren();
    state.editing.paths.forEach((value, index) => {
      const row = el("div", "path-edit-row");
      const input = el("input");
      input.type = "text";
      input.value = value;
      input.spellcheck = false;
      input.autocomplete = "off";
      input.setAttribute("aria-label", "工作目录");
      input.addEventListener("input", () => {
        state.editing.paths[index] = input.value;
      });
      const remove = el("button", "button quiet small", "移除");
      remove.type = "button";
      remove.setAttribute("aria-label", "移除路径");
      remove.addEventListener("click", () => {
        state.editing.paths.splice(index, 1);
        renderEditorPaths();
      });
      row.append(input, remove);
      container.append(row);
    });
    if (!state.editing.paths.length) container.append(el("p", "field-empty", "暂无路径"));
  }

  async function chooseFolder() {
    try {
      const result = await call("choose_vault");
      const path = typeof result.path === "string" ? result.path : typeof result.folder === "string" ? result.folder : "";
      return path.trim();
    } catch (error) {
      if (!error.bridge) toast(errorText(error), true);
      return "";
    }
  }

  function askDelete(project) {
    state.deleting = project.name || "";
    $("delete-question").textContent = `删除“${project.name || ""}”？`;
    $("delete-dialog").showModal();
  }

  /* ---------- settings ---------- */

  async function loadSettings() {
    if (!hasBridge()) {
      renderSettingsOffline();
      return;
    }
    try {
      const status = await call("status");
      state.status = status;
      updateStatusUI(status);
      let settings = status.settings && typeof status.settings === "object" ? status.settings : null;
      if (Array.isArray(status.timezones)) state.timezones = status.timezones;
      if (!settings || !state.timezones.length) {
        try {
          const result = await call("settings");
          if (result.settings && typeof result.settings === "object") settings = result.settings;
          if (Array.isArray(result.timezones) && !state.timezones.length) state.timezones = result.timezones;
        } catch (error) {
          if (!error.bridge) throw error;
        }
      }
      state.settings = settings || {};
      renderSettings(state.settings);
      state.setup = await call("setup_info");
      $("install-card").hidden = state.setup.installed;
    } catch (error) {
      if (error.bridge) renderSettingsOffline();
      else toast(errorText(error), true);
      renderHosts(null);
      return;
    }
    await loadDoctor();
  }

  function renderSettings(settings) {
    $("vault-path").value = settings.vault_path || "";
    const hours = Number(settings.timeout_hours);
    $("timeout-hours").value = Number.isFinite(hours) && hours > 0 ? hours : 6;
    $("ntp-enabled").checked = settings.ntp_enabled !== false;
    $("reminder-context-switch").checked = !!settings.reminder_context_switch;
    renderTimezoneOptions(settings.timezone || "");
  }

  function renderSettingsOffline() {
    state.settings = null;
    $("vault-path").value = "";
    $("timeout-hours").value = "";
    $("ntp-enabled").checked = false;
    $("reminder-context-switch").checked = false;
    renderTimezoneOptions("");
    $("version-value").textContent = "v—";
    $("update-status").textContent = "";
    updateStatusUI(null);
    renderHosts(null);
  }

  function renderTimezoneOptions(current) {
    const select = $("timezone");
    select.replaceChildren();
    select.append(new Option("跟随系统", ""));
    const zones = [];
    for (const zone of state.timezones || []) {
      if (typeof zone === "string" && zone && !zones.includes(zone)) zones.push(zone);
    }
    if (current && !zones.includes(current)) zones.unshift(current);
    if (!zones.length) zones.push("UTC");
    for (const zone of zones) select.append(new Option(zone, zone));
    select.value = current;
  }

  async function loadDoctor() {
    if (!hasBridge()) {
      renderHosts(null);
      return;
    }
    try {
      const result = await call("doctor");
      state.doctor = result || {};
      renderHosts(state.doctor);
    } catch (error) {
      state.doctor = null;
      renderHosts(null);
      if (!error.bridge) toast(errorText(error), true);
    }
  }

  function hostStatusClass(status) {
    return status === "green" || status === "yellow" || status === "red" ? status : "";
  }

  function renderHosts(result) {
    const container = $("host-list");
    container.replaceChildren();
    if (!hasBridge() || !result) {
      container.append(emptyState(hasBridge() ? "未读取" : OFFLINE));
      return;
    }
    const items = Array.isArray(result.items) ? result.items : [];
    for (const host of CONNECTABLE) {
      const item = items.find(item => item.host === host) || {};
      const row = el("article", "agent-item");
      const header = el("div", "agent-row");
      const summary = el("button", "agent-summary");
      summary.type = "button";
      summary.setAttribute("aria-expanded", "false");
      summary.setAttribute("aria-controls", `agent-detail-${host}`);
      const icon = el("span", `agent-icon agent-icon-${host}`);
      icon.textContent = { "claude-code": "✳", codex: ">_", dsh: "D", pi: "π" }[host];
      icon.setAttribute("aria-hidden", "true");
      const info = el("span", "agent-info");
      info.append(el("span", "agent-name", HOSTS[host]), el("span", "agent-path", (item.paths || [])[0] || "尚未读取配置位置"));
      summary.append(icon, info);
      const enabled = !!item.enabled;
      const complete = host === "pi" ? item.tools && item.hooks : item.mcp && item.hooks;
      const badge = el("span", `agent-badge ${enabled ? hostStatusClass(item.status) : ""}`, enabled ? complete ? "已配置" : "部分接入" : "未接入");
      const label = el("label", "switch agent-switch");
      const toggle = document.createElement("input");
      toggle.type = "checkbox";
      toggle.checked = enabled;
      toggle.indeterminate = enabled && !complete;
      toggle.dataset.host = host;
      toggle.setAttribute("role", "switch");
      toggle.setAttribute("aria-label", `${HOSTS[host]} 接入`);
      toggle.disabled = !enabled && state.setup && !state.setup.hosts[host];
      label.append(toggle, el("span"));
      const chevron = el("span", "chevron agent-chevron");
      chevron.innerHTML = ICONS.chevron;
      summary.append(chevron);
      header.append(summary, badge, label);
      const detail = el("div", "agent-details");
      detail.id = `agent-detail-${host}`;
      detail.hidden = true;
      const components = host === "pi" ? [["扩展工具", item.tools], ["生命周期事件", item.hooks]] : [["MCP 工具", item.mcp], ["Hook", item.hooks]];
      const chips = el("div", "agent-components");
      for (const [name, configured] of components) chips.append(el("span", configured ? "component-on" : "", `${name} · ${configured ? "已配置" : "未配置"}`));
      detail.append(chips, el("p", "agent-detail-status", item.detail || "未读取状态"));
      for (const path of item.paths || []) detail.append(el("code", "agent-config-path", path));
      detail.append(el("p", "agent-reload", item.reload || "重开 Agent 后加载配置。"));
      summary.addEventListener("click", () => {
        detail.hidden = !detail.hidden;
        summary.setAttribute("aria-expanded", String(!detail.hidden));
        row.classList.toggle("expanded", !detail.hidden);
      });
      row.append(header, detail);
      container.append(row);
    }
    $("agent-runtime").textContent = items.filter(item => !CONNECTABLE.includes(item.host)).map(item => item.detail).join(" · ");
  }

  function updateMessage(result) {
    if (!result || typeof result !== "object") return "检查失败";
    if (result.newer) {
      const version = result.latest || result.version || "";
      return version ? `发现新版本 v${version}` : "发现新版本";
    }
    if (result.message) return String(result.message);
    if (result.ok === false) return "检查失败";
    return "已是最新版本";
  }

  /* ---------- markdown ---------- */

  const ESCAPE_MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, (character) => ESCAPE_MAP[character]);
  }

  function inlineMarkdown(text) {
    return escapeHtml(text)
      .replace(/`([^`\n]+)`/g, (_, code) => `<code>${code}</code>`)
      .replace(/\[\[([^\]\n]+)\]\]/g, (_, name) => `<span class="wikilink">${name}</span>`)
      .replace(/\*\*([^*\n]+)\*\*/g, (_, strong) => `<strong>${strong}</strong>`)
      .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, (_, label, href) => `<a href="${href}" rel="noopener noreferrer">${label}</a>`);
  }

  function renderMarkdown(source) {
    const text = String(source == null ? "" : source).replace(/\r\n?/g, "\n").trim();
    if (!text) return "";
    let body = text;
    let frontmatter = "";
    if (body.startsWith("---\n")) {
      const end = body.indexOf("\n---", 4);
      if (end !== -1) {
        frontmatter = body.slice(0, end + 4);
        body = body.slice(end + 4).replace(/^\n+/, "");
      }
    }
    const html = [];
    if (frontmatter) html.push(`<pre class="md-frontmatter">${escapeHtml(frontmatter)}</pre>`);
    const lines = body.split("\n");
    let paragraph = [];
    let list = null;
    let fence = null;
    const flushParagraph = () => {
      if (!paragraph.length) return;
      html.push(`<p>${paragraph.map(inlineMarkdown).join("<br>")}</p>`);
      paragraph = [];
    };
    const flushList = () => {
      if (!list) return;
      html.push(`<${list.type}>${list.items.map((item) => `<li>${inlineMarkdown(item)}</li>`).join("")}</${list.type}>`);
      list = null;
    };
    for (const line of lines) {
      if (fence) {
        if (/^\s*```/.test(line)) {
          html.push(`<pre><code>${escapeHtml(fence.join("\n"))}</code></pre>`);
          fence = null;
        } else {
          fence.push(line);
        }
        continue;
      }
      if (/^\s*```/.test(line)) {
        flushParagraph();
        flushList();
        fence = [];
        continue;
      }
      if (!line.trim()) {
        flushParagraph();
        flushList();
        continue;
      }
      const heading = line.match(/^(#{1,6})\s+(.*)$/);
      if (heading) {
        flushParagraph();
        flushList();
        const level = Math.min(heading[1].length + 1, 6);
        html.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);
        continue;
      }
      if (/^\s*(?:---+|\*\*\*+)\s*$/.test(line)) {
        flushParagraph();
        flushList();
        html.push("<hr>");
        continue;
      }
      const bullet = line.match(/^\s*[-*+]\s+(.*)$/);
      if (bullet) {
        flushParagraph();
        if (!list || list.type !== "ul") {
          flushList();
          list = { type: "ul", items: [] };
        }
        list.items.push(bullet[1].replace(/^\[([ xX])\]\s*/, (match, mark) => (mark.trim() ? "☑ " : "☐ ")));
        continue;
      }
      const ordered = line.match(/^\s*\d+[.)]\s+(.*)$/);
      if (ordered) {
        flushParagraph();
        if (!list || list.type !== "ol") {
          flushList();
          list = { type: "ol", items: [] };
        }
        list.items.push(ordered[1]);
        continue;
      }
      const quote = line.match(/^\s*>\s?(.*)$/);
      if (quote) {
        flushParagraph();
        flushList();
        html.push(`<blockquote>${inlineMarkdown(quote[1])}</blockquote>`);
        continue;
      }
      flushList();
      paragraph.push(line);
    }
    if (fence) html.push(`<pre><code>${escapeHtml(fence.join("\n"))}</code></pre>`);
    flushParagraph();
    flushList();
    return html.join("");
  }

  /* ---------- events ---------- */

  function bindEvents() {
    $("setup-browse").addEventListener("click", event => withBusy(event.currentTarget, async () => {
      const path = await chooseFolder();
      if (path) $("setup-vault").value = path;
    }));
    $("setup-refresh").addEventListener("click", event => withBusy(event.currentTarget, loadSetup));
    $("open-setup").addEventListener("click", event => withBusy(event.currentTarget, loadSetup));
    $("setup-form").addEventListener("submit", event => {
      event.preventDefault();
      withBusy($("setup-submit"), async () => {
        const selected = [...$("setup-hosts").querySelectorAll("input:checked")].map(input => input.value);
        const result = await call("setup", $("setup-vault").value, selected);
        const list = $("setup-results");
        list.replaceChildren(el("p", "", "Tbrain 已安装，知识库已就绪。"));
        for (const item of result.hosts) {
          const row = el("div", "host-row");
          row.append(el("span", "host-name", HOSTS[item.host]), el("span", "host-detail", item.error || item.detail));
          list.append(row);
        }
        state.status = await call("status");
        state.setup.required = false;
        updateStatusUI(state.status);
        $("setup-form").hidden = true;
        $("setup-result").hidden = false;
        document.querySelectorAll(".nav-item").forEach(item => { item.disabled = false; });
        state.sessionsLoaded = false;
        state.projectsLoaded = false;
      });
    });
    $("setup-finish").addEventListener("click", () => goTo("overview"));
    document.querySelectorAll(".nav-item").forEach((button) => {
      button.addEventListener("click", () => goTo(button.dataset.page));
    });
    $("app-version").addEventListener("click", () => goTo("settings"));

    $("status-filters").addEventListener("click", (event) => {
      const chip = event.target.closest("[data-filter]");
      if (!chip) return;
      state.filter = chip.dataset.filter;
      $("status-filters").querySelectorAll("[data-filter]").forEach((item) => {
        const active = item === chip;
        item.classList.toggle("active", active);
        item.setAttribute("aria-pressed", String(active));
      });
      $("status-menu").open = false;
      renderSessions();
    });
    document.addEventListener("click", event => {
      if (!$("status-menu").contains(event.target)) $("status-menu").open = false;
    });
    $("status-menu").addEventListener("keydown", event => {
      if (event.key === "Escape") { $("status-menu").open = false; $("status-menu").querySelector("summary").focus(); }
    });

    $("refresh-sessions").addEventListener("click", (event) => withBusy(event.currentTarget, async () => {
      await loadSessions(true);
      await loadGlobalPrompt();
    }));

    $("toggle-prompt").addEventListener("click", () => setPromptOpen(!$("prompt-card").classList.contains("open")));
    $("global-prompt").addEventListener("input", () => {
      state.promptDirty = true;
      $("prompt-saved").classList.remove("show");
    });
    $("save-prompt").addEventListener("click", saveGlobalPrompt);

    $("close-drawer").addEventListener("click", closeDrawer);
    $("drawer-scrim").addEventListener("click", closeDrawer);
    $("rename-session").addEventListener("click", beginRename);
    $("open-session-file").addEventListener("click", openSessionFile);
    $("rename-save").addEventListener("click", commitRename);
    $("rename-cancel").addEventListener("click", () => { $("rename-row").hidden = true; });
    $("rename-input").addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        commitRename();
      } else if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        $("rename-row").hidden = true;
      }
    });
    $("detail-body").addEventListener("click", (event) => {
      if (event.target.closest("a")) event.preventDefault();
    });

    $("refresh-projects").addEventListener("click", event => withBusy(event.currentTarget, () => loadProjects(true)));
    $("new-project").addEventListener("click", () => openProjectEditor(null));
    $("cancel-project").addEventListener("click", () => $("project-dialog").close());
    $("add-project-path").addEventListener("click", () => {
      state.editing.paths.push("");
      renderEditorPaths();
      const inputs = $("project-paths").querySelectorAll("input");
      if (inputs.length) inputs[inputs.length - 1].focus();
    });
    $("browse-project-path").addEventListener("click", (event) => withBusy(event.currentTarget, async () => {
      const path = await chooseFolder();
      if (!path) return;
      state.editing.paths = state.editing.paths.filter((item) => String(item || "").trim() && String(item).trim() !== path);
      state.editing.paths.push(path);
      renderEditorPaths();
    }));
    $("project-form").addEventListener("submit", (event) => {
      event.preventDefault();
      const name = $("project-name").value.trim();
      if (!name) {
        $("project-name").focus();
        return;
      }
      const paths = [];
      for (const value of state.editing.paths) {
        const path = String(value || "").trim();
        if (path && !paths.includes(path)) paths.push(path);
      }
      withBusy($("save-project"), async () => {
        await call("project_save", name, paths, $("project-parent").value, $("project-allow-child").checked, state.editing.original);
        $("project-dialog").close();
        await loadProjects(true);
        toast("已保存");
      });
    });
    $("cancel-delete").addEventListener("click", () => $("delete-dialog").close());
    $("confirm-delete").addEventListener("click", (event) => withBusy(event.currentTarget, async () => {
      await call("project_delete", state.deleting);
      $("delete-dialog").close();
      await loadProjects(true);
      toast("已删除");
    }));

    $("settings-form").addEventListener("submit", (event) => {
      event.preventDefault();
      if (!hasBridge()) return;
      const rawHours = Number($("timeout-hours").value);
      const timeoutHours = Number.isFinite(rawHours) && rawHours > 0 ? Math.round(rawHours) : 6;
      const values = {
        vault_path: $("vault-path").value.trim(),
        timezone: $("timezone").value,
        ntp_enabled: $("ntp-enabled").checked,
        timeout_hours: timeoutHours,
        reminder_context_switch: $("reminder-context-switch").checked,
      };
      withBusy(event.submitter || $("save-settings"), async () => {
        const result = await call("save_settings", values);
        state.sessionsLoaded = false;
        state.projectsLoaded = false;
        state.projectView = "";
        if (result.settings && typeof result.settings === "object") state.settings = result.settings;
        toast(result.restart_required ? "已保存，需重启" : "已保存");
        try {
          const status = await call("status");
          state.status = status;
          updateStatusUI(status);
        } catch (error) {
          /* Keep the current labels. */
        }
      });
    });
    $("choose-vault").addEventListener("click", (event) => withBusy(event.currentTarget, async () => {
      const path = await chooseFolder();
      if (path) $("vault-path").value = path;
    }));

    $("appearance").addEventListener("click", (event) => {
      const button = event.target.closest("[data-appearance]");
      if (!button) return;
      state.appearance = APPEARANCES.includes(button.dataset.appearance) ? button.dataset.appearance : "system";
      applyTheme(true);
    });

    $("check-update").addEventListener("click", (event) => withBusy(event.currentTarget, async () => {
      const result = await call("update_check", true);
      state.update = result || {};
      const message = updateMessage(state.update);
      $("update-status").textContent = message;
      toast(message);
    }));
    $("apply-update").addEventListener("click", (event) => withBusy(event.currentTarget, async () => {
      const version = state.update && (state.update.latest || state.update.version) ? String(state.update.latest || state.update.version) : "";
      const result = await call("update_apply");
      const message = result.error || result.message || (result.restarting ? "升级完成，正在重启 Tbrain" : result.upgraded === false ? "已是最新版本" : "正在升级");
      $("update-status").textContent = message;
      toast(message, result.started === false);
    }));
    $("refresh-doctor").addEventListener("click", (event) => withBusy(event.currentTarget, loadDoctor));
    $("host-list").addEventListener("change", async (event) => {
      const toggle = event.target.closest("input[data-host]");
      if (!toggle) return;
      const enabled = toggle.checked;
      toggle.disabled = true;
      try {
        const result = await call(enabled ? "connect" : "disconnect", toggle.dataset.host);
        if (result.ok === false) throw new Error(result.error || result.detail || "操作失败");
        toast(enabled ? "接入配置已写入，请展开查看生效步骤" : "已关闭 Tbrain 接入，重开 Agent 可移除缓存工具列表");
      } catch (error) {
        toggle.checked = !enabled;
        toast(errorText(error), true);
      } finally {
        await loadDoctor();
      }
    });

    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      if (!$("rename-row").hidden) {
        $("rename-row").hidden = true;
        return;
      }
      closeDrawer();
    });

    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
      if (state.appearance === "system") applyTheme(false);
    });
  }

  function offlinePreview() {
    document.querySelectorAll("main button, main input, main textarea, main select").forEach((control) => {
      control.disabled = true;
    });
  }

  async function boot() {
    if (state.booted) return;
    state.booted = true;
    applyTheme(false);
    bindEvents();
    if (!hasBridge()) { offlinePreview(); goTo("overview"); return; }
    try {
      state.status = await call("status");
      updateStatusUI(state.status);
      if (!state.status.settings.vault_path) {
        document.querySelectorAll(".nav-item").forEach(item => { item.disabled = true; });
        await loadSetup();
      } else goTo("overview");
    } catch (error) { toast(errorText(error), true); }
  }

  window.addEventListener("desktopready", boot, { once: true });
  if (document.readyState !== "loading") boot();
})();
