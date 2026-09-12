/**
 * 磐石 · 配置面板前端逻辑
 *
 * 通过 window.AstrBotPluginPage bridge 与插件后端通信。
 * 主题由 bridge 同步到 <html data-theme="light|dark">，样式表直接消费。
 */

const bridge = window.AstrBotPluginPage;
const PLUGIN = "astrbot_plugin_panshi";

/** 全局状态 */
const state = {
  schema: [],
  groups: [],
  global: null,
  overview: null,
  meta: {},
  connection: null, // 协议端连接诊断（后端 connection() 的返回）
  connExpanded: false, // 诊断卡是否展开
  selected: null, // 当前选中：{ group_id, ... }
  draft: {}, // 当前编辑中的配置 { 分组: { 字段: 值 } }
  followDefault: true,
  collapsed: {},
  keyword: "",
};

/* ==================================================================
 *  工具
 * ================================================================== */
const $ = (sel) => document.querySelector(sel);

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "html") node.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") {
      node.addEventListener(k.slice(2).toLowerCase(), v);
    } else if (v !== undefined && v !== null && v !== false) {
      node.setAttribute(k, v === true ? "" : v);
    }
  }
  for (const c of [].concat(children)) {
    if (c == null || c === false) continue;
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return node;
}

function toast(text, type = "ok") {
  const box = $("#toast");
  box.textContent = text;
  box.className = `toast show ${type}`;
  clearTimeout(box._timer);
  box._timer = setTimeout(() => {
    box.className = "toast";
  }, 2400);
}

function showError(msg, hints) {
  const box = $("#alertBox");
  box.innerHTML = "";
  if (!msg) return;
  const node = el("div", { class: "alert error" });
  const head = el("div", { class: "alert-head" });
  head.appendChild(el("strong", { text: "出错了：" }));
  head.appendChild(document.createTextNode(msg));
  head.appendChild(
    el("button", {
      class: "btn tiny",
      text: "关闭",
      style: "margin-left:auto",
      onClick: () => (box.innerHTML = ""),
    })
  );
  node.appendChild(head);

  const list = hints || groupErrorHints(msg);
  if (list && list.length) {
    const ul = el("ul", { class: "alert-hints" });
    for (const h of list) ul.appendChild(el("li", { text: h }));
    node.appendChild(ul);
  }
  box.appendChild(node);
}

/** 针对「群列表拉取失败」给出可操作的排查步骤。 */
function groupErrorHints(msg) {
  const text = String(msg || "");
  if (text.includes("未找到平台适配器")) {
    return [
      "打开 AstrBot 控制台 →「平台」页，确认已添加 aiocqhttp 适配器且已启用。",
      "适配器类型需为 aiocqhttp（适用于 NapCat / OneBot v11，反向 WebSocket）。",
      "保存后重启 AstrBot，回到本页面点击「同步」重试。",
    ];
  }
  if (text.includes("尚未与协议端建立连接")) {
    return [
      "NapCat 已启动，但还没连上 AstrBot——两个程序之间是「NapCat 主动连接 AstrBot」。",
      "在 NapCat 的「网络配置」中新增一个「WebSocket 客户端」，URL 填 ws://<AstrBot 地址>:<端口>/ws。",
      "端口取 AstrBot aiocqhttp 适配器配置里的「反向 WebSocket 端口」（默认 6199）。",
      "若设置了 Token，两边必须填一致；NapCat 侧的 Access Token 与 AstrBot 的 ws_reverse_token 要相同。",
      "注意：NapCat 容器/面板内要用宿主机可达的 IP，不能用 127.0.0.1（除非同机同网络命名空间）。",
    ];
  }
  if (text.includes("事件通道")) {
    return [
      "协议端的 WS 连接只上报事件、不能调 API。请把 NapCat 的「WebSocket 客户端」连接方式改为 universal。",
      "如果用的是拆分模式（API 与 EVENT 分开两条连接），请确认两条都启用且地址、Token 一致。",
    ];
  }
  if (text.includes("超时")) {
    return [
      "协议端连接存在但响应慢：可能是 NapCar 负载高或网络抖动，稍等片刻再点「同步」。",
      "若反复出现，请重启 NapCat 并观察其日志中的接口报错。",
    ];
  }
  if (text.includes("无法识别") || text.includes("未能从协议端")) {
    return [
      "协议端返回了异常数据，可在 AstrBot 日志中查看 [磐石] 相关输出定位原因。",
      "确认 NapCat 版本支持 OneBot v11 的 get_group_list 接口。",
    ];
  }
  return [];
}

