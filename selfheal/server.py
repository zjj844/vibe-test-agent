"""M10 · 本地报告台（server.py）—— 给工具配一个只读界面

=====================================================================
本模块职责
=====================================================================
把 `reports/*.md` 变成可以在浏览器里看的东西，并支持"点一下跑一次"。

=====================================================================
它「不是」什么（这条比它是什么更重要）
=====================================================================
不是测试平台。没有用户系统、没有任务调度、没有多租户、没有历史趋势库。
界面上只有三块：历史报告列表、报告详情、运行一次。

为什么必须把边界划清楚：
一旦管自己叫"平台"，面试官下一句就是"调度怎么做的？并发怎么控制？
权限模型呢？"——这些这个项目**都没有**，也不打算有。
**定位成"本地报告台"，边界才是自洽的。**

=====================================================================
技术选型（刻意最小）
=====================================================================
- 后端：Python 标准库 `http.server`，不引 Flask/FastAPI
  → 目的是"打开就能看"，不是"再装一套依赖"
- 前端：Vue 3 全局构建（`web/vendor/vue.global.prod.js`），**零构建、无 npm**
  → 一个 index.html + 一个 app.js 就是全部，克隆下来直接跑
- Markdown → HTML：自己写 60 行（标题/表格/列表/代码/粗体），不引 markdown 库
  → 报告格式是我们自己定的，解析自己写反而更可控

=====================================================================
接口
=====================================================================
    GET  /                    → web/index.html
    GET  /web/<file>          → 静态资源（app.js / style.css / vendor/*）
    GET  /api/targets         → 有哪些被测项目契约
    GET  /api/reports         → 历史报告清单（含通过率、结论、缺陷数）
    GET  /api/report?name=xx  → 单份报告（原始 md + 渲染后 html + 元信息）
    GET  /api/contract?name=x → 读一份已有契约（用于编辑）
    POST /api/contract/draft  → AI 读源码起草契约（**不落盘**，等人工确认）
    POST /api/contract/save   → 把人工确认过的契约写进 configs/
    POST /api/run             → 后台跑一次自愈循环 {target, n_cases, max_rounds}
    GET  /api/run/status      → 当前运行状态（供前端轮询）

=====================================================================
安全
=====================================================================
只监听 127.0.0.1（本机），不监听 0.0.0.0。
报告文件名走白名单校验（拒绝 `..`、路径分隔符），防止目录穿越。
"""
from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
REPORT_DIR = ROOT / "reports"
WEB_DIR = ROOT / "web"
CONFIG_DIR = ROOT / "selfheal" / "configs"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}


# ---------------------------------------------------------------------------
# Markdown → HTML（够用就行的 60 行）
# ---------------------------------------------------------------------------
# 为什么自己写：报告是我们自己生成的，规范由我们定（标题、表格、列表、代码、
# 粗体），没必要为这点格式引入一个完整 Markdown 实现。少一个依赖少一处坑。
_INLINE_BOLD = re.compile(r"\*\*(.+?)\*\*")
_INLINE_CODE = re.compile(r"`([^`]+)`")


def _inline(text: str) -> str:
    text = _escape(text)
    text = _INLINE_BOLD.sub(r"<strong>\1</strong>", text)
    text = _INLINE_CODE.sub(r"<code>\1</code>", text)
    return text


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def render_markdown(md: str) -> str:
    """把报告 Markdown 渲染成 HTML 片段。

    支持：H1~H4、表格、有序/无序列表、代码块、分隔线、粗体、行内代码、段落。
    不支持（也用不上）：图片、链接、嵌套列表 —— 报告里没有这些东西。
    """
    lines = md.splitlines()
    out: list[str] = []
    i = 0
    in_list = False
    in_code = False
    code_buf: list[str] = []

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # 代码块
        if stripped.startswith("```"):
            if in_code:
                out.append("<pre><code>" + _escape("\n".join(code_buf)) + "</code></pre>")
                code_buf, in_code = [], False
            else:
                close_list()
                in_code = True
            i += 1
            continue
        if in_code:
            code_buf.append(line)
            i += 1
            continue

        # 表格：当前行是 |...|，下一行是 |---|---|
        if stripped.startswith("|") and i + 1 < len(lines) and re.match(
            r"^\s*\|[\s:\-|]+\|\s*$", lines[i + 1]
        ):
            close_list()
            header = [c.strip() for c in stripped.strip("|").split("|")]
            out.append("<table><thead><tr>")
            out.extend(f"<th>{_inline(c)}</th>" for c in header)
            out.append("</tr></thead><tbody>")
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in cells) + "</tr>")
                i += 1
            out.append("</tbody></table>")
            continue

        # 标题
        m = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if m:
            close_list()
            level = len(m.group(1))
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            i += 1
            continue

        # 分隔线
        if re.match(r"^-{3,}$", stripped):
            close_list()
            out.append("<hr>")
            i += 1
            continue

        # 列表（- 或 1.）
        m = re.match(r"^(?:[-*]|\d+\.)\s+(.*)$", stripped)
        if m:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(m.group(1))}</li>")
            i += 1
            continue

        # 空行
        if not stripped:
            close_list()
            i += 1
            continue

        # 普通段落
        close_list()
        out.append(f"<p>{_inline(stripped)}</p>")
        i += 1

    if in_code and code_buf:
        out.append("<pre><code>" + _escape("\n".join(code_buf)) + "</code></pre>")
    close_list()
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 报告解析：把一份 md 里的关键数字抠成结构化数据
# ---------------------------------------------------------------------------
_META_LINE = re.compile(r"^-\s*([^：:]+)[：:]\s*(.+)$")


