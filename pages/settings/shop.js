/**
 * 磐石 · 商品与奖池管理
 *
 * 两个独立视图（用户要求分开，不要混在一页）：
 *   · 商品  renderItemsView  —— 商品名 / 价格 / 库存
 *   · 抽奖  renderPrizesView —— 奖品名 / 概率 / 可中次数
 *
 * 为什么做这个界面：商品和奖池原本只能去 AstrBot 配置页手写 JSON 数组，
 * 加一件商品要小心补逗号引号，还容易把整个配置写坏。
 * 这里改成表格 + 「＋ 添加」，填空就行，保存后立即生效。
 *
 * 自带工具函数而不用 import：面板其余脚本都是普通 <script>，
 * app.js 也没有导出任何东西。
 */

const PLUGIN = "astrbot_plugin_panshi";

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on") && typeof v === "function") {
      node.addEventListener(k.slice(2).toLowerCase(), v);
    } else if (k === "checked") node.checked = !!v;
    else if (k === "value") node.value = v;
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of [].concat(children || [])) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

/* 面板跑在受限 iframe 里，bridge 注入在本页面自己的 window 上 */
function bridge() {
  try {
    return window.AstrBotPluginPage || null;
  } catch (e) {
    return null;
  }
}

function apiGet(endpoint, params) {
  const b = bridge();
  if (!b) throw new Error("未检测到页面通信接口，请从「插件管理」里打开本插件页面。");
  const qs = new URLSearchParams(
    Object.entries(params || {}).filter(([, v]) => v !== undefined && v !== null)
  ).toString();
  return b.apiGet(PLUGIN + "/" + endpoint + (qs ? "?" + qs : ""));
}

function apiPost(endpoint, body) {
  const b = bridge();
  if (!b) throw new Error("未检测到页面通信接口，请从「插件管理」里打开本插件页面。");
  return b.apiPost(PLUGIN + "/" + endpoint, body || {});
}

function toast(text, type = "ok") {
  const t = document.getElementById("toast");
  if (!t) return;
  t.textContent = text;
  t.className = "toast show " + type;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { t.className = "toast"; }, 2600);
}

function showError(msg) {
  const box = document.getElementById("alertBox");
  if (!box) return;
  box.innerHTML = "";
  if (!msg) return;
  box.append(el("div", { class: "alert error", text: String(msg) }));
}

/* 自绘确认框：iframe 里原生 confirm() 可能被 allow-modals 拦掉且不报错 */
function askConfirm({ title, message, confirmText = "确定", danger = false } = {}) {
  return new Promise((resolve) => {
    let done = false;
    const finish = (v) => {
      if (done) return;
      done = true;
      document.removeEventListener("keydown", onKey, true);
      overlay.remove();
      resolve(v);
    };
    const onKey = (e) => {
      if (e.key === "Escape") finish(false);
      else if (e.key === "Enter") finish(true);
    };
    const overlay = el("div", { class: "modal-overlay" }, [
      el("div", { class: "modal-card" }, [
        el("h3", { class: "modal-title", text: title || "确认" }),
        el("p", { class: "modal-msg", text: message || "" }),
        el("div", { class: "modal-actions" }, [
          el("button", { class: "btn ghost", text: "取消", onClick: () => finish(false) }),
          el("button", {
            class: "btn " + (danger ? "danger" : "primary"),
            text: confirmText, onClick: () => finish(true),
          }),
        ]),
      ]),
    ]);
    overlay.addEventListener("click", (e) => { if (e.target === overlay) finish(false); });
    document.addEventListener("keydown", onKey, true);
    document.body.append(overlay);
  });
}

/* ------------------------------------------------------------------ *
 *  表格：可增删的一行行输入
 * ------------------------------------------------------------------ */

