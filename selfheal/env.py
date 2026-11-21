"""极简环境变量加载器（不引入 python-dotenv，少一个依赖）。

三件必须在 RAG 项目里做对的事：

1. **优先级**：进程已有的环境变量 > `.env` 文件。
   否则仓库里的 .env 会覆盖 CI 注入的变量——这是 dotenv 的经典坑。
2. **Windows 用户级变量兜底**：GUI 里 setx 设的变量，只在"之后新启动的进程"里可见。
   如果 DSH/终端比它启动得早，就读不到。这里直接查注册表补上。
3. **代理**：程序里统一走 `urllib_opener()`，避免每个调用点各写一遍。
"""
from __future__ import annotations

import os
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 只有这些关键项才去查注册表，避免把整台机器的环境变量都搬进来
_FALLBACK_KEYS = (
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_MODEL",
    "RAG_LLM",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
)


def _windows_user_env(name: str) -> str | None:
    """读 HKCU\\Environment（setx 写的地方）。非 Windows 直接返回 None。"""
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
            return value or None
    except OSError:
        return None


def load_env(path: str | Path | None = None, verbose: bool = False) -> dict:
    """加载 .env 并补齐注册表里的用户级变量，返回本次实际注入的键值。"""
    loaded: dict[str, str] = {}

    env_path = Path(path) if path else ROOT / ".env"
    if env_path.exists():
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            loaded[k] = v
            if not os.environ.get(k):
                os.environ[k] = v
                if verbose:
                    print(f"[env] 从 .env 注入 {k}")

    for k in _FALLBACK_KEYS:
        if os.environ.get(k):
            continue
        v = _windows_user_env(k)
        if v:
            os.environ[k] = v
            loaded.setdefault(k, v)
            if verbose:
                print(f"[env] 从 Windows 用户环境变量注入 {k}")

    return loaded


def mask(secret: str | None) -> str:
    if not secret:
        return "(未设置)"
    if len(secret) <= 10:
        return "(已设置，长度 %d)" % len(secret)
    return f"{secret[:5]}…{secret[-4:]}（长度 {len(secret)}）"


def proxy_url() -> str | None:
    return (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
    )


def urllib_opener() -> urllib.request.OpenerDirector:
    """显式代理优先；没配就走系统代理（Windows 下 urllib 会自动读注册表）。"""
    proxy = proxy_url()
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        return urllib.request.build_opener(handler)
    return urllib.request.build_opener()