/* ==================================================================
 *  连接状态（顶栏胶囊 + 诊断卡）
 * ================================================================== */

/** 由连接诊断结果推导顶栏胶囊的视觉状态 */
function connVisual(conn) {
  const s = conn && conn.state;
  if (s === "connected") {
    const n = (conn.self_ids || []).length;
    return {
      cls: "is-ok",
      dot: "ok",
      text: n > 1 ? `已连接 · ${n} 个账号` : "已连接",
    };
  }
  if (s === "event_only") return { cls: "is-warn", dot: "warn", text: "通道异常" };
  if (s === "no_adapter") return { cls: "is-bad", dot: "bad", text: "无适配器" };
  if (s === "no_client") return { cls: "is-bad", dot: "bad", text: "客户端不可用" };
  if (s === "not_connected") return { cls: "is-bad", dot: "bad", text: "未连接" };
  return { cls: "", dot: "pulse", text: "检测中…" };
}

function setOnline(conn) {
  const pill = $("#statusPill");
  const v = connVisual(conn);
  pill.className = `status-pill ${v.cls}`;
  const dot = $("#statusDot");
  dot.className = `dot ${v.dot}`;
  $("#statusText").textContent = v.text;
}

const CONN_STATE_BADGE = {
  connected: { text: "正常", cls: "ok" },
  event_only: { text: "通道异常", cls: "warn" },
  no_adapter: { text: "无适配器", cls: "bad" },
  no_client: { text: "客户端不可用", cls: "bad" },
  not_connected: { text: "未连接", cls: "bad" },
};

/** 渲染连接诊断卡（statusPill 点击展开 / 收起） */
function renderConnection() {
  const box = $("#connBox");
  box.innerHTML = "";
  if (!state.connExpanded) {
    box.hidden = true;
    return;
  }
  box.hidden = false;

  const conn = state.connection;
  const s = conn ? conn.state : "unknown";
  const badge = CONN_STATE_BADGE[s] || { text: "检测中", cls: "off" };

  const card = el("div", { class: "conn-card" });
  const head = el("div", { class: "conn-head" }, [
    el("span", { class: "conn-title", text: "协议端连接诊断" }),
    el("span", { class: `conn-badge ${badge.cls}`, text: badge.text }),
    el("button", {
      class: "btn tiny ghost",
      text: "收起",
      onClick: () => {
        state.connExpanded = false;
        renderConnection();
      },
    }),
  ]);
  card.appendChild(head);

  const body = el("div", { class: "conn-body" });

  if (!conn) {
    body.appendChild(
      el("p", { class: "conn-msg", text: "尚未取得连接诊断数据，请先刷新面板。" })
    );
    card.appendChild(body);
    box.appendChild(card);
    return;
  }

  body.appendChild(el("p", { class: "conn-msg", text: conn.message || "—" }));

  // 事实数据条
  const facts = el("div", { class: "conn-facts" });
  facts.appendChild(
    el("span", { class: "fact-chip", text: `适配器 ${conn.adapters ?? 0} 个` })
  );
  facts.appendChild(
    el("span", { class: "fact-chip", text: `客户端 ${conn.clients ?? 0} 个` })
  );
  const ids = conn.self_ids || [];
  if (ids.length) {
    facts.appendChild(
      el("span", { class: "fact-chip ok", text: `在线账号 ${ids.join("、")}` })
    );
  }
  if (conn.event_only) {
    facts.appendChild(el("span", { class: "fact-chip", text: "仅有事件通道" }));
  }
  if (conn.groups_cached) {
    facts.appendChild(
      el("span", { class: "fact-chip", text: `缓存群 ${conn.groups_cached} 个` })
    );
  }
  body.appendChild(facts);

  // 有问题时给出排查步骤
  if (s !== "connected") {
    const hints = groupErrorHints(conn.message || "") || [];
    if (hints.length) {
      const ul = el("ul", { class: "conn-hints" });
      for (const h of hints) ul.appendChild(el("li", { text: h }));
      body.appendChild(ul);
    }
  } else if (conn.last_error) {
    body.appendChild(
      el("p", {
        class: "conn-msg",
        text: `（最近一次拉取群列表的提示：${conn.last_error}）`,
      })
    );
  }

  card.appendChild(body);
  box.appendChild(card);
}

