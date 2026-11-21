"""M1 · 契约翻译官（targets.py）

=====================================================================
本模块职责
=====================================================================
把一份 JSON 配置文件（target）读成程序能用的对象，并能"探活"
（发一个最小请求，确认被测服务到底起没起来）。

=====================================================================
在整个系统里的位置
=====================================================================
    main.py → loop.py → {generator, runner, triage} → 【targets.py】

它是整条链路最底层：**只有它知道"要测的是谁、接口长什么样"**。
它不依赖任何其它模块（所以它最容易测、也最不容易被改坏）。

=====================================================================
它调用谁 / 被谁调用
=====================================================================
- 调用：只用 Python 标准库 + requests（发 HTTP）
- 被调用：generator（要知道有哪些接口）、runner（要知道 base_url 才能发请求）、
          main（命令行入口）

=====================================================================
为什么要"探活"（这是本模块最容易被忽视的一步）
=====================================================================
改成真实 HTTP 之后（为了能测 Java 项目），服务没起来会伪装成
"所有测试都失败了"。如果不先探活，自愈模块就会去改一堆
**根本没问题的测试用例**——白改，还把好用例改坏。

所以：**探活失败 → 一个字节都不许改。**
=====================================================================
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import requests

# 配置文件放在这个目录下：一个被测项目 = 一个 json 文件
CONFIG_DIR = Path(__file__).resolve().parent / "configs"

# 本机地址清单：对这些地址发请求时**必须绕过代理**
LOCAL_HOSTS = {"127.0.0.1", "localhost", "0.0.0.0", "::1"}


def is_local(url: str) -> bool:
    """判断是不是本机地址。"""
    return (urlparse(url).hostname or "").lower() in LOCAL_HOSTS


def make_session(url: str) -> requests.Session:
    """按目标地址造一个 requests 会话。

    ⚠️ 这是实测踩到的坑，必须处理：
    本机开着 Clash（HTTP_PROXY=http://127.0.0.1:7877），requests 会**连 127.0.0.1
    也走代理**。于是"服务没启动"本该报 ConnectionError（连接被拒），
    实际却变成 **502**（代理转发失败）——分流模块会把它误判成"系统 Bug"，
    然后去写出错误的缺陷报告。

    所以：对本机地址一律 `trust_env = False`，完全无视环境里的代理设置。
    """
    session = requests.Session()
    if is_local(url):
        session.trust_env = False
    return session


# ---------------------------------------------------------------------------
# 数据结构：用一个"盒子"装一个接口的全部信息
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Endpoint:
    """一个待测接口。

    frozen=True 的意思是"创建后不能改"。
    为什么刻意不可变：契约一旦定下来，后面所有模块（生成/执行/分流）都应该
    看到同一份数据。**可变的配置是 bug 的温床**。
    """

    id: str                      # 唯一标识，例如 "ask"
    method: str                  # GET / POST
    path: str                    # 例如 "/api/ask"（不含域名，域名在 Target 里）
    expect_status: int = 200     # 期望的 HTTP 状态码
    expect_fields: tuple[str, ...] = ()   # 响应 JSON 里必须有的字段
    notes: str = ""              # 这个接口的业务含义（会塞进 LLM 的提示词）
    body: dict | None = None     # POST 请求体；GET 为 None

    # ---- 小工具：把"域名 + 路径"拼成完整网址 ----
    def url(self, base_url: str) -> str:
        return base_url.rstrip("/") + "/" + self.path.lstrip("/")

    # ---- 小工具：给命令行/文档展示用的一行摘要 ----
    def summary(self) -> str:
        fields = ",".join(self.expect_fields) if self.expect_fields else "-"
        body = "（有请求体）" if self.body else ""
        return f"{self.method:<5} {self.path:<22} 期望 {self.expect_status}  字段 {fields}{body}"


@dataclass(frozen=True)
class Target:
    """一个被测项目。

    它不知道自己是"Flask 后端"还是"Java CMS"——
    只要它能通过 HTTP 说话，就能被这个工具测。
    """

    name: str
    base_url: str
    endpoints: tuple[Endpoint, ...]
    health: Endpoint                       # 探活用的那个接口（也必须是 endpoints 之一）
    timeout: float = 10.0                  # 单次 HTTP 超时（秒）
    source_file: Path | None = None        # 从哪个 json 读出来的（便于报错定位）

    # ---- 按 id 找接口 ----
    def endpoint(self, endpoint_id: str) -> Endpoint | None:
        for ep in self.endpoints:
            if ep.id == endpoint_id:
                return ep
        return None


@dataclass(frozen=True)
class ProbeResult:
    """探活结果。特意做成"只报告、不抛异常"：

    网络错误是**预期内**的情况（服务没起、端口写错），
    不该让整个程序崩掉，而应该转成一条人类能看懂的话。
    """

    ok: bool
    status: int | None
    detail: str
    latency_ms: float
    target_name: str = ""

    def line(self) -> str:
        mark = "✅" if self.ok else "❌"
        return f"{mark} {self.detail}（{self.latency_ms:.0f} ms）"


# ---------------------------------------------------------------------------
# 读配置
# ---------------------------------------------------------------------------
class TargetConfigError(Exception):
    """配置文件写错时抛这个（而不是让 KeyError 到处飞）。"""


def load_target(name_or_path: str | Path) -> Target:
    """读一份 target 配置。

    参数可以是：
      - 名字（"rag-backend"）→ 去 selfheal/configs/rag-backend.json 找
      - 路径（"D:/x/y.json"）→ 直接读

    为什么允许两种：写代码时用名字最省事；临时试别的项目用路径最灵活。
    """
    raw_path = Path(name_or_path)
    path = raw_path if raw_path.suffix == ".json" else (CONFIG_DIR / f"{raw_path}.json")

    if not path.exists():
        raise TargetConfigError(
            f"找不到 target 配置：{path}\n"
            f"可用配置：{', '.join(p.stem for p in sorted(CONFIG_DIR.glob('*.json'))) or '（目录为空）'}"
        )

    # ⚠️ 用 utf-8-sig 而不是 utf-8：Windows 上用记事本 / PowerShell
    # `Set-Content -Encoding UTF8` 保存的 json 会带 BOM（文件开头三个不可见字节），
    # json.loads 会直接报 "Unexpected UTF-8 BOM"。
    # utf-8-sig 会**自动剥掉** BOM，对不带 BOM 的文件也兼容。
    data = json.loads(path.read_text(encoding="utf-8-sig"))

    # ---- 必填字段检查：早失败，错误信息要说清缺什么 ----
    for key in ("name", "base_url", "endpoints"):
        if key not in data:
            raise TargetConfigError(f"{path.name} 缺少必填字段：{key}")

    endpoints = tuple(
        Endpoint(
            id=ep["id"],
            method=ep.get("method", "GET").upper(),
            path=ep["path"],
            expect_status=int(ep.get("expect_status", 200)),
            expect_fields=tuple(ep.get("expect_fields", ())),
            notes=ep.get("notes", ""),
            body=ep.get("body"),
        )
        for ep in data["endpoints"]
    )
    if not endpoints:
        raise TargetConfigError(f"{path.name} 里 endpoints 是空的，没东西可测")

    # ---- 探活接口：没显式写就用第一个接口 ----
    health_raw = data.get("health")
    if health_raw:
        health_id = health_raw.get("id")
        health = next((e for e in endpoints if e.id == health_id), None) if health_id else None
        if health is None:
            # 允许 health 单独声明一个不在 endpoints 里的最小接口
            health = Endpoint(
                id="__health__",
                method=health_raw.get("method", "GET").upper(),
                path=health_raw["path"],
                expect_status=int(health_raw.get("expect_status", 200)),
            )
    else:
        health = endpoints[0]

    return Target(
        name=data["name"],
        base_url=data["base_url"],
        endpoints=endpoints,
        health=health,
        timeout=float(data.get("timeout", 10.0)),
        source_file=path,
    )


# ---------------------------------------------------------------------------
# 探活
# ---------------------------------------------------------------------------
def probe(target: Target, retries: int = 1, backoff: float = 0.5) -> ProbeResult:
    """发一个最小请求，确认服务活着。

    为什么允许 retries：Java 的 Spring 启动慢（10-20 秒），
    刚 `mvn jetty:run` 完第一次请求常常被拒。重试 2 次能避免假失败。
    """
    ep = target.health
    url = ep.url(target.base_url)
    last_detail = ""
    started = time.perf_counter()
    session = make_session(url)  # 本机地址不走代理（见 make_session 的注释）

    for attempt in range(1, max(1, retries) + 1):
        try:
            resp = session.request(
                ep.method,
                url,
                json=ep.body,
                timeout=target.timeout,
            )
            latency = (time.perf_counter() - started) * 1000
            if resp.status_code == ep.expect_status:
                return ProbeResult(True, resp.status_code, f"服务可达：{ep.method} {url} → {resp.status_code}", latency, target.name)
            return ProbeResult(
                False, resp.status_code,
                f"服务有响应但状态码不符：{ep.method} {url} → {resp.status_code}（期望 {ep.expect_status}）",
                latency, target.name,
            )
        except requests.exceptions.ConnectionError:
            last_detail = f"连接被拒：{ep.method} {url}\n   → 被测服务没启动？先起服务再测。"
        except requests.exceptions.Timeout:
            last_detail = f"请求超时（>{target.timeout:.0f}s）：{ep.method} {url}\n   → 服务卡住了，或接口太慢。"
        except requests.exceptions.RequestException as exc:  # 兜底：其它网络类错误
            last_detail = f"请求异常：{type(exc).__name__}: {exc}"

        if attempt < retries:
            time.sleep(backoff * attempt)

    latency = (time.perf_counter() - started) * 1000
    return ProbeResult(False, None, last_detail, latency, target.name)


# ---------------------------------------------------------------------------
# 命令行入口：python -m selfheal.targets --target rag-backend
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="查看 target 契约并探活")
    parser.add_argument("--target", default="rag-backend", help="名字或 json 路径")
    parser.add_argument("--retries", type=int, default=2, help="探活重试次数（默认 2）")
    parser.add_argument("--skip-probe", action="store_true", help="只看契约，不发请求")
    args = parser.parse_args(argv)

    target = load_target(args.target)
    print("=" * 62)
    print(f" target: {target.name}")
    print("=" * 62)
    print(f" 配置文件 : {target.source_file}")
    print(f" base_url : {target.base_url}")
    print(f" 接口数量 : {len(target.endpoints)}")
    for i, ep in enumerate(target.endpoints, 1):
        print(f"  [{i}] {ep.summary()}")
        if ep.notes:
            print(f"        └ {ep.notes}")

    if args.skip_probe:
        return 0

    print("-" * 62)
    result = probe(target, retries=args.retries)
    print(" 探活 : " + result.line())
    if not result.ok:
        print("\n⚠️  探活失败时，自愈模块应当【一个字节都不改】——否则会去修一堆没问题的用例。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
