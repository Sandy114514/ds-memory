"""自检：把新增能力全部跑一遍（分组隔离 / 碎碎念画像 / 连续性 / 备份恢复 / 界面 API）。

    python selfcheck.py
"""
import json
import shutil
import tempfile
import zipfile
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from dsmem import archive, backup, continuity, daily, identity, journal, murmurs, personality, webui
from dsmem.config import config_for_preset
from dsmem.spaces import SpaceRegistry
from dsmem.store import Store

OK, FAIL = "PASS", "FAIL"
results = []


def check(name, cond, detail=""):
    results.append((OK if cond else FAIL, name, detail))
    print(f"  [{OK if cond else FAIL}] {name}" + (f"  {detail}" if detail else ""))


def set_levels(store, **kv):
    for k, v in kv.items():
        store.x("UPDATE self_model SET level=?, value=? WHERE key=?",
                (v, personality.fmt_level(k, v), k))


def main():
    root = Path(tempfile.mkdtemp(prefix="dsmem-check-"))
    print(f"\n临时根目录 {root}")
    reg = SpaceRegistry(root)
    cfg = config_for_preset("长期伴侣")
    now = datetime(2026, 10, 26, 23, 0).timestamp()

    # ---------- 1. 分组隔离 ----------
    print("\n① 分组：一空间一文件")
    a = reg.create("技术群", "group")
    b = reg.create("家人群", "group")
    c = reg.create("项目A", "group", copy_persona=False)
    check("创建 3 个分组", len(reg.list()) == 4, f"共 {len(reg.list())} 个（含默认）")
    check("数据库文件物理隔离", len({reg.db_path(s["id"]) for s in reg.list()}) == len(reg.list()))

    sa, sb, sc = reg.open(a["id"]), reg.open(b["id"]), reg.open(c["id"])
    sa.add_turn("x", "user", "技术群里聊了向量库选型", now)
    sb.add_turn("y", "user", "家人群里说明天回家吃饭", now)
    sa.x("INSERT INTO journal(day,body,mood,bytes,created_at) VALUES(?,?,?,?,?)",
         ("2026-10-26", "技术群的日记：今天定了架构。", "focus", 40, now))
    check("A 组看不到 B 组的缓冲", len(sa.q("SELECT * FROM buffer")) == 1
          and len(sb.q("SELECT * FROM buffer")) == 1)
    check("A 组日记不串到 C 组", sa.q1("SELECT COUNT(*) n FROM journal")["n"] == 1
          and sc.q1("SELECT COUNT(*) n FROM journal")["n"] == 0)

    # ---------- 2. 碎碎念：人格决定频率与长度 ----------
    print("\n② 碎碎念：频率/长度随人格变化")
    set_levels(sa, humor=0.80, closeness=0.75, directness=0.40)
    set_levels(sb, humor=0.30, closeness=0.90, directness=0.40)
    set_levels(sc, humor=0.20, closeness=0.30, directness=0.85)
    pa, pb, pc = (murmurs.profile(s, cfg) for s in (sa, sb, sc))
    print(f"    技术群  每天 {pa['per_day']:>4} 条 / {pa['length']:>2} 字 / {pa['tone']}")
    print(f"    家人群  每天 {pb['per_day']:>4} 条 / {pb['length']:>2} 字 / {pb['tone']}")
    print(f"    项目A   每天 {pc['per_day']:>4} 条 / {pc['length']:>2} 字 / {pc['tone']}")
    check("三组口吻不同", len({pa["tone"], pb["tone"], pc["tone"]}) == 3)
    check("高直接度更短", pc["length"] < pb["length"] and pc["length"] <= 18)
    check("高亲密更频繁", pb["per_day"] > pc["per_day"])

    made = []
    for s in (sa, sb, sc):
        made.append(murmurs.generate(s, cfg, now, count=2, trigger="check"))
    for tag, out in zip(("技术群", "家人群", "项目A"), made):
        for m in out:
            print(f"      {tag}：{m['text']}")
    check("每天上限 8 条", all(len(x) == 2 for x in made))
    check("碎碎念落库可分日期查", len(murmurs.by_day(sb, "2026-10")) == 1)

    # ---------- 3. 连续性 ----------
    print("\n③ 连续性：删除前折进月度章节")
    old = (datetime(2026, 3, 12, 20, 0).timestamp())
    fid = sa.add_fragment(anchor_id=None, created_at=old, day="2026-03-12",
                          who="用户", what="讨论过用 SQLite 还是 Postgres",
                          whn="2026-03-12", whr="线上对话", why="技术选型",
                          how="对比了两者的运维成本", gist="讨论过用 SQLite 还是 Postgres",
                          salience=0.45, dims="{}", stability_h=1.0, last_review=old,
                          tags=json.dumps(["sqlite", "选型"]), entities="[]", state="cold")
    from dsmem.consolidate import nightly
    nightly(sa, cfg, now, "2026-10-26")
    gone = sa.q1("SELECT COUNT(*) n FROM fragments WHERE id=?", (fid,))["n"] == 0
    ch = sa.q1("SELECT * FROM chapters WHERE period='2026-03'")
    check("过期片段已删除", gone)
    check("痕迹折进 2026-03 章节", bool(ch and "SQLite" in (ch["text"] or "")),
          (ch["text"] if ch else "")[:46])
    cov = continuity.coverage(sa)
    check("时间线有跨度", cov["span"] >= 6, f"{cov['months']} 个月 / 跨度 {cov['span']}")
    from dsmem import recall as recall_mod
    r = recall_mod.recall(sa, cfg, "3 月那阵子在忙什么", now + 86400)
    has_ch = "chapters" in r.layers
    check("细节没了仍能召回章节", has_ch, f"{r.tokens} tok / {r.layers}")

    # ---------- 3.5 群聊 / 用户 的记忆归属 ----------
    print("\n③-b 群聊与个人用户：在群共享，不在群独立")
    sp = reg.create("归属测试", "group")
    st = reg.open(sp["id"])
    g = identity.add_member(st, "group", "技术群")
    u1 = identity.add_member(st, "user", "小王")
    u2 = identity.add_member(st, "user", "小李")
    identity.link(st, g["id"], u1["id"])
    sc_a = identity.resolve(st, u1["id"])
    sc_b = identity.resolve(st, u2["id"])
    check("群内用户共享群记忆池", sc_a["shared"] and sc_a["write"] == g["id"], sc_a["write"])
    check("群外用户使用独立池", (not sc_b["shared"]) and sc_b["write"] == u2["id"], sc_b["write"])

    t0 = datetime(2026, 10, 20, 21, 0).timestamp()
    st.add_turn("s-wang", "user", "有件事你记住：技术群决定不换向量库了，以后都用 SQLite。", t0,
                sc_a["write"])
    st.add_turn("s-li", "user", "有件事你记住：我下周要请假回老家，别在群里提。", t0, sc_b["write"])
    nightly(st, cfg, t0 + 3600, "2026-10-20", scope=sc_a["write"])
    nightly(st, cfg, t0 + 3600, "2026-10-20", scope=sc_b["write"])
    n_g = st.q1("SELECT COUNT(*) n FROM fragments WHERE scope=?", (sc_a["write"],))["n"]
    n_u = st.q1("SELECT COUNT(*) n FROM fragments WHERE scope=?", (sc_b["write"],))["n"]
    check("两条记忆分别落库", n_g == 1 and n_u == 1, f"群池 {n_g} / 私池 {n_u}")
    r_g = recall_mod.recall(st, cfg, "向量库还换吗", t0 + 7200, scopes=[sc_a["write"]])
    r_u = recall_mod.recall(st, cfg, "向量库还换吗", t0 + 7200, scopes=[sc_b["write"]])
    check("群池查得到、私池查不到", "fragments" in r_g.layers and "fragments" not in r_u.layers,
          f"群 {r_g.layers} / 私 {r_u.layers}")
    r2 = recall_mod.recall(st, cfg, "请假回老家", t0 + 7200, scopes=[sc_a["write"]])
    check("私聊内容不会漏到群里", "fragments" not in r2.layers)

    adv_bad = identity.advise(st, [g["id"]], [u2["id"]])
    adv_ok = identity.advise(st, [g["id"]], [u1["id"]])
    check("选到群外用户会提醒", adv_bad["ok"] is False and adv_bad["warnings"])
    check("选到群内用户不告警", adv_ok["ok"] is True and adv_ok["suggest"])
    identity.set_private(st, u1["id"], True)
    check("私密标记优先于入群", identity.resolve(st, u1["id"])["shared"] is False)
    identity.set_private(st, u1["id"], False)

    personality.observe_session(st, cfg, [{"role": "user", "content": "哈哈这个还挺好笑的"}],
                                "2026-10-20", t0)
    personality.observe_session(st, cfg, [{"role": "user", "content": "说重点，别绕弯子"}],
                                "2026-10-20", t0)
    ev = st.q1("SELECT COUNT(*) n FROM self_evidence")["n"]
    check("人格是空间级：两个渠道的证据进同一张表", ev >= 2, f"证据 {ev} 条")
    check("人格只有一份，不按渠道复制",
          st.q1("SELECT COUNT(*) n FROM self_model WHERE key='humor'")["n"] == 1)

    print("\n③-c 插件行为 -> 日记 / 碎碎念")
    st.x("INSERT INTO activities(ts,day,scope,plugin,kind,title,detail,weight,consumed) "
         "VALUES(?,?,?,?,?,?,?,?,0)",
         (t0, "2026-10-20", sc_b["write"], "builtin.gardener", "prune",
          "清掉了 2 条想不起来的旧记忆", "", 0.5))
    jj = journal.write_journal(st, cfg, "2026-10-20", t0 + 100, [], scope=sc_b["write"])
    check("日记会写入插件的产出", "清掉了 2 条" in jj["body"], jj["body"][:44])
    mm = murmurs.generate(st, cfg, t0 + 200, count=1, scope=sc_b["write"])
    check("碎碎念优先取插件行为作话题", bool(mm) and mm[0]["topic"] == "插件",
          mm[0]["text"] if mm else "")
    jj2 = journal.write_journal(st, cfg, "2026-10-20", t0 + 300, [],
                                scope=sc_b["write"], activities=[])
    check("插件产出可被显式排除", "清掉了 2 条" not in jj2["body"], jj2["body"][:30])
    st.close()

    # ---------- 3.6 碎碎念：条数与字数可调 ----------
    print("\n③-d 碎碎念：数量与容量由用户调")
    ms = Store(":memory:")
    ms.meta_set("last_gc", {"deleted": 0, "budget_pruned": 0})
    mcfg = config_for_preset("长期伴侣")
    p_auto = murmurs.profile(ms, mcfg)
    check("默认跟随人格", p_auto["per_day_source"] == "auto"
          and p_auto["length_source"] == "auto" and p_auto["daily_cap"] == 8,
          f"{p_auto['per_day']} 条 / {p_auto['length']} 字")

    mcfg2 = config_for_preset("长期伴侣")
    mcfg2.murmur_per_day, mcfg2.murmur_length = 4, 50
    p_user = murmurs.profile(ms, mcfg2)
    check("条数可按用户设定", p_user["per_day"] == 4 and p_user["daily_cap"] == 4,
          f"cap={p_user['daily_cap']}")
    check("字数上限可按用户设定", p_user["length"] == 50
          and p_user["length_source"] == "user")
    made = murmurs.generate(ms, mcfg2, 1_700_000_000, count=None)
    check("按设定条数生成", len(made) == 4, f"{len(made)} 条")
    check("每条不超过字数上限", all(len(m["text"]) <= 50 for m in made),
          f"最长 {max(len(m['text']) for m in made)} 字")
    check("当天到量就不再生成", murmurs.generate(ms, mcfg2, 1_700_000_100) == [])
    mcfg3 = config_for_preset("长期伴侣")
    check("设回 0 就恢复自动",
          murmurs.profile(ms, mcfg3)["per_day_source"] == "auto")
    ms.close()

    # ---------- 4. 归档 ----------
    print("\n④ 日记与碎碎念：分开存、只追加、不删")
    out = reg.export_dir(a["id"])
    archive.sync_space(sa, out)
    jf = out / "journal" / "2026-10.md"
    mf = out / "murmurs" / "2026-10.md"
    check("生成日记文件", jf.exists())
    check("生成碎碎念文件", mf.exists(), f"{round(mf.stat().st_size)}B")
    check("两条流分目录", jf.parent != mf.parent)
    before = jf.read_text(encoding="utf-8")
    sa.x("UPDATE journal SET body=? WHERE day=? AND scope=?", ("改过的日记正文", "2026-10-26", "main"))
    archive.sync_space(sa, out)
    after = jf.read_text(encoding="utf-8")
    check("重写同一天不覆盖旧内容", "改过的日记正文" in after and len(after) > len(before))
    n_mur_before = sa.q1("SELECT COUNT(*) n FROM murmurs")["n"]
    n_j_before = sa.q1("SELECT COUNT(*) n FROM journal")["n"]
    nightly(sa, cfg, now + 86400 * 2, "2026-10-28")
    check("清理不碰日记与碎碎念",
          sa.q1("SELECT COUNT(*) n FROM murmurs")["n"] == n_mur_before
          and sa.q1("SELECT COUNT(*) n FROM journal")["n"] == n_j_before)

    # ---------- 5. 备份与恢复 ----------
    print("\n⑤ 备份与恢复")
    dest = root / "my-backups"
    bk = backup.create(reg, dest, note="自检")
    check("备份文件生成", Path(bk["path"]).exists(), f"{round(bk['bytes']/1024,1)} KB")
    v = backup.verify(bk["path"])
    check("sha256 校验通过", v.get("ok") is True, f"校验 {v.get('checked')} 个文件")
    lst = backup.list_backups(dest)
    check("能列出备份", len(lst) == 1 and not lst[0].get("broken"))
    check("备份带版本类型", lst[0].get("kind") == "full", lst[0].get("kind_label"))

    cur = backup.create(reg, dest, note="当前版", kind="current")
    check("当前版备份生成", Path(cur["path"]).exists(),
          f"完整 {round(bk['bytes']/1024,1)} KB / 当前 {round(cur['bytes']/1024,1)} KB")
    groups = backup.group(backup.list_backups(dest))
    check("界面能分成两类", len(groups["full"]) == 1 and len(groups["current"]) == 1)
    with zipfile.ZipFile(cur["path"]) as z:
        tmpdb = Path(tempfile.mkdtemp()) / "c.db"
        tmpdb.write_bytes(z.read(f"spaces/{a['id']}.db"))
    st_c = Store(str(tmpdb))
    check("当前版不留人格证据流水", st_c.q1("SELECT COUNT(*) n FROM self_evidence")["n"] == 0)
    check("当前版保留当前人格", st_c.q1("SELECT COUNT(*) n FROM self_model")["n"] >= 8)
    check("当前版清掉原始缓冲", st_c.q1("SELECT COUNT(*) n FROM buffer")["n"] == 0)
    st_c.close()

    sa.x("DELETE FROM journal")
    check("准备：已清空 A 组日记", sa.q1("SELECT COUNT(*) n FROM journal")["n"] == 0)
    sa.close(); sb.close(); sc.close()          # 真实场景下先关闭，再恢复
    r = backup.restore(bk["path"], reg, dest)
    check("恢复成功", r.get("ok") is True, f"{len(r.get('restored', []))} 个分组")
    check("恢复前自动再备份", Path(r["pre_backup"]).exists())
    sa2 = reg.open(a["id"])
    n_j = sa2.q1("SELECT COUNT(*) n FROM journal")["n"]
    check("日记已找回", n_j == 1, f"journal={n_j} / {reg.db_path(a['id'])}")
    check("分组清单完好", len(reg.list()) == 5)

    ddir = root / "diary-backups"
    d_bk1 = backup.create(reg, ddir, kind="diary")
    d_bk2 = backup.create(reg, ddir, kind="diary")
    check("日记备份单独一份", Path(d_bk1["path"]).exists())
    check("内容没变就不重复生成", d_bk1.get("deduped") is False and d_bk2.get("deduped") is True)
    r1 = daily.run(reg, dest=dest, diary_dest=ddir, note="自检")
    r2 = daily.run(reg, dest=dest, diary_dest=ddir)
    check("每日任务跑三类", r1.get("skipped") is False and "saved" in r1)
    check("日记备份落在独立目录", len(backup.list_backups(ddir)) >= 1)
    check("同一天只自动跑一次", r2.get("skipped") is True, r2.get("reason", ""))

    # ---------- 5.5 旧库迁移 ----------
    print("\n⑤-b 旧库升级：journal/chapters 的唯一键变了")
    oldp = root / "legacy.db"
    import sqlite3 as _sq
    c = _sq.connect(str(oldp))
    c.executescript("""
        CREATE TABLE journal (id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT UNIQUE, body TEXT,
                              mood TEXT, bytes INTEGER, created_at REAL);
        CREATE TABLE chapters (id INTEGER PRIMARY KEY AUTOINCREMENT, period TEXT UNIQUE, text TEXT,
                               tags TEXT, count INTEGER DEFAULT 0, deleted_n INTEGER DEFAULT 0,
                               salience REAL DEFAULT 0, created_at REAL, updated_at REAL);
        INSERT INTO journal(day,body,mood,bytes,created_at) VALUES('2026-01-01','旧日记','calm',9,0);
        INSERT INTO chapters(period,text,tags,count,deleted_n,salience,created_at,updated_at)
               VALUES('2026-01','旧章节','[]',3,0,0.5,0,0);
    """)
    c.commit(); c.close()
    st_old = Store(str(oldp))
    cols = st_old._cols("journal")
    check("迁移补上 scope 列", "scope" in cols)
    check("旧日记没丢", st_old.q1("SELECT COUNT(*) n FROM journal")["n"] == 1)
    check("旧章节没丢", st_old.q1("SELECT COUNT(*) n FROM chapters")["n"] == 1)
    st_old.x("INSERT INTO journal(day,body,mood,bytes,created_at,scope) VALUES('2026-01-01','新','x',1,0,'g:a')")
    check("同一天可以按池各存一份", st_old.q1("SELECT COUNT(*) n FROM journal")["n"] == 2)
    # 老版本把容量上限存在配置里，迁移必须抹掉，否则界面又会显示 4096 KB
    legacy_cfg = json.loads(cfg.to_json())
    legacy_cfg["max_bytes"] = 4194304
    legacy_cfg["space_mb"] = 4.0
    st_old.meta_set("config", json.dumps(legacy_cfg, ensure_ascii=False))
    st_old.close()
    st_old2 = Store(str(oldp))
    raw_cfg = st_old2.meta_get("config")
    raw_cfg = json.loads(raw_cfg) if isinstance(raw_cfg, str) else raw_cfg
    check("旧配置里的容量上限被抹掉",
          "max_bytes" not in raw_cfg and "space_mb" not in raw_cfg,
          f"剩余 {len(raw_cfg)} 个键")
    st_old2.close()

    check("配置对象没有容量字段", not hasattr(cfg, "max_bytes"))
    gc_store = Store(":memory:")
    gc_res = nightly(gc_store, cfg, now, "2026-10-27")
    check("清理不再按容量淘汰", gc_res["gc"].get("budget_pruned", 0) == 0,
          f"deleted={gc_res['gc']['deleted']}")
    gc_store.close()

    # ---------- 6. 界面 API ----------
    print("\n⑥ 管理界面 API")
    api = webui.Api(root)
    st = api.state()
    check("/api/state 返回分组", len(st["spaces"]) >= 4 and st["root"] == str(root))
    ov = api.overview(a["id"])
    check("/api/overview 统计正常", ov["stats"]["murmurs"] >= 2, f"注入 {ov['prompt_tokens']} tok")
    check("界面不再报容量上限", ov["limit_kb"] == 0, f"limit_kb={ov['limit_kb']}")
    ms1 = api.murmur_settings(a["id"], {"per_day": 3, "length": 30})
    check("界面能保存碎碎念设置", ms1["profile"]["per_day"] == 3
          and ms1["profile"]["length"] == 30 and ms1["profile"]["daily_cap"] == 3)
    ms2 = api.murmur_settings(a["id"], {"per_day": 0, "length": 0})
    check("界面设 0 恢复跟随人格", ms2["profile"]["per_day_source"] == "auto")
    mm = api.month(a["id"], "2026-10")
    check("/api/month 返回日记+碎碎念", len(mm["journals"]) >= 1 and len(mm["murmurs"]) >= 1)
    fs = api.fs(str(root))
    check("/api/fs 可列目录", isinstance(fs["dirs"], list) and fs["drives"])

    server = webui.ThreadingHTTPServer(("127.0.0.1", 0),
                                       webui.make_handler(api, quiet=True))
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.3)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3) as resp:
            html = resp.read().decode("utf-8")
        check("首页可访问", "dsmem" in html and "备份与恢复" in html)
        check("页面已去掉「上限 x KB」文案", "上限 '" not in html and "不限容量" in html)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        check("HTTP 接口可用", data["active"] == st["active"])
    finally:
        server.shutdown()

    # ---------- 汇总 ----------
    for s in (sa, sb, sc, sa2):
        try:
            s.close()
        except Exception:
            pass
    bad = [r for r in results if r[0] == FAIL]
    print(f"\n{'='*60}\n{len(results) - len(bad)}/{len(results)} 通过")
    for _, name, detail in bad:
        print(f"  FAIL {name} {detail}")
    shutil.rmtree(root, ignore_errors=True)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