/** 主动重新检测连接状态 */
async function checkConnection() {
  try {
    state.connection = await apiGet("connection");
  } catch {
    state.connection = null;
  }
  setOnline(state.connection);
  renderConnection();
}

/* ==================================================================
 *  API 封装
 * ================================================================== */
async function apiGet(endpoint, params) {
  return bridge.apiGet(endpoint, params || {});
}

async function apiPost(endpoint, body) {
  return bridge.apiPost(endpoint, body || {});
}

/* ==================================================================
 *  数据加载
 * ================================================================== */
async function loadAll(force = false) {
  const btn = $("#btnReload");
  btn.disabled = true;
  btn.textContent = "加载中…";
  try {
    const data = await apiGet("bootstrap");
    state.schema = data.schema || [];
    state.groups = data.groups || [];
    state.global = data.global || null;
    state.meta = data.meta || {};
    state.connection = state.meta.connection || null;
    state.collapsed = {};
    for (const g of state.schema) state.collapsed[g.key] = true;

    // 版本徽标
    const vb = $("#versionBadge");
    if (state.meta.version) {
      vb.textContent = state.meta.version;
      vb.hidden = false;
    }

    if (state.meta.group_cache_error) {
      showError(state.meta.group_cache_error);
    } else {
      showError("");
    }

    setOnline(state.connection);
    if (state.connExpanded) renderConnection();
    await loadOverview();
    renderGroups();

    // 保持当前选择，否则默认选全局
    const keep = state.selected && state.selected.group_id;
    await selectGroup(keep || state.meta.default_group_id || "__default__");
  } catch (e) {
    setOnline(null);
    showError(e && e.message ? e.message : String(e));
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<span class="btn-icon">⟳</span>刷新';
  }
}

async function loadOverview() {
  try {
    state.overview = await apiGet("overview");
  } catch {
    state.overview = null;
  }
  renderOverview();
}

async function refreshGroups() {
  const btn = $("#btnSync");
  btn.disabled = true;
  btn.textContent = "同步中…";
  try {
    const res = await apiPost("groups/refresh");
    state.groups = (res && res.groups) || [];
    if (res && res.error) {
      showError(res.error);
      toast("同步失败，已显示缓存数据", "err");
    } else {
      showError("");
      toast(`已同步 ${state.groups.length} 个群`);
    }
    await loadOverview();
    await checkConnection();
    renderGroups();
  } catch (e) {
    toast("同步失败：" + (e.message || e), "err");
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<span class="btn-icon">⇵</span>同步';
  }
}

