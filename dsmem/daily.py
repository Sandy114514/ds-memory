"""每日备份：一天一次，完整版 + 当前版各一份。

    完整版  什么都在，用来"回到某一天"
    当前版  只留此刻还记得的 + 此刻的人格，用来搬走或瘦身恢复

三种触发方式，任选其一：
1. 管理界面开着时，后台线程每 10 分钟检查一次（`serve()` 自动启动）；
2. 命令行 `python -m dsmem.daily`，可挂到系统计划任务里，不依赖界面；
3. 界面上的"立即执行今日备份"按钮。

同一天重复调用只会备份一次（除非 force=True）。
"""
import argparse
import json
import threading
import time
from datetime import datetime

from . import backup
from .spaces import SpaceRegistry


def _day(now: float | None = None) -> str:
    return datetime.fromtimestamp(now or time.time()).strftime("%Y-%m-%d")


def last_run(reg: SpaceRegistry) -> str | None:
    return (reg.data.get("settings") or {}).get("last_daily")


def due(reg: SpaceRegistry, now: float | None = None) -> bool:
    return last_run(reg) != _day(now)


def run(reg: SpaceRegistry, now: float | None = None, dest=None, diary_dest=None,
        kinds=backup.KINDS, note: str = "每日自动备份", force: bool = False) -> dict:
    now = now or time.time()
    day = _day(now)
    if not force and last_run(reg) == day:
        return {"ok": True, "skipped": True, "day": day, "reason": "今天已经备份过"}
    made, skipped = {}, []
    for k in kinds:
        d = diary_dest if k == backup.KIND_DIARY else dest
        r = backup.create(reg, d, note=note, kind=k)
        if r.get("deduped"):
            skipped.append({"kind": k, "reason": r.get("reason")})
            continue
        made[k] = {"name": r["name"], "bytes": r["bytes"], "path": r["path"],
                   "spaces": r["spaces"]}
    reg.data.setdefault("settings", {})["last_daily"] = day
    reg._save()
    return {"ok": True, "skipped": False, "day": day, "backups": made,
            "deduped": skipped,                    # 内容没变的那几类，不生成新文件
            "saved": len(made)}


def start(reg: SpaceRegistry, interval: int = 600, stop: threading.Event | None = None,
          quiet: bool = True) -> threading.Thread:
    """后台线程：定期检查，到点就补一份当天的备份。"""
    stop = stop or threading.Event()

    def loop():
        while not stop.is_set():
            try:
                if due(reg):
                    r = run(reg)
                    if not quiet and not r.get("skipped"):
                        print(f"[daily] {r['day']} 备份完成")
            except Exception as e:                    # 备份失败不该拖垮界面
                if not quiet:
                    print(f"[daily] 失败：{type(e).__name__}: {e}")
            stop.wait(interval)

    t = threading.Thread(target=loop, name="dsmem-daily-backup", daemon=True)
    t.start()
    return t


def schedule_hint(root: str | None = None) -> str:
    root = root or str(SpaceRegistry().root)
    return (f"想让它在界面关闭时也每天跑，挂一条计划任务：\n"
            f'  Windows: schtasks /create /tn "dsmem 每日备份" /sc daily /st 03:00 '
            f'/tr "python -m dsmem.daily --root {root}"\n'
            f'  Linux/macOS: 0 3 * * * cd {root} && python -m dsmem.daily --root {root}')


def main(argv=None):
    ap = argparse.ArgumentParser(description="dsmem 每日备份")
    ap.add_argument("--root", default=None)
    ap.add_argument("--dest", default=None, help="记忆备份目录，默认用设置里的位置")
    ap.add_argument("--diary-dest", default=None, help="日记备份目录，默认 diary-backups/")
    ap.add_argument("--kinds", default="full,current", help="full,current 或只写其中一个")
    ap.add_argument("--force", action="store_true", help="今天已经备过也再来一次")
    args = ap.parse_args(argv)

    reg = SpaceRegistry(args.root)
    kinds = tuple(k.strip() for k in args.kinds.split(",") if k.strip() in backup.KINDS)
    out = run(reg, dest=args.dest, diary_dest=args.diary_dest,
              kinds=kinds or backup.KINDS, force=args.force)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
