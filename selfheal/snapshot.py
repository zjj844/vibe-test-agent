"""M7 · 后悔药（snapshot.py）

=====================================================================
本模块职责
=====================================================================
在**动任何文件之前**先备份，并记录"为什么备份"；需要时一键还原。

=====================================================================
在整个系统里的位置
=====================================================================
    loop（主循环）→ 【snapshot】→ 文件系统
                      ↑
                 改文件之前必须先走这里

它调用：shutil / json / hashlib（全是标准库）
它被调用：loop

=====================================================================
为什么"让 AI 改代码"必须有后悔药
=====================================================================
AI 改代码是不可预测的：
  - 它可能把 3 个用例改成 5 个；
  - 它可能把 `assert x == 200` 改成 `assert x != 500`（**把断言改弱**）；
  - 它可能顺手删掉一个你不想要它动的文件。

没有快照，你只有"改坏了但不知道坏了什么"这一种结局。
有快照，最坏情况是 `restore()` 一键回到上一版，**试错成本归零**。

=====================================================================
⚠️ 诚实说明（MVP 的边界）
=====================================================================
本 MVP 的自愈方式是"**整批重新生成**"，不是"就地改文件"。
所以这里的快照主要作用是：
  1. 给每一轮的产物留一个**可回滚的批次状态**；
  2. 记录"这一轮为什么改"，事后能复盘。

真正需要"改前快照"的是 v2 的"就地修文件"模式——那时候这个模块就是关键防线。
现在它已经就位，且被 loop 每轮调用。
=====================================================================
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

# 快照仓库放在仓库根目录下的 .snapshots/（已在 .gitignore 里）
ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_ROOT = ROOT / ".snapshots"
MANIFEST = "manifest.json"


@dataclass(frozen=True)
class Snapshot:
    """一次快照。"""

    dir: Path                 # 快照目录
    label: str                # 简短标签（会出现在目录名里）
    reason: str = ""          # 为什么要备份（写给人看）
    created_at: str = ""
    files: tuple[str, ...] = ()   # 备份了哪些文件（相对仓库根的路径）

    def summary(self) -> str:
        return f"{self.label}（{len(self.files)} 个文件）→ {self.dir}"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()[:16]


def _rel(path: Path) -> str:
    """统一用"相对仓库根"的路径记录，避免换机器后还原失败。"""
    try:
        return str(path.resolve().relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


def take(paths, label: str = "", reason: str = "") -> Snapshot:
    """把一批文件备份到 `.snapshots/<时间戳>-<label>/`。

    只备份**存在的普通文件**：目录、不存在的路径会被跳过（并如实记录）。
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in (label or "auto"))[:24]
    dest = SNAPSHOT_ROOT / f"{stamp}-{safe}"
    dest.mkdir(parents=True, exist_ok=True)

    entries: list[dict] = []
    for raw in paths:
        src = Path(raw)
        if not src.is_file():
            continue
        rel = _rel(src)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        entries.append({"path": rel, "sha256": _sha256(src), "size": src.stat().st_size})

    manifest = {
        "label": label or "auto",
        "reason": reason,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "files": entries,
    }
    (dest / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    return Snapshot(
        dir=dest, label=manifest["label"], reason=reason,
        created_at=manifest["created_at"], files=tuple(e["path"] for e in entries),
    )


def load(snapshot_dir: Path | str) -> Snapshot:
    """读回一个已有快照。"""
    d = Path(snapshot_dir)
    manifest = json.loads((d / MANIFEST).read_text(encoding="utf-8-sig"))
    return Snapshot(
        dir=d, label=manifest.get("label", ""), reason=manifest.get("reason", ""),
        created_at=manifest.get("created_at", ""),
        files=tuple(e["path"] for e in manifest.get("files", [])),
    )


def list_snapshots() -> list[Snapshot]:
    """列出所有快照，按时间从新到旧。"""
    if not SNAPSHOT_ROOT.exists():
        return []
    out: list[Snapshot] = []
    for d in sorted(SNAPSHOT_ROOT.iterdir(), reverse=True):
        if d.is_dir() and (d / MANIFEST).exists():
            try:
                out.append(load(d))
            except (OSError, json.JSONDecodeError):
                continue
    return out


def latest() -> Snapshot | None:
    snaps = list_snapshots()
    return snaps[0] if snaps else None


def restore(snapshot: Snapshot) -> list[Path]:
    """把快照里的文件还原回原位置，返回被还原的文件列表。

    注意：**只还原、不删除**。快照里有、但现在目录里多余的其它文件不会被清理
    （因为"删"是不可逆的，宁可少做一步）。
    """
    restored: list[Path] = []
    for rel in snapshot.files:
        src = snapshot.dir / rel
        if not src.is_file():
            continue
        dst = ROOT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        restored.append(dst)
    return restored


def main(argv: list[str] | None = None) -> int:
    """python -m selfheal.snapshot --list         列出所有快照
       python -m selfheal.snapshot --latest       看最新一个
       python -m selfheal.snapshot --restore      还原最新一个
    """
    import argparse
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="快照查看与还原")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--latest", action="store_true")
    parser.add_argument("--restore", action="store_true")
    args = parser.parse_args(argv)

    snaps = list_snapshots()
    if args.list:
        if not snaps:
            print("（还没有任何快照）")
            return 0
        for s in snaps:
            print(f"  {s.created_at}  {s.label:<20} {len(s.files)} 个文件  {s.reason[:40]}")
        return 0

    snap = snaps[0] if snaps else None
    if snap is None:
        print("（还没有任何快照）")
        return 1

    if args.restore:
        files = restore(snap)
        print(f"已还原 {len(files)} 个文件（来自 {snap.label}）")
        for f in files:
            print(f"  ← {_rel(f)}")
        return 0

    print(f"最新快照：{snap.created_at}  {snap.label}")
    print(f"原因：{snap.reason or '（未记录）'}")
    for rel in snap.files:
        print(f"  · {rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