async function selectGroup(groupId) {
  if (!groupId) return;
  try {
    if (groupId === (state.meta.default_group_id || "__default__")) {
      const data = state.global || (await apiGet("global"));
      state.global = data;
      state.selected = {
        group_id: groupId,
        group_name: "全局默认配置",
        is_default: true,
        member_count: 0,
      };
      state.followDefault = false;
      state.draft = deepClone(data.config || {});
    } else {
      const data = await apiGet("group", { group_id: groupId });
      state.selected = data;
      state.followDefault = !!data.follow_default;
      state.draft = deepClone(
        state.followDefault ? data.effective || {} : mergeOverride(data)
      );
    }
    renderGroups();
    renderContent();
  } catch (e) {
    toast("加载配置失败：" + (e.message || e), "err");
  }
}

/** 把「全局 + 群覆盖」合并成可编辑的草稿 */
function mergeOverride(data) {
  const base = deepClone(data.effective || {});
  const override = data.override || {};
  for (const [gkey, gval] of Object.entries(override)) {
    if (gkey === "follow_default") continue;
    if (gval && typeof gval === "object" && !Array.isArray(gval)) {
      base[gkey] = { ...(base[gkey] || {}), ...deepClone(gval) };
    } else {
      base[gkey] = deepClone(gval);
    }
  }
  return base;
}

function deepClone(obj) {
  return JSON.parse(JSON.stringify(obj ?? {}));
}

/**
 * 只保留「用户在本群独立配置里显式改过」的字段，其余不写进 override。
 *
 * 为什么必须这样：override 是「与全局默认的差异」，不是完整配置快照。
 * 若把界面上看到的全部生效值都存进 override，就会把当前全局值固化成该群专属值——
 * 之后管理员再改「全局默认」，这个群不会跟着变，用户会觉得「配置被写死了」。
 */
function buildOverridePayload(effective, globalCfg, followDefault) {
  const payload = { follow_default: !!followDefault };
  for (const gkey of Object.keys(effective || {})) {
    const cur = effective[gkey];
    const base = (globalCfg || {})[gkey];
    if (!cur || typeof cur !== "object" || Array.isArray(cur)) continue;

    const diff = {};
    for (const [fkey, fval] of Object.entries(cur)) {
      const bval = base && typeof base === "object" ? base[fkey] : undefined;
      if (!isSameValue(fval, bval)) diff[fkey] = fval;
    }
    if (Object.keys(diff).length) payload[gkey] = diff;
  }
  return payload;
}

function isSameValue(a, b) {
  if (Array.isArray(a) && Array.isArray(b)) {
    return a.length === b.length && a.every((x, i) => String(x) === String(b[i]));
  }
  return a === b;
}

/* ==================================================================
 *  保存
 * ================================================================== */
async function save() {
  if (!state.selected) return;
  const btn = document.querySelector(".panel-actions .btn.primary");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "保存中…";
  }
  try {
    if (state.selected.is_default) {
      state.global = await apiPost("global", { config: state.draft });
      toast("全局默认配置已保存");
    } else {
      // 关键 1：必须显式带上 follow_default，否则后端会按默认值 true 处理，
      //   把该群刚保存的独立配置清空、退回跟随全局。
      // 关键 2：只提交「与全局默认不同的字段」，不要把界面上的生效值整组写回去，
      //   否则会把当前全局值固化成该群专属值（表现为「配置被写死」）。
      const payload = {
        group_id: state.selected.group_id,
        config: buildOverridePayload(
          state.draft,
          state.global && state.global.config,
          state.followDefault
        ),
      };
      const data = await apiPost("group", payload);
      state.selected = data;
      state.followDefault = !!data.follow_default;
      state.draft = deepClone(
        state.followDefault ? data.effective || {} : mergeOverride(data)
      );
      toast(state.followDefault ? "已保存（跟随全局默认）" : "该群独立配置已保存");
    }
    await loadOverview();
    renderGroups();
    renderContent();
  } catch (e) {
    toast("保存失败：" + (e.message || e), "err");
    showError(e && e.message ? e.message : String(e));
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "保存配置";
    }
  }
}

