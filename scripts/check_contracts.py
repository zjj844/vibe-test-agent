"""契约自检：把 selfheal/configs/*.json 全部读一遍。

为什么要单独一个脚本（而不是塞在 CI 的 heredoc 里）：
1. 本地也能跑（`check.cmd` 用的是同一份逻辑），"本地绿 = CI 绿"才成立
2. 契约是我们手写的 JSON，写错一个逗号/BOM 就会让整个 target 加载失败，
   而这种错误**只要加载一次就能发现**，不值得等到跑真机才炸
3. 它顺带打印每个项目有几个接口，方便在 CI 日志里一眼看出覆盖范围

用法：python scripts/check_contracts.py
返回码：0 全部可解析；1 有任何一个加载失败
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from selfheal import targets          # noqa: E402  必须在 sys.path 之后导入


def main() -> int:
    configs = sorted((ROOT / "selfheal" / "configs").glob("*.json"))
    if not configs:
        print("没有找到任何契约文件：selfheal/configs/*.json")
        return 1

    failures: list[str] = []
    for path in configs:
        try:
            target = targets.load_target(path)
        except Exception as exc:                          # noqa: BLE001
            failures.append(f"{path.name}: {type(exc).__name__}: {exc}")
            print(f"FAIL {path.name}: {exc}")
            continue
        auth = "需要登录" if target.auth else "无需登录"
        reqs = "有需求文档" if target.requirements_dir else "使用默认需求文档"
        print(
            f"OK   {path.name}: {len(target.endpoints)} 个接口 · "
            f"{target.base_url} · {auth} · {reqs}"
        )

    if failures:
        print(f"\n{len(failures)} 个契约加载失败：")
        for line in failures:
            print("  - " + line)
        return 1

    print(f"\n全部 {len(configs)} 个契约可解析")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
