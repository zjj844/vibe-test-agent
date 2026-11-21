# vibe-test-agent

<!-- 推上 GitHub 后，把下面徽章里的 USERNAME 换成你的 GitHub 用户名，CI 还没跑绿之前可以先删掉这一行 -->
![ci](https://github.com/USERNAME/vibe-test-agent/actions/workflows/ci.yml/badge.svg)

**AI 增强的接口自动化测试工具（自愈测试 MVP）**

> 给它一个被测服务的接口契约，它读需求、生成 pytest 用例、执行、把失败分类，
> 能安全修的自动修，不能修的出缺陷报告。

不依赖任何 agent 编排框架，**纯 Python + pytest + requests + DeepSeek**。

---

## 30 秒上手（不用 API key）

```bash
pip install -r requirements.txt        # 只有 requests + pytest 两个依赖
python demo/buggy_service.py           # 终端 1：起一个"故意带 Bug"的示例服务
python -m selfheal.loop --target demo-buggy --n 3 --rounds 2   # 终端 2
```

跑完你会看到：**1 条通过 / 1 条系统缺陷 / 1 条契约不一致**，以及一份带缺陷清单的报告。
示例服务里埋了两种坏法（5xx 与 200 但数据算错），工具对两者的反应不同 —— 这就是这个项目的核心。

想测自己的项目？复制 `selfheal/configs/rag-backend.json` 改一改，**不需要改任何代码**。
（真机生成用例需要 `DEEPSEEK_API_KEY`，见 `.env.example`）

要求：**Python ≥ 3.10**（CI 在 3.10 / 3.13 上各跑一遍）。本地自检：`check.cmd`。

---

## 它解决什么问题

跑自动化测试时，最大的浪费不是"用例跑不过"，而是三件事：

1. **不知道该测什么** —— 需求文档摆着，没人把它和接口对应起来
2. **失败不会归类** —— 红了就去改脚本，结果把"接口真坏了"改成"测试通过"
3. **重复犯同一个错** —— 这次改了，下次换个接口又犯一次

这个工具依次对付这三件事：**RAG 读需求** → **六类失败分流** → **有界自愈 + 快照回滚**。

---

## 流程（背这一行）

```
探 → 读 → 写 → 校验 → 评 → 快照 → 跑 → 分 → 定 → 报
```

| 步 | 动作 | 对应模块 |
|---|---|---|
| 探 | 探活，服务没起就停手 | `targets.py` ✅ |
| 读 | 读契约 + 从需求文档检索"该测什么" | `kb.py` ✅ |
| 写 | 生成 pytest 用例 | `generator.py` ✅ |
| 校验 | 静态检查（毫秒级，不合规就不执行） | `generator.py` ✅ |
| 评 | 按 rubric 自评，2 次取中位 | `critic.py` ✅ |
| 快照 | 每轮产物留回滚点 | `snapshot.py` ✅ |
| 跑 | 执行 pytest，解析 junitxml | `runner.py` ✅ |
| 分 | 失败分 5 类 | `triage.py` ✅ |
| 定 | 继续 / 停手 / 出缺陷报告（≤2 轮） | `loop.py` ✅ |
| 报 | 出 Markdown 报告 | `report.py` ✅ |

**一条命令跑完全流程：**

```bash
python -m selfheal.loop --target rag-backend --n 3 --rounds 2
```

---

## 快速开始

```bash
pip install -r requirements.txt
cp .env.example .env        # 填入 DEEPSEEK_API_KEY

# 0) 一条命令跑完整个自愈闭环（需要被测服务已经跑起来）
python -m selfheal.loop --target rag-backend --n 3 --rounds 2

# 1) 看契约 + 探活（需要被测服务已经跑起来）
python -m selfheal.targets --target rag-backend
# 或从任意目录调用（自动 cd 到本仓库根）
probe.cmd --target rag-backend

# 2) 跑手工基线测试（人写的 3 条标准答案）
python -m pytest tests/test_manual_baseline.py -v

# 3) 全部离线测试（不需要被测服务）
python -m pytest -q
```

**被测服务不可达时，基线测试会 `skip` 而不是 `fail`** —— 因为"服务没起来"不是用例的错，
把它算进失败率会污染统计数字。

---

## 复现一份真实的缺陷报告（约 1 分钟）

「系统 Bug 不许改测试」是这个项目最重要的一条原则。为了让人能亲眼看到它，
仓库里内置了一个**故意带 Bug 的示例服务**（`demo/buggy_service.py`），里面埋了两种不同的坏法：

| 接口 | 坏法 | 工具应当怎么处理 |
|---|---|---|
| `GET /api/orders/42` | 订单不存在时**直接 500** | 判为「系统 Bug」→ 出缺陷清单，**不改测试** |
| `GET /api/orders/41/total` | HTTP **200**，但总价忘了乘数量（返回 399，应为 798） | 判为「契约与实现不一致」→ **挂起交给人**，不改也不重写 |

```bash
# 终端 1：起示例服务
python demo/buggy_service.py

# 终端 2：让工具去测它
python -m selfheal.loop --target demo-buggy --n 3 --rounds 2
```

预期输出（节选）：

```
  跑   通过 1 / 共 3（1.0s）
  分   共 2 条失败：系统Bug 1、契约与实现不一致 1。
       其中 1 条是**服务端缺陷**，不许改测试。另有 1 条疑似契约与实现不一致，需人工确认（不改测试）。
  定   发现 1 条系统缺陷 → 不改测试，出报告

结果：⚠️ defect
  ✅ test_items_list_normal_flow                                  passed
  ❌ test_order_detail_not_found_returns_404_not_500              failed
  ❌ test_order_total_must_equal_unit_price_times_quantity        failed

缺陷清单（工具不改测试，需人工跟进）：
  · …::test_order_detail_not_found_returns_404_not_500｜接口返回 5xx：这是服务端缺陷…
需人工确认（契约与实现不一致：不改测试、也不重写）：
  · …::test_order_total_must_equal_unit_price_times_quantity｜HTTP 通了但数据不对…
AI 辅助判断（仅供参考，无任何动作权限）：
  · …｜[AI 判断] 服务端缺陷 —— total 恰好等于单价 399，说明金额合计未按数量计算…
```

四个关键点：

1. **通过的用例照常通过** —— 工具不会"一律报错"来刷存在感
2. **两种失败都没改测试**，也没触发第二轮重写：改断言让它们变绿等于把缺陷藏起来
3. **「200 但数据错」这一类被单独拎出来挂起** —— 因为责任可能是服务、契约、用例三者之一，
   只有人能分清（这是本项目最容易被忽略的坑：如果判成"断言问题"，第 2 轮就会把断言改宽）
4. **AI 只在规则判不了时出场**，而且只产出一段建议，**没有任何动作权限**

样例报告与生成的用例：`docs/samples/REPORT-demo-buggy-defect.md`、`docs/samples/generated-cases-demo-defect.py`

---

## 本地报告台（可选界面）

```bash
python -m selfheal.server          # 或双击 report.cmd
# → 打开 http://127.0.0.1:8100/
```

界面上只有三块：

- **左列**：历史报告（结论徽章 / 通过率 / 缺陷数 / 轮数 / 耗时）
- **右列**：报告详情（Markdown 渲染成表格与清单）
- **顶栏**：选被测项目 + 用例数 + 轮数上限 → 点「运行一次」，实时显示走到流水线第几步

三条硬边界（代码注释里也写了）：

1. **只监听 `127.0.0.1`**，不对外提供服务
2. **只读 + 单次运行**：没有任务调度、没有账号系统、没有多租户、没有历史趋势库
3. **零构建**：Vue 3 全局构建放在 `web/vendor/`，克隆下来直接跑，**不需要 `npm install`**

它叫「报告台」，不叫「平台」—— 因为"平台"要回答调度、并发、权限模型，这三样它都没有，也不打算有。
（判据很简单：**如果打开界面要先 `npm install`，它就从"工具"变成了"项目"**，受限网络下直接打不开。）

---

## 换个被测项目 = 换个 json

目前有两个真实被测项目，语言/框架/端口完全不同，**工具代码一行没改**：

| 被测项目 | 技术栈 | base_url | 难点 |
|---|---|---|---|
| `rag-backend` | Python + Flask | `http://127.0.0.1:8000` | 接口慢（真的在调模型） |
| `java-cms` | Java + Spring MVC(SSM) | `http://localhost:8080/cms` | **要登录**（拦截器校验 Bearer token）+ 统一响应体 `{code,msg,data}` |

两个项目都真跑过，报告与生成的用例在 `docs/samples/`：

| 被测项目 | 用例 | 自评 | 亮点 |
|---|---|---|---|
| `rag-backend` | 3/3 通过 | 3.90/5 | 覆盖"空问题必须 400"的边界 |
| `java-cms` | 3/3 通过 | 4.20/5 | 一次生成就写出"未登录取分类树必须 `code=401`"的安全用例 |

```bash
# 测 Java CMS（Spring），需先启动 CMS（mvn jetty:run）并导入 cms.sql
python -m selfheal.loop --target java-cms --n 3 --rounds 2
# 或者只探活
python -m selfheal.targets --target java-cms
```

契约格式（`selfheal/configs/*.json`），最简版：

```json
{
  "name": "rag-backend",
  "base_url": "http://127.0.0.1:8000",
  "health": { "id": "health" },
  "endpoints": [
    { "id": "ask", "method": "POST", "path": "/api/ask",
      "expect_status": 200, "expect_fields": ["answer", "citations"],
      "body": { "question": "..." }, "notes": "业务含义，会进提示词" }
  ]
}
```

测真实项目时还会用到这四样（`java-cms.json` 全用上了）：

```json
{
  "auth": { "endpoint": "login", "token_path": "data.token",
            "header": "Authorization", "prefix": "Bearer " },

  "endpoints": [
    { "id": "article-page", "method": "GET", "path": "/api/article/page",
      "params": { "pageNum": 1, "pageSize": 10 },
      "expect_json": { "code": 200 } },

    { "id": "category-tree-anon", "method": "GET", "path": "/api/category/tree",
      "no_auth": true, "expect_json": { "code": 401 } }
  ]
}
```

- `auth` —— 工具自动调登录接口换 token 并注入后续请求。**登录对模型透明**：模型写的用例里不出现任何 token。
- `params` —— 查询串参数（真实项目的分页）。
- `expect_json` —— 断言响应体里某字段的**值**。必须有它：Java 项目的拒绝方式是
  `HTTP 200 + body.code=401`，只查"字段存在"根本测不出鉴权。
- `no_auth` —— 这个接口**故意不带**登录态，用来验证"未登录必须被拒绝"这条安全边界。

**工具不认识 Python 或 Java，只认识 HTTP 契约。**

---

## 五个关键设计决策

| # | 决策 | 为什么 |
|---|---|---|
| 1 | **失败分 5 类**：服务不可达 / 系统 Bug / 脚本问题 / 断言问题 / 生成代码问题 | 自愈的第一条规则不是"怎么修"，而是"**哪些不许修**"。把真 Bug 改成"通过"比漏测更危险 |
| 2 | **分流用规则，不用模型** | 规则的判断可复现、可审计、可写单测；模型的判断做不到 |
| 3 | **改前快照 + 一键回滚** | 让 AI 改代码必须有后悔药，否则最坏情况是"改坏了但不知道坏了什么" |
| 4 | **自愈上限 2 轮** | 收益递减 + 成本翻倍 + 轮数越多模型越倾向"把断言改弱以求通过" |
| 5 | **自评 2 次取中位** | 单次打分有抖动、不可比较；多次采样让分数可复现 |

---

## 目录

```
selfheal/
├── targets.py       契约读取 + 探活         ✅
├── http.py          发请求（不抛异常）      ✅
├── kb.py            需求知识库（层级分块+检索）✅
├── critic.py        用例自评（rubric+中位）  ✅
├── retrieval/       精简检索组件（从 RAG 项目移植）
├── configs/         被测项目契约（一个项目一个 json）
├── requirements_docs/ 需求文档（RAG 检索的语料）
├── generator.py     用例生成（模板头+四道校验+有界重试）✅
├── runner.py        执行 + 解析 junitxml    ✅
├── triage.py        失败分流（6 类 → 6 动作）✅
├── advisor.py       AI 辅助归因（规则判不了才出场）✅
├── snapshot.py      快照 / 回滚点            ✅
├── loop.py          主循环（有界自愈）       ✅
├── report.py        Markdown 报告           ✅
└── server.py        本地报告台（stdlib HTTP）✅
web/
├── index.html       报告台页面（Vue 3 零构建）
├── app.js           前端逻辑（选项式 API）
├── style.css        样式（单文件，无预处理器）
└── vendor/          vue.global.prod.js（本地化，免 npm）
demo/
└── buggy_service.py 故意带 Bug 的示例服务（两种坏法，用于复现缺陷报告）
scripts/
└── check_contracts.py 契约自检（CI 与 check.cmd 共用）
.github/workflows/ci.yml  CI：py3.10 / 3.13 双版本跑离线测试
docs/
├── 00-零基础入门.md   ← 先读这个
├── 01-MVP架构设计.md
├── 02-工具调用Loop.md
├── 03-理解检查题库.md  ← 30+ 道自测题（含参考答案）
├── 04-简历素材.md      ← 这个项目怎么写成简历条目
└── samples/          样例报告（GitHub 上不跑也能看到输出长什么样）
tests/                离线测试 + 手工基线（127 passed）
```

---

## 本地自检 = CI 做的事

```bash
check.cmd          # Windows 一键
# 或者三条分开跑：
python -m compileall -q selfheal demo scripts
python scripts/check_contracts.py
python -m pytest -q
```

`check.cmd` 跑的就是 CI 里那三步 —— **本地绿了，CI 基本不会红**。
（CI 在 GitHub 上还会额外跑一遍 Python 3.10，验证"不只在我这台 3.13 上能跑"。）

---

## 许可

MIT，见 `LICENSE`。

---

## 刻意不做

Playwright / UI 自动化 · CrewAI · Langfuse 追踪 · 多 Agent 编排 · Web 看板 · 自动修系统 Bug

这些属于"完整平台"的能力，本项目是**分模块小步交付的 MVP**（每晚一个模块，模块各自可跑、可测）。完整愿景见 `docs/01-MVP架构设计.md`。

---

## 与另一个项目的关系

同一作者还有一个 RAG 问答产品（`vibe-coding-rag-lab`）。

**本工具用它当第一个被测对象**（dogfooding），同时也从它那里移植了纯算法模块（`retrieval/`）。
但两者独立成仓库：删掉任一个，另一个都能单独跑。

---

## 参考

目录划分与"故障域分流"思路参考了开源项目
[AISquare-Studio-QA](https://github.com/AISquare-Studio/AISquare-Studio-QA)（Apache-2.0）。
本项目**未使用**其代码与 CrewAI 编排：控制流刻意写成显式四步 + 有界重试，便于定位问题。
