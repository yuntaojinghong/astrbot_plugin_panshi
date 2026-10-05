/* 商城管理界面（jsdom）
 *
 * 验证用户要的那件事：点「＋」填几个框就能加一条，不用写 JSON。
 * 同时验证概率总和 > 1 会被拦下——这是用户明确要求的约束。
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
const $$ = (s) => [...doc.querySelectorAll(s)];
const click = (n) => n.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));

/**
 * 按文案在某个区块里找按钮。
 *
 * 必须限定区块：商品和奖池各有一个「＋ 添加…」按钮，
 * 只按文案全局找会命中错的（我第一版就点到了奖品的添加按钮，
 * 结果把奖品字段填进了商品表，排查了好一会儿）。
 */
function btnIn(cardIndex, text) {
  const card = $$(".shop-card")[cardIndex];
  if (!card) return null;
  return [...card.querySelectorAll("button")]
    .find((b) => (b.textContent || "").trim() === text) || null;
}

/** 某个区块里的所有数据行。 */
function rowsIn(cardIndex) {
  const card = $$(".shop-card")[cardIndex];
  return card ? [...card.querySelectorAll("tr.shop-row")] : [];
}

(async () => {
  const host = $("#content");
  await window.PanshiShop.renderShopView(host);
  await settle();

  check("渲染出两个区块（商品 / 奖池）", $$(".shop-card").length === 2,
        $$(".shop-card").length);
  check("列出了已有商品", rowsIn(0).some((r) =>
    (r.querySelector('input[type="text"]') || {}).value === "奶茶"));
  check("列出了已有奖品", rowsIn(1).some((r) =>
    (r.querySelector('input[type="text"]') || {}).value === "金牌"));

  // ---------- 加一件商品：只需三个框 ----------
  const addItemBtn = btnIn(0, "＋ 添加商品");
  check("商品区有「＋ 添加商品」按钮", !!addItemBtn,
        $$("button").map((b) => b.textContent.trim()));

  const before = rowsIn(0).length;
  if (addItemBtn) {
    click(addItemBtn);
    await settle();
    const rows = rowsIn(0);
    check("点加号后商品多出一行", rows.length === before + 1,
          { before, after: rows.length });

    const newRow = rows[rows.length - 1];
    const inputs = [...newRow.querySelectorAll("input")];
    const texts = inputs.filter((i) => i.type === "text" || i.type === "number");
    check("新行是三个框（商品名 / 价格 / 库存）", texts.length === 3,
          inputs.map((i) => i.type));

    texts[0].value = "表情包";
    texts[1].value = "30";
    texts[2].value = "5";
    await settle();

    const saveBtn = btnIn(0, "保存商品");
    check("有「保存商品」按钮", !!saveBtn);
    if (saveBtn) {
      click(saveBtn);
      await settle();
      const post = calls.filter((c) =>
        c[0] === "POST" && String(c[1]).includes("shop/items")).pop();
      check("保存时发出了 POST", !!post);
      check("提交内容含新加的那件（名字/价格/库存都对）",
            !!post && post[2].items.some((i) =>
              i.name === "表情包" && i.cost === 30 && i.stock === 5),
            post ? post[2].items : null);
      check("原有商品被保留（不是只提交新行）",
            !!post && post[2].items.some((i) => i.name === "奶茶"),
            post ? post[2].items.map((i) => i.name) : null);
    }
  }

  // ---------- 删除一行 ----------
  await window.PanshiShop.renderShopView(host);
  await settle();
  const delBtns = rowsIn(0).map((r) => r.querySelector(".btn.danger")).filter(Boolean);
  check("商品每行都有删除按钮", delBtns.length >= 1, delBtns.length);
  const n0 = rowsIn(0).length;
  if (delBtns.length) {
    click(delBtns[delBtns.length - 1]);
    await settle();
    check("删除后行数减少", rowsIn(0).length === n0 - 1,
          { before: n0, after: rowsIn(0).length });
  }

  // ---------- 概率合计 ----------
  await window.PanshiShop.renderShopView(host);
  await settle();
  let total = $(".shop-total");
  check("显示中奖概率合计", !!total && /中奖合计/.test(total.textContent || ""),
        total ? total.textContent : null);
  check("合计数值正确（0.70+0.01=0.71）",
        !!total && /71\.00%/.test(total.textContent || ""),
        total ? total.textContent : null);
  check("同时显示未中奖概率",
        !!total && /未中奖/.test(total.textContent || ""), null);

  // ---------- 概率超过 1 被拦下 ----------
  const pr = rowsIn(1);
  const goldRow = pr[pr.length - 1];
  const goldChance = goldRow && goldRow.querySelector('input[type="number"]');
  if (goldChance) {
    goldChance.value = "0.9";           // 0.7 + 0.9 = 1.6 > 1
    goldChance.dispatchEvent(new window.Event("input", { bubbles: true }));
    await settle();

    total = $(".shop-total");
    check("超限时合计区标红", !!total && total.classList.contains("over"),
          total ? total.className : null);
    check("超限时给出提示", !!total && /超过 1/.test(total.textContent || ""),
          total ? total.textContent : null);

    const savePrizes = btnIn(1, "保存奖池");
    if (savePrizes) {
      click(savePrizes);
      await settle();
      const box = $("#alertBox");
      check("保存被拒绝并展示原因",
            !!box && /超过/.test(box.textContent || ""),
            box ? box.textContent : null);
    }
  }

  // ---------- 加奖品：名字 / 概率 / 次数 ----------
  await window.PanshiShop.renderShopView(host);
  await settle();
  const addPrizeBtn = btnIn(1, "＋ 添加奖品");
  check("奖池区有「＋ 添加奖品」按钮", !!addPrizeBtn);
  if (addPrizeBtn) {
    const n1 = rowsIn(1).length;
    click(addPrizeBtn);
    await settle();
    check("点加号后奖池多出一行", rowsIn(1).length === n1 + 1,
          { before: n1, after: rowsIn(1).length });
    const row = rowsIn(1)[rowsIn(1).length - 1];
    const nums = [...row.querySelectorAll('input[type="number"]')];
    check("奖品行有概率与可中次数两个数字框", nums.length === 2, nums.length);
  }

  console.log(out.join("\n"));
  console.log(`\n结果: ${pass} 通过, ${fail} 失败`);
  process.exit(fail ? 1 : 0);
})();
