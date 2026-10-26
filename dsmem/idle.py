"""空闲行为调度器 + 插件接口。

约束：
1. 空闲任务只允许读 L1 许可范围内的数据，默认不给 transcript 权限；
2. 每个插件有 token 预算和冷却时间，跑完写 idle_log，结果进日记草稿，不打扰用户；
3. 插件通过 manifest.json 声明能力，宿主按版本号做兼容检查。
"""
import importlib.util
import json
import time
from difflib import SequenceMatcher
from datetime import datetime
from pathlib import Path
from . import decay, journal
from .config import CurveConfig
from .store import Store, jdumps, jloads

API_VERSION = 1

# 内置任务：不装任何插件也能运行的最小子集
BUILTIN = [
    {"name": "builtin.gardener", "version": "1.0", "capabilities": ["prune"], "tier_access": ["L1"],
     "token_budget": 60, "priority": 9, "cooldown_s": 3600},
    {"name": "builtin.reviewer", "version": "1.0", "capabilities": ["review"], "tier_access": ["L1"],
     "token_budget": 220, "priority": 6, "cooldown_s": 7200},
    {"name": "builtin.murmur", "version": "1.0", "capabilities": ["murmur"], "tier_access": ["L1"],
     "token_budget": 80, "priority": 7, "cooldown_s": 5400},
]


def register(store: Store, manifest: dict):
    store.x(
        "INSERT OR REPLACE INTO plugins(name,version,capabilities,tier_access,token_budget,priority,"
        "cooldown_s,last_run,storage_quota,enabled) VALUES(?,?,?,?,?,?,?,COALESCE((SELECT last_run FROM "
        "plugins WHERE name=?),0),?,1)",
        (manifest["name"], manifest["version"], jdumps(manifest.get("capabilities", [])),
         jdumps(manifest.get("tier_access", ["L1"])), manifest.get("token_budget", 300),
         manifest.get("priority", 5), manifest.get("cooldown_s", 3600), manifest["name"],
         manifest.get("storage_quota", 64 * 1024)))


def load_plugins(store: Store, plugins_dir: str | Path) -> list[dict]:
    out = []
    for m in BUILTIN:
        register(store, m)
        out.append(m["name"])
    d = Path(plugins_dir)
    if d.exists():
        for man in sorted(d.glob("*/manifest.json")):
            try:
                data = json.loads(man.read_text(encoding="utf-8"))
                if int(data.get("api_version", 1)) > API_VERSION:
                    continue
                register(store, data)
                out.append(data["name"])
            except Exception:
                continue
    return out


def _run_builtin(store: Store, cfg: CurveConfig, name: str, now: float, scope: str = "main") -> dict:
    if name == "builtin.gardener":
        st = store.stats()
        gc = store.meta_get("last_gc") or {}
        acts = []
        n = (gc.get("deleted") or 0) + (gc.get("budget_pruned") or 0)
        if n:
            acts.append({"kind": "prune", "title": f"清掉了 {n} 条想不起来的旧记忆",
                         "weight": 0.5})
        return {"note": f"空间巡检：当前 {st['_total_kb']}KB / 不限容量",
                "tokens": 20, "activities": acts}
    if name == "builtin.reviewer":
        # 挑一条最接近遗忘阈值的记忆做"主动回顾"，强化它
        best, best_r = None, 1.01      # 刚建好的片段 R 正好是 1.0，别把它漏掉
        for row in store.q("SELECT * FROM fragments WHERE state!='cold' AND pinned=0 AND scope=?",
                           (scope,)):
            r = decay.retrievability(row["stability_h"], row["last_review"], now)
            if r < best_r:
                best, best_r = row, r
        if not best:
            return {"note": "没有待回顾的记忆", "tokens": 0, "activities": []}
        new_s = decay.reinforce(best["stability_h"], best_r, cfg)
        store.x("UPDATE fragments SET stability_h=?, last_review=?, retrievals=retrievals+1 WHERE id=?",
                (new_s, now, best["id"]))
        return {"note": f"回顾「{best['what'][:24]}」，R={best_r:.2f}，稳定性 {best['stability_h']:.0f}h→{new_s:.0f}h",
                "tokens": 30,
                "activities": [{"kind": "review",
                                "title": f"把「{_brief(best['what'], 14)}」又看了一遍",
                                "weight": 0.45}]}
    if name == "builtin.murmur":
        from . import murmurs
        out = murmurs.generate(store, cfg, now, trigger="idle", scope=scope)
        if not out:
            return {"note": "今天说得够多了，不再碎碎念", "tokens": 0}
        return {"note": f"{len(out)} 条：" + " / ".join(m["text"][:18] for m in out),
                "tokens": 20 + sum(m["length"] for m in out) // 3,
                "activities": []}            # 碎碎念是产出，不是"做过的事"
    return {"note": "无此内置任务", "tokens": 0}