async function enableOverride() {
  if (!state.selected) return;
  try {
    // 刚切换为独立配置时还没有任何差异，只需带上标记；
    // 否则等于把当前全局值原样固化成该群专属值。
    const data = await apiPost("group", {
      group_id: state.selected.group_id,
      config: { follow_default: false },
    });
    state.selected = data;
    state.followDefault = false;
    state.draft = deepClone(mergeOverride(data));
    toast("已改为独立配置，可自由修改");
    renderGroups();
    renderContent();
  } catch (e) {
    toast("操作失败：" + (e.message || e), "err");
  }
}

async function resetCurrentGroup() {
  if (!state.selected || state.selected.is_default) return;
  if (!confirm("确定要清除该群的独立配置，恢复跟随全局默认吗？")) return;
  try {
    const data = await apiPost("group/reset", {
      group_id: state.selected.group_id,
    });
    state.selected = data;
    state.followDefault = !!data.follow_default;
    state.draft = deepClone(data.effective || {});
    toast("已恢复跟随全局默认");
    renderGroups();
    renderContent();
  } catch (e) {
    toast("重置失败：" + (e.message || e), "err");
  }
}

/* ==================================================================
 *  渲染 — 概览
 * ================================================================== */
function renderOverview() {
  const box = $("#overview");
  box.innerHTML = "";
  if (!state.overview) return;
  const items = [
    { label: "纳管群聊", value: state.overview.tracked_groups, icon: "💬", tone: "" },
    { label: "记录用户", value: state.overview.tracked_users, icon: "👥", tone: "tone-ok" },
    { label: "累计警告", value: state.overview.total_warnings, icon: "⚠️", tone: "tone-warn", cls: "warn" },
    { label: "黑名单", value: state.overview.blocked_users, icon: "🚫", tone: "tone-danger", cls: "danger" },
  ];
  for (const it of items) {
    box.appendChild(
      el("div", { class: `stat-card ${it.tone || ""}`.trim() }, [
        el("span", { class: "stat-top" }, [
          el("span", { class: "stat-icon", text: it.icon }),
          el("span", { class: "stat-label", text: it.label }),
        ]),
        el("span", {
          class: "stat-value" + (it.cls ? " " + it.cls : ""),
          text: String(it.value ?? 0),
        }),
      ])
    );
  }

  // 快捷开关状态条
  const qs = $("#quickStatus");
  qs.innerHTML = "";
  const flips = (state.overview && state.overview.quick_status) || [];
  for (const f of flips) {
    qs.appendChild(
      el("span", { class: `qs-chip ${f.on ? "on" : "off"}` }, [
        el("i", {}),
        document.createTextNode(f.label),
      ])
    );
  }
  qs.style.display = flips.length ? "" : "none";
}

/* ==================================================================
 *  渲染 — 群列表
 * ================================================================== */
function filteredGroups() {
  const kw = state.keyword.trim().toLowerCase();
  if (!kw) return state.groups;
  return state.groups.filter(
    (g) =>
      (g.group_name || "").toLowerCase().includes(kw) ||
      String(g.group_id).includes(kw)
  );
}

