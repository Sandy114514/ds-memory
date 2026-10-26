"""夜间固化：L0 -> discard / L1 / L2，并维护标签索引、人格证据与连续性章节。"""
from datetime import datetime

from . import continuity, decay, personality, rating
from .config import CurveConfig
from .store import Store, jdumps, jloads


def known_entities(store: Store) -> set[str]:
    s = set()
    for row in store.q("SELECT entities FROM fragments"):
        s.update(jloads(row["entities"], []))
    return s


def consolidate_session(store: Store, cfg: CurveConfig, sid: str, now: float,
                        day: str, user_name: str = "用户", goals: list[str] | None = None,
                        scope: str = "main") -> dict:
    turns = store.session_turns(sid, scope)
    if not turns:
        return {"session": sid, "action": "empty"}
    goals = goals or []
    rub = rating.current_rubric(store)
    dims, terms = rating.extract_signals(turns, known_entities(store), goals)
    sal, floor_note = rating.finalize(dims, rub["weights"])
    reason = rating.explain(dims, rub["weights"]) + (f"；{floor_note}" if floor_note else "")
    tier = decay.decide_tier(sal, cfg)
    n_chars = sum(len(t["content"]) for t in turns)

    # 无论这段对话被丢弃还是保留，它对人格的证据都被计入
    evs = personality.observe_session(store, cfg, turns, day, now, ref=sid)

    if tier == "discard":
        store.x("INSERT OR REPLACE INTO discard_stats(day,sessions,fragments,bytes) "
                "VALUES(?,COALESCE((SELECT sessions FROM discard_stats WHERE day=?),0)+1,"
                "COALESCE((SELECT fragments FROM discard_stats WHERE day=?),0),"
                "COALESCE((SELECT bytes FROM discard_stats WHERE day=?),0)+?)",
                (day, day, day, day, n_chars))
        store.drop_session(sid, scope)
        return {"session": sid, "action": "discard", "salience": sal, "reason": reason,
                "chars": n_chars, "evidence": evs}

    el = rating.six_elements(turns, day, user_name)
    anchor_id = None
    if tier == "l2":
        anchor_id = store.add_anchor(
            day=day, created_at=now, title=el["what"], salience=sal, dims=dims, reason=reason,
            tags=terms[:8], entities=terms[:12], transcript=turns, scope=scope)

    stab = decay.initial_stability(sal, cfg, core=(tier == "l2"))
    fid = store.add_fragment(
        anchor_id=anchor_id, created_at=now, day=day,
        who=el["who"], what=el["what"], whn=el["whn"], whr=el["whr"], why=el["why"], how=el["how"],
        gist=el["gist"], salience=sal, dims=jdumps(dims), stability_h=stab,
        last_review=now, tags=jdumps(terms[:8]), entities=jdumps(terms[:12]), state="full",
        scope=scope)
    store.drop_session(sid, scope)
    return {"session": sid, "action": tier, "salience": sal, "reason": reason,
            "fragment": fid, "anchor": anchor_id, "chars": n_chars,
            "stability_h": round(stab, 1), "evidence": evs}


def nightly(store: Store, cfg: CurveConfig, now: float, day: str,
            user_name: str = "用户", goals: list[str] | None = None,
            scope: str = "main") -> dict:
    results = [consolidate_session(store, cfg, sid, now, day, user_name, goals, scope)
               for sid in store.sessions_pending(until=now, scope=scope)]
    store.purge_expired(now, cfg.l0_ttl_h)
    aged = age_fragments(store, cfg, now)
    gc = garbage_collect(store, cfg, now)
    drift = personality.integrate(store, cfg, now, day)
    personality.prune_evidence(store, now)
    return {"sessions": results, "aged": aged, "gc": gc, "drift": drift}


def age_fragments(store: Store, cfg: CurveConfig, now: float) -> dict:
    """按 R 值降级：full -> skeleton -> cold，并侵蚀长期未动的稳定性。"""
    moved = {"skeleton": 0, "cold": 0, "dead": 0}
    for row in store.q("SELECT * FROM fragments"):
        r = decay.retrievability(row["stability_h"], row["last_review"], now)
        age_days = (now - row["created_at"]) / 86400
        nxt = decay.next_state(row["state"], r, cfg, age_days)
        if nxt != row["state"]:
            store.x("UPDATE fragments SET state=?, stability_h=? WHERE id=?",
                    (nxt, decay.decay_stability(row["stability_h"]), row["id"]))
            if nxt in moved:
                moved[nxt] += 1
        elif r < 0.5:
            store.x("UPDATE fragments SET stability_h=? WHERE id=?",
                    (decay.decay_stability(row["stability_h"]), row["id"]))
    for row in store.q("SELECT * FROM anchors WHERE state='hot'"):
        r = decay.retrievability(1e9, row["last_access"] or row["created_at"], now)
        if (now - (row["last_access"] or row["created_at"])) / 86400 > cfg.core_cold_days and r > 0:
            store.x("UPDATE anchors SET state='cold' WHERE id=?", (row["id"],))
    return moved


def garbage_collect(store: Store, cfg: CurveConfig, now: float) -> dict:
    deleted = 0
    absorbed = 0
    for row in store.q("SELECT * FROM fragments"):
        r = decay.retrievability(row["stability_h"], row["last_review"], now)
        age_days = (now - row["created_at"]) / 86400
        if decay.should_delete(row["state"], r, cfg, age_days, row["pinned"]):
            continuity.absorb(store, dict(row), now)     # 先折进章节，再删
            store.x("DELETE FROM fragments WHERE id=?", (row["id"],))
            deleted += 1
            absorbed += 1
    # 没有容量上限：删记忆只有一个原因 —— 遗忘曲线判它该走了。
    # budget_pruned 保留为 0，只为兼容旧库里已有的 last_gc 记录与界面字段。
    st = store.stats()
    pruned = 0
    today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    for sc in {r["scope"] for r in store.q("SELECT DISTINCT scope FROM fragments")}:
        continuity.ensure_month(store, today, now, scope=sc)
    store.meta_set("last_gc", {"deleted": deleted, "budget_pruned": pruned,
                               "at": now, "day": today})
    return {"deleted": deleted, "budget_pruned": pruned, "absorbed": absorbed,
            "total_kb": st["_total_kb"]}
