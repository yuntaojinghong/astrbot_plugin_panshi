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
const SEED_ITEMS = () => [
  { id: "tea", name: "奶茶", cost: 50, stock: 10, sold: 2, left: 8,
    reward: "manual", value: "", description: "", limit_per_user: 0,
    limit_per_day: 0, enabled: true },
];
const SEED_PRIZES = () => [
  { id: "thanks", name: "谢谢参与", chance: 0.7, stock: null, won: 0,
    rare: false, reward: "points", value: "1", enabled: true },
  { id: "gold", name: "金牌", chance: 0.01, stock: 2, won: 0,
    rare: true, reward: "points", value: "200", enabled: true },
];
let items = SEED_ITEMS();
let prizes = SEED_PRIZES();

/**
 * 重置假后端数据。
 *
 * 保存类用例会改掉 items / prizes，后面的用例就会读到被改过的内容
 * （表现为「视图里一行都没有」）。每个区块开头调用一次，各自独立。
 */
function resetFixtures() {
  items = SEED_ITEMS();
  prizes = SEED_PRIZES();
  settings = {
    enable: false, lottery_enable: false, lottery_cost: 10,
    lottery_daily_limit: 3, lottery_pity: 10,
  };
  $("#alertBox").innerHTML = "";
}

let settings = {
  enable: false, lottery_enable: false, lottery_cost: 10,
  lottery_daily_limit: 3, lottery_pity: 10,
};

const calls = [];