function renderGroups() {
  const list = $("#groupList");
  list.innerHTML = "";
  const defaultId = state.meta.default_group_id || "__default__";

  const countEl = $("#groupCount");
  if (countEl) {
    countEl.textContent = state.groups.length
      ? `${state.groups.length} 个群`
      : "";
  }

  // 全局默认项
  list.appendChild(
    groupItem({
      group_id: defaultId,
      group_name: "全局默认配置",
      sub: `作为所有群的模板${
        state.overview ? ` · ${state.overview.tracked_groups} 个群在用` : ""
      }`,
      isDefault: true,
      active: state.selected && state.selected.group_id === defaultId,
    })
  );

  const items = filteredGroups();
  if (!items.length) {
    list.appendChild(
      el("li", {
        class: "group-empty",
        text: state.keyword
          ? "没有匹配的群"
          : state.meta.group_cache_error
            ? "未能获取群列表，请按上方红色提示排查后点「同步」。"
            : "未发现群聊。请确认协议端（NapCat）已连接，然后点上方「同步」。",
      })
    );
    return;
  }

  for (const g of items) {
    list.appendChild(
      groupItem({
        group_id: g.group_id,
        group_name: g.group_name,
        sub: `${g.group_id} · ${g.member_count} 人`,
        tags: g.enabled
          ? g.has_override
            ? [{ text: "独立配置", cls: "tag-custom" }]
            : []
          : [{ text: "未启用", cls: "tag-off" }],
        active: state.selected && state.selected.group_id === g.group_id,
      })
    );
  }
}

function groupItem({ group_id, group_name, sub, tags = [], isDefault, active }) {
  const subNode = el("span", { class: "group-sub" });
  subNode.appendChild(document.createTextNode(sub || ""));
  for (const t of tags) {
    subNode.appendChild(el("span", { class: `tag ${t.cls}`, text: t.text }));
  }

  return el(
    "li",
    {
      class: "group-item" + (active ? " active" : ""),
      onClick: () => {
        state.keyword = "";
        const input = $("#search");
        if (input) input.value = "";
        selectGroup(group_id);
      },
    },
    [
      el("span", {
        class: "avatar" + (isDefault ? " default" : ""),
        text: isDefault ? "默" : (group_name || "?").slice(0, 1),
      }),
      el("div", { class: "group-meta" }, [
        el("span", { class: "group-name", text: group_name }),
        subNode,
      ]),
    ]
  );
}

/* ==================================================================
 *  渲染 — 配置表单
 * ================================================================== */
function renderContent() {
  const box = $("#content");
  box.innerHTML = "";

  if (!state.selected) {
    box.appendChild(
      el("div", { class: "placeholder" }, [
        el("div", { class: "placeholder-icon", text: "🪨" }),
        el("p", { text: "从左侧选择一个群或「全局默认配置」开始编辑。" }),
      ])
    );
    return;
  }

  const readonly = !state.selected.is_default && state.followDefault;
  const sel = state.selected;

  // 标题区
  const head = el("div", { class: "panel-head" }, [
    el("div", {}, [
      el("h2", { text: sel.group_name }),
      el("p", {
        class: "panel-sub",
        text: sel.is_default
          ? "这里的设置会作为所有群的默认模板"
          : `群号 ${sel.group_id}${sel.member_count ? ` · ${sel.member_count} 人` : ""}`,
      }),
    ]),
  ]);

  const actions = el("div", { class: "panel-actions" });
  if (!sel.is_default && state.followDefault) {
    actions.appendChild(
      el("button", { class: "btn primary", text: "改为独立配置", onClick: enableOverride })
    );
  } else {
    if (!sel.is_default) {
      actions.appendChild(
        el("button", { class: "btn danger-ghost", text: "恢复默认", onClick: resetCurrentGroup })
      );
    }
    if (sel.is_default) {
      actions.appendChild(
        el("button", { class: "btn ghost", text: "导出配置", title: "导出全部配置为 JSON 备份", onClick: exportConfig })
      );
      actions.appendChild(
        el("button", { class: "btn ghost", text: "导入配置", title: "从 JSON 备份恢复全部配置", onClick: importConfig })
      );
    }
    actions.appendChild(
      el("button", {
        class: "btn primary",
        text: "保存配置",
        onClick: save,
      })
    );
  }
  head.appendChild(actions);
  box.appendChild(head);

  // 跟随默认提示
  if (readonly) {
    box.appendChild(
      el("div", { class: "alert info" }, [
        el("span", {
          html: "该群正在<b>跟随全局默认配置</b>，下面的内容仅供预览、不可编辑。点击右上角「改为独立配置」即可为本群单独设置。",
        }),
      ])
    );
  }

  // 配置分组
  const wrap = el("div", { class: "groups" });
  for (const group of state.schema) {
    wrap.appendChild(renderGroupSection(group, readonly));
  }
  box.appendChild(wrap);
}

