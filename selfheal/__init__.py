"""selfheal —— AI 增强的接口自动化测试模块（自愈测试 MVP）。

位置：它**不属于** rag/（那个是被测对象），是一个独立工具，能测任何 HTTP 服务。

八个模块的依赖方向（单向，越往下越不容易被改坏）：

    main.py  →  loop.py  →  {generator, runner, triage}  →  targets.py
                    ↓                ↓
                 report.py       snapshot.py / critic.py / kb.py

本包只交付一个模块一批代码，绝不一次性甩全量。
"""
import os

# 全局兜底：让**所有** requests 调用（包括 LLM 生成的测试代码）默认不走代理访问本机。
# 原因见 targets.make_session 的注释——代理会把"连接被拒"变成 502，把排查带偏。
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost,::1")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost,::1")
