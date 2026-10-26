"""端到端演示：3 天对话 -> 夜间固化 -> 召回 -> 日记/日志 -> 人格演化 -> 空闲行为。

运行： python demo.py
"""
import os
import time
from datetime import datetime, timedelta

from dsmem import (Store, config_for_preset, rating, journal, personality, prompt, idle,
                   recall, decay, murmurs)
from dsmem.consolidate import nightly
from dsmem.settings import init_memory, interview
from dsmem.config import build_config

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo_memory.db")
PLUGINS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugins")

LINE = "─" * 74


def hr(title):
    print(f"\n{LINE}\n{title}\n{LINE}")


def ts(day, h, m=0):
    return datetime(2026, 10, day, h, m).timestamp()


def say(store, sid, who, text, t):
    store.add_turn(sid, who, text, t)


def main():
    if os.path.exists(DB):
        os.remove(DB)
        for suf in ("-wal", "-shm"):
            if os.path.exists(DB + suf):
                os.remove(DB + suf)

    # ---------- 1. 最初的人为 setting ----------
    hr("① 人为 setting：只表达倾向，不写任何参数")
    needs = interview({"forget": 0.25, "detail": 0.6, "journal": "散文",
                       "personality_strength": 0.85})
    cfg = build_config(needs)
    print(f"输入：忘多快={needs.forget}  细节={needs.detail}  日记={needs.journal}  "
          f"人格强度={needs.personality_strength}  容量=不限")
    print(f"推导：丢弃阈值={cfg.t_discard:.2f}  核心阈值={cfg.t_core:.2f}  "
          f"半衰期基底={cfg.half_life_gain_h:.0f}h  单次召回预算={cfg.recall_budget_tokens}tok")
    out = init_memory(DB, needs, now=ts(24, 8, 0))
    store = Store(DB)
    rub = rating.current_rubric(store)
    print(f"评级体系 v{rub['version']}（由模型生成，非人工填写）：")
    print("  " + "  ".join(f"{rating.DIM_LABEL[k]}={v:.2f}" for k, v in rub["weights"].items()))

    goals = ["做一个可配置记忆曲线的 DeepSeek 记忆插件"]

    # ---------- 2. 三天对话 ----------
    hr("② 三天对话（全部先落 L0 缓冲层）")
    # Day 1
    say(store, "d1-s1", "user", "今天天气还行啊，你吃饭了吗？", ts(24, 9, 10))
    say(store, "d1-s1", "assistant", "还没，我不用吃。天气不错，有事随时说。", ts(24, 9, 11))
    say(store, "d1-s1", "user", "哈哈没事，就是随便问问。", ts(24, 9, 12))
    say(store, "d1-s2", "user", "帮我记一下：下周三我要去杭州出差，记得带合同章和那台旧笔记本。", ts(24, 15, 2))
    say(store, "d1-s2", "assistant", "记下了：下周三杭州出差，合同章、旧笔记本。", ts(24, 15, 3))
    # Day 2
    say(store, "d2-s1", "user", "有件事你可能得记住：我特别讨厌别人在我写代码的时候打断我。", ts(25, 10, 0))
    say(store, "d2-s1", "assistant", "明白，专注时不打扰。", ts(25, 10, 1))
    say(store, "d2-s1", "user", "以前有个助手老在我专注的时候弹提醒，我一被打断就特别烦，效率直接崩，后来我把那个功能关了。以后都别这样，尤其下午。", ts(25, 10, 2))
    say(store, "d2-s1", "assistant", "记住了：下午默认静默，不主动弹提醒。", ts(25, 10, 3))
    say(store, "d2-s2", "user", "中午吃啥好呢？", ts(25, 12, 30))
    say(store, "d2-s2", "assistant", "楼下那家面还可以。", ts(25, 12, 31))
    # Day 3
    say(store, "d3-s1", "user", "我们决定把这个记忆插件做成三层：临时层按天清，情节层留六要素，核心层才存完整对话。你把这个设计记住。", ts(26, 14, 0))
    say(store, "d3-s1", "assistant", "三层：L0 缓冲 / L1 六要素骨架 / L2 完整原话，按评级自动迁移。", ts(26, 14, 1))
    say(store, "d3-s1", "user", "还有，评级别让我手动填权重，让模型自己先给一版，之后按实际使用慢慢调。", ts(26, 14, 3))
    say(store, "d3-s1", "assistant", "评级体系自带版本号，召回行为反向校准权重。", ts(26, 14, 4))
    print(f"L0 中共 {len(store.q('SELECT id FROM buffer'))} 轮原始消息，尚未分层。")

    # ---------- 3. 夜间固化 ----------
    hr("③ 夜间固化：L0 -> 丢弃 / L1 / L2")
    days = [("2026-10-24", ts(24, 23, 30)), ("2026-10-25", ts(25, 23, 30)), ("2026-10-26", ts(26, 23, 30))]
    for day, t in days:
        res = nightly(store, cfg, t, day, user_name="用户", goals=goals)
        print(f"\n[{day}]")
        for s in res["sessions"]:
            tag = {"discard": "丢弃", "l1": "→ L1", "l2": "→ L2"}.get(s["action"], s["action"])
            extra = f"  保留原话(锚点#{s.get('anchor')})" if s.get("anchor") else ""
            print(f"  {s['session']:<7} {tag:<4} 重要度 {s.get('salience', 0):.2f}  "
                  f"({s['reason']}){extra}")
        frags = [dict(r) for r in store.q(
            "SELECT * FROM fragments WHERE day=? ORDER BY salience DESC", (day,))]
        j = journal.write_journal(store, cfg, day, t + 600, frags)
        print(f"  日记 {len(j['body'].encode())}B，提炼 {len(j['logs'])} 条日志")
        print(f"  降级 {res['aged']}  回收 {res['gc']}")

    # ---------- 4. 存储账本 ----------
    hr("④ 空间账本（三层各自多大）")
    st = store.stats()
    for k in ("L0", "L1", "L2", "SELF", "META"):
        print(f"  {k:<5} {st[k]['kb']:>7.1f} KB   {st[k]['rows']:>3} 行")
    print(f"  合计 {st['_total_kb']:.1f} KB / 不限容量")
    disc = store.q1("SELECT SUM(sessions) s, SUM(bytes) b FROM discard_stats")
    print(f"  已丢弃 {disc['s'] or 0} 个低价值会话，相当于省下 {(disc['b'] or 0)/1024:.1f} KB")
    for a in store.q("SELECT id,raw_bytes,comp_bytes FROM anchors"):
        print(f"  锚点#{a['id']} 完整对话压缩 {a['raw_bytes']}B -> {a['comp_bytes']}B "
              f"({a['comp_bytes']/max(1,a['raw_bytes'])*100:.0f}%)")

    # ---------- 5. 召回 ----------
    hr("⑤ 召回：按预算分层读，默认不碰原话")
    queries = [("我讨厌什么？", False), ("杭州出差要带什么", False),
               ("我当时原话怎么说的？关于被打断这件事", False)]
    now3 = ts(26, 23, 40)
    for q, force in queries:
        r = recall.recall(store, cfg, q, now3, force_detail=force)
        print(f"\nQ: {q}")
        print(f"   用了 {r.tokens} tok（预算 {cfg.recall_budget_tokens}），层级 {r.layers}，"
              f"读原话={r.detail_used}")
        for line in r.text.split("\n"):
            print("   " + line)

    # ---------- 6. 评级自校准 ----------
    hr("⑥ 评级自校准：用真实召回行为反向调权重")
    before = rating.current_rubric(store)
    cal = rating.calibrate(store, cfg)
    if cal["changed"]:
        after = rating.current_rubric(store)
        print(f"v{before['version']} -> v{after['version']}（{cal['notes']}）")
        print("  说明：召回过的记忆 = 评级预测正确的记忆；它们的维度均值与全体均值的差，"
              "就是权重该移动的方向。")
        for k in rating.DIMENSIONS:
            d = after["weights"][k] - before["weights"][k]
            if abs(d) > 1e-4:
                print(f"  {rating.DIM_LABEL[k]}：{before['weights'][k]:.3f} -> "
                      f"{after['weights'][k]:.3f}  ({d:+.3f})")
    else:
        print(cal.get("reason"))

    # ---------- 7. 人格 ----------
    hr('⑦ 人格：现在长什么样，以及"一次对话改不动它"')
    print(personality.self_block(store))

    print("\n这三天留下的人格证据（只记录，未生效）：")
    for e in personality.evidence_ledger(store, limit=5):
        d = "+" if e["direction"] > 0 else "-"
        print(f"  {e['day']} {e['trait']:<10} {d} 强度 {e['intensity']:.1f} 情境 {e['context']:.2f}  {e['note']}")
    print("\n夜间积分的结果：")
    for r in personality.integrate(store, cfg, ts(26, 23, 45), "2026-10-26"):
        if r["applied"]:
            print(f"  → {r['trait']:<10} 生效 {r['before']} -> {r['after']}")
        else:
            print(f"  → {r['trait']:<10} {r['status']:<12} 压力 {r['pressure']:+.2f}  "
                  f"（证据 {r['days']} 个日子 / 跨度 {r['span']} 天）{r['reason']}")

    print("\n核心层收到一次性明确要求：")
    d2 = personality.propose_delta(store, cfg, "address_user", "老板",
                                   "用户要求换称呼", "d3-s1", ts(26, 20, 5))
    print(f"  {d2['status']}（挂起等确认，不会自己改）")

    # ---------- 8. 日记与日志 ----------
    hr("⑧ 日记 -> 重点提取 -> 日志")
    for j in journal.journals(store, limit=3):
        print(f"\n[{j['day']}] 心情={j['mood']}  {len(j['body'].encode())}B")
        print("  " + j["body"].replace("\n", "\n  "))
    print("\n日志（召回主入口，比原文便宜一个数量级）：")
    for l in store.q("SELECT day,kind,text,weight FROM logs ORDER BY weight DESC LIMIT 6"):
        print(f"  {l['day']} [{l['kind']:<10}] w={l['weight']:.2f}  {l['text'][:60]}")

    # ---------- 9. 空闲行为 ----------
    hr("⑨ 空闲行为：插件按能力/预算/冷却自行运行")
    reg = idle.load_plugins(store, PLUGINS)
    print(f"已注册插件：{reg}")
    for t in idle.run_idle(store, cfg, ts(26, 23, 50), PLUGINS, max_tasks=3):
        print(f"  {t['plugin']:<24} {t['tokens']:>3}tok  {t['note']}")

    hr("⑨-b 日记与碎碎念：从其它插件的行为里取材")
    acts = store.activities_for("2026-10-26", "main")
    print("今天插件做过的事（行为总线）：")
    for a in acts:
        print(f"  · [{a['kind']:<7}] {a['title']}   ← {a['plugin']}")
    frags = [dict(r) for r in store.q(
        "SELECT * FROM fragments WHERE day=? AND scope='main'", ("2026-10-26",))]
    j = journal.write_journal(store, cfg, "2026-10-26", ts(26, 23, 55), frags)
    print("\n补写日记（会自动带上这些行为）：")
    for line in j["body"].split("\n"):
        print("  " + line)
    ms = murmurs.generate(store, cfg, ts(26, 23, 56), count=2)
    print("\n碎碎念（话题优先取插件行为）：")
    for m in ms:
        print(f"  [{m['tone']}/{m['topic']}] {m['text']}")

    # ---------- 10. 注入成本 ----------
    hr("⑩ 每轮对话的记忆注入成本")
    p = prompt.build_system_prompt(store, cfg, now3, goals)
    print(f"系统提示 {prompt.prompt_cost(store, cfg, now3, goals)} tok / 上限 700 tok")
    print(LINE)
    print(p)
    print(LINE)

    # 校验：超过 30 天后仍在的记忆
    hr("⑪ 时间推移 30 天后的状态（曲线是否真的在忘）")
    later = ts(26, 23, 50) + 30 * 86400
    for f in store.q("SELECT * FROM fragments ORDER BY salience DESC"):
        r = decay.retrievability(f["stability_h"], f["last_review"], later)
        print(f"  #{f['id']} sal={f['salience']:.2f} S={f['stability_h']:>6.0f}h "
              f"R(30d)={r:.2f} state={f['state']}  {f['what'][:34]}")
    # ---------- 12. 人格慢通道 ----------
    hr("⑫ 人格的慢通道：60 天里它到底怎么被改变")

    def show_levels(tag):
        print(f"  {tag:<8}" + "   ".join(
            f"{personality.NUMERIC[k]['label']} {personality.level(store, k):.3f}"
            for k in personality.NUMERIC))

    def drift_of(result):
        for x in result:
            if x["applied"]:
                print(f"    {x['trait']:<10} 生效 {x['before']} -> {x['after']}"
                      f"  （{x['reason']}）")
            else:
                print(f"    {x['trait']:<10} {x['status']:<13} 压力 {x['pressure']:+.2f}  "
                      f"证据 {x['days']} 个日子/跨度 {x['span']} 天  {x['reason']}")

    show_levels("起始")

    print("\n[第 1 天] 一次吐槽 —— 只留证据，不动等级")
    t = datetime(2026, 10, 27, 15, 0)
    say(store, "ev1", "user", "你说话太冷了，一点人味都没有。", t.timestamp())
    drift_of(nightly(store, cfg, t.timestamp() + 3600, "2026-10-27")["drift"])

    print("\n[第 2 天] 改成明确要求 —— 走快车道，但幅度仍受预算约束")
    t2 = datetime(2026, 10, 28, 23, 10)
    say(store, "ev2", "user", "说真的，以后别这么闷，多说点有意思的，我需要点活人感。", t2.timestamp())
    drift_of(nightly(store, cfg, t2.timestamp() + 3600, "2026-10-28")["drift"])

    print("\n[第 3–62 天] 连续深夜长谈：证据持续累积，等级一点点挪")
    d40_pressure = None
    for d in range(3, 63):
        tt = datetime(2026, 10, 29, 23, 0) + timedelta(days=d - 3)
        sid = f"sim{d}"
        if d == 40:
            # 只有一句反话，没有玩笑：看单次负向证据能造成什么
            s40_before = personality.evidence_stats(store, "humor", tt.timestamp() - 60, cfg)["s"]
            say(store, sid, "user", "今天别开玩笑了，我心情不好。", tt.timestamp())
        else:
            say(store, sid, "user", "又加班到现在，脑子有点木，压力大得慌，陪我说两句吧，随便扯点没用的。", tt.timestamp())
            say(store, sid, "assistant", "行，那就扯点没用的。", tt.timestamp() + 60)
            say(store, sid, "user", "哈哈你这话还挺冷的，不过有点好笑，再来一句。", tt.timestamp() + 120)
        if d % 3 == 0 and d >= 6:
            say(store, sid, "user", "说重点，别绕弯子，直接给结论。", tt.timestamp() + 180)
        if d == 40:
            s40_before = personality.evidence_stats(store, "humor", tt.timestamp() + 300, cfg)["s"]
        dr = nightly(store, cfg, tt.timestamp() + 3600, tt.strftime("%Y-%m-%d"))["drift"]
        if d == 40:
            s40_after = personality.evidence_stats(store, "humor", tt.timestamp() + 3600, cfg)["s"]
            d40_pressure = {x["trait"]: x["pressure"] for x in dr}
        if d in (5, 15, 30, 45, 62):
            show_levels(f"d{d}")

    print("\n净变化（相对起始）：")
    for k, meta in personality.NUMERIC.items():
        cur = personality.level(store, k)
        start = personality.START_LEVEL[k]
        cap = cfg.core_window_budget if meta["layer"] == "core" else cfg.evolving_window_budget
        tag = "  ← 已触本季上限，再多相处也要等到下个季度" if cur - start >= cap - 0.002 else ""
        print(f"  {meta['label']:<4} {start:.2f} -> {cur:.3f}   {cur - start:+.3f}"
              f"   （层：{'核心' if meta['layer'] == 'core' else '演化'}，"
              f"惯性 {meta['inertia']}，本季上限 {cap}）{tag}")

    if d40_pressure:
        print("\n第 40 天那次发火（1 条 -1 证据，强度 0.4，深夜情境）：")
        print(f"  幽默的证据压力 S {s40_before:+.2f} -> {s40_after:+.2f}，"
              f"只抵消了约 {(s40_before - s40_after) / 0.25:.1f} 天的积累")
        print("  → 方向没变。单次情绪能压低斜率，但翻不了盘；要翻盘得靠持续的反向相处。")

    print("\n核心层（直接度）的变动记录：")
    rows = personality.history(store, "directness")
    if rows:
        first = rows[-1]
        print(f"  首次生效 {datetime.fromtimestamp(first['ts']).strftime('%Y-%m-%d')}："
              f"{first['d_before']:.3f} -> {first['d_after']:.3f}")
        print(f"  {first['reason']}")
        print(f"  之后每晚微调，累计 {len(rows)} 次，总共只挪了 "
              f"{sum(abs(r['d_after'] - r['d_before']) for r in rows):.3f}")
        print("  → 核心层两个月只动了 0.02：这就是「慢」的量化含义。想让它明显变，得以年为单位。")
    else:
        print("  还没有生效过 —— 核心层要求 ≥8 个不同日子、跨度 ≥45 天、压力 ≥0.5")

    print("\n证据账本（直接度，最近 5 条）：")
    for e in personality.evidence_ledger(store, "directness", limit=5):
        print(f"  {e['day']} {'+' if e['direction'] > 0 else '-'} 强度 {e['intensity']:.1f} "
              f"情境 {e['context']:.2f}  {e['note']}")
    n_ev = store.q1("SELECT COUNT(*) AS n FROM self_evidence")["n"]
    print(f"\n证据总量 {n_ev} 条，占用 {store.stats()['SELF']['kb']} KB")

    store.close()


if __name__ == "__main__":
    t0 = time.time()
    main()
    print(f"\n完成，用时 {time.time()-t0:.2f}s")
