"""示例插件：只读 L1（片段摘要），不碰 transcript。

宿主约定：
    def run(ctx) -> {"note": str, "tokens": int}
ctx = {store, cfg, now, budget}
"""


def run(ctx):
    store, now = ctx["store"], ctx["now"]
    scope = ctx.get("scope", "main")
    rows = store.q("SELECT * FROM fragments WHERE state!='cold' AND scope=? "
                   "ORDER BY salience DESC LIMIT 3", (scope,))
    if not rows:
        return {"note": "还没有可反思的片段", "tokens": 12}
    pick = rows[0]
    others = len(rows) - 1
    note = (f"重读「{pick['what'][:30]}」，想到：当时它被记为 {pick['salience']:.2f} 级，"
            f"理由是{pick['why']}；另外 {others} 条同类事也许可以归到一起")
    brief = pick["what"] if len(pick["what"]) <= 14 else pick["what"][:14] + "…"
    return {"note": note[:180], "tokens": 90,
            "activities": [{"kind": "reflect",
                            "title": f"重读了「{brief}」，想到它当时的分量",
                            "detail": note[:200], "weight": 0.6}]}