def run_idle(store: Store, cfg: CurveConfig, now: float, plugins_dir: str | Path | None = None,
             max_tasks: int = 2, scope: str = "main") -> list[dict]:
    if plugins_dir:
        load_plugins(store, plugins_dir)
    rows = store.q("SELECT * FROM plugins WHERE enabled=1 ORDER BY priority DESC")
    done = []
    for p in rows:
        if len(done) >= max_tasks:
            break
        if now - (p["last_run"] or 0) < p["cooldown_s"]:
            continue
        budget = min(p["token_budget"], 500)
        if p["name"].startswith("builtin."):
            res = _run_builtin(store, cfg, p["name"], now, scope)
        else:
            res = _run_external(store, cfg, p["name"], now, budget, plugins_dir, scope)
        store.x("UPDATE plugins SET last_run=? WHERE name=?", (now, p["name"]))
        store.x("INSERT INTO idle_log(ts,plugin,task,tokens,note) VALUES(?,?,?,?,?)",
                (now, p["name"], "idle", res.get("tokens", 0), res.get("note", "")))
        res["activities"] = _record(store, now, p["name"], res, scope)
        done.append({"plugin": p["name"], **res})
    return done


def _brief(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + "…"


def _record(store: Store, now: float, plugin: str, res: dict, scope: str) -> list[dict]:
    """把插件产出写进行为总线，供日记和碎碎念取材。"""
    day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    if "activities" in res:                 # 显式给了空列表 = 这次不算"做过事"
        acts = list(res.get("activities") or [])
    else:
        acts = [{"kind": "note", "title": str(res["note"])[:60], "weight": 0.35}] \
            if res.get("note") else []
    out = []
    for a in acts:
        title = str(a.get("title") or "").strip()
        if not title:
            continue
        weight = float(a.get("weight", 0.5))
        # 同一天里两个插件很可能在说同一件事：留权重更高的那条
        dup = None
        for prev in store.activities_for(day, scope, 8):
            if SequenceMatcher(None, title, prev["title"]).ratio() > 0.6:
                dup = prev
                break
        if dup:
            if weight <= (dup["weight"] or 0):
                continue
            store.x("DELETE FROM activities WHERE id=?", (dup["id"],))
        aid = store.x("INSERT INTO activities(ts,day,scope,plugin,kind,title,detail,weight,consumed) "
                      "VALUES(?,?,?,?,?,?,?,?,0)",
                      (now, day, scope, plugin, a.get("kind", "note"), title[:120],
                       str(a.get("detail", ""))[:400], weight))
        out.append({"id": aid, "kind": a.get("kind", "note"), "title": title, "plugin": plugin})
    return out


def today_activities(store: Store, day: str, scope: str = "main", limit: int = 6) -> list[dict]:
    return store.activities_for(day, scope, limit)


def _run_external(store: Store, cfg: CurveConfig, name: str, now: float, budget: int,
                  plugins_dir, scope: str = "main") -> dict:
    hook = Path(plugins_dir) / name.split(".", 1)[-1] / "hook.py"
    if not hook.exists():
        return {"note": "hook 缺失", "tokens": 0}
    try:
        spec = importlib.util.spec_from_file_location(f"dsmem_plugin_{abs(hash(name))}", hook)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        ctx = {"store": store, "cfg": cfg, "now": now, "budget": budget, "scope": scope}
        res = mod.run(ctx) or {}
        note = str(res.get("note", ""))[:200]
        tokens = int(res.get("tokens", budget // 2))
        # 插件产出只进日记草稿，不直接进对话
        store.x("INSERT INTO logs(day,kind,text,weight,refs,tags,created_at) VALUES(?,?,?,?,?,?,?)",
                (time.strftime("%Y-%m-%d", time.localtime(now)), "insight", f"[空闲/{name}] {note}",
                 0.4, "[]", "[]", now))
        return {"note": note, "tokens": tokens, "activities": res.get("activities") or []}
    except Exception as e:
        return {"note": f"插件异常：{type(e).__name__}", "tokens": 0}


def status(store: Store) -> list[dict]:
    return [dict(r) for r in store.q("SELECT name,version,capabilities,enabled,last_run FROM plugins")]