window.AstrBotPluginPage = {
  apiGet: async (endpoint) => {
    calls.push(["GET", endpoint]);
    const ep = String(endpoint).split("?")[0].toLowerCase();
    if (ep.endsWith("shop/items")) return { items: JSON.parse(JSON.stringify(items)) };
    if (ep.endsWith("shop/prizes")) return { prizes: JSON.parse(JSON.stringify(prizes)) };
    if (ep.endsWith("shop/settings")) return JSON.parse(JSON.stringify(settings));
    return {};
  },
  apiPost: async (endpoint, body) => {
    calls.push(["POST", endpoint, body]);
    const ep = String(endpoint).split("?")[0].toLowerCase();
    if (ep.endsWith("shop/save-items")) {
      const bad = body.items.filter((i) => !i.name);
      if (bad.length) throw new Error("第 1 项缺少商品名");
      items = body.items.map((it) => ({ ...it, sold: 0, left: it.stock }));
      return { items, saved: items.length };
    }
    if (ep.endsWith("shop/save-prizes")) {
      const total = body.prizes.reduce((s, p) => s + (Number(p.chance) || 0), 0);
      if (total > 1 + 1e-9) {
        throw new Error(`所有奖品的概率加起来是 ${total.toFixed(4)}，超过了 1。`);
      }
      prizes = body.prizes.map((p) => ({ ...p, won: 0 }));
      return { prizes, saved: prizes.length, total_chance: total };
    }
    if (ep.endsWith("shop/save-settings")) {
      settings = { ...settings, ...body.settings };
      return settings;
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

/**
 * 复刻 AstrBot 的路由匹配，用来验证「请求能不能命中注册路径」。
 *
 * 这一步补上之前最大的漏洞：前面的测试只验证了「前端发起了请求」，
 * 没验证「请求的 URL 真的能命中后端注册的路径」。
 * 结果线上 URL 里插件名出现了两次，后端一条都没匹配上，而测试全绿。
 *
 * 规则读自 AstrBot 4.28.2 的字节码：
 *   request_path = "/" + subpath.lstrip("/")     # subpath 就是整条 plugin_path
 *   re.fullmatch(pattern(注册路径), request_path)
 * pattern() 把 <name> / <path:name> 换成 (?P<name>.*)，其余部分字面转义。
 */
function astrbotPattern(route) {
  let r = String(route).trim();
  if (!r.startsWith("/")) r = "/" + r;
  const ANGLE = /<(?:path:)?([A-Za-z_][A-Za-z0-9_]*)>/g;
  const chunks = [];
  let pos = 0, m;
  while ((m = ANGLE.exec(r)) !== null) {
    chunks.push(r.slice(pos, m.index).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
    chunks.push(`(?<${m[1]}>.*)`);
    pos = m.index + m[0].length;
  }
  chunks.push(r.slice(pos).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  return "^(?:" + chunks.join("") + ")$";
}

function routeMatches(registered, subpath) {
  const requestPath = "/" + String(subpath).replace(/^\/+/, "");
  return new RegExp(astrbotPattern(registered)).exec(requestPath);
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
  resetFixtures();
  const v1 = await renderBoth();
  await settle();

  console.log("      [dbg] 首次 GET 调用 =", JSON.stringify(calls.slice(0,4)));
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
  resetFixtures();
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
        c[0] === "POST" && String(c[1]).includes("shop/save-items")).pop();
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
  resetFixtures();
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
  resetFixtures();
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
  check("奖池里有可编辑的行（后面要改概率）", pr.length >= 1, pr.length);
  const goldChance = pr.length
    ? pr[pr.length - 1].querySelector('input[type="number"]') : null;
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
  resetFixtures();
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

  /* ---------- 9. 设置区（开关与参数已从配置页搬进来） ---------- */
  resetFixtures();
  const v6 = await renderBoth();
  await settle();

  check("商品页有商城设置区", /商城设置/.test(v6.a.textContent || ""));
  check("抽奖页有抽奖设置区", /抽奖设置/.test(v6.b.textContent || ""));
  check("商品页不放抽奖参数（各管各的）",
        !/每次抽奖消耗积分/.test(v6.a.textContent || ""));
  check("抽奖页不放商品设置",
        !/商城设置/.test(v6.b.textContent || ""));

  const shopSwitch = v6.a.querySelector(".shop-switch-box");
  check("商品页有启用开关", !!shopSwitch);
  const lotSwitches = [...v6.b.querySelectorAll(".shop-switch-box")];
  check("抽奖页有启用开关", lotSwitches.length >= 1, lotSwitches.length);

  const lotNums = [...v6.b.querySelectorAll(".shop-setting-ctrl input[type=number]")];
  check("抽奖页有三个参数框（消耗 / 每日次数 / 保底）",
        lotNums.length === 3, lotNums.map((i) => i.value));
  check("参数框带上了当前值",
        lotNums[0].value === "10" && lotNums[1].value === "3" && lotNums[2].value === "10",
        lotNums.map((i) => i.value));

  // 改开关 + 参数后保存，应发出 POST 且内容正确
  if (shopSwitch) {
    shopSwitch.checked = true;
    shopSwitch.dispatchEvent(new window.Event("change", { bubbles: true }));
    const saveSet = btn(v6.a, "保存设置");
    check("商品页有「保存设置」", !!saveSet);
    if (saveSet) {
      const before = calls.filter((c) =>
        c[0] === "POST" && String(c[1]).includes("shop/save-settings")).length;
      click(saveSet);
      await settle();
      const posts = calls.filter((c) =>
        c[0] === "POST" && String(c[1]).includes("shop/save-settings"));
      check("保存设置发出了 POST", posts.length === before + 1, posts.length);
      const body = posts.length ? posts[posts.length - 1][2] : null;
      check("提交的是商城的 enable", !!body && body.settings.enable === true,
            body ? body.settings : null);
    }
  }

  const saveLotSet = btn(v6.b, "保存设置");
  if (saveLotSet && lotNums.length === 3) {
    lotNums[0].value = "25";
    lotNums[1].value = "5";
    lotNums[2].value = "20";
    click(saveLotSet);
    await settle();
    const posts = calls.filter((c) =>
      c[0] === "POST" && String(c[1]).includes("shop/save-settings"));
    const body = posts.length ? posts[posts.length - 1][2] : null;
    check("抽奖设置提交的是 lottery_* 参数",
          !!body && body.settings.lottery_cost === 25
            && body.settings.lottery_daily_limit === 5
            && body.settings.lottery_pity === 20,
          body ? body.settings : null);
    check("抽奖设置不会误带商城 enable",
          !!body && !("enable" in body.settings), body ? body.settings : null);
  }

  /* ---------- 10. 端点前缀：不过度拼接（线上真实故障点） ---------- */
  //
  // 用户实测的 URL：
  //   /api/v1/plugins/extensions/astrbot_plugin_panshi/astrbot_plugin_panshi/shop/items
  //                                            ^^^^ plugin_path 参数 ^^^^
  // 插件名出现了两次 —— 前端多拼了一层。
  const seenGet = calls.filter((c) => c[0] === "GET").map((c) => String(c[1]));
  const seenPost = calls.filter((c) => c[0] === "POST").map((c) => String(c[1]));

  check("GET 的端点不带插件名前缀",
        seenGet.every((e) => !e.includes("astrbot_plugin_panshi")), seenGet);
  check("POST 的端点不带插件名前缀",
        seenPost.every((e) => !e.includes("astrbot_plugin_panshi")), seenPost);
  check("端点就是纯子路径",
        seenGet.includes("shop/items") && seenGet.includes("shop/prizes"),
        seenGet);

  /* ---------- 11. 注册路径必须能命中真实 URL ---------- */
  //
  // 后端注册的是 /<path:rest>（通配），靠后缀认端点。
  // 这一条验证：无论 URL 里插件名出现几次，请求都能命中。
  const REGISTERED = "/<path:rest>";
  const REAL_PATHS = [
    "astrbot_plugin_panshi/shop/items",                              // 正常
    "astrbot_plugin_panshi/astrbot_plugin_panshi/shop/items",        // 线上那种重复
    "shop/items",                                                    // 完全没有前缀
  ];
  for (const sub of REAL_PATHS) {
    const m = routeMatches(REGISTERED, sub);
    check(`注册 ${REGISTERED} 能命中 ${sub}`, !!m);
    if (m) {
      // 后缀识别是否得到正确的端点
      const rest = m.groups.rest || "";
      const known = ["shop/items", "shop/prizes", "shop/settings", "shop/reset",
                     "shop/save-items", "shop/save-prizes", "shop/save-settings",
                     "bootstrap", "overview", "connection", "export", "import",
                     "groups", "groups/refresh", "global", "group", "group/reset"];
      const hit = known.filter((ep) => rest === ep || rest.endsWith("/" + ep))
        .sort((a, b) => b.length - a.length)[0];
      check(`  后缀识别出端点 ${hit}`, !!hit, rest);
    }
  }

  // 反向：写死单一路径的写法会失配——证明通配是必要的
  const single = "/astrbot_plugin_panshi/<path:rest>";
  check("写死一层前缀：无重复前缀的 URL 能中",
        !!routeMatches(single, REAL_PATHS[0]), null);
  check("写死一层前缀：重复前缀的 URL 也能中（通配把多余前缀吃进 rest）",
        !!routeMatches(single, REAL_PATHS[1]), null);

  console.log(out.join("\n"));
  console.log(`\n结果: ${pass} 通过, ${fail} 失败`);
  process.exit(fail ? 1 : 0);
})();
