/**
 * selfheal 报告台 · 前端逻辑（Vue 3 全局构建，选项式 API）
 *
 * 为什么用 Vue 3 而不是自己写原生 DOM：
 *   列表 ↔ 详情 ↔ 运行状态这三块是要跟着数据联动的，原生 JS 得手写一堆
 *   querySelector + innerHTML 拼装，容易出错也难读。Vue 的响应式正好对上这个场景。
 * 为什么零构建（用 vendor 里的全局构建而不是 npm + vite）：
 *   这个仓库的目的是"克隆下来就能跑"。判据很简单：**如果跑界面要先 npm install，
 *   那它就从"工具"变成了"项目"**——依赖装不上（受限网络）的人直接打不开。
 *
 * 数据流：GET /api/targets → 顶栏下拉；GET /api/reports → 左列；
 *        GET /api/report?name=… → 右列（服务端已渲染好 HTML）；
 *        POST /api/run + 轮询 GET /api/run/status → 进度条。
 */
const { createApp } = Vue;

// ---------------------------------------------------------------------------
// 出错不许黑屏
// ---------------------------------------------------------------------------
// 实测踩过：模板里引用了一个 data 里不存在的字段，Vue 渲染时抛错，
// **整个页面直接白屏**，而且控制台在折叠面板里，用户只会说"黑屏了"。
// 所以这里挂一个兜底：任何渲染/运行时错误都画成一条红色横幅，
// 把错误消息露在页面上 —— 出问题要看得见，而不是变成"打不开"。
function showFatal(message) {
  let bar = document.getElementById("fatal-bar");
  if (!bar) {
    bar = document.createElement("div");
    bar.id = "fatal-bar";
    bar.style.cssText =
      "position:fixed;left:0;right:0;bottom:0;z-index:9999;padding:10px 16px;" +
      "background:#5c2626;color:#ffd9d9;font:12.5px/1.6 monospace;border-top:1px solid #f87171;" +
      "max-height:40vh;overflow:auto;white-space:pre-wrap";
    document.body.appendChild(bar);
  }
  bar.textContent = "界面出错（已捕获，不是白屏）：\n" + message;
}

window.addEventListener("error", (event) => showFatal(String(event.message || event.error)));
window.addEventListener("unhandledrejection", (event) => showFatal(String(event.reason)));

