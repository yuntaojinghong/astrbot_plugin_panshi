/* 磐石面板前端：群头像 / 品牌图标 / 切换加载态 / 自绘确认框
 *
 * 用 jsdom 直接渲染 renderGroups()，断言：
 *   1. 群头像用真实图片地址，且带 onerror 回退
 *   2. 全局默认项仍用「默」字图标（没有群号可接头像）
 *   3. 品牌图标逐个试候选路径，全失败则保留「磐」字
 *   4. 切换群时有加载态（列表加 busy、内容区转圈）
 *   5. 确认框是页面内自绘的，不调用原生 confirm()
 */
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const REPO = process.env.REPO || path.resolve(__dirname, "..");
const HTML = fs.readFileSync(path.join(REPO, "pages/settings/index.html"), "utf-8");
const APP = fs.readFileSync(path.join(REPO, "pages/settings/app.js"), "utf-8");

const failures = [];
function assert(label, cond, detail) {
  console.log(`  ${cond ? "OK  " : "FAIL"} ${label}${cond ? "" : "   " + (detail ?? "")}`);
  if (!cond) failures.push(label);
}

const dom = new JSDOM(HTML, {
  runScripts: "outside-only",
  url: "https://example.invalid/plugin-page/astrbot_plugin_panshi/settings",
  pretendToBeVisual: true,
});
const { window } = dom;

// jsdom 没实现 matchMedia，而面板的粒子背景会读它。桩掉即可 —— 顺便让
// 「减少动效」为真，跳过 canvas 绘制，测试更快也不需要真实 canvas。
window.matchMedia = () => ({
  matches: true,
  addEventListener() {},
  removeEventListener() {},
  addListener() {},
  removeListener() {},
});

// ---- 计数：原生 confirm 绝不能被调用 ----
let nativeConfirmCalls = 0;
window.confirm = () => {
  nativeConfirmCalls += 1;
  return true;
};

// ---- 记录加载过的图片地址（jsdom 不会真的下载，onload 需要我们手动触发）----
const imageLoads = [];
const NativeImage = window.Image;
window.Image = function PatchedImage() {
  const img = new NativeImage();
  const origSrc = Object.getOwnPropertyDescriptor(
    window.HTMLImageElement.prototype, "src"
  );
  Object.defineProperty(img, "src", {
    configurable: true,
    get() {
      return origSrc.get.call(img);
    },
    set(v) {
      imageLoads.push(String(v));
      origSrc.set.call(img, String(v));
    },
  });
  return img;
};

const resp = {
  bootstrap: {
    schema: [],
    groups: [
      { group_id: "123456", group_name: "示例群", member_count: 128, enabled: true,
        has_override: false, bot_role: "admin" },
      { group_id: "654321", group_name: "普通群", member_count: 20, enabled: true,
        has_override: false, bot_role: "member" },
    ],
    global: { config: { basic: {} } },
    meta: { default_group_id: "__default__", plugin_name: "astrbot_plugin_panshi" },
  },
  overview: null,
};

window.AstrBotPluginPage = {
  async ready() { return { isDark: true }; },
  onContext() {},
  async apiGet(endpoint) {
    return resp[endpoint];
  },
  async apiPost() {
    return {};
  },
};