function renderGroupSection(group, readonly) {
  const collapsed = !!state.collapsed[group.key];
  const section = el("section", {
    class: "config-group" + (collapsed ? " collapsed" : ""),
  });

  section.appendChild(
    el("button", {
      class: "group-head",
      onClick: () => {
        state.collapsed[group.key] = !state.collapsed[group.key];
        renderContent();
      },
    }, [
      el("span", { class: "group-icon", text: group.icon || "🔧" }),
      el("span", { class: "group-title", text: group.title }),
      el("span", { class: "group-count", text: `${group.fields.length} 项` }),
      el("span", { class: "chevron", text: "›" }),
    ])
  );

  if (collapsed) return section;

  const body = el("div", { class: "group-body" });
  if (group.hint) {
    body.appendChild(el("p", { class: "group-hint", text: group.hint }));
  }

  const fields = el("div", { class: "fields" });
  for (const field of group.fields) {
    fields.appendChild(renderField(group.key, field, readonly));
  }
  body.appendChild(fields);
  section.appendChild(body);
  return section;
}

function renderField(groupKey, field, readonly) {
  const row = el("div", { class: `field field-${field.type}` });

  // 标签
  const label = el("label", { class: "field-label" }, [
    el("span", { class: "field-name", text: field.label }),
  ]);
  if (field.hint) {
    label.appendChild(
      el("span", { class: "hint-icon", text: "?", title: field.hint })
    );
  }
  row.appendChild(label);

  const value = getValue(groupKey, field.key);

  // 控件
  if (field.type === "bool") {
    const input = el("input", {
      type: "checkbox",
      checked: value ? "checked" : false,
      disabled: readonly,
    });
    input.addEventListener("change", () =>
      setValue(groupKey, field.key, input.checked)
    );
    const text = el("span", { class: "switch-text", text: value ? "已开启" : "已关闭" });
    input.addEventListener("change", () => {
      text.textContent = input.checked ? "已开启" : "已关闭";
    });
    row.appendChild(
      el("label", { class: "switch" }, [input, el("span", { class: "slider" }), text])
    );
  } else if (field.type === "int") {
    const attrs = {
      class: "input",
      type: "number",
      value: value ?? 0,
      disabled: readonly,
      step: field.slider ? field.slider.step : 1,
    };
    if (field.slider) {
      attrs.min = field.slider.min;
      attrs.max = field.slider.max;
    }
    const input = el("input", attrs);
    input.addEventListener("input", () =>
      setValue(groupKey, field.key, input.value)
    );
    row.appendChild(input);
    if (field.slider) {
      row.appendChild(
        el("span", {
          class: "field-tail",
          text: `范围 ${field.slider.min} ~ ${field.slider.max}`,
        })
      );
    }
  } else if (field.type === "list") {
    const list = Array.isArray(value) ? value : [];
    const input = el("textarea", {
      class: "input textarea",
      rows: 3,
      disabled: readonly,
      placeholder: "每行一项，或用逗号分隔",
    });
    input.value = list.join("\n");
    const tail = el("span", { class: "field-tail", text: `共 ${list.length} 项` });
    input.addEventListener("input", () => {
      setValue(groupKey, field.key, input.value);
      const n = parseList(input.value).length;
      tail.textContent = `共 ${n} 项`;
    });
    row.appendChild(input);
    row.appendChild(tail);
  } else if (field.options && field.options.length) {
    const select = el("select", { class: "input", disabled: readonly });
    for (const opt of field.options) {
      const o = el("option", { value: opt, text: opt });
      if (opt === value) o.selected = true;
      select.appendChild(o);
    }
    select.addEventListener("change", () =>
      setValue(groupKey, field.key, select.value)
    );
    row.appendChild(select);
  } else {
    const input = el("input", {
      class: "input",
      type: "text",
      value: value ?? "",
      disabled: readonly,
    });
    input.addEventListener("input", () =>
      setValue(groupKey, field.key, input.value)
    );
    row.appendChild(input);
  }

  // 说明文字
  if (field.hint) {
    row.appendChild(el("p", { class: "field-hint", text: field.hint }));
  }
  return row;
}

