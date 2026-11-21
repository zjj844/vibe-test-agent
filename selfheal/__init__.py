"""selfheal —— AI 增强的接口自动化测试工具（自愈测试 MVP）。

位置：它是一个**独立工具**，不属于任何被测项目；只要对方能通过 HTTP 说话就能测。

模块依赖方向（单向，越往下越不容易被改坏）：

    main.py → loop.py → {generator, runner, triage} → targets.py
                   ↓            ↓
                report.py   snapshot.py / critic.py / kb.py

模块状态：
    ✅ targets.py   契约读取 + 探活
    ✅ http.py      真实 HTTP 请求层
    ✅ kb.py        需求知识库（层级分块 + 检索）
    ✅ critic.py    用例自评（rubric 5 维，多次采样取中位）
    ✅ generator.py 用例生成（模板头 + 三道静态检查 + 有界重试）
    ⬜ runner.py / triage.py / snapshot.py / loop.py / report.py

本包每次只交付一个模块，绝不一次性甩全量。
"""
import os

from .env import load_env

# 导入本包时就把 .env（以及 Windows 用户级变量）补齐。
# 为什么放这里：所有入口（CLI、pytest、将来的 main.py）都会 import selfheal，
# 一处加载 = 全链路生效，不用每个脚本各写一遍。
load_env()

# 全局兜底：让**所有** requests 调用（包括 LLM 生成的测试代码）默认不走代理访问本机。
# 原因见 targets.make_session 的注释——代理会把"连接被拒"变成 502，把排查带偏。
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost,::1")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost,::1")