(async () => {
  // 注意：app.js 用 `const state = ...` 声明，const/let 不会挂到 window 上，
  // 所以测试代码必须**和 app.js 拼在同一次 eval 里**执行，才能访问它。
  // 需要跨 eval 使用的函数挂到 window.__t 上。
  window.eval(
    APP +
      `
    window.__t = { state, setSelectionLoading, askConfirm, renderGroups };
  `
  );
  await new Promise((r) => setTimeout(r, 400));

  console.log("断言：");

  // ---- 1/2. 群头像 ----
  const list = window.document.querySelector("#groupList");
  assert("渲染出了群列表", !!list && list.children.length >= 1,
    list ? `children=${list.children.length}` : "(无列表)");

  // 只看「群」条目（跳过第一条全局默认项），避免选到默认项的图标
  const groupOnly = [...(list ? list.children : [])].filter(
    (li) => !li.querySelector(".avatar.default")
  );
  const img = groupOnly.length ? groupOnly[0].querySelector("img.avatar-img") : null;
  assert("群条目使用真实头像图片", !!img, img ? "ok" : "没找到 img.avatar-img");
  if (img) {
    assert("头像地址指向腾讯群头像接口",
      /p\.qlogo\.cn\/gh\/123456\/123456/.test(img.getAttribute("src")),
      img.getAttribute("src"));
    assert("头像带 lazy 加载", img.getAttribute("loading") === "lazy");
  }

  const defaultItem = list && list.querySelector(".group-item .avatar.default");
  assert("全局默认项仍用「默」字图标", !!defaultItem && defaultItem.textContent === "默",
    defaultItem ? defaultItem.textContent : "(无)");

  // 触发 onerror，应回退成首字图标
  if (img) {
    img.dispatchEvent(new window.Event("error"));
    const fallback = groupOnly[0].querySelector(".avatar:not(.avatar-img)");
    assert("头像加载失败时回退成首字图标",
      !!fallback && fallback.textContent === "示",
      fallback ? fallback.textContent : "(无回退)");
  }

  // ---- 2.5 机器人在群里的身份徽章 ----
  //
  // 回归背景：机器人不是管理员时，插件照样能"看"（检测/记警告/扣分），
  // 但所有"动手"的操作都会失败。面板以前完全不显示身份，用户只能靠
  // "点了没反应"去猜。所以这几条要一直守着。
  const lis = [...(list ? list.children : [])];
  const adminLi = lis.find((li) => li.textContent.includes("示例群"));
  const memberLi = lis.find((li) => li.textContent.includes("普通群"));
  const adminTag = adminLi ? adminLi.querySelector(".tag-role-admin") : null;
  const memberTag = memberLi ? memberLi.querySelector(".tag-role-member") : null;
  assert("管理员群显示「管理员」徽章",
    !!adminTag && adminTag.textContent === "管理员",
    adminLi ? adminLi.textContent : "(没找到这个群)");
  assert("普通成员群显示「普通成员」徽章",
    !!memberTag && memberTag.textContent === "普通成员",
    memberLi ? memberLi.textContent : "(没找到这个群)");
  assert("普通成员徽章带「会失败」的说明（鼠标悬停可见）",
    !!memberTag && /会失败/.test(memberTag.getAttribute("title") || ""),
    memberTag ? memberTag.getAttribute("title") : "(无 title)");

  // ---- 3. 品牌图标 ----
  //
  // 改成内嵌 svg 之后不再发网络请求。
  // 线上用 ./logo.png 会 401：AstrBot 的插件页面服务只给 HTML 里静态写死的
  // 资源地址补 asset_token，JS 动态换 src 拿不到那个 token。
  // 所以这里断言「没有为图标发请求」，并且品牌位确实换成了内嵌图。
  const logoRequests = imageLoads.filter((u) => /logo\.(png|svg)/.test(u));
  assert("品牌图标不再发网络请求（内嵌 svg，避开 401）",
    logoRequests.length === 0, JSON.stringify(imageLoads));

  const brandImg = window.document.querySelector("#brandLogo, .logo-img");
  assert("品牌位已换成内嵌图",
    !!brandImg, brandImg ? brandImg.tagName : "(无)");
  assert("内嵌图用的是 data URI",
    !!brandImg && String(brandImg.getAttribute("src") || "").startsWith("data:image/svg+xml"),
    brandImg ? String(brandImg.getAttribute("src") || "").slice(0, 40) : "(无)");

  // 脚本没跑起来时要能看到「磐」字兜底
  const brandSpan = window.document.querySelector("span#brandLogo");
  assert("兜底仍保留「磐」字（脚本未跑时不空白）",
    !brandSpan || brandSpan.textContent === "磐",
    brandSpan ? brandSpan.textContent : "(已替换为图)")

  // ---- 4. 切换加载态 ----
  window.__t.setSelectionLoading(true);
  assert("加载态给列表加了 busy（禁用点击）",
    window.document.querySelector("#groupList").classList.contains("busy"));
  assert("加载态显示转圈", !!window.document.querySelector(".spinner"));
  window.__t.setSelectionLoading(false);

  // ---- 5. 自绘确认框 ----
  const askedPromise = window.__t.askConfirm({
    title: "恢复默认配置", message: "确定吗", confirmText: "恢复默认", danger: true,
  });
  await new Promise((r) => setTimeout(r, 30));
  const overlay = window.document.querySelector(".modal-overlay");
  assert("确认框是页面内自绘的（.modal-overlay）", !!overlay);
  assert("确认框标题正确",
    !!window.document.querySelector(".modal-title") &&
    window.document.querySelector(".modal-title").textContent === "恢复默认配置");
  assert("没有调用原生 confirm", nativeConfirmCalls === 0, `calls=${nativeConfirmCalls}`);

  // 点「取消」应返回 false 且移除遮罩
  const cancelBtn = [...window.document.querySelectorAll(".modal-actions .btn")]
    .find((b) => b.textContent === "取消");
  assert("确认框有取消按钮", !!cancelBtn);
  if (cancelBtn) {
    cancelBtn.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    const result = await askedPromise;
    assert("点取消返回 false", result === false, String(result));
    await new Promise((r) => setTimeout(r, 20));
    assert("取消后遮罩已移除", !window.document.querySelector(".modal-overlay"));
  }

  console.log("\n结论:", failures.length ? `失败 ${failures.length} 项` : "面板前端改动均生效");
  process.exit(failures.length ? 1 : 0);
})();
