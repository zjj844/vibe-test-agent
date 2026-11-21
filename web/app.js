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

createApp({
  data() {
    return {
      targets: [],
      reports: [],
      current: null,
      error: "",
      form: { target: "", n_cases: 3, max_rounds: 2 },
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
}).mount("#app");
