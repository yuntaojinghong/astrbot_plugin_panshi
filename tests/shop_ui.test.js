/* 商品 / 抽奖管理界面（jsdom）
 *
 * 用户要求：
 *   · 商品与抽奖**分开成两页**
 *   · 每页有「← 返回」
 *   · 点「＋」填几个框就能加一条，不用写 JSON
 *   · 概率之和不得超过 1
 *
 * 跑法：REPO=<插件目录> node tests/shop_ui.test.js
 */
const fs = require("fs");
const path = require("path");

const REPO = process.env.REPO || process.cwd();
const { JSDOM } = require("jsdom");

let pass = 0, fail = 0;
const out = [];
function check(label, cond, extra) {
  if (cond) { pass++; out.push(`  OK   ${label}`); }
  else { fail++; out.push(`  FAIL ${label}${extra !== undefined ? "   " + JSON.stringify(extra) : ""}`); }
}

const HTML = `<!doctype html><html><body>
  <div id="alertBox"></div><div id="toast"></div>
  <main id="content"></main>
</body></html>`;

const dom = new JSDOM(HTML, { runScripts: "outside-only", pretendToBeVisual: true });
const { window } = dom;
const doc = window.document;

/* ---------------- 假后端 ---------------- */
let items = [
  { id: "tea", name: "奶茶", cost: 50, stock: 10, sold: 2, left: 8,
    reward: "manual", value: "", description: "", limit_per_user: 0,
    limit_per_day: 0, enabled: true },
];
let prizes = [
  { id: "thanks", name: "谢谢参与", chance: 0.7, stock: null, won: 0,
    rare: false, reward: "points", value: "1", enabled: true },
  { id: "gold", name: "金牌", chance: 0.01, stock: 2, won: 0,
    rare: true, reward: "points", value: "200", enabled: true },
];

const calls = [];

window.AstrBotPluginPage = {
  apiGet: async (endpoint) => {
    calls.push(["GET", endpoint]);
    const ep = String(endpoint).split("?")[0];
    if (ep.endsWith("/shop/items")) return { items: JSON.parse(JSON.stringify(items)) };
    if (ep.endsWith("/shop/prizes")) return { prizes: JSON.parse(JSON.stringify(prizes)) };
    return {};
  },
  apiPost: async (endpoint, body) => {
    calls.push(["POST", endpoint, body]);
    const ep = String(endpoint).split("?")[0];
    if (ep.endsWith("/shop/items")) {
      const bad = body.items.filter((i) => !i.name);
      if (bad.length) throw new Error("第 1 项缺少商品名");
      items = body.items.map((it) => ({ ...it, sold: 0, left: it.stock }));
      return { items, saved: items.length };
    }
    if (ep.endsWith("/shop/prizes")) {
      const total = body.prizes.reduce((s, p) => s + (Number(p.chance) || 0), 0);
      if (total > 1 + 1e-9) {
        throw new Error(`所有奖品的概率加起来是 ${total.toFixed(4)}，超过了 1。`);
      }
      prizes = body.prizes.map((p) => ({ ...p, won: 0 }));
      return { prizes, saved: prizes.length, total_chance: total };
    }
    return {};
  },
};

window.eval(fs.readFileSync(path.join(REPO, "pages/settings/shop.js"), "utf8"));

const settle = () => new Promise((r) => setTimeout(r, 40));
const $ = (s) => doc.querySelector(s);
const click = (n) => n.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));

/** 精确按文案找按钮（避免「＋ 添加商品」被子串误命中）。 */
function btn(root, text) {
  return [...root.querySelectorAll("button")]
    .find((b) => (b.textContent || "").trim() === text) || null;
}

/** 把两个视图分别渲染到各自的容器里，便于同时断言。 */
async function renderBoth() {
  const host = $("#content");
  host.innerHTML = "";
  const a = doc.createElement("div");
  const b = doc.createElement("div");
  host.append(a, b);
  let backA = 0, backB = 0;
  await window.PanshiShop.renderItemsView(a, () => { backA++; });
  await window.PanshiShop.renderPrizesView(b, () => { backB++; });
  return { a, b, getBackA: () => backA, getBackB: () => backB };
}