/* ==================================================================
 *  配置备份：导出 / 导入
 * ================================================================== */
async function exportConfig() {
  try {
    const data = await apiGet("export");
    const blob = new Blob([JSON.stringify(data, null, 2)], {
      type: "application/json",
    });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `panshi_config_${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
    toast("配置已导出为 JSON 备份");
  } catch (e) {
    toast("导出失败：" + (e.message || e), "err");
  }
}

function importConfig() {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = ".json,application/json";
  input.onchange = async () => {
    const file = input.files && input.files[0];
    if (!file) return;
    if (
      !confirm(
        "导入会覆盖当前的全部配置与各群独立配置，且不可撤销。\n确定继续吗？"
      )
    ) {
      return;
    }
    try {
      const payload = JSON.parse(await file.text());
      const res = await apiPost("import", payload);
      toast(`配置已导入（恢复 ${res && res.restored_groups ? res.restored_groups : 0} 个群的独立配置）`);
      await loadAll(true);
    } catch (e) {
      toast("导入失败：" + (e.message || e), "err");
    }
  };
  input.click();
}

/* ==================================================================
 *  取值 / 赋值
 * ================================================================== */
function getValue(groupKey, fieldKey) {
  const grp = state.draft[groupKey];
  if (!grp || typeof grp !== "object") return undefined;
  return grp[fieldKey];
}

function setValue(groupKey, fieldKey, raw) {
  if (!state.draft[groupKey] || typeof state.draft[groupKey] !== "object") {
    state.draft[groupKey] = {};
  }
  const field = findField(groupKey, fieldKey);
  let value = raw;
  if (field) {
    if (field.type === "bool") value = !!raw;
    else if (field.type === "int") {
      const n = parseInt(raw, 10);
      value = Number.isNaN(n) ? 0 : n;
    } else if (field.type === "list") value = parseList(raw);
    else value = String(raw ?? "");
  }
  state.draft[groupKey][fieldKey] = value;
}

function findField(groupKey, fieldKey) {
  const group = state.schema.find((g) => g.key === groupKey);
  return group ? group.fields.find((f) => f.key === fieldKey) : null;
}

function parseList(text) {
  if (Array.isArray(text)) return text.map((x) => String(x).trim()).filter(Boolean);
  return String(text || "")
    .split(/[\n,，;；]+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

/* ==================================================================
 *  启动
 * ================================================================== */
async function main() {
  if (!bridge) {
    showError("未检测到 AstrBot 页面桥接（bridge）。请从 AstrBot 插件详情页打开本面板。");
    setOnline(null);
    return;
  }

  try {
    await bridge.ready();
  } catch {
    /* 桥接未就绪也继续尝试 */
  }

  $("#btnReload").addEventListener("click", () => loadAll(true));
  $("#btnSync").addEventListener("click", refreshGroups);
  $("#statusPill").addEventListener("click", () => {
    state.connExpanded = !state.connExpanded;
    if (state.connExpanded) checkConnection();
    else renderConnection();
  });
  $("#search").addEventListener("input", (e) => {
    state.keyword = e.target.value;
    renderGroups();
  });

  await loadAll();

  // 连接状态自动轮询：每 30 秒静默刷新一次（页面可见时才请求）
  setInterval(() => {
    if (document.visibilityState !== "visible") return;
    checkConnection();
  }, 30000);
}

main();
