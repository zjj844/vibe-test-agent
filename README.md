# vibe-test-agent

**AI 增强的接口自动化测试工具（自愈测试 MVP）**

> 给它一个被测服务的接口契约，它读需求、生成 pytest 用例、执行、把失败分类，
> 能安全修的自动修，不能修的出缺陷报告。

不依赖任何 agent 编排框架，**纯 Python + pytest + requests + DeepSeek**。

---

## 它解决什么问题

跑自动化测试时，最大的浪费不是"用例跑不过"，而是三件事：

1. **不知道该测什么** —— 需求文档摆着，没人把它和接口对应起来
2. **失败不会归类** —— 红了就去改脚本，结果把"接口真坏了"改成"测试通过"
3. **重复犯同一个错** —— 这次改了，下次换个接口又犯一次

这个工具依次对付这三件事：**RAG 读需求** → **五类失败分流** → **有界自愈 + 快照回滚**。

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
├── triage.py        失败分流                ✅
├── snapshot.py      快照 / 回滚点            ✅
├── loop.py          主循环（有界自愈）       ✅
└── report.py        Markdown 报告           ✅
docs/
├── 00-零基础入门.md   ← 先读这个
├── 01-MVP架构设计.md
├── 02-工具调用Loop.md
├── 03-理解检查题库.md  ← 30 道自测题（含参考答案）
├── 04-简历素材.md      ← 这个项目怎么写成简历条目
└── samples/          样例报告（GitHub 上不跑也能看到输出长什么样）
tests/                离线测试 + 手工基线（88 passed）
```

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