function buildTable({ columns, rows, makeRow, kind = "条目", emptyHint }) {
  const tbody = el("tbody");
  const span = String(columns.length);

  const emptyRow = () => el("tr", { class: "shop-empty-row" }, [
    el("td", { colspan: span, class: "muted", text: emptyHint || "还没有内容" }),
  ]);

  const remove = (row) => {
    row.remove();
    if (!tbody.querySelector("tr.shop-row")) tbody.append(emptyRow());
  };

  for (const r of rows) tbody.append(makeRow(r, remove));
  if (!rows.length) tbody.append(emptyRow());

  const table = el("table", { class: "shop-table" }, [
    el("thead", {}, [
      el("tr", {}, columns.map((c) =>
        el("th", { class: c.cls || "", text: c.label })
      )),
    ]),
    tbody,
  ]);

  const read = () => {
    const out = [];
    for (const tr of tbody.children) {
      if (tr.classList.contains("shop-empty-row")) continue;
      if (typeof tr._read === "function") out.push(tr._read());
    }
    return out;
  };

  const addBlank = (seed = {}) => {
    const e = tbody.querySelector(".shop-empty-row");
    if (e) e.remove();
    const row = makeRow(seed, remove);
    tbody.append(row);
    const first = row.querySelector("input");
    if (first) first.focus();
    return row;
  };

  return { table, read, addBlank, tbody };
}

function itemRow(it, onRemove) {
  const name = el("input", {
    class: "shop-input grow-2", type: "text", value: it.name || "",
    placeholder: "商品名（必填）",
  });
  const cost = el("input", {
    class: "shop-input num", type: "number", min: "0", step: "1",
    value: it.cost ?? 0, placeholder: "价格",
  });
  const stock = el("input", {
    class: "shop-input num", type: "number", min: "0", step: "1",
    value: it.stock === null || it.stock === undefined ? "" : it.stock,
    placeholder: "不限",
  });
  const row = el("tr", { class: "shop-row" }, [
    el("td", {}, [name]),
    el("td", {}, [cost]),
    el("td", {}, [stock]),
    el("td", { class: "shop-meta" }, [
      el("span", { class: "muted", text: it.sold ? `已售 ${it.sold}` : "" }),
    ]),
    el("td", { class: "shop-ops" }, [
      el("button", {
        class: "btn tiny danger", type: "button", text: "✕",
        title: "删除这行", onClick: () => onRemove(row),
      }),
    ]),
  ]);
  row._read = () => ({
    id: it.id, name: name.value.trim(),
    cost: Number(cost.value || 0),
    stock: stock.value === "" ? null : Number(stock.value),
    // 保留原有字段，改名字/价格时不要把发放方式丢掉
    reward: it.reward || "manual", value: it.value || "",
    description: it.description || "",
    limit_per_user: it.limit_per_user || 0,
    limit_per_day: it.limit_per_day || 0,
    enabled: it.enabled !== false,
  });
  return row;
}

function prizeRow(p, onRemove) {
  const name = el("input", {
    class: "shop-input grow-2", type: "text", value: p.name || "",
    placeholder: "奖品名（必填）",
  });
  const chance = el("input", {
    class: "shop-input num", type: "number", min: "0", max: "1", step: "0.01",
    value: p.chance ?? 0, placeholder: "0~1",
  });
  const stock = el("input", {
    class: "shop-input num", type: "number", min: "0", step: "1",
    value: p.stock === null || p.stock === undefined ? "" : p.stock,
    placeholder: "不限",
  });
  const rare = el("input", {
    type: "checkbox", checked: !!p.rare, title: "标记为稀有，供保底使用",
  });

  const pct = el("span", { class: "shop-pct" });
  const paint = () => {
    const v = Number(chance.value || 0);
    pct.textContent = `${(v * 100).toFixed(2)}%`;
    pct.classList.toggle("warn", v > 1);
  };
  chance.addEventListener("input", paint);
  paint();

  const row = el("tr", { class: "shop-row" }, [
    el("td", {}, [name]),
    el("td", { class: "shop-num-cell" }, [chance, pct]),
    el("td", {}, [stock]),
    el("td", { class: "shop-center" }, [rare]),
    el("td", { class: "shop-meta" }, [
      el("span", { class: "muted", text: p.won ? `已中 ${p.won}` : "" }),
    ]),
    el("td", { class: "shop-ops" }, [
      el("button", {
        class: "btn tiny danger", type: "button", text: "✕",
        title: "删除这行", onClick: () => onRemove(row),
      }),
    ]),
  ]);
  row._read = () => ({
    id: p.id, name: name.value.trim(),
    chance: Number(chance.value || 0),
    stock: stock.value === "" ? null : Number(stock.value),
    rare: rare.checked,
    reward: p.reward || "points", value: p.value || "0",
    enabled: p.enabled !== false,
  });
  return row;
}

