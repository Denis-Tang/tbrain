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
    projectViews: {},
    projectViewsKey: "",
    activeRoot: "",
    detailsSeq: 0,
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
      head.append(icon("group-folder", ICONS.folder), el("span", "group-name", name), el("span", "count", `${sessions.length} 个会话`));
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

  const ROOT_VIEW = "";
  let treeObserver = null;
  let menuTrigger = null;

  function currentVaultPath() {
    const settings = state.settings || (state.status && state.status.settings) || {};
    return String(settings.vault_path || "");
  }

  function loadProjectViews() {
    const key = `tbrain-project-views:${currentVaultPath()}`;
    if (state.projectViewsKey === key) return;
    state.projectViewsKey = key;
    try {
      const parsed = JSON.parse(localStorage.getItem(key) || "{}");
      state.projectViews = parsed && typeof parsed === "object" ? parsed : {};
    } catch (error) {
      state.projectViews = {};
    }
  }

  function saveProjectViews() {
    try {
      localStorage.setItem(state.projectViewsKey, JSON.stringify(state.projectViews));
    } catch (error) {
      /* 本地存储不可用时只保留内存状态 */
    }
  }

  function viewState(root = state.activeRoot) {
    const key = root || "roots";
    const value = state.projectViews[key];
    if (!value || typeof value !== "object") state.projectViews[key] = {};
    return state.projectViews[key];
  }

  function hasChildren(name) {
    return state.projects.some(project => project && project.parent === name);
  }

  function childrenOf(name) {
    return state.projects.filter(project => project && project.parent === name);
  }

  function expanded(name, root = state.activeRoot) {
    const saved = viewState(root)[name];
    return saved === undefined ? false : !!saved;
  }

  function rootProjects() {
    return state.projects.filter(project => project && project.name
      && (!project.parent || !state.projects.some(item => item.name === project.parent)));
  }

  function treeProjects(root = state.activeRoot) {
    if (!root) return [];
    const parent = state.projects.find(project => project && project.name === root);
    return parent ? [parent, ...childrenOf(root)] : [];
  }

  // 新建的项目排在最前面：前端按创建时间倒序，同一时间按名称稳定排序。
  function sortProjects(projects) {
    return [...projects].sort((left, right) => {
      const a = String((left && left.created_at) || "");
      const b = String((right && right.created_at) || "");
      if (a !== b) return a < b ? 1 : -1;
      return String((left && left.name) || "").localeCompare(String((right && right.name) || ""), "zh-CN", { numeric: true });
    });
  }

  async function loadProjects(force = false) {
    if (force) state.sessionsLoaded = false;
    if (state.projectsLoaded && !force) {
      renderProjects();
      return;
    }
    if (!hasBridge()) {
      state.projects = [];
      renderProjects();
      return;
    }
    try {
      const result = await call("projects");
      const list = Array.isArray(result) ? result : Array.isArray(result.projects) ? result.projects : [];
      state.projects = sortProjects(list);
      state.projectsLoaded = true;
      loadProjectViews();
    } catch (error) {
      state.projects = [];
      if (!error.bridge) toast(errorText(error), true);
    }
    renderProjects();
  }

  function renderProjects() {
    closeMenu();
    const list = $("project-list");
    list.replaceChildren();
    const roots = rootProjects();
    $("project-count").textContent = `全部项目（${roots.length}）`;
    if (!hasBridge()) {
      list.append(emptyState(OFFLINE));
      refreshViewToggle(ROOT_VIEW);
      return;
    }
    if (!roots.length) list.append(emptyState("暂无项目"));
    for (const project of roots) list.append(projectCard(project, ROOT_VIEW));
    refreshViewToggle(ROOT_VIEW);
    if (state.activeRoot && $("tree-dialog").open) renderTree();
  }

  function refreshViewToggle(root = state.activeRoot) {
    const visible = root ? treeProjects(root) : rootProjects();
    const detailed = visible.length > 0 && visible.every(project => expanded(project.name, root));
    const button = $(root ? "tree-view-toggle" : "projects-view-toggle");
    if (!button) return;
    button.textContent = detailed ? "简洁" : "详细";
    button.disabled = visible.length === 0;
  }

  function applyCardState(card, open, root = state.activeRoot) {
    card.classList.toggle("is-compact", !open);
    const title = card.querySelector(".project-title");
    const name = card.dataset.project || "";
    if (title && (root || !hasChildren(name))) title.setAttribute("aria-expanded", String(!!open));
    const details = card.querySelector(".card-details");
    if (!details) return;
    details.inert = !open;
    details.setAttribute("aria-hidden", String(!open));
  }

  function projectCard(project, root) {
    const card = el("article", "project-card");
    card.dataset.project = project.name || "";
    card.tabIndex = -1;
    const heading = el("div", "project-heading");
    const h2 = el("h2");
    const title = el("button", "project-title");
    title.type = "button";
    title.append(icon("folder-icon", ICONS.folder));
    const info = el("span", "project-info");
    const nameLine = el("span", "project-name-line");
    nameLine.append(el("span", "name-text", project.name || "未命名"));
    if (hasChildren(project.name)) {
      const dot = el("span", "child-dot");
      dot.setAttribute("aria-hidden", "true");
      nameLine.append(dot);
    }
    const paths = Array.isArray(project.paths) ? project.paths.filter((item) => typeof item === "string" && item.trim()) : [];
    const pathText = paths.length ? (paths.length > 1 ? `${paths[0]} 等 ${paths.length} 个路径` : paths[0]) : "未关联";
    info.append(nameLine, el("span", "project-meta", `${Number(project.sessions) || 0} 个会话 · ${pathText}`));
    title.append(info);
    title.setAttribute("aria-label", hasChildren(project.name) ? `${project.name}，包含子项目` : String(project.name || ""));
    h2.append(title);

    const detailsId = `project-details-${++state.detailsSeq}`;
    title.setAttribute("aria-controls", !root && hasChildren(project.name) ? "tree-dialog" : detailsId);
    if (!root && hasChildren(project.name)) title.setAttribute("aria-haspopup", "dialog");

    const actions = el("div", "project-actions");
    const edit = el("button", "button secondary small", "编辑");
    edit.type = "button";
    edit.addEventListener("click", () => openProjectEditor(project));
    const more = el("button", "button quiet menu-trigger", "···");
    more.type = "button";
    more.setAttribute("aria-label", `${project.name || ""} 更多操作`);
    more.setAttribute("aria-haspopup", "menu");
    more.setAttribute("aria-expanded", "false");
    const entries = [];
    if (!project.parent) entries.push({ label: "新建子项目", action: () => openProjectEditor(null, project) });
    entries.push({ label: "复制名称", action: () => copyText(project.name || "") });
    entries.push({ label: "删除项目", danger: true, action: () => askDelete(project) });
    more.addEventListener("click", () => openMenu(more, entries));
    card.addEventListener("contextmenu", (event) => {
      if (event.target.closest("button,a,input,textarea,label")) return;
      event.preventDefault();
      openMenu(card, entries, { x: event.clientX, y: event.clientY });
    });
    actions.append(edit, more);
    heading.append(h2, actions);

    const details = el("div", "card-details");
    details.id = detailsId;
    const inner = el("div", "details-inner");
    const body = el("div", "project-body");
    const pathField = el("div", "project-field");
    pathField.append(el("h3", "", "工作目录"));
    if (paths.length) {
      for (const path of paths) {
        const row = el("div", "project-path-row");
        row.append(el("span", "", path));
        const copy = el("button", "button quiet small", "复制");
        copy.type = "button";
        copy.addEventListener("click", () => copyText(path));
        row.append(copy);
        pathField.append(row);
      }
    } else {
      pathField.append(el("p", "field-empty", "未关联"));
    }
    body.append(pathField);

    if (hasChildren(project.name) || project.allow_child) {
      const switchRow = el("div", "switch-row");
      const switchText = el("div", "switch-text");
      switchText.append(el("h3", "", "允许读取子项目"));
      const switchLabel = el("label", "switch");
      const toggle = document.createElement("input");
      toggle.type = "checkbox";
      toggle.checked = !!project.allow_child;
      toggle.setAttribute("role", "switch");
      toggle.setAttribute("aria-label", "允许读取子项目");
      toggle.addEventListener("change", () => {
        const desired = toggle.checked;
        withBusy(toggle, async () => {
          try {
            const saved = await call("project_save", project.name, project.paths || [], project.parent || "", desired, "");
            project.allow_child = !!(saved && saved.allow_child !== undefined ? saved.allow_child : desired);
            toast("已保存");
          } catch (error) {
            toggle.checked = !desired;
            throw error;
          }
        });
      });
      switchLabel.append(toggle, el("span"));
      switchRow.append(switchText, switchLabel);
      body.append(switchRow);
    }

    const footer = el("div", "project-footer");
    const prompt = el("button", "button secondary small", "项目提示词");
    prompt.type = "button";
    prompt.addEventListener("click", () => openProjectPrompt(project));
    footer.append(prompt);
    body.append(footer);
    inner.append(body);
    details.append(inner);
    card.append(heading, details);

    const activate = () => toggleCard(project, card, root);
    title.addEventListener("click", activate);
    card.addEventListener("click", (event) => {
      if (event.target.closest("button,a,input,textarea,label")) return;
      if (getSelection().toString()) return;
      activate();
    });
    applyCardState(card, expanded(project.name, root), root);
    return card;
  }

  function toggleCard(project, card, root) {
    if (hasChildren(project.name)) {
      if (!root) {
        openTree(project);
        return;
      }
      if (root !== project.name) {
        state.activeRoot = project.name;
        renderTree();
        return;
      }
    }
    const open = !expanded(project.name, root);
    viewState(root)[project.name] = open;
    applyCardState(card, open, root);
    refreshViewToggle(root);
    saveProjectViews();
  }

  function openTree(project) {
    state.activeRoot = project.name;
    $("tree-dialog").showModal();
    renderTree();
  }

  function renderTree() {
    const board = $("tree-board");
    const dialog = $("tree-dialog");
    if (!board) return;
    if (treeObserver) treeObserver.disconnect();
    treeObserver = null;
    board.replaceChildren();
    const visible = treeProjects();
    if (!visible.length) {
      if (dialog.open) dialog.close();
      return;
    }
    const frame = el("div", "tree-frame");
    frame.classList.toggle("is-leaf", visible.length === 1);
    const lines = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    lines.classList.add("tree-lines");
    lines.setAttribute("aria-hidden", "true");
    lines.append(document.createElementNS("http://www.w3.org/2000/svg", "path"));
    const children = el("div", "tree-children");
    for (const child of visible.slice(1)) children.append(projectCard(child, state.activeRoot));
    frame.append(lines, projectCard(visible[0], state.activeRoot), children);
    board.append(frame);
    $("tree-title").textContent = visible[0].name || "";
    refreshViewToggle(state.activeRoot);
    if (window.ResizeObserver) {
      treeObserver = new ResizeObserver(drawConnections);
      treeObserver.observe(frame);
      frame.querySelectorAll(".project-card").forEach((card) => treeObserver.observe(card));
    }
    requestAnimationFrame(drawConnections);
  }

  function drawConnections() {
    const dialog = $("tree-dialog");
    const board = $("tree-board");
    if (!dialog || !board || !dialog.open) return;
    const frame = board.querySelector(".tree-frame");
    const svg = frame && frame.querySelector("svg.tree-lines");
    const rootCard = frame && frame.querySelector(":scope > .project-card");
    if (!frame || !svg || !rootCard) return;
    const bounds = frame.getBoundingClientRect();
    svg.setAttribute("viewBox", `0 0 ${bounds.width} ${bounds.height}`);
    const path = svg.querySelector("path");
    const root = rootCard.getBoundingClientRect();
    const children = [...frame.querySelectorAll(".tree-children > .project-card")].map((card) => card.getBoundingClientRect());
    if (!children.length) {
      path.setAttribute("d", "");
      return;
    }
    const x = root.right - bounds.left;
    const y = (root.top + root.bottom) / 2 - bounds.top;
    const middle = (root.right + children[0].left) / 2 - bounds.left;
    const points = children.map((rect) => ({ x: rect.left - bounds.left, y: (rect.top + rect.bottom) / 2 - bounds.top }));
    const top = Math.min(y, ...points.map((point) => point.y));
    const bottom = Math.max(y, ...points.map((point) => point.y));
    path.setAttribute("d", `M ${x} ${y} H ${middle} M ${middle} ${top} V ${bottom} ${points.map((point) => `M ${middle} ${point.y} H ${point.x}`).join(" ")}`);
  }

  function closeTreeDialog() {
    const previous = state.activeRoot;
    closeMenu();
    state.activeRoot = "";
    if (treeObserver) {
      treeObserver.disconnect();
      treeObserver = null;
    }
    $("tree-board").replaceChildren();
    if (!previous) return;
    const card = $("project-list").querySelector(`[data-project="${escapeSelector(previous)}"]`);
    const title = card && card.querySelector(".project-title");
    if (title) title.focus({ preventScroll: true });
  }

  function escapeSelector(value) {
    const text = String(value || "");
    if (window.CSS && typeof window.CSS.escape === "function") return window.CSS.escape(text);
    return text.replace(/["\\]/g, "\\$&");
  }

  function closeMenu(restoreFocus = false) {
    const menu = $("project-menu");
    if (!menu || menu.hidden) return;
    const trigger = menuTrigger;
    menu.hidden = true;
    menu.replaceChildren();
    menuTrigger = null;
    if (!trigger) return;
    trigger.setAttribute("aria-expanded", "false");
    if (restoreFocus) trigger.focus({ preventScroll: true });
  }

  function openMenu(trigger, entries, position = null) {
    const menu = $("project-menu");
    if (!menu) return;
    if (menuTrigger === trigger && !menu.hidden) {
      closeMenu(true);
      return;
    }
    closeMenu();
    (trigger.closest("dialog") || document.body).append(menu);
    menu.replaceChildren();
    menuTrigger = trigger;
    trigger.setAttribute("aria-expanded", "true");
    for (const entry of entries) {
      const item = el("button", "menu-item", entry.label);
      item.type = "button";
      item.setAttribute("role", "menuitem");
      if (entry.danger) item.classList.add("destructive");
      item.addEventListener("click", () => {
        closeMenu(true);
        entry.action();
      });
      menu.append(item);
    }
    menu.hidden = false;
    const anchor = trigger.getBoundingClientRect();
    const width = menu.offsetWidth;
    const height = menu.offsetHeight;
    const left = position ? position.x : anchor.right - width;
    const top = position ? position.y : anchor.bottom + 7;
    const leftClamped = Math.max(8, Math.min(left, window.innerWidth - width - 8));
    const topClamped = top + height > window.innerHeight - 8 ? Math.max(8, anchor.top - height - 7) : Math.max(8, top);
    menu.style.left = `${leftClamped}px`;
    menu.style.top = `${topClamped}px`;
    const first = menu.firstElementChild;
    if (first) first.focus({ preventScroll: true });
  }

  async function copyText(text) {
    const value = String(text || "");
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
      toast("已复制");
    } catch (error) {
      toast("复制失败", true);
    }
  }

  function openProjectEditor(project, parent = null) {
    const parentName = project ? project.parent || "" : parent ? parent.name : "";
    state.editing = {
      original: project ? project.name : "",
      parent: parentName,
      paths: project && Array.isArray(project.paths) ? [...project.paths] : [],
    };
    $("project-dialog-title").textContent = project ? "编辑项目" : parentName ? "新建子项目" : "新增项目";
    const parentLabel = $("project-parent-label");
    parentLabel.hidden = !parentName;
    parentLabel.textContent = parentName ? `主项目：${parentName}` : "";
    $("project-name").value = project ? project.name : "";
    renderEditorPaths();
    $("project-allow-child").checked = !!(project && project.allow_child);
    $("save-project").textContent = project ? "保存修改" : "创建项目";
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

  function openProjectPrompt(project) {
    $("prompt-dialog-title").textContent = "项目提示词";
    $("prompt-dialog-project").textContent = project.name || "";
    const container = $("prompt-editor");
    container.replaceChildren();
    delete container.dataset.loaded;
    $("prompt-dialog").showModal();
    loadProjectPrompt(project, container);
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
    const editor = el("div", "prompt-form");
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
    $("app-version").addEventListener("click", (event) => withBusy(event.currentTarget, async () => {
      $("apply-update").hidden = true;
      $("update-status").textContent = "正在检查更新…";
      $("update-notes").innerHTML = "";
      $("update-dialog").showModal();
      try {
        const result = await call("update_check", true);
        $("update-status").textContent = updateMessage(result);
        $("update-notes").innerHTML = result.newer ? renderMarkdown(result.notes || "") : "";
        $("apply-update").hidden = !result.newer;
      } catch (error) {
        $("update-status").textContent = error.bridge ? OFFLINE : errorText(error);
      }
    }));
    $("cancel-update").addEventListener("click", () => $("update-dialog").close());
    $("update-dialog").addEventListener("cancel", (event) => {
      if ($("cancel-update").disabled) event.preventDefault();
    });

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
        await call("project_save", name, paths, state.editing.parent || "", $("project-allow-child").checked, state.editing.original);
        $("project-dialog").close();
        await loadProjects(true);
        toast("已保存");
      });
    });
    $("close-tree").addEventListener("click", () => $("tree-dialog").close());
    $("tree-dialog").addEventListener("close", closeTreeDialog);
    $("new-child").addEventListener("click", () => {
      const root = state.projects.find(project => project.name === state.activeRoot);
      if (root) openProjectEditor(null, root);
    });
    for (const [id, inTree] of [["projects-view-toggle", false], ["tree-view-toggle", true]]) {
      $(id).addEventListener("click", () => {
        const root = inTree ? state.activeRoot : ROOT_VIEW;
        const visible = root ? treeProjects(root) : rootProjects();
        const open = !visible.every(project => expanded(project.name, root));
        for (const project of visible) viewState(root)[project.name] = open;
        const container = inTree ? $("tree-board") : $("project-list");
        container.querySelectorAll(".project-card").forEach(card => applyCardState(card, open, root));
        refreshViewToggle(root);
        saveProjectViews();
      });
    }
    $("close-prompt").addEventListener("click", () => $("prompt-dialog").close());
    $("prompt-dialog").addEventListener("close", () => {
      const container = $("prompt-editor");
      container.replaceChildren();
      delete container.dataset.loaded;
    });
    $("cancel-delete").addEventListener("click", () => $("delete-dialog").close());
    $("confirm-delete").addEventListener("click", (event) => withBusy(event.currentTarget, async () => {
      await call("project_delete", state.deleting);
      $("delete-dialog").close();
      await loadProjects(true);
      toast("已删除");
    }));
    document.addEventListener("pointerdown", (event) => {
      const menu = $("project-menu");
      if (!menu || menu.hidden || menu.contains(event.target)) return;
      if (menuTrigger && menuTrigger.contains(event.target)) return;
      closeMenu();
    });
    document.addEventListener("keydown", (event) => {
      const menu = $("project-menu");
      if (event.key !== "Escape" || !menu || menu.hidden) return;
      event.stopPropagation();
      closeMenu(true);
    }, true);
    window.addEventListener("resize", () => closeMenu());

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

    $("apply-update").addEventListener("click", (event) => withBusy(event.currentTarget, async () => {
      $("cancel-update").disabled = true;
      $("update-status").textContent = "正在下载并安装更新，请稍候…";
      try {
        const result = await call("update_apply");
        $("update-status").textContent = result.message || (result.restarting ? "升级完成，正在重启 Tbrain" : "已是最新版本");
        $("apply-update").hidden = true;
      } catch (error) {
        $("update-status").textContent = errorText(error);
      } finally {
        $("cancel-update").disabled = false;
      }
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
