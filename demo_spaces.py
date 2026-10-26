"""演示：多分组隔离 / 碎碎念画像 / 连续性 / 备份恢复。

    python demo_spaces.py

和 demo.py 的区别：demo.py 讲"一段记忆怎么生老病死"，这个讲"多个分组怎么共存"。
"""
import argparse
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path

import os

from dsmem import archive, backup, continuity, daily, identity, idle, journal, murmurs, personality
from dsmem.config import config_for_preset
from dsmem.spaces import SpaceRegistry

LINE = "─" * 74
PLUGINS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugins")


def hr(t):
    print(f"\n{LINE}\n{t}\n{LINE}")


def set_levels(store, **kv):
    for k, v in kv.items():
        store.x("UPDATE self_model SET level=?, value=? WHERE key=?",
                (v, personality.fmt_level(k, v), k))


def main():
    root = Path(tempfile.mkdtemp(prefix="dsmem-demo-"))
    now = datetime(2026, 10, 26, 23, 0).timestamp()
    cfg = config_for_preset("长期伴侣")

    hr("① 分组：一个分组一个库文件，物理隔离")
    reg = SpaceRegistry(root)
    groups = [
        ("技术群", "group", dict(humor=0.80, closeness=0.75, directness=0.40)),
        ("家人群", "group", dict(humor=0.30, closeness=0.90, directness=0.40)),
        ("项目A", "group", dict(humor=0.20, closeness=0.30, directness=0.85)),
    ]
    for name, kind, lv in groups:
        info = reg.create(name, kind)
        st = reg.open(info["id"])
        set_levels(st, **lv)
        st.close()
        print(f"  {name:<5} → {info['id']:<14} {reg.db_path(info['id']).name}")
    print(f"  根目录 {root}")
    print(f"  共 {len(reg.list())} 个分组（含默认），各自独立 .db")

    hr("② 群聊 + 个人用户：在群就共通，不在群就分开")
    work = reg.create("归属演示", "group")
    ws = reg.open(work["id"])
    g = identity.add_member(ws, "group", "技术群")
    wang = identity.add_member(ws, "user", "小王")
    li = identity.add_member(ws, "user", "小李")
    identity.link(ws, g["id"], wang["id"])
    for who in (wang, li):
        r = identity.resolve(ws, who["id"])
        print(f"  {who['name']}：{'在群里 → 与群共享' if r['shared'] else '不在群 → 独立记忆'}"
              f"   池 = {r['write']}")
    t = datetime(2026, 10, 20, 21, 0).timestamp()
    ws.add_turn("w1", "user", "有件事你记住：技术群决定不换向量库了，以后都用 SQLite。", t,
                identity.resolve(ws, wang["id"])["write"])
    ws.add_turn("l1", "user", "有件事你记住：我下周要请假回老家，别在群里提。", t,
                identity.resolve(ws, li["id"])["write"])
    from dsmem.consolidate import nightly as _nightly
    from dsmem import recall as _recall
    for who in (wang, li):
        _nightly(ws, cfg, t + 3600, "2026-10-20", scope=identity.resolve(ws, who["id"])["write"])
    q = "向量库还换吗"
    rg = _recall.recall(ws, cfg, q, t + 7200, scopes=[g["id"]])
    rl = _recall.recall(ws, cfg, q, t + 7200, scopes=[li["id"]])
    print(f"  群里问「{q}」：{'想得起来' if 'fragments' in rg.layers else '想不起来'}")
    print(f"  小李问同一句：{'想得起来' if 'fragments' in rl.layers else '想不起来'}"
          f"（他的记忆在自己的池里）")
    adv = identity.advise(ws, [g["id"]], [li["id"]])
    print(f"  选择提醒：{adv['warnings'][0] if adv['warnings'] else '无需提醒'}")
    print(f"  人格：全空间只有一份 —— 幽默 "
          f"{personality.level(ws, 'humor'):.2f}，两个渠道的证据都记进它")
    ws.close()

    hr("②-b 插件行为 -> 日记 / 碎碎念")
    ws2 = reg.open(next(x for x in reg.list() if x["name"] == "归属演示")["id"])
    print("  空闲时插件跑了：")
    for task in idle.run_idle(ws2, cfg, t + 5400, PLUGINS, max_tasks=4, scope=g["id"]):
        print(f"    {task['plugin']:<24} {task['tokens']:>3}tok  {task['note'][:52]}")
    acts = ws2.activities_for("2026-10-20", g["id"])
    print("  行为总线记下：")
    for a in acts:
        print(f"    · [{a['kind']}] {a['title']}   ← {a['plugin']}")
    j = journal.write_journal(ws2, cfg, "2026-10-20", t + 6000, [], scope=g["id"])
    print("  只有插件在干活的一天，日记照样有：")
    for line in j["body"].split("\n"):
        print(f"    {line}")
    m = murmurs.generate(ws2, cfg, t + 6600, count=1, scope=g["id"])
    if m:
        print(f"  碎碎念：{m[0]['text']}   [话题={m[0]['topic']}]")
    ws2.close()

    hr("③ 同一个模型，在不同分组里是不同的性格")
    for name, _, _ in groups:
        info = next(s for s in reg.list() if s["name"] == name)
        st = reg.open(info["id"])
        p = murmurs.profile(st, cfg)
        print(f"  {name:<5} 每天 {p['per_day']:>4} 条 · 每条约 {p['length']:>2} 字 · 口吻 {p['tone']}")
        for m in murmurs.generate(st, cfg, now, count=2, trigger="demo"):
            print(f"          「{m['text']}」")
        st.close()

    hr("③-b 用户手动调节：每天几条、每条多长")
    info = next(s for s in reg.list() if s["name"] == "技术群")
    st = reg.open(info["id"])
    auto = murmurs.profile(st, cfg)
    print(f"  自动（跟人格）：每天 {auto['per_day']} 条 · 每条约 {auto['auto_length']} 字 · {auto['tone']}")
    mine = config_for_preset("长期伴侣")
    mine.murmur_per_day, mine.murmur_length = 5, 60
    p2 = murmurs.profile(st, mine)
    print(f"  手动 5 条 / 60 字上限：每天 {p2['per_day']} 条（硬上限 {p2['daily_cap']}）· "
          f"单条最多 {p2['length']} 字 · 预计 {p2['est_bytes_per_day'] / 1024:.1f} KB/天")
    for m in murmurs.generate(st, mine, now + 60, count=3, trigger="demo"):
        print(f"          ({len(m['text'])}字)「{m['text']}」")
    st.x("DELETE FROM murmurs WHERE day=?", ("2026-10-26",))
    print(f"  设回 0：{murmurs.profile(st, cfg)['per_day_source']} —— 立刻回到人格自动值")
    st.close()

    hr("④ 日记与碎碎念：按日期分节、按月分文件")
    for name, _, _ in groups:
        info = next(s for s in reg.list() if s["name"] == name)
        st = reg.open(info["id"])
        st.x("INSERT OR REPLACE INTO journal(day,body,mood,bytes,created_at) VALUES(?,?,?,?,?)",
             ("2026-10-26", f"{name}今天的日记：只写这一句。", "calm", 40, now))
        archive.sync_space(st, reg.export_dir(info["id"]))
        st.close()
        base = reg.export_dir(info["id"])
        for kind in ("journal", "murmurs"):
            p = base / kind / "2026-10.md"
            if p.exists():
                print(f"  {name:<5} {kind:<8} diary/{info['id']}/{kind}/2026-10.md  "
                      f"{p.stat().st_size}B")

    hr("⑤ 连续性：片段被删，时间线不断")
    info = next(s for s in reg.list() if s["name"] == "技术群")
    st = reg.open(info["id"])
    old = datetime(2026, 3, 12, 20, 0).timestamp()
    st.add_fragment(anchor_id=None, created_at=old, day="2026-03-12", who="用户",
                    what="讨论过用 SQLite 还是 Postgres", whn="2026-03-12", whr="线上",
                    why="技术选型", how="比了运维成本", gist="讨论过用 SQLite 还是 Postgres",
                    salience=0.45, dims="{}", stability_h=1.0, last_review=old,
                    tags='["sqlite","选型"]', entities="[]", state="cold")
    from dsmem.consolidate import nightly
    before = st.q1("SELECT COUNT(*) n FROM fragments")["n"]
    res = nightly(st, cfg, now, "2026-10-26")
    after = st.q1("SELECT COUNT(*) n FROM fragments")["n"]
    print(f"  片段 {before} → {after}，折入章节 {res['gc']['absorbed']} 条")
    for c in continuity.timeline(st):
        print(f"  [{c['period']}] {c['text'][:40]}")
    cov = continuity.coverage(st)
    print(f"  连续性：{cov['months']} 个月有记录，跨度 {cov['span']} 个月，缺口 {cov['gaps'] or '无'}")
    st.close()

    hr("⑥ 备份：完整 / 当前 / 日记 三类，每天各一份，内容重复的不再生成")
    # 先造一点历史，让两种版本的差别可量化
    hist = reg.open(next(x for x in reg.list() if x["name"] == "技术群")["id"])
    for i in range(20):
        d0 = datetime(2026, 9, 1 + i % 28, 20, 0).timestamp()
        hist.add_fragment(anchor_id=None, created_at=d0, day=f"2026-09-{1 + i % 28:02d}",
                          who="用户", what=f"九月里的旧事 {i + 1}", whn="", whr="线上",
                          why="日常", how="口述", gist=f"九月里的旧事 {i + 1}",
                          salience=0.4, dims="{}", stability_h=1.0, last_review=d0,
                          tags="[]", entities="[]", state="cold")
    for i in range(30):
        hist.x("INSERT INTO self_evidence(trait,ts,day,intensity,context,direction,explicit,note,ref) "
               "VALUES(?,?,?,?,?,?,?,?,?)",
               ("humor", now - i * 86400, f"2026-10-{1 + i % 26:02d}", 0.6, 0.6, 1, 0,
                "历史证据", "demo"))
    hist.close()
    dest = root / "backups"
    full = backup.create(reg, dest, note="完整版", kind=backup.KIND_FULL)
    cur = backup.create(reg, dest, note="当前版", kind=backup.KIND_CURRENT)
    print(f"  完整版 {round(full['bytes']/1024,1):>6} KB  {Path(full['path']).name}")
    print(f"  当前版 {round(cur['bytes']/1024,1):>6} KB  {Path(cur['path']).name}")
    dropped = {}
    for sp_id, d in (cur.get("trimmed") or {}).items():
        for k, n in d.items():
            if n:
                dropped[k] = dropped.get(k, 0) + n
    if dropped:
        print("  当前版删掉：", "、".join(f"{k} {n} 行" for k, n in sorted(dropped.items())))
    v = backup.verify(cur["path"])
    print(f"  校验：{'通过' if v['ok'] else '失败'}（{v.get('kind_label')}，比对 {v.get('checked')} 个文件）")
    d1 = daily.run(reg, dest=dest)
    d2 = daily.run(reg, dest=dest)
    print(f"  每日任务：生成 {d1.get('saved', 0)} 份、{len(d1.get('deduped', []))} 类因内容未变跳过；"
          f"再跑一次「{d2.get('reason')}」")
    diary_dir = root / "diary-backups"
    d1 = backup.create(reg, diary_dir, note="日记", kind=backup.KIND_DIARY)
    d2 = backup.create(reg, diary_dir, note="日记", kind=backup.KIND_DIARY)
    print(f"  日记备份 {round(d1['bytes']/1024,1)} KB → {Path(d1['path']).parent.name}/")
    print(f"  再备一次：{'内容没变，复用上一份，不产生新文件' if d2.get('deduped') else '又生成了一份'}")
    print("  归档文件（只追加，GC 与清理都不碰）：")
    for f in archive.files(reg)[:4]:
        print(f"    diary/{f['rel']}  {round(f['bytes']/1024,1)} KB")
    items = backup.list_backups(dest) + backup.list_backups(diary_dir)
    g = backup.group(items)
    print(f"  界面三摞：完整 {len(g['full'])} 份 / 当前 {len(g['current'])} 份 / 日记 {len(g['diary'])} 份")

    hr("⑦ 恢复：先自动备份现状，再覆盖")
    st = reg.open(info["id"])
    st.x("DELETE FROM journal")
    st.close()
    st = reg.open(info["id"])
    print(f"  删掉日记后：journal={st.q1('SELECT COUNT(*) n FROM journal')['n']}")
    st.close()
    r = backup.restore(full["path"], reg, dest)
    st = reg.open(info["id"])
    print(f"  恢复后：journal={st.q1('SELECT COUNT(*) n FROM journal')['n']}")
    st.close()
    print(f"  恢复前自动备份 → {Path(r['pre_backup']).name}")
    print(f"  共恢复 {len(r['restored'])} 个分组")

    hr("⑧ 界面")
    print("  python -m dsmem.webui        # 打开 http://127.0.0.1:8765/")
    print("  页面里可以：切换/新建分组、看总览、看日记与碎碎念、选备份目录、备份、校验、恢复")
    if KEEP:
        print(f"  演示数据保留在：{root}")
        print(f"  用界面打开：python -m dsmem.webui --root \"{root}\"")
    else:
        print(f"  演示数据已清理：{root}（加 --keep 可保留，便于用界面查看）")
        shutil.rmtree(root, ignore_errors=True)


KEEP = False

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="多分组演示")
    ap.add_argument("--keep", action="store_true", help="保留演示数据，方便用界面查看")
    KEEP = ap.parse_args().keep
    t = time.time()
    main()
    print(f"\n完成，用时 {time.time() - t:.2f}s")