const app = createApp({
  data() {
    return {
      targets: [],
      reports: [],
      current: null,
      error: "",
      form: { target: "", n_cases: 3, max_rounds: 2 },
      imp: {
        open: false,
        name: "",
        base_url: "",
        files: [],         // [{path, content}] —— 浏览器读文件内容，服务端不碰文件系统
        contract: null,
        busy: false,
        saving: false,
        force: false,
        message: "",
        error: "",
        reqText: "",
        reqFile: "",
        reqBusy: false,
        reqMsg: "",
        reqFiles: [],
      },
      runState: { status: "idle", target: "", steps: [], message: "", started_at: "", finished_at: "" },
      pollTimer: null,
    };
  },

  computed: {
    running() {
      return this.runState.status === "running";
    },
    runMessage() {
      // 只在"刚跑完/出错"时展示进度条，idle 状态不占地方
      return this.runState.status === "done" || this.runState.status === "error";
    },
    /** 草案里的接口行：直接在表格里改，保存时用的就是改后的内容 */
    draftRows() {
      return this.imp.contract && Array.isArray(this.imp.contract.endpoints)
        ? this.imp.contract.endpoints
        : [];
    },
    /** 该项目已有的需求文档（模板里直接读 reqFiles，别让模板自己去猜路径） */
    reqFiles() {
      return Array.isArray(this.imp.reqFiles) ? this.imp.reqFiles : [];
    },
  },

  watch: {
    /** 切换项目名时，顺手把该项目已有的需求文档列出来 */
    "imp.name"(value) {
      this.loadRequirements(value);
    },
  },

  mounted() {
    this.loadTargets();
    this.loadReports();
    this.pollStatus();          // 页面一打开就同步一次（可能后台正在跑）
  },
  beforeUnmount() {
    if (this.pollTimer) clearInterval(this.pollTimer);
  },

  methods: {
    async loadTargets() {
      try {
        const data = await this.getJSON("/api/targets");
        this.targets = data.targets || [];
        if (!this.form.target && this.targets.length) {
          this.form.target = this.targets[0].name;
        }
      } catch (e) {
        this.error = "读取被测项目失败：" + e.message;
      }
    },

    async loadReports() {
      try {
        const data = await this.getJSON("/api/reports");
        this.reports = data.reports || [];
        this.error = "";
        // 第一次进来默认打开最新一份
        if (!this.current && this.reports.length) {
          this.openReport(this.reports[0].name);
        }
      } catch (e) {
        this.error = "读取报告列表失败：" + e.message;
      }
    },

    async openReport(name) {
      try {
        this.current = await this.getJSON("/api/report?name=" + encodeURIComponent(name));
      } catch (e) {
        this.error = "读取报告失败：" + e.message;
      }
    },

    async runOnce() {
      this.error = "";
      try {
        const resp = await fetch("/api/run", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(this.form),
        });
        const data = await resp.json();
        if (!data.ok) {
          this.error = data.error || "启动失败";
          return;
        }
        this.runState = { status: "running", target: data.target, steps: [], message: "", started_at: "", finished_at: "" };
        this.startPolling();
      } catch (e) {
        this.error = "运行请求失败：" + e.message;
      }
    },

    startPolling() {
      if (this.pollTimer) clearInterval(this.pollTimer);
      this.pollTimer = setInterval(() => this.pollStatus(), 1500);
    },

    // ---------------- 导入新项目：读文件 → AI 起草 → 人工确认 → 保存 ----------------
    pickFiles(event) {
      const picked = Array.from(event.target.files || []);
      this.imp.files = [];
      this.imp.error = "";
      if (!picked.length) return;

      let pending = picked.length;
      picked.forEach((file) => {
        const reader = new FileReader();
        reader.onload = () => {
          this.imp.files.push({ path: file.name, content: String(reader.result || "") });
          if (--pending === 0) {
            this.imp.message = `已读取 ${this.imp.files.length} 个文件，可以起草了`;
          }
        };
        reader.onerror = () => {
          this.imp.error = `读取文件失败：${file.name}`;
          if (--pending === 0) return;
        };
        reader.readAsText(file, "utf-8");
      });
    },

    async draftContract() {
      this.imp.busy = true;
      this.imp.error = "";
      this.imp.message = "正在让模型读源码…";
      this.imp.contract = null;
      try {
        const resp = await fetch("/api/contract/draft", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            name: this.imp.name,
            base_url: this.imp.base_url,
            files: this.imp.files,
          }),
        });
        const data = await resp.json();
        if (!data.ok) {
          this.imp.error = (data.violations && data.violations.join("；")) || data.error || "起草失败";
          this.imp.message = "";
          // 失败时也把半成品摆出来，方便人工改
          if (data.contract && data.contract.endpoints) this.imp.contract = data.contract;
          return;
        }
        this.imp.contract = data.contract;
        this.imp.message = data.summary || "起草完成";
      } catch (e) {
        this.imp.error = "起草请求失败：" + e.message;
      } finally {
        this.imp.busy = false;
      }
    },

    addEndpoint() {
      if (!this.imp.contract) return;
      if (!Array.isArray(this.imp.contract.endpoints)) this.imp.contract.endpoints = [];
      this.imp.contract.endpoints.push({
        id: "new-endpoint",
        method: "GET",
        path: "/api/xxx",
        expect_status: 200,
        expect_fields: [],
        notes: "",
      });
    },

    // ---------------- 需求文档：列表 + 粘贴保存 ----------------
    async loadRequirements(target) {
      if (!target) { this.imp.reqFiles = []; return; }
      try {
        const data = await this.getJSON(
          "/api/requirements?target=" + encodeURIComponent(target)
        );
        this.imp.reqFiles = data.files || [];
      } catch (e) {
        this.imp.reqFiles = [];
      }
    },

    async saveRequirements() {
      this.imp.reqBusy = true;
      this.imp.reqMsg = "";
      this.imp.error = "";
      try {
        const resp = await fetch("/api/requirements/save", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            target: this.imp.name,
            text: this.imp.reqText,
            filename: this.imp.reqFile,
            force: this.imp.force,
          }),
        });
        const data = await resp.json();
        if (!data.ok) {
          this.imp.error = data.error || "保存需求失败";
          return;
        }
        this.imp.reqMsg =
          `已保存 ${data.file}（${data.sections} 节）；${data.contract}`;
        this.imp.reqText = "";
        this.imp.reqFile = "";
        await this.loadRequirements(this.imp.name);
      } catch (e) {
        this.imp.error = "保存需求请求失败：" + e.message;
      } finally {
        this.imp.reqBusy = false;
      }
    },

    async saveContract() {
      if (!this.imp.contract) return;
      this.imp.saving = true;
      this.imp.error = "";
      try {
        const resp = await fetch("/api/contract/save", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ contract: this.imp.contract, force: this.imp.force }),
        });
        const data = await resp.json();
        if (!data.ok) {
          this.imp.error = data.error || "保存失败";
          if (data.exists) this.imp.error += "（勾选\"覆盖同名契约\"可覆盖）";
          return;
        }
        this.imp.message = `已保存为契约：${data.name}.json —— 现在可以在上面下拉框里选它并运行`;
        await this.loadTargets();
        this.form.target = data.name;
      } catch (e) {
        this.imp.error = "保存请求失败：" + e.message;
      } finally {
        this.imp.saving = false;
      }
    },

    async pollStatus() {
      try {
        const state = await this.getJSON("/api/run/status");
        this.runState = state;
        if (state.status === "done") {
          this.stopPolling();
          await this.loadReports();
          if (state.report) this.openReport(state.report);
        } else if (state.status === "error") {
          this.stopPolling();
        }
      } catch (e) {
        this.stopPolling();
        this.error = "查询运行状态失败：" + e.message;
      }
    },

    stopPolling() {
      if (this.pollTimer) {
        clearInterval(this.pollTimer);
        this.pollTimer = null;
      }
    },

    async getJSON(url) {
      const resp = await fetch(url);
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || resp.status + " " + resp.statusText);
      return data;
    },

    // ---- 展示用的小工具 ----
    badgeClass(conclusion) {
      if (!conclusion) return "unknown";
      if (conclusion.includes("全部通过")) return "ok";
      if (conclusion.includes("缺陷")) return "defect";
      if (conclusion.includes("不可达")) return "stop";
      if (conclusion.includes("上限")) return "gaveup";
      return "unknown";
    },

    shortConclusion(text) {
      if (!text) return "—";
      return text.replace(/^[✅⚠️⛔❌❗\s]+/, "").slice(0, 8);
    },

    labelOf(status) {
      return {
        idle: "空闲",
        running: "运行中",
        done: "已结束",
        error: "出错",
      }[status] || status;
    },
  },
});

// 渲染期错误也要落到底部横幅上（否则 Expander 一折叠就没人看得到）
app.config.errorHandler = (err, _instance, info) => {
  showFatal(`${err && err.stack ? err.stack : err}\n（位置：${info}）`);
};

app.mount("#app");
