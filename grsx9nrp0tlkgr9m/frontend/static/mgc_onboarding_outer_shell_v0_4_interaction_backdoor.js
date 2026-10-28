/*!
 * MGC Onboarding Outer Shell v0.4.0
 * Purpose: Pure external onboarding shell for the MGC research UI.
 * Safety contract:
 * - Does NOT overwrite or wrap existing app functions.
 * - Does NOT change existing input values, button onclick handlers, API calls, state variables, or report-generation logic.
 * - Does NOT append helper cards inside existing business panels.
 * - Only appends one Shadow-DOM host to document.body and reads target positions for highlight overlays.
 * - Visual guidance can be fully removed with window.MGCOnboardingOuterShell.destroy(); the UI close button now calls destroy() by default.
 */
(function () {
  "use strict";

  const NAMESPACE = "MGCOnboardingOuterShell";
  const HOST_ID = "mgc-onboarding-outer-shell-host";
  const VERSION = "0.4.0";

  if (window[NAMESPACE] && typeof window[NAMESPACE].destroy === "function") {
    window[NAMESPACE].destroy();
  }

  const steps = [
    {
      n: 1,
      selector: "#reportModeSection",
      title: "① 选择报告模式",
      text: "非创新药公司使用「通用深研模式」。创新药公司才切换到「创新药模式」。普通股票研究默认不要改。"
    },
    {
      n: 2,
      selector: 'label[for="stock_name"]',
      title: "② 输入研究对象",
      text: "输入公司名称和证券代码。名称与代码必须对应，否则后面的数据可能跑错对象。"
    },
    {
      n: 3,
      selector: 'label[for="comein_industry_text"]',
      title: "③ 填行业或主题",
      text: "用豆包、通义、ChatGPT 或其他 AI 先查公司所在行业，再填到这里。例如：动力电池与储能电池、化工新材料、CXO、保险。"
    },
    {
      n: 4,
      selector: 'label[for="comein_focus"]',
      title: "④ 填研究关注点",
      text: "写清楚希望系统重点看什么。建议包含：主营业务、技术壁垒、全球竞争格局、盈利能力、估值与主要风险。"
    },
    {
      n: 5,
      selector: 'label[for="comein_report_type"]',
      title: "⑤ 检查默认参数",
      text: "一般不用改。报告期默认 Q2 中报；报告角色默认买方；业绩风格默认精简版。只有明确知道要改时再改。"
    },
    {
      n: 6,
      selector: 'button[onclick="runAllComeInCoreJobs()"]',
      title: "⑥ 一键运行 9 张进门能力卡",
      text: "点击后系统会跑 9 个有效进门能力。原 12 个里面，搜图表第一阶段、走势复盘第一阶段、走势复盘第二阶段已不需要。"
    },
    {
      n: 7,
      selector: "#comeinLivePanel",
      title: "⑦ 等待进门卡片全部成功",
      text: "通常等待 10–15 分钟。出现失败时，往下找到失败的那张卡，单独点击卡片里的「运行」重跑。全部成功后再继续。"
    },
    {
      n: 8,
      selector: 'button[onclick="assembleComeInPack()"]',
      title: "⑧ 组装并检查进门数据包",
      text: "9 张进门卡全部成功后，点击「组装进门数据包」。再到右侧「进门数据底稿」检查公司名称、证券代码、行业是否一致。"
    },
    {
      n: 9,
      selector: "#generateReportButton",
      title: "⑨ 生成最终研报",
      text: "确认进门数据包已经组装并检查通过后，再点击生成最终研报。点击后不要刷新页面。"
    },
    {
      n: 10,
      selector: "#manusTaskMonitor",
      title: "⑩ 查看 Manus 任务监控",
      text: "在这里看生成进度。时间线持续更新说明任务还在运行。看到成功提示或最终附件后，再到右侧「最终研报」查看。"
    },
    {
      n: 11,
      selector: "#tabBtn_final",
      title: "⑪ 查看最终研报",
      text: "点击「最终研报」。先看滚动条长度和章节完整性；明显太短、缺章节或对象不一致时，不要直接交付。"
    },
    {
      n: 12,
      selector: "#tabBtn_interaction",
      title: "⑫ 打开交互聊天记录",
      text: "报告生成完成后，「＋」会解锁为交互聊天入口。点击进入「交互聊天记录」，基于同一个 Manus 任务继续追问。"
    },
    {
      n: 13,
      selector: 'button[onclick="activateEmbeddedProxyReport()"]',
      title: "⑬ 进入交互后门 Demo",
      text: "如果只是演示报告生成后的交互位置，点击「载入交互后门（附宁德时代）」。它会用代理报告解锁交互聊天记录，不会创建新的 Manus 任务，也不会消耗额度。"
    },
    {
      n: 14,
      selector: 'label[for="interactionInput"]',
      title: "⑭ 输入追问提示词 / 斜杠指令",
      text: "在这里输入对最终研报的追问、反驳、补充分析，或以精确斜杠命令开头触发 Manus 技能。消息会发送到同一个任务编号，不会创建新任务。"
    },
    {
      n: 15,
      selector: "#interactionSendButton",
      title: "⑮ 发送消息并查看交互附件",
      text: "点击「发送消息」后等待回复。下方 Task artifacts 会显示交互中识别到的文件，可下载代理报告或后续产物。记得保存会话编号和任务编号方便恢复。"
    }
  ];

  const css = `
    :host {
      all: initial;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      color: #111827;
    }

    .shell-topbar {
      position: fixed;
      left: 24px;
      right: 24px;
      top: 18px;
      z-index: 2147483000;
      background: #ffffff;
      border: 2px solid #111827;
      border-radius: 18px;
      box-shadow: 0 22px 60px rgba(15, 23, 42, 0.22);
      padding: 14px 16px;
      box-sizing: border-box;
    }

    .shell-topbar.is-collapsed {
      display: none;
    }

    .shell-main {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 14px;
      flex-wrap: wrap;
    }

    .shell-title {
      font-size: 18px;
      font-weight: 950;
      line-height: 1.4;
      color: #111827;
    }

    .shell-text {
      margin-top: 5px;
      font-size: 13px;
      line-height: 1.55;
      color: #4b5563;
      max-width: 980px;
    }

    .shell-actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }

    button {
      appearance: none;
      border: 0;
      border-radius: 12px;
      padding: 10px 12px;
      font-weight: 850;
      font-size: 13px;
      cursor: pointer;
      background: #111827;
      color: #ffffff;
      box-sizing: border-box;
    }

    button:hover {
      transform: translateY(-1px);
      background: #1f2937;
    }

    button.light {
      background: #e5e7eb;
      color: #111827;
    }

    button.warn {
      background: #f59e0b;
      color: #111827;
    }

    .progress {
      display: flex;
      flex-wrap: wrap;
      gap: 7px;
      margin-top: 11px;
      padding-top: 11px;
      border-top: 1px solid #e5e7eb;
    }

    .chip {
      border: 1px solid #d1d5db;
      border-radius: 999px;
      background: #f9fafb;
      color: #374151;
      padding: 7px 10px;
      font-size: 12px;
      font-weight: 850;
      cursor: pointer;
      user-select: none;
    }

    .chip:hover {
      background: #eef2ff;
      transform: translateY(-1px);
    }

    .chip.active {
      background: #111827;
      color: #ffffff;
      border-color: #111827;
    }

    .chip.done {
      background: #dcfce7;
      color: #166534;
      border-color: #86efac;
    }

    .coach-card {
      position: fixed;
      right: 28px;
      bottom: 28px;
      z-index: 2147483001;
      width: min(430px, calc(100vw - 42px));
      background: #111827;
      color: #ffffff;
      border-radius: 20px;
      box-shadow: 0 26px 70px rgba(15, 23, 42, 0.38);
      padding: 18px;
      box-sizing: border-box;
      display: none;
    }

    .coach-card.active {
      display: block;
    }

    .coach-card h3 {
      margin: 0 0 8px;
      color: #ffffff;
      font-size: 18px;
      line-height: 1.35;
    }

    .coach-card p {
      margin: 0;
      color: #e5e7eb;
      line-height: 1.6;
      font-size: 14px;
    }

    .coach-actions {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
      margin-top: 14px;
    }

    .coach-actions button {
      padding: 10px 9px;
      font-size: 13px;
      background: #374151;
    }

    .coach-actions button.primary {
      background: #f59e0b;
      color: #111827;
    }

    .target-outline {
      position: fixed;
      z-index: 2147482999;
      pointer-events: none;
      border: 4px solid rgba(245, 158, 11, 0.95);
      box-shadow: 0 0 0 9999px rgba(15, 23, 42, 0.12), 0 0 0 10px rgba(254, 243, 199, 0.68);
      border-radius: 16px;
      display: none;
      box-sizing: border-box;
      transition: left 0.12s ease, top 0.12s ease, width 0.12s ease, height 0.12s ease;
    }

    .target-outline.active {
      display: block;
    }

    .step-pin {
      position: fixed;
      z-index: 2147483002;
      display: none;
      align-items: center;
      justify-content: center;
      width: 38px;
      height: 38px;
      border-radius: 999px;
      background: #111827;
      color: #ffffff;
      font-size: 20px;
      font-weight: 950;
      box-shadow: 0 10px 22px rgba(17, 24, 39, 0.28);
      pointer-events: none;
    }

    .step-pin.active {
      display: inline-flex;
    }

    .mini-launcher {
      position: fixed;
      left: 22px;
      bottom: 22px;
      z-index: 2147483000;
      display: none;
      border-radius: 999px;
      padding: 12px 14px;
      box-shadow: 0 16px 42px rgba(15, 23, 42, 0.24);
      background: #111827;
      color: #ffffff;
    }

    .mini-launcher.active {
      display: block;
    }

    @media (max-width: 720px) {
      .shell-topbar {
        left: 10px;
        right: 10px;
        top: 8px;
      }
      .coach-card {
        left: 12px;
        right: 12px;
        bottom: 12px;
        width: auto;
      }
    }
  `;

  let currentStep = 1;
  let started = false;
  let destroyed = false;
  let host;
  let root;
  let topbar;
  let progress;
  let coach;
  let coachTitle;
  let coachText;
  let outline;
  let pin;
  let launcher;

  function escapeHtml(value) {
    return String(value || "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function getTarget(step) {
    if (!step || !step.selector) return null;
    const el = document.querySelector(step.selector);
    if (!el) return null;
    if (el.tagName === "LABEL" && el.parentElement) return el.parentElement;
    return el;
  }

  function getCurrentStep() {
    return steps.find((item) => item.n === currentStep) || steps[0];
  }

  function renderShell() {
    host = document.createElement("div");
    host.id = HOST_ID;
    host.setAttribute("data-version", VERSION);

    root = host.attachShadow({ mode: "open" });

    const style = document.createElement("style");
    style.textContent = css;

    const wrapper = document.createElement("div");
    wrapper.innerHTML = `
      <div class="shell-topbar" part="topbar">
        <div class="shell-main">
          <div>
            <div class="shell-title">非创新药标准流程：按 ① 到 ⑮ 顺序操作</div>
            <div class="shell-text">
              不要跳步。先填公司名称、代码、行业和关注点；再跑进门 9 张能力卡；全部成功并组装进门数据包后再生成最终研报。
              研报生成完成后，进入「交互聊天记录」继续追问；如需演示，可先点击「载入交互后门」解锁交互入口。
            </div>
          </div>
          <div class="shell-actions">
            <button type="button" data-action="start">开始新手引导</button>
            <button type="button" class="light" data-action="close">关闭并移除引导</button>
          </div>
        </div>
        <div class="progress"></div>
      </div>

      <div class="target-outline"></div>
      <div class="step-pin"></div>

      <div class="coach-card">
        <h3></h3>
        <p></p>
        <div class="coach-actions">
          <button type="button" data-action="prev">上一步</button>
          <button type="button" class="primary" data-action="next">下一步</button>
          <button type="button" data-action="close">关闭并移除</button>
        </div>
      </div>

      <button type="button" class="mini-launcher" data-action="open">打开新手引导</button>
    `;

    root.appendChild(style);
    root.appendChild(wrapper);
    document.body.appendChild(host);

    topbar = root.querySelector(".shell-topbar");
    progress = root.querySelector(".progress");
    coach = root.querySelector(".coach-card");
    coachTitle = root.querySelector(".coach-card h3");
    coachText = root.querySelector(".coach-card p");
    outline = root.querySelector(".target-outline");
    pin = root.querySelector(".step-pin");
    launcher = root.querySelector(".mini-launcher");

    progress.innerHTML = steps.map((step) => {
      const label = step.n <= 9 ? `0${step.n}` : String(step.n);
      return `<button type="button" class="chip" data-step="${step.n}">${label}</button>`;
    }).join("");

    root.addEventListener("click", (event) => {
      const el = event.target;
      if (!(el instanceof Element)) return;

      const action = el.getAttribute("data-action");
      const stepValue = el.getAttribute("data-step");

      if (stepValue) {
        goTo(Number(stepValue), true);
        return;
      }

      if (action === "start") start();
      if (action === "close") close();
      if (action === "open") openTopbar();
      if (action === "next") next();
      if (action === "prev") previous();
    });

    window.addEventListener("resize", updateOverlay, { passive: true });
    window.addEventListener("scroll", updateOverlay, { passive: true });
  }

  function updateProgress() {
    if (!progress) return;
    progress.querySelectorAll(".chip").forEach((chip) => {
      const n = Number(chip.getAttribute("data-step"));
      chip.classList.toggle("active", started && n === currentStep);
      chip.classList.toggle("done", n < currentStep);
    });
  }

  function updateCoach(step) {
    if (!coachTitle || !coachText || !step) return;
    coachTitle.textContent = step.title;
    coachText.textContent = step.text;
  }

  function updateOverlay() {
    if (destroyed || !started) return;

    const step = getCurrentStep();
    const target = getTarget(step);
    updateCoach(step);

    if (!target) {
      if (outline) outline.classList.remove("active");
      if (pin) pin.classList.remove("active");
      return;
    }

    const rect = target.getBoundingClientRect();
    const pad = 7;

    if (outline) {
      outline.style.left = `${Math.max(8, rect.left - pad)}px`;
      outline.style.top = `${Math.max(8, rect.top - pad)}px`;
      outline.style.width = `${Math.max(28, rect.width + pad * 2)}px`;
      outline.style.height = `${Math.max(28, rect.height + pad * 2)}px`;
      outline.classList.add("active");
    }

    if (pin) {
      pin.textContent = String(step.n);
      pin.style.left = `${Math.max(8, rect.left - 19)}px`;
      pin.style.top = `${Math.max(8, rect.top - 19)}px`;
      pin.classList.add("active");
    }
  }

  function goTo(n, shouldStart) {
    const step = steps.find((item) => item.n === n);
    if (!step) return false;
    currentStep = n;

    if (shouldStart) started = true;
    if (coach) coach.classList.toggle("active", started);
    if (launcher) launcher.classList.remove("active");

    const target = getTarget(step);
    if (target) {
      target.scrollIntoView({ behavior: "smooth", block: "center", inline: "nearest" });
      setTimeout(updateOverlay, 240);
    } else {
      updateOverlay();
    }

    updateCoach(step);
    updateProgress();
    return true;
  }

  function start() {
    started = true;
    if (topbar) topbar.classList.remove("is-collapsed");
    if (coach) coach.classList.add("active");
    if (launcher) launcher.classList.remove("active");
    goTo(currentStep || 1, true);
  }

  function close() {
    // Full recovery mode: "Close" means completely remove the outer shell.
    // This returns the page to the normal foundation UI instead of leaving a mini launcher.
    destroy();
  }

  function openTopbar() {
    if (topbar) topbar.classList.remove("is-collapsed");
    if (launcher) launcher.classList.remove("active");
  }

  function next() {
    goTo(Math.min(steps.length, currentStep + 1), true);
  }

  function previous() {
    goTo(Math.max(1, currentStep - 1), true);
  }

  function destroy() {
    destroyed = true;
    window.removeEventListener("resize", updateOverlay);
    window.removeEventListener("scroll", updateOverlay);
    if (host && host.parentNode) host.parentNode.removeChild(host);
    if (window[NAMESPACE] && window[NAMESPACE].version === VERSION) {
      delete window[NAMESPACE];
    }
  }

  function init() {
    renderShell();
    updateProgress();
  }

  window[NAMESPACE] = Object.freeze({
    version: VERSION,
    steps: steps.map((step) => Object.freeze({ ...step })),
    start,
    close,
    open: openTopbar,
    goTo,
    next,
    previous,
    destroy,
    remove: destroy,
    safetyContract: Object.freeze({
      coreFunctionsOverwritten: false,
      coreEventHandlersModified: false,
      inputValuesModified: false,
      existingDomNodesMutatedForGuidance: false,
      appendedNodes: "one shadow host only"
    })
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, { once: true });
  } else {
    init();
  }
})();
