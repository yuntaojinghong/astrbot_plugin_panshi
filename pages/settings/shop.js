/**
 * 磐石 · 积分商城 / 奖池 可视化管理
 *
 * 为什么单独做一个界面：商品和奖池原本只能去 AstrBot 配置页手写 JSON 数组，
 * 加一件商品要小心补逗号引号、还容易把整个配置写坏（v1.8.3 就因为 schema
 * 写错导致插件装不上）。这里改成表格 + 「＋」按钮，填空就行。
 *
 * 数据接口：/shop/items、/shop/prizes（见 pages_api.py）。
 * 保存后立即生效，不需要重载插件。
 */

/* 自成一体：app.js 没有导出任何东西（它是直接执行的面板脚本），
 * 所以这里自带一份最小的工具函数，避免为了复用去改动已经跑通的面板。 */

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

function req(endpoint, params) {
  const b = bridge();
  if (!b) throw new Error("未检测到页面通信接口，请从「插件管理」里打开本插件页面。");
  const qs = new URLSearchParams(
    Object.entries(params || {}).filter(([, v]) => v !== undefined && v !== null)
  ).toString();
  return b.apiGet(endpoint + (qs ? "?" + qs : ""));
}

function post(endpoint, body) {
  const b = bridge();
  if (!b) throw new Error("未检测到页面通信接口，请从「插件管理」里打开本插件页面。");
  return b.apiPost(endpoint, body || {});
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

/* 兼容原来的名字 */
const apiGet = (endpoint, params) => req(PLUGIN + "/" + endpoint, params);
const apiPost = (endpoint, body) => post(PLUGIN + "/" + endpoint, body);

/** 商品行：名字 / 价格 / 库存 三个框（用户明确要的最小集） */
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
      el("span", {
        class: "muted",
        text: it.sold ? `已售 ${it.sold}` : "",
      }),
    ]),
    el("td", { class: "shop-ops" }, [
      el("button", {
        class: "btn tiny danger", type: "button", text: "✕",
        title: "删除这行",
        onClick: () => onRemove(row),
      }),
    ]),
  ]);
  row._read = () => ({
    id: it.id, name: name.value.trim(),
    cost: Number(cost.value || 0),
    stock: stock.value === "" ? null : Number(stock.value),
    // 保留原有字段，避免编辑名字/价格时把发放方式丢掉
    reward: it.reward || "manual", value: it.value || "",
    description: it.description || "",
    limit_per_user: it.limit_per_user || 0,
    limit_per_day: it.limit_per_day || 0,
    enabled: it.enabled !== false,
    rare: it.rare,
  });
  return row;
}

/** 奖品行：名字 / 概率 / 库存 */
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
        title: "删除这行",
        onClick: () => onRemove(row),
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

