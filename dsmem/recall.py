"""按预算召回：先自我 -> 再日志 -> 再情节骨架 -> 只有明确要细节才解压原话。

一条硬规则：默认永不读取完整对话。transcript 只在 query 命中"细节意图"
（原话/当时怎么说/逐字/记录）时才解压，且只解压一个锚点的一段。
"""
from dataclasses import dataclass, field
from . import decay, rating
from .config import CurveConfig
from .store import Store, est_tokens, jdumps, jloads

DETAIL_HINTS = ["原话", "当时怎么", "逐字", "完整", "详细说", "记录里", "一字不差", "原文"]


@dataclass
class RecallResult:
    text: str = ""
    tokens: int = 0
    layers: list[str] = field(default_factory=list)
    hit_ids: dict = field(default_factory=dict)
    detail_used: bool = False
    truncated: bool = False

    def as_dict(self):
        return {"text": self.text, "tokens": self.tokens, "layers": self.layers,
                "hit_ids": self.hit_ids, "detail_used": self.detail_used,
                "truncated": self.truncated}


def _terms(q: str) -> list[str]:
    return rating._keyterms(q)


TIME_HINTS = ["那阵", "那段", "那个月", "上个月", "上上个月", "去年", "月初", "月底", "当时"]


def _time_hint(query: str) -> bool:
    import re
    return bool(re.search(r"\d{1,2}\s*月", query)) or any(h in query for h in TIME_HINTS)


def _overlap(terms: list[str], bag: str) -> float:
    if not terms:
        return 0.0
    hits = sum(1 for t in terms if t and t in bag)
    return hits / len(terms)


def _budget_add(buf: list[str], line: str, budget: int) -> tuple[bool, int]:
    used = est_tokens("\n".join(buf))
    need = est_tokens(line)
    if used + need > budget:
        return False, used
    buf.append(line)
    return True, used + need


def recall(store: Store, cfg: CurveConfig, query: str, now: float,
           budget_tokens: int | None = None, force_detail: bool = False,
           scopes: list[str] | None = None) -> RecallResult:
    scopes = list(scopes or ["main"])
    ph = ",".join("?" * len(scopes))
    budget = budget_tokens or cfg.recall_budget_tokens
    terms = _terms(query)
    detail = force_detail or any(h in query for h in DETAIL_HINTS)
    res = RecallResult()
    buf: list[str] = []
    used = 0

    # --- Layer A: 自我（固定开销，最小） ---
    from . import personality
    self_lines = personality.self_block(store).split("\n")
    buf.extend(self_lines[:6])
    used = est_tokens("\n".join(buf))
    res.layers.append("self")

    # --- Layer B: 日志（近 14 天，最便宜的"发生过什么"） ---
    rows = store.q(f"SELECT * FROM logs WHERE scope IN ({ph}) ORDER BY day DESC LIMIT 60", scopes)
    scored = []
    for r in rows:
        ov = _overlap(terms, f"{r['text']}{r['tags']}")
        if ov < 0.04:
            continue
        scored.append((ov * 2.2 + 0.25 * (r["weight"] or 0), r))
    scored.sort(key=lambda x: x[0], reverse=True)
    log_ids = []
    for s, r in scored[:4]:
        ok, used = _budget_add(buf, f"[{r['day']}·{r['kind']}] {r['text']}", budget)
        if not ok:
            res.truncated = True
            break
        log_ids.append(r["id"])
    if log_ids:
        res.layers.append("logs")
        res.hit_ids["logs"] = log_ids

    # --- Layer C: 情节骨架（六要素摘要，不含原文） ---
    frags = store.q(f"SELECT * FROM fragments WHERE state!='cold' AND scope IN ({ph})", scopes)
    fscored = []
    for f in frags:
        bag = f"{f['what']} {f['gist']} {f['tags']} {f['entities']} {f['why']}"
        ov = _overlap(terms, bag)
        if ov <= 0:
            continue
        r = decay.retrievability(f["stability_h"], f["last_review"], now)
        fscored.append((ov * 2.0 + f["salience"] * 0.8 + r * 0.4, f))
    fscored.sort(key=lambda x: x[0], reverse=True)
    fids = []
    for s, f in fscored[:4]:
        w = f["what"].rstrip("…")
        head = f["gist"] if (w and w in f["gist"]) else f"{w}｜{f['gist']}"
        line = f"[{f['day']}] {head}（{f['whn']}｜{f['why']}）"
        ok, used = _budget_add(buf, line, budget)
        if not ok:
            res.truncated = True
            break
        fids.append(f["id"])
    if fids:
        res.layers.append("fragments")
        res.hit_ids["fragments"] = fids
        _touch(store, cfg, fids, now)

    # --- Layer C2: 月度章节（连续性兜底：细节没了也要答得出"那段时间"） ---
    picked = []
    if not fids or _time_hint(query):
        for c in store.q(f"SELECT * FROM chapters WHERE scope IN ({ph}) "
                         f"ORDER BY period DESC LIMIT 6", scopes):
            if fids and _overlap(terms, f"{c['text']}{c['tags']}") <= 0.05:
                continue
            ok, used = _budget_add(buf, f"[{c['period']}·月记] {(c['text'] or '')[:90]}", budget)
            if not ok:
                res.truncated = True
                break
            picked.append(c["period"])
            if len(picked) >= 2:
                break
    if picked:
        res.layers.append("chapters")
        res.hit_ids["chapters"] = picked

    # --- Layer D: 原话（唯一会解压 transcript 的地方） ---
    if detail:
        cands = [f for s, f in fscored if f["anchor_id"]][:1]
        if cands:
            f = cands[0]
            turns = store.read_transcript(f["anchor_id"], now)
            snippet = " / ".join(f"{t['role']}:{t['content'][:70]}" for t in turns[:4])
            line = f"（原话[{f['day']}]）{snippet}"
            ok, used = _budget_add(buf, line, budget + 200)
            if ok:
                res.layers.append("transcript")
                res.detail_used = True
                res.hit_ids["anchor"] = f["anchor_id"]
                store.x("UPDATE fragments SET retrievals=retrievals+1 WHERE id=?", (f["id"],))
            else:
                res.truncated = True

    res.text = "\n".join(buf)
    res.tokens = est_tokens(res.text)
    store.x("INSERT INTO recall_receipts(ts,query,tokens,layers,hit_ids) VALUES(?,?,?,?,?)",
            (now, query[:120], res.tokens, jdumps(res.layers), jdumps(res.hit_ids)))
    return res


def _touch(store: Store, cfg: CurveConfig, ids: list[int], now: float):
    """召回即强化（间隔效应）：R 越低被想起，稳定性涨得越多。"""
    for fid in ids:
        row = store.q1("SELECT stability_h,last_review FROM fragments WHERE id=?", (fid,))
        if not row:
            continue
        r = decay.retrievability(row["stability_h"], row["last_review"], now)
        new_s = decay.reinforce(row["stability_h"], r, cfg)
        store.x("UPDATE fragments SET stability_h=?, last_review=?, retrievals=retrievals+1, "
                "state=CASE WHEN state='cold' THEN 'skeleton' ELSE state END WHERE id=?",
                (new_s, now, fid))


def receipts(store: Store, limit: int = 10) -> list[dict]:
    return [dict(r) for r in store.q("SELECT * FROM recall_receipts ORDER BY id DESC LIMIT ?", (limit,))]