/* ------------------------------------------------------------------ *
 *  视图
 * ------------------------------------------------------------------ */

/** 统一的页面头：标题 + 返回按钮。 */
function viewHead(title, subtitle, onBack, onReset) {
  const right = [];
  if (onReset) {
    right.push(el("button", {
      class: "btn ghost", type: "button", text: "恢复默认",
      title: "丢弃这里的修改，回到配置文件里的内容",
      onClick: onReset,
    }));
  }
  right.push(el("button", {
    class: "btn", type: "button", text: "← 返回",
    title: "回到配置",
    onClick: onBack,
  }));
  return el("div", { class: "shop-head" }, [
    el("div", {}, [
      el("h2", { text: title }),
      subtitle ? el("p", { class: "sub", text: subtitle }) : null,
    ]),
    el("div", { class: "shop-head-actions" }, right),
  ]);
}

/** 一行设置项：标签 + 控件 + 说明。 */
function settingRow(label, control, hint) {
  return el("div", { class: "shop-setting" }, [
    el("div", { class: "shop-setting-label", text: label }),
    el("div", { class: "shop-setting-ctrl" }, [control]),
    hint ? el("div", { class: "shop-setting-hint", text: hint }) : null,
  ]);
}

/** 数字输入（带上下限）。 */
function numInput(value, { min = 0, max = 99999, step = 1 } = {}) {
  return el("input", {
    class: "shop-input num", type: "number",
    min: String(min), max: String(max), step: String(step),
    value: value ?? 0,
  });
}

/**
 * 开关（把 checkbox 样式化成滑块）。
 *
 * 返回 ``{el, box}``：``el`` 是给页面用的 `<label>`，
 * 真正带 ``checked`` 的是里面的 ``box``。
 *
 * 必须把两者分开返回：之前只返回 `<label>`，读 ``label.checked``
 * 永远是 ``undefined``，于是「启用商城」怎么点都保存不上，
 * 而且不抛任何错——最难查的那种。
 */
function toggleInput(checked) {
  const box = el("input", { type: "checkbox", class: "shop-switch-box", checked: !!checked });
  const label = el("label", { class: "shop-switch" }, [
    box, el("span", { class: "shop-switch-track" }, [el("span", { class: "shop-switch-dot" })]),
  ]);
  return { el: label, box };
}

/**
 * 设置区：把原本在 AstrBot 原生配置页里的开关与参数搬到这里。
 *
 * 每页只放自己那部分——商品页放商城的，抽奖页放抽奖的。
 * 返回 {section, read}，read() 给出待保存的值。
 */
function settingsSection(kind, settings, onSaved) {
  const isShop = kind === "shop";

  const enable = toggleInput(isShop ? settings.enable : settings.lottery_enable);
  const rows = [
    settingRow(
      isShop ? "启用积分商城" : "启用抽奖",
      enable.el,
      isShop ? "开启后群友可用 /商城、/购买 <商品名>"
             : "开启后群友可用 /抽奖。可以先只开商城不开抽奖。",
    ),
  ];

  let cost = null, daily = null, pity = null;
  if (!isShop) {
    cost = numInput(settings.lottery_cost, { max: 100000 });
    daily = numInput(settings.lottery_daily_limit, { max: 10000 });
    pity = numInput(settings.lottery_pity, { max: 100000 });
    rows.push(
      settingRow("每次抽奖消耗积分", cost, "设为 0 则免费抽"),
      settingRow("每人每日抽奖次数", daily, "0 = 不限次数"),
      settingRow("抽奖保底次数", pity,
        "连续这么多次没抽中「稀有」档时，下一次必出稀有。0 = 不要保底。"),
    );
  }

  const read = () => {
    const out = isShop
      ? { enable: enable.box.checked }
      : {
        lottery_enable: enable.box.checked,
        lottery_cost: Number(cost.value || 0),
        lottery_daily_limit: Number(daily.value || 0),
        lottery_pity: Number(pity.value || 0),
      };
    return out;
  };

  const section = el("section", { class: "shop-card" }, [
    el("div", { class: "shop-card-head" }, [
      el("strong", { text: isShop ? "商城设置" : "抽奖设置" }),
      el("span", { class: "muted", text: "改完点保存，立即生效" }),
    ]),
    el("div", { class: "shop-settings" }, rows),
    el("div", { class: "shop-actions" }, [
      el("button", {
        class: "btn primary", type: "button", text: "保存设置",
        onClick: async () => {
          try {
            await apiPost("shop/settings", { settings: read() });
            toast("设置已保存");
            if (onSaved) await onSaved();
          } catch (e) {
            showError(String(e.message || e));
          }
        },
      }),
      el("span", {
        class: "muted",
        text: isShop ? "关闭后本群不显示商城、不能购买与抽奖，也不再扣违规积分"
                     : "关闭后 /抽奖 不可用，商城不受影响",
      }),
    ]),
  ]);

  return { section, read };
}

