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
    ✅ runner.py    执行层（pytest + junitxml 解析）
    ✅ triage.py    失败分流（5 类失败 → 5 种动作）
    ✅ snapshot.py  快照 / 回滚点
    ✅ report.py    Markdown 报告
    ✅ loop.py      主循环（有界自愈）
    ✅ server.py    本地报告台（stdlib HTTP + web/ 下的 Vue 3 零构建前端）

本包每次只交付一个模块，绝不一次性甩全量。
"""
import os
import sys

from .env import load_env

# 导入本包时就把 .env（以及 Windows 用户级变量）补齐。
# 为什么放这里：所有入口（CLI、pytest、将来的 main.py）都会 import selfheal，
# 一处加载 = 全链路生效，不用每个脚本各写一遍。
load_env()

# ---------------------------------------------------------------------------
# 让终端输出永远不因"编码"崩掉
# ---------------------------------------------------------------------------
# 实测踩到的坑：Windows 控制台默认是 GBK（代码页 936），而我们所有输出都带
# ✅/❌ 这类字符。一旦输出被管道接走（`... | Set-Content`、重定向到文件），
# Python 会用 GBK 编码去写，立刻抛
#     UnicodeEncodeError: 'gbk' codec can't encode character '\u2705'
# ——**循环其实跑完了，结果却因为打印失败而丢掉**，非常坑。
# 处理办法：优先把 stdout/stderr 切成 utf-8；不行就退回"编码不了的字符用 ? 代替"。
# 关键点是 errors="replace"：宁可显示成 ?，也绝不因为一个表情符号丢掉整份结果。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")   # type: ignore[union-attr]
    except (AttributeError, ValueError, OSError):
        try:
            _stream.reconfigure(errors="replace")                 # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass

# 全局兜底：让**所有** requests 调用（包括 LLM 生成的测试代码）默认不走代理访问本机。
# 原因见 targets.make_session 的注释——代理会把"连接被拒"变成 502，把排查带偏。
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost,::1")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost,::1")