/** 建一张可增删的表格；返回 { wrap, read, addBlank, tbody } */
function buildTable({ columns, rows, makeRow, kind = "条目" }) {
  const tbody = el("tbody");
  const remove = (row) => {
    row.remove();
    if (!tbody.children.length) tbody.append(emptyRow(columns.length));
  };
  const emptyRow = (span) =>
    el("tr", { class: "shop-empty-row" }, [
      el("td", { colspan: String(span), class: "muted", text: `还没有内容，点上面的「＋ 添加${kind}」` }),
    ]);

  for (const r of rows) tbody.append(makeRow(r, remove));
  if (!rows.length) tbody.append(emptyRow(columns.length));

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

/** 渲染整个商城管理视图 */
async function renderShopView(host) {
  host.innerHTML = "";
  const wrap = el("div", { class: "shop-wrap" });

  wrap.append(el("div", { class: "shop-head" }, [
    el("div", {}, [
      el("h2", { text: "🛒 积分商城与奖池" }),
      el("p", {
        class: "sub",
        text: "在这里点「＋」加一条、填几个框就行——不用再去配置页手写 JSON 数组。"
          + "保存后立即生效。留空库存表示不限量。",
      }),
    ]),
    el("button", {
      class: "btn ghost", type: "button", text: "恢复默认",
      title: "丢弃这里的修改，回到配置文件里的默认商品与奖池",
      onClick: async () => {
        const ok = await askConfirm({
          title: "恢复默认",
          message: "会丢弃你在本页添加/修改的商品与奖池，回到配置文件里的默认内容。",
          confirmText: "恢复", danger: true,
        });
        if (!ok) return;
        try {
          await apiPost("shop/reset", {});
          toast("已恢复默认");
          await renderShopView(host);
        } catch (e) {
          showError(String(e.message || e));
        }
      },
    }),
  ]));

  let itemsData = { items: [] };
  let prizesData = { prizes: [] };
  try {
    [itemsData, prizesData] = await Promise.all([
      apiGet("shop/items", {}),
      apiGet("shop/prizes", {}),
    ]);
  } catch (e) {
    wrap.append(el("div", { class: "alert error", text: String(e.message || e) }));
    host.append(wrap);
    return;
  }

  /* ---------------- 商品 ---------------- */
  const items = buildTable({
    columns: [
      { label: "商品名", cls: "grow-2" },
      { label: "价格", cls: "num" },
      { label: "库存", cls: "num" },
      { label: "统计", cls: "shop-meta" },
      { label: "", cls: "shop-ops" },
    ],
    rows: itemsData.items || [],
    makeRow: itemRow,
    kind: "商品",
  });

  wrap.append(el("section", { class: "shop-card" }, [
    el("div", { class: "shop-card-head" }, [
      el("strong", { text: "商品" }),
      el("span", { class: "muted", text: "群友用「/购买 <商品名>」下单" }),
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
            await renderShopView(host);
          } catch (e) {
            showError(String(e.message || e));
          }
        },
      }),
    ]),
  ]));

  /* ---------------- 奖池 ---------------- */
  const prizes = buildTable({
    columns: [
      { label: "奖品名", cls: "grow-2" },
      { label: "概率（0~1）", cls: "num" },
      { label: "可中次数", cls: "num" },
      { label: "稀有", cls: "shop-center" },
      { label: "统计", cls: "shop-meta" },
      { label: "", cls: "shop-ops" },
    ],
    rows: prizesData.prizes || [],
    makeRow: prizeRow,
    kind: "奖品",
  });

  const totalBox = el("div", { class: "shop-total" });
  const paintTotal = () => {
    const rows = prizes.read();
    const total = rows.filter((r) => r.enabled !== false)
      .reduce((s, r) => s + (Number(r.chance) || 0), 0);
    const miss = Math.max(0, 1 - total);
    totalBox.innerHTML = "";
    totalBox.append(
      el("span", { text: `中奖合计 ${(total * 100).toFixed(2)}%` }),
      el("span", { class: "muted", text: `未中奖 ${(miss * 100).toFixed(2)}%` }),
    );
    totalBox.classList.toggle("over", total > 1.0000001);
    if (total > 1.0000001) {
      totalBox.append(el("span", {
        class: "shop-over-hint",
        text: "超过 1，保存会被拒绝——请调低某些奖品",
      }));
    }
  };
  prizes.tbody.addEventListener("input", paintTotal);
  prizes.tbody.addEventListener("click", () => setTimeout(paintTotal, 0));
  paintTotal();

  wrap.append(el("section", { class: "shop-card" }, [
    el("div", { class: "shop-card-head" }, [
      el("strong", { text: "奖池" }),
      el("span", { class: "muted", text: "群友用「/抽奖」抽；概率之和不能超过 1" }),
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
            await renderShopView(host);
          } catch (e) {
            showError(String(e.message || e));
          }
        },
      }),
    ]),
  ]));

  host.append(wrap);
}


/* 挂到 window 上供 app.js 调用。
 * 不用 ES module：面板其余脚本都是普通 <script>，
 * 混用模块与非模块反而容易出现加载顺序问题。 */
window.PanshiShop = { renderShopView };