def parse_report(md: str) -> dict:
    """从报告 Markdown 里取出概览信息（列表页要用它排序与展示）。"""
    info: dict = {
        "target": "",
        "conclusion": "",
        "started": "",
        "finished": "",
        "rounds": 0,
        "rounds_limit": 0,
        "total": 0,
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "pass_rate": 0.0,
        "duration_s": 0.0,
        "defects": 0,
    }

    title = re.search(r"^#\s*测试报告\s*·\s*(\S+)", md, re.M)
    if title:
        info["target"] = title.group(1)

    for raw in md.splitlines():
        line = raw.strip()
        if line.startswith("- 结论"):
            m = re.search(r"结论[：:]\s*(.+)$", line)
            if m:
                # 去掉 markdown 粗体标记，留下文字
                info["conclusion"] = m.group(1).replace("**", "").strip()
        elif line.startswith("- 轮数"):
            m = re.search(r"轮数[：:]\s*(\d+)\s*（上限\s*(\d+)）", line)
            if m:
                info["rounds"] = int(m.group(1))
                info["rounds_limit"] = int(m.group(2))
        elif line.startswith("- 开始"):
            m = re.search(r"开始[：:]\s*(.+?)\s*结束[：:]\s*(.+)$", line)
            if m:
                info["started"] = m.group(1).strip()
                info["finished"] = m.group(2).strip()

    # 概览表格：| 指标 | 数值 |
    table = dict()
    for line in md.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 2 and cells[0] not in ("指标", "---"):
            table[cells[0]] = cells[1]

    def _num(key: str) -> int:
        raw = table.get(key, "0")
        m = re.search(r"\d+", raw)
        return int(m.group(0)) if m else 0

    info["total"] = _num("用例总数")
    info["passed"] = _num("通过")
    info["skipped"] = _num("跳过")
    # "失败 / 错误" 单独一格，形如 "0 / 0"
    fe = table.get("失败 / 错误", "0 / 0")
    nums = re.findall(r"\d+", fe)
    info["failed"] = sum(int(n) for n in nums)
    rate = table.get("通过率", "")
    m = re.search(r"([\d.]+)%", rate)
    info["pass_rate"] = float(m.group(1)) if m else 0.0
    dur = table.get("执行耗时", "")
    m = re.search(r"([\d.]+)", dur)
    info["duration_s"] = float(m.group(1)) if m else 0.0

    # 缺陷清单：数表格里 <tbody> 的行（跳过表头与分隔行）
    if "## 缺陷清单" in md:
        block = md.split("## 缺陷清单", 1)[1].split("##", 1)[0]
        rows = [ln for ln in block.splitlines() if ln.strip().startswith("|")]
        info["defects"] = max(0, len(rows) - 2)      # 去掉表头 + 分隔行
    return info