(async () => {
  $("#alertBox").innerHTML = "";

  /* ---------- 1. 两个视图是分开的 ---------- */
  const v1 = await renderBoth();
  await settle();

  check("商品视图只包含商品行", v1.a.querySelectorAll("tr.shop-row").length === 1,
        v1.a.querySelectorAll("tr.shop-row").length);
  check("抽奖视图只包含奖品行", v1.b.querySelectorAll("tr.shop-row").length === 2,
        v1.b.querySelectorAll("tr.shop-row").length);
  check("两个视图是两个独立容器", v1.a !== v1.b);

  /* ---------- 2. 返回按钮 ---------- */
  check("商品视图有「← 返回」", !!btn(v1.a, "← 返回"));
  check("抽奖视图有「← 返回」", !!btn(v1.b, "← 返回"));
  const backBtn = btn(v1.a, "← 返回");
  if (backBtn) {
    click(backBtn);
    await settle();
    check("点返回会触发回调", v1.getBackA() === 1, v1.getBackA());
  }

  /* ---------- 3. 商品：加号 + 三个框 ---------- */
  const v2 = await renderBoth();
  await settle();
  const addItem = btn(v2.a, "＋ 添加商品");
  check("商品区有「＋ 添加商品」", !!addItem,
        [...v2.a.querySelectorAll("button")].map((b) => b.textContent.trim()));
  check("商品区没有奖品按钮（已分页）", !btn(v2.a, "＋ 添加奖品"));

  if (addItem) {
    const before = v2.a.querySelectorAll("tr.shop-row").length;
    click(addItem);
    await settle();
    const rows = [...v2.a.querySelectorAll("tr.shop-row")];
    check("点加号后商品多一行", rows.length === before + 1,
          { before, after: rows.length });

    const inputs = [...rows[rows.length - 1].querySelectorAll("input")];
    const texts = inputs.filter((i) => i.type === "text" || i.type === "number");
    check("新行是三个框（商品名 / 价格 / 库存）", texts.length === 3,
          inputs.map((i) => i.type));

    texts[0].value = "表情包";
    texts[1].value = "30";
    texts[2].value = "5";
    await settle();

    const save = btn(v2.a, "保存商品");
    check("有「保存商品」", !!save);
    if (save) {
      click(save);
      await settle();
      const post = calls.filter((c) =>
        c[0] === "POST" && String(c[1]).includes("shop/items")).pop();
      check("提交内容含新加的那件（名字/价格/库存都对）",
            !!post && post[2].items.some((i) =>
              i.name === "表情包" && i.cost === 30 && i.stock === 5),
            post ? post[2].items : null);
      check("原有商品被保留（不是只提交新行）",
            !!post && post[2].items.some((i) => i.name === "奶茶"),
            post ? post[2].items.map((i) => i.name) : null);
    }
  }

  /* ---------- 4. 删除行 ---------- */
  const v3 = await renderBoth();
  await settle();
  const del = v3.a.querySelector("tr.shop-row .btn.danger");
  check("商品行有删除按钮", !!del);
  const n0 = v3.a.querySelectorAll("tr.shop-row").length;
  if (del) {
    click(del);
    await settle();
    check("删除后行数减少", v3.a.querySelectorAll("tr.shop-row").length === n0 - 1,
          { before: n0, after: v3.a.querySelectorAll("tr.shop-row").length });
  }

  /* ---------- 5. 概率合计 ---------- */
  const v4 = await renderBoth();
  await settle();
  const total = v4.b.querySelector(".shop-total");
  check("抽奖页显示中奖概率合计",
        !!total && /中奖合计/.test(total.textContent || ""),
        total ? total.textContent : null);
  check("合计数值正确（0.70+0.01=0.71）",
        !!total && /71\.00%/.test(total.textContent || ""),
        total ? total.textContent : null);
  check("商品页不显示概率合计", !v4.a.querySelector(".shop-total"));

  /* ---------- 6. 概率超过 1 被拦下 ---------- */
  const pr = [...v4.b.querySelectorAll("tr.shop-row")];
  const goldChance = pr[pr.length - 1].querySelector('input[type="number"]');
  if (goldChance) {
    goldChance.value = "0.9";           // 0.7 + 0.9 = 1.6
    goldChance.dispatchEvent(new window.Event("input", { bubbles: true }));
    await settle();
    const t2 = v4.b.querySelector(".shop-total");
    check("超限时合计区标红", !!t2 && t2.classList.contains("over"),
          t2 ? t2.className : null);
    check("超限时给出提示", !!t2 && /超过 1/.test(t2.textContent || ""),
          t2 ? t2.textContent : null);

    const save = btn(v4.b, "保存奖池");
    if (save) {
      click(save);
      await settle();
      check("保存被拒绝并展示原因",
            /超过/.test($("#alertBox").textContent || ""),
            $("#alertBox").textContent);
    }
  }

  /* ---------- 7. 抽奖页加奖品 ---------- */
  const v5 = await renderBoth();
  await settle();
  const addPrize = btn(v5.b, "＋ 添加奖品");
  check("抽奖区有「＋ 添加奖品」", !!addPrize);
  check("抽奖区没有商品按钮（已分页）", !btn(v5.b, "＋ 添加商品"));
  if (addPrize) {
    const n1 = v5.b.querySelectorAll("tr.shop-row").length;
    click(addPrize);
    await settle();
    check("点加号后奖池多一行",
          v5.b.querySelectorAll("tr.shop-row").length === n1 + 1,
          { before: n1, after: v5.b.querySelectorAll("tr.shop-row").length });
    const row = [...v5.b.querySelectorAll("tr.shop-row")].pop();
    check("奖品行有概率与可中次数两个数字框",
          row.querySelectorAll('input[type="number"]').length === 2);
  }

  /* ---------- 8. 路由失败时给出可操作的提示 ---------- */
  //
  // 线上遇到过「未找到该路由」：页面标题渲染出来了（HTML 是静态的），
  // 但 apiGet 拿不到数据。只把原始错误抛出来，用户看不出该做什么。
  const savedGet = window.AstrBotPluginPage.apiGet;
  window.AstrBotPluginPage.apiGet = async () => {
    throw new Error("未找到该路由");
  };
  const host6 = $("#content");
  host6.innerHTML = "";
  const solo = doc.createElement("div");
  host6.append(solo);
  await window.PanshiShop.renderItemsView(solo, () => {});
  await settle();
  const alertText = ($("#alertBox").textContent || "") + (solo.textContent || "");
  check("路由失败时提示里指明要重载插件",
        /重载/.test(alertText), alertText.slice(0, 120));
  window.AstrBotPluginPage.apiGet = savedGet;

  console.log(out.join("\n"));
  console.log(`\n结果: ${pass} 通过, ${fail} 失败`);
  process.exit(fail ? 1 : 0);
})();