/** 商品管理（含商城设置）。 */
async function renderItemsView(host, onBack) {
  host.innerHTML = "";
  const wrap = el("div", { class: "shop-wrap" });

  let data = { items: [] };
  let settings = { enable: false };
  try {
    const [itemsRes, setRes] = await Promise.all([
      apiGet("shop/items", {}),
      apiGet("shop/settings", {}).catch(() => ({})),
    ]);
    data = itemsRes;
    settings = { ...settings, ...setRes };
  } catch (e) {
    wrap.append(el("div", {
      class: "alert error",
      text: `读取商品失败：${e.message || e}。若提示「未找到该路由」，` +
        `说明插件的商城接口没注册上——请在插件管理里重载磐石，` +
        `再看日志里有没有「[磐石] 配置面板」相关的告警。`,
    }));
  }

  const items = buildTable({
    columns: [
      { label: "商品名", cls: "grow-2" },
      { label: "价格", cls: "num" },
      { label: "库存", cls: "num" },
      { label: "统计", cls: "shop-meta" },
      { label: "", cls: "shop-ops" },
    ],
    rows: data.items || [],
    makeRow: itemRow,
    emptyHint: "还没有商品，点右边「＋ 添加商品」",
  });

  wrap.append(viewHead("🛒 商品", null, onBack, async () => {
    const ok = await askConfirm({
      title: "恢复默认",
      message: "会丢弃这里的商品改动。",
      confirmText: "恢复", danger: true,
    });
    if (!ok) return;
    try {
      await apiPost("shop/reset", {});
      toast("已恢复默认");
      await renderItemsView(host, onBack);
    } catch (e) {
      showError(String(e.message || e));
    }
  }));

  // 商城设置放在列表上面：先决定开不开，再管具体商品
  wrap.append(settingsSection("shop", settings,
    () => renderItemsView(host, onBack)).section);

  wrap.append(el("section", { class: "shop-card" }, [
    el("div", { class: "shop-card-head" }, [
      el("strong", { text: "商品列表" }),
      el("span", { class: "muted", text: "群友用 /购买 <商品名> 下单" }),
      el("span", { class: "spacer" }),
      el("button", {
        class: "btn tiny", type: "button", text: "＋ 添加商品",
        onClick: () => items.addBlank({ cost: 50, stock: null, reward: "manual" }),
      }),
    ]),
    items.table,
    el("div", { class: "shop-actions" }, [
      el("button", {
        class: "btn primary", type: "button", text: "保存商品",
        onClick: async () => {
          try {
            const r = await apiPost("shop/items", { items: items.read() });
            toast(`已保存 ${r.saved} 件商品`);
            await renderItemsView(host, onBack);
          } catch (e) {
            showError(String(e.message || e));
          }
        },
      }),
      el("span", { class: "muted", text: "库存留空 = 不限量" }),
    ]),
  ]));

  host.append(wrap);
}