def list_reports() -> list[dict]:
    """历史报告清单，按修改时间倒序。"""
    if not REPORT_DIR.exists():
        return []
    items: list[dict] = []
    for path in sorted(REPORT_DIR.glob("REPORT-*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            md = path.read_text(encoding="utf-8")
        except OSError:
            continue
        info = parse_report(md)
        info["name"] = path.name
        info["size"] = path.stat().st_size
        info["mtime"] = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(path.stat().st_mtime)
        )
        items.append(info)
    return items


def load_report(name: str) -> dict:
    """读一份报告。name 只允许是 reports/ 下的 md 文件名（防目录穿越）。"""
    safe = Path(name).name
    if not safe.endswith(".md") or safe != name:
        raise ValueError(f"非法的报告名：{name}")
    path = REPORT_DIR / safe
    if not path.is_file():
        raise FileNotFoundError(f"报告不存在：{safe}")
    md = path.read_text(encoding="utf-8")
    return {
        "name": safe,
        "markdown": md,
        "html": render_markdown(md),
        "info": parse_report(md),
    }


def list_targets() -> list[dict]:
    """可用的被测项目契约。"""
    if not CONFIG_DIR.exists():
        return []
    out: list[dict] = []
    for path in sorted(CONFIG_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        out.append(
            {
                "name": path.stem,
                "base_url": data.get("base_url", ""),
                "endpoint_count": len(data.get("endpoints", [])),
                "needs_auth": bool(data.get("auth")),
            }
        )
    return out


# ---------------------------------------------------------------------------
# 契约：读 / 起草 / 保存
# ---------------------------------------------------------------------------
def load_contract(name: str) -> dict:
    """读一份已有契约（前端编辑用）。name 同样做白名单校验。"""
    safe = Path(name).stem
    if safe != name or "/" in name or "\\" in name or ".." in name:
        raise ValueError(f"非法的契约名：{name}")
    path = CONFIG_DIR / f"{safe}.json"
    if not path.is_file():
        raise FileNotFoundError(f"契约不存在：{safe}.json")
    return {"name": safe, "contract": json.loads(path.read_text(encoding="utf-8-sig"))}


def draft_contract(payload: dict) -> dict:
    """AI 读源码起草契约。**只返回草案，绝不落盘。**"""
    from . import bootstrap

    files = payload.get("files") or []
    if not isinstance(files, list) or not files:
        return {"ok": False, "error": "没有收到任何源码文件"}
    result = bootstrap.draft(
        str(payload.get("name") or ""),
        str(payload.get("base_url") or ""),
        files,
    )
    return {
        "ok": result.ok,
        "summary": result.summary(),
        "contract": result.contract,
        "violations": result.violations,
        "endpoint_count": result.endpoint_count,
        "repaired": result.repaired,
    }


def save_contract(payload: dict) -> dict:
    """把人工确认过的契约写进 configs/（存在则要求 force）。"""
    from . import bootstrap

    contract = payload.get("contract")
    if not isinstance(contract, dict):
        return {"ok": False, "error": "contract 必须是 JSON 对象"}
    try:
        path = bootstrap.save_contract(
            contract, config_dir=CONFIG_DIR, force=bool(payload.get("force"))
        )
    except FileExistsError as exc:
        return {"ok": False, "error": str(exc), "exists": True}
    except Exception as exc:                                  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "name": path.stem, "path": str(path)}



# ---------------------------------------------------------------------------
# 后台运行一次循环（前端点按钮触发）
# ---------------------------------------------------------------------------
# 为什么是"单例 + 线程"而不是任务队列：
# 这是本机单人用的报告台，同时只允许跑一次；再起一个任务队列就是在做平台了。
_RUN_LOCK = threading.Lock()
_RUN_STATE: dict = {
    "status": "idle",      # idle / running / done / error
    "target": "",
    "steps": [],           # [{"round": 1, "stage": "探活"}, ...]
    "message": "",
    "started_at": "",
    "finished_at": "",
    "report": "",          # 跑完之后产出的报告文件名
    "status_code": "",     # success / defect / unreachable / gave_up / error
}


def run_state() -> dict:
    with _RUN_LOCK:
        return json.loads(json.dumps(_RUN_STATE))     # 深拷贝，避免前端读到半截状态


def _worker(target: str, n_cases: int, max_rounds: int) -> None:
    from . import loop as cycle_loop

    def on_step(round_idx: int, stage: str) -> None:
        with _RUN_LOCK:
            _RUN_STATE["steps"].append({"round": round_idx, "stage": stage})

    try:
        cycle = cycle_loop.run_cycle(
            target, n_cases=n_cases, max_rounds=max_rounds, on_step=on_step
        )
        with _RUN_LOCK:
            _RUN_STATE["status"] = "done"
            _RUN_STATE["status_code"] = cycle.status
            _RUN_STATE["report"] = cycle.report_path.name if cycle.report_path else ""
            _RUN_STATE["message"] = cycle.status
            _RUN_STATE["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    except Exception as exc:                       # noqa: BLE001 任何失败都要回给前端看
        with _RUN_LOCK:
            _RUN_STATE["status"] = "error"
            _RUN_STATE["message"] = f"{type(exc).__name__}: {exc}"
            _RUN_STATE["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")


def start_run(target: str, n_cases: int = 3, max_rounds: int = 2) -> dict:
    """启动一次运行。已有任务在跑时拒绝（返回 running）。"""
    known = {t["name"] for t in list_targets()}
    if target not in known:
        return {"ok": False, "error": f"未知的 target：{target}（可用：{', '.join(sorted(known))}）"}

    with _RUN_LOCK:
        if _RUN_STATE["status"] == "running":
            return {"ok": False, "error": "已有任务在运行，请等它结束"}
        _RUN_STATE.update(
            status="running",
            target=target,
            steps=[],
            message="",
            started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            finished_at="",
            report="",
            status_code="",
        )

    thread = threading.Thread(
        target=_worker, args=(target, int(n_cases), int(max_rounds)), daemon=True
    )
    thread.start()
    return {"ok": True, "target": target}


# ---------------------------------------------------------------------------
# HTTP 处理器
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "selfheal-report/1.0"

    # ---- 统一响应 ----
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _text(self, text: str, status: int = 200, ctype: str = "text/plain; charset=utf-8") -> None:
        self._send(status, text.encode("utf-8"), ctype)

    # ---- 路由 ----
    def do_GET(self) -> None:               # noqa: N802 标准库要求的名字
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        try:
            if path in ("/", "/index.html"):
                return self._serve_file(WEB_DIR / "index.html")
            if path.startswith("/web/"):
                rel = path[len("/web/"):]
                target = (WEB_DIR / rel).resolve()
                if WEB_DIR.resolve() not in target.parents and target.parent != WEB_DIR.resolve():
                    return self._json({"error": "非法路径"}, 400)
                return self._serve_file(target)
            if path == "/api/targets":
                return self._json({"targets": list_targets()})
            if path == "/api/reports":
                return self._json({"reports": list_reports()})
            if path == "/api/report":
                name = (query.get("name") or [""])[0]
                return self._json(load_report(name))
            if path == "/api/contract":
                name = (query.get("name") or [""])[0]
                return self._json(load_contract(name))
            if path == "/api/run/status":
                return self._json(run_state())
            return self._json({"error": f"没有这个接口：{path}"}, 404)
        except FileNotFoundError as exc:
            return self._json({"error": str(exc)}, 404)
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        except Exception as exc:                       # noqa: BLE001
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self) -> None:              # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path
        if route not in ("/api/run", "/api/contract/draft", "/api/contract/save"):
            return self._json({"error": "没有这个接口"}, 404)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return self._json({"error": "请求体不是合法 JSON"}, 400)

        if route == "/api/contract/draft":
            # 这一步会真的调模型（10~20 秒）：前端要显示"正在起草…"
            result = draft_contract(payload)
            return self._json(result, 200 if result.get("ok") else 400)

        if route == "/api/contract/save":
            result = save_contract(payload)
            return self._json(result, 200 if result.get("ok") else 400)

        result = start_run(
            payload.get("target", ""),
            payload.get("n_cases", 3),
            payload.get("max_rounds", 2),
        )
        return self._json(result, 200 if result.get("ok") else 409)

    # ---- 静态文件 ----
    def _serve_file(self, path: Path) -> None:
        if not path.is_file():
            return self._json({"error": f"找不到文件：{path.name}"}, 404)
        ctype = CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
        self._send(200, path.read_bytes(), ctype)

    def log_message(self, fmt: str, *args) -> None:      # 静音：别刷终端
        return


def serve(host: str = "127.0.0.1", port: int = 8100) -> ThreadingHTTPServer:
    """起服务（不阻塞）。返回 server 对象，方便测试里 shut down。"""
    httpd = ThreadingHTTPServer((host, port), Handler)
    return httpd


def main(argv: list[str] | None = None) -> int:
    """python -m selfheal.server --port 8100

    然后打开 http://127.0.0.1:8100/
    """
    import argparse

    ap = argparse.ArgumentParser(description="selfheal 本地报告台")
    ap.add_argument("--host", default="127.0.0.1", help="只建议 127.0.0.1")
    ap.add_argument("--port", type=int, default=8100)
    args = ap.parse_args(argv)

    httpd = serve(args.host, args.port)
    host, port = httpd.server_address[0], httpd.server_address[1]
    print(f"报告台已启动：http://{host}:{port}/")
    print(f"报告目录：{REPORT_DIR}")
    print("Ctrl+C 退出")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
