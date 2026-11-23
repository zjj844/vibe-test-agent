"""把外部规范文档抓到本地语料目录（演示"外部资料 + 向量检索"用）。

来源：
  1. 阿里巴巴《Java 开发手册》p3c 的 gitbook markdown（走 jsDelivr CDN，GitHub 直连超时）
  2. 阿里云帮助中心 OpenAPI 规范页（help.aliyun.com 的 .md 版本是纯 markdown，好抓）

只保留抓成功的；抓不到就跳过 —— **不编造内容**。
超时给得短（单个 8 秒）：抓不动就跳过，别让整个脚本卡住。
"""
from __future__ import annotations

import sys
import urllib.parse
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "corpus_external"
HDR = {"User-Agent": "Mozilla/5.0 (selfheal corpus fetcher)"}
TIMEOUT = (3.5, 8.0)

P3C_CDNS = [
    "https://fastly.jsdelivr.net/gh/alibaba/p3c@master/",
    "https://gcore.jsdelivr.net/gh/alibaba/p3c@master/",
]

P3C_FILES = [
    "编程规约/命名风格.md",
    "编程规约/常量定义.md",
    "编程规约/代码格式.md",
    "编程规约/OOP规约.md",
    "编程规约/集合处理.md",
    "编程规约/并发处理.md",
    "编程规约/控制语句.md",
    "编程规约/注释规约.md",
    "编程规约/其它.md",
    "异常日志/异常处理.md",
    "异常日志/日志规约.md",
    "单元测试/单元测试.md",
    "安全规约/安全规约.md",
    "MySQL数据库/建表规约.md",
    "MySQL数据库/索引规约.md",
    "MySQL数据库/SQL语句.md",
    "MySQL数据库/ORM映射.md",
    "工程结构/应用分层.md",
    "工程结构/二方库依赖.md",
    "工程结构/服务器.md",
    "设计规约/设计规约.md",
]

ALIYUN_FILES = [
    "zh/sdk/product-overview/openapi-style.md",
]


def get(url: str) -> str | None:
    try:
        r = requests.get(url, timeout=TIMEOUT, headers=HDR)
    except Exception:                                    # noqa: BLE001
        return None
    if r.status_code != 200:
        return None
    text = r.content.decode("utf-8", errors="replace")
    if len(text) < 400 or text.lstrip().startswith("<!"):
        return None                                      # 太短，或只是 HTML 壳
    return text


def main() -> int:
    session_note: list[str] = []
    saved: list[tuple[str, int]] = []

    for rel in P3C_FILES:
        quoted = urllib.parse.quote(rel)
        text = None
        for cdn in P3C_CDNS:
            text = get(cdn + "p3c-gitbook/" + quoted)
            if text:
                session_note.append(cdn.split("//")[1].split("/")[0])
                break
        if not text:
            print(f"FAIL {rel}", flush=True)
            continue
        dest = OUT / "alibaba-p3c" / Path(rel).name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(
            f"# 来源：阿里巴巴 Java 开发手册 · {Path(rel).name}\n\n{text}", encoding="utf-8"
        )
        saved.append((f"alibaba-p3c/{dest.name}", len(text)))
        print(f"OK   {len(text):>7,}  {rel}", flush=True)

    for rel in ALIYUN_FILES:
        text = get("https://help.aliyun.com/" + rel)
        if not text:
            print(f"FAIL {rel}", flush=True)
            continue
        dest = OUT / "aliyun-openapi" / (Path(rel).stem + ".md")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(f"# 来源：阿里云帮助中心 {rel}\n\n{text}", encoding="utf-8")
        saved.append((f"aliyun-openapi/{dest.name}", len(text)))
        print(f"OK   {len(text):>7,}  {rel}", flush=True)

    total = sum(n for _, n in saved)
    print(f"\n共 {len(saved)} 个文件，合计 {total:,} 字符", flush=True)
    used = sorted(set(session_note))
    if used:
        print("CDN 命中：", ", ".join(used), flush=True)
    return 0 if saved else 1


if __name__ == "__main__":
    sys.exit(main())
