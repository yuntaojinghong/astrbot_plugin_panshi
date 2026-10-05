/* 用线上日志里的确切数据跑一遍渲染，看界面为什么是空的。
 *
 * 日志（v1.9.9，用户实测）：
 *   已保存商城设置 {'enable': True} -> .../panshi_data.json
 *   读取商城配置：... items=1 prizes=0 settings={'enable': True, ...}
 *
 * 但界面显示「还没有商品」、开关是关的。
 * 这个脚本把同样的数据喂给渲染函数，看问题出在哪一层。
 */

const fs = require("fs");
const path = require("path");

const REPO = process.env.REPO || process.cwd();
const { JSDOM } = require("jsdom");

const HTML = `<!doctype html><html><body>
  <div id="alertBox"></div><div id="toast"></div>
  <main id="content"></main>
</body></html>`;

const dom = new JSDOM(HTML, { runScripts: "outside-only", pretendToBeVisual: true });
const { window } = dom;
const doc = window.document;

// 后端日志里说的实际内容
const ITEMS = [{
  id: "1", name: "1", cost: 50, stock: 1, description: "",
  limit_per_user: 0, limit_per_day: 0, reward: "manual", value: "",
  enabled: true, sold: 0, left: 1,
}];
const SETTINGS = {
  enable: true, lottery_enable: true, lottery_cost: 10,
  lottery_daily_limit: 3, lottery_pity: 10,
};

const calls = [];
window.AstrBotPluginPage = {
  apiGet: async (endpoint) => {
    calls.push(["GET", endpoint]);
    const ep = String(endpoint).split("?")[0].toLowerCase();
    if (ep.endsWith("shop/items")) return { items: JSON.parse(JSON.stringify(ITEMS)) };
    if (ep.endsWith("shop/settings")) return JSON.parse(JSON.stringify(SETTINGS));
    if (ep.endsWith("shop/prizes")) return { prizes: [] };
    return {};
  },
  apiPost: async (endpoint, body) => {
    calls.push(["POST", endpoint, body]);
    return { ok: true };
  },
};

window.eval(fs.readFileSync(path.join(REPO, "pages/settings/shop.js"), "utf8"));

const settle = () => new Promise((r) => setTimeout(r, 50));
const $ = (s) => doc.querySelector(s);

(async () => {
  const host = doc.createElement("div");
  $("#content").append(host);

  await window.PanshiShop.renderItemsView(host, () => {});
  await settle();

  console.log("  GET 调用:", JSON.stringify(calls.filter((c) => c[0] === "GET")));
  console.log();

  const rows = host.querySelectorAll("tr.shop-row");
  console.log("  商品行数        :", rows.length, rows.length ? "(有)" : "(空 ← 问题在这)");
  const empty = host.querySelector(".shop-empty-row");
  console.log("  是否显示空提示  :", !!empty);

  const sw = host.querySelector(".shop-switch-box");
  console.log("  开关 checked    :", sw ? sw.checked : "(没有开关)");

  const texts = [...host.querySelectorAll('input[type="text"]')].map((i) => i.value);
  console.log("  文本框里的值    :", JSON.stringify(texts));
  const nums = [...host.querySelectorAll('input[type="number"]')].map((i) => i.value);
  console.log("  数字框里的值    :", JSON.stringify(nums));

  console.log();
  console.log("  textContent 片段:",
    JSON.stringify((host.textContent || "").slice(0, 120)));

  if (!rows.length) {
    console.log();
    console.log("  === 排查：数据到渲染之间哪一步丢了 ===");
    const res = await window.AstrBotPluginPage.apiGet("shop/items");
    console.log("  apiGet('shop/items') 直接返回:", JSON.stringify(res));
    console.log("  res.items 是数组吗:", Array.isArray(res.items));
  }
})();