/** 抽奖管理。 */
async function renderPrizesView(host, onBack) {
  host.innerHTML = "";
  const wrap = el("div", { class: "shop-wrap" });

  let data = { prizes: [] };
  let settings = { lottery_enable: false, lottery_cost: 10,
                   lottery_daily_limit: 3, lottery_pity: 10 };
  try {
    const [prizeRes, setRes] = await Promise.all([
      apiGet("shop/prizes", {}),
      apiGet("shop/settings", {}).catch(() => ({})),
    ]);
    data = prizeRes;
    settings = { ...settings, ...setRes };
  } catch (e) {
    wrap.append(el("div", {
      class: "alert error",
      text: `读取奖池失败：${e.message || e}。若提示「未找到该路由」，` +
        `说明插件的商城接口没注册上——请在插件管理里重载磐石。`,
    }));
  }

  const prizes = buildTable({
    columns: [
      { label: "奖品名", cls: "grow-2" },
      { label: "概率（0~1）", cls: "num" },
      { label: "可中次数", cls: "num" },
      { label: "稀有", cls: "shop-center" },
      { label: "统计", cls: "shop-meta" },
      { label: "", cls: "shop-ops" },
    ],
    rows: data.prizes || [],
    makeRow: prizeRow,
    emptyHint: "还没有奖品，点右边「＋ 添加奖品」",
  });

  const totalBox = el("div", { class: "shop-total" });
  const paintTotal = () => {
    const rows = prizes.read();
    const total = rows.filter((r) => r.enabled !== false)
      .reduce((s, r) => s + (Number(r.chance) || 0), 0);
    totalBox.innerHTML = "";
    totalBox.append(
      el("span", { text: `中奖合计 ${(total * 100).toFixed(2)}%` }),
      el("span", { class: "muted", text: `未中奖 ${(Math.max(0, 1 - total) * 100).toFixed(2)}%` }),
    );
    totalBox.classList.toggle("over", total > 1.0000001);
    if (total > 1.0000001) {
      totalBox.append(el("span", {
        class: "shop-over-hint", text: "超过 1，保存会被拒绝——请调低某些奖品",
      }));
    }
  };
  prizes.tbody.addEventListener("input", paintTotal);
  prizes.tbody.addEventListener("click", () => setTimeout(paintTotal, 0));
  paintTotal();

  wrap.append(viewHead("🎰 抽奖", null, onBack, async () => {
    const ok = await askConfirm({
      title: "恢复默认",
      message: "会丢弃这里的奖池改动。",
      confirmText: "恢复", danger: true,
    });
    if (!ok) return;
    try {
      await apiPost("shop/reset", {});
      toast("已恢复默认");
      await renderPrizesView(host, onBack);
    } catch (e) {
      showError(String(e.message || e));
    }
  }));

  // 抽奖设置放在列表上面：先决定开不开，再管具体奖品
  wrap.append(settingsSection("lottery", settings,
    () => renderPrizesView(host, onBack)).section);

  wrap.append(el("section", { class: "shop-card" }, [
    el("div", { class: "shop-card-head" }, [
      el("strong", { text: "奖池" }),
      el("span", { class: "muted", text: "群友用 /抽奖 抽" }),
      el("span", { class: "spacer" }),
      el("button", {
        class: "btn tiny", type: "button", text: "＋ 添加奖品",
        onClick: () => { prizes.addBlank({ chance: 0.1, stock: null }); paintTotal(); },
      }),
    ]),
    prizes.table,
    el("div", { class: "shop-actions" }, [totalBox]),
    el("div", { class: "shop-actions" }, [
      el("button", {
        class: "btn primary", type: "button", text: "保存奖池",
        onClick: async () => {
          try {
            const r = await apiPost("shop/prizes", { prizes: prizes.read() });
            toast(`已保存 ${r.saved} 个奖品，中奖合计 `
              + `${((r.total_chance || 0) * 100).toFixed(2)}%`);
            await renderPrizesView(host, onBack);
          } catch (e) {
            showError(String(e.message || e));
          }
        },
      }),
      el("span", { class: "muted", text: "可中次数留空 = 不限；概率之和不超过 1" }),
    ]),
  ]));

  host.append(wrap);
}

/* 挂到 window 上供 app.js 调用。
 * 不用 ES module：面板其余脚本都是普通 <script>，
 * 混用模块与非模块容易出现加载顺序问题。 */
window.PanshiShop = { renderItemsView, renderPrizesView };
