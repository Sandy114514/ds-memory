"""本地管理界面：分组选择、日记/碎碎念、备份与恢复。

零依赖（http.server），只绑 127.0.0.1。启动：
    python -m dsmem.webui --root ~/.dsmem --port 8765
"""
import argparse
import json
import mimetypes
import os
import string
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import archive, backup, continuity, daily, identity, murmurs, personality, prompt
from .config import PRESETS, build_config, config_for_preset
from .spaces import SpaceRegistry
from .store import Store


class Api:
    def __init__(self, root: str | Path | None = None):
        self.reg = SpaceRegistry(root)

    # ---------- 工具 ----------
    def _open(self, sid: str | None) -> tuple[Store, str]:
        ids = sid or self.reg.active()
        if not self.reg.get(ids):
            raise KeyError(f"分组不存在：{ids}")
        return self.reg.open(ids), ids

    def _cfg(self, store: Store):
        raw = store.meta_get("config")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = None
        if isinstance(raw, dict):
            cfg = config_for_preset("长期伴侣")
            for k, v in raw.items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
            return cfg
        return config_for_preset("长期伴侣")

    # ---------- 渠道与记忆池 ----------
    def default_scope(self, store: Store) -> str:
        g = store.q1("SELECT id FROM members WHERE kind='group' ORDER BY created_at LIMIT 1")
        if g:
            return g["id"]          # 群成员 id 就是记忆池 id
        any_scope = store.q1("SELECT scope FROM fragments ORDER BY id DESC LIMIT 1")
        return any_scope["scope"] if any_scope else "main"

    def channels(self, store: Store, space: str) -> list[dict]:
        out = []
        for m in identity.list_members(store):
            scope = m["scope"]
            out.append({"channel": m["id"], "name": m["name"], "kind": m["kind"],
                        "scope": scope, "shared": m["shared"], "label": m["scope_label"],
                        "warnings": m["warnings"],
                        "counts": {
                            "fragments": store.q1("SELECT COUNT(*) n FROM fragments WHERE scope=?",
                                                  (scope,))["n"],
                            "logs": store.q1("SELECT COUNT(*) n FROM logs WHERE scope=?",
                                             (scope,))["n"],
                            "journals": store.q1("SELECT COUNT(*) n FROM journal WHERE scope=?",
                                                 (scope,))["n"],
                            "murmurs": store.q1("SELECT COUNT(*) n FROM murmurs WHERE scope=?",
                                                (scope,))["n"]}})
        return out

    def members(self, sid: str | None) -> dict:
        store, real = self._open(sid)
        try:
            return {"space": real, "members": identity.list_members(store),
                    "channels": self.channels(store, real), "default_scope": self.default_scope(store)}
        finally:
            store.close()

    def add_member(self, body: dict) -> dict:
        store, real = self._open(body.get("id"))
        try:
            info = identity.add_member(store, body.get("kind", "user"), body.get("name", ""),
                                       body.get("ext", ""), body.get("note", ""),
                                       bool(body.get("private")))
            warn = []
            if info["kind"] == "user" and store.q1("SELECT 1 FROM members WHERE kind='group'"):
                warn.append("该用户暂不属于任何群聊，当前使用独立记忆；"
                            "建议在上方勾选到某个群聊里，和群共享记忆。")
            return {"ok": True, "member": info, "warnings": warn}
        finally:
            store.close()

    # ---------- 状态 ----------
    def state(self) -> dict:
        spaces = self.reg.list(include_archived=True)
        return {"root": str(self.reg.root), "active": self.reg.active(),
                "backup_dir": str(self.reg.backup_dir), "spaces": spaces,
                "bindings": self.reg.data.get("bindings") or {}}

    def overview(self, sid: str | None, channel: str | None = None) -> dict:
        store, real = self._open(sid)
        try:
            cfg = self._cfg(store)
            scope = identity.resolve(store, channel)["write"] if channel else self.default_scope(store)
            st = store.stats()
            logs = [dict(r) for r in store.q(
                "SELECT day,kind,text,weight FROM logs WHERE scope=? ORDER BY id DESC LIMIT 8",
                (scope,))]
            frags = [dict(r) for r in store.q(
                "SELECT id,day,what,salience,stability_h,state FROM fragments WHERE scope=? "
                "ORDER BY salience DESC LIMIT 6", (scope,))]
            prof = murmurs.profile(store, cfg)
            n_frag = store.q1("SELECT COUNT(*) n FROM fragments WHERE scope=?", (scope,))["n"]
            return {"space": real, "name": (self.reg.get(real) or {}).get("name", real),
                    "stats": {"L0": st["L0"]["kb"], "L1": st["L1"]["kb"], "L2": st["L2"]["kb"],
                              "total_kb": st["_total_kb"],
                              "fragments": n_frag,
                              "murmurs": murmurs.count(store, scope)},
                    "logs": logs, "fragments": frags,
                    "timeline": continuity.timeline(store, scope=scope),
                    "coverage": continuity.coverage(store, scope=scope),
                    "profile": prof, "self": personality.self_block(store),
                    "prompt_tokens": prompt.prompt_cost(store, cfg, time.time(), scope=scope),
                    "limit_kb": 0,          # 容量不设上限，字段保留给老前端
                    "scope": scope, "scope_label": identity.describe_scope(store, scope),
                    "activities": [dict(r) for r in store.q(
                        "SELECT * FROM activities WHERE scope=? ORDER BY ts DESC LIMIT 5", (scope,))],
                    "channels": self.channels(store, real),
                    "members": identity.list_members(store)}
        finally:
            store.close()

    def month(self, sid: str | None, month: str, channel: str | None = None) -> dict:
        store, real = self._open(sid)
        try:
            scope = identity.resolve(store, channel)["write"] if channel else self.default_scope(store)
            data = archive.read_month(store, month, scope)
            data["space"] = real
            data["scope_label"] = identity.describe_scope(store, scope)
            return data
        finally:
            store.close()

    def make_murmur(self, sid: str | None, count: int | None = None,
                    channel: str | None = None) -> dict:
        store, real = self._open(sid)
        try:
            cfg = self._cfg(store)
            scope = identity.resolve(store, channel)["write"] if channel else self.default_scope(store)
            out = murmurs.generate(store, cfg, time.time(), count=count, trigger="manual", scope=scope)
            if out:
                archive.export_month(store, self.reg.export_dir(real), out[0]["day"][:7])
            return {"ok": True, "created": out, "profile": murmurs.profile(store, cfg)}
        finally:
            store.close()

    def murmur_settings(self, sid: str | None, body: dict | None = None) -> dict:
        store, real = self._open(sid)
        try:
            cfg = self._cfg(store)
            if body is not None:
                cfg.murmur_per_day = min(30.0, max(0.0, float(body.get("per_day") or 0)))
                cfg.murmur_length = min(200, max(0, int(body.get("length") or 0)))
                store.meta_set("config", cfg.to_json())
            day = datetime.now().strftime("%Y-%m-%d")
            scope = identity.resolve(store, (body or {}).get("channel"))["write"] \
                if (body or {}).get("channel") else self.default_scope(store)
            used = store.q1("SELECT COUNT(*) n FROM murmurs WHERE day=? AND scope=?",
                            (day, scope))["n"]
            prof = murmurs.profile(store, cfg)
            return {"ok": True, "space": real, "scope": scope, "today": used,
                    "left": max(0, prof["daily_cap"] - used), "profile": prof}
        finally:
            store.close()

    def timeline(self, sid: str | None, channel: str | None = None) -> dict:
        store, real = self._open(sid)
        try:
            scope = identity.resolve(store, channel)["write"] if channel else self.default_scope(store)
            return {"space": real, "scope": scope, "timeline": continuity.timeline(store, scope=scope),
                    "coverage": continuity.coverage(store, scope=scope)}
        finally:
            store.close()

    # ---------- 分组 ----------
    def create_space(self, body: dict) -> dict:
        info = self.reg.create(body.get("name", ""), body.get("kind", "group"),
                               body.get("note", ""), copy_persona=body.get("copy_persona", True))
        store = self.reg.open(info["id"])
        try:
            if not store.q1("SELECT 1 FROM rubric LIMIT 1"):
                from . import rating
                from .config import PRESETS as P
                needs = P.get(body.get("preset", "长期伴侣"), P["长期伴侣"])
                rating.save_rubric(store, rating.bootstrap_rubric(needs))
            if not store.q1("SELECT 1 FROM self_model LIMIT 1"):
                personality.seed(store, time.time())
            continuity.ensure_month(store, datetime.now().strftime("%Y-%m-%d"), time.time())
        finally:
            store.close()
        return {"ok": True, "space": info}

    def _member_op(self, body: dict, fn) -> dict:
        store, real = self._open(body.get("id"))
        try:
            if fn is identity.link or fn is identity.unlink:
                r = fn(store, body["group"], body["user"])
            else:
                r = fn(store, body["member"], bool(body.get("private")))
            return {"ok": True, **r, "members": identity.list_members(store)}
        finally:
            store.close()

    def advise(self, body: dict) -> dict:
        store, real = self._open(body.get("id"))
        try:
            return identity.advise(store, body.get("groups") or [], body.get("users") or [])
        finally:
            store.close()

    # ---------- 文件系统与备份 ----------
    def fs(self, path: str | None) -> dict:
        p = Path(path).expanduser() if path else Path.home()
        if not p.exists() or not p.is_dir():
            p = p.parent if p.parent.exists() else Path.home()
        dirs = []
        try:
            for child in sorted(p.iterdir(), key=lambda x: x.name.lower()):
                if child.is_dir() and not child.name.startswith("."):
                    dirs.append({"name": child.name, "path": str(child)})
        except PermissionError:
            pass
        if os.name == "nt":
            drives = [f"{d}:\\" for d in string.ascii_uppercase if Path(f"{d}:\\").exists()]
        else:
            drives = ["/"]                      # POSIX 下根目录就是唯一的"盘"
        return {"path": str(p), "parent": str(p.parent) if p.parent != p else None,
                "dirs": dirs, "drives": drives}

    def do_backup(self, body: dict) -> dict:
        kind = body.get("kind") or backup.KIND_FULL
        dest = body.get("dir") if kind != backup.KIND_DIARY else body.get("diary_dir")
        res = backup.create(self.reg, dest, note=body.get("note", ""),
                            kind=kind, dedupe=bool(body.get("dedupe", True)))
        if kind == backup.KIND_DIARY:
            if body.get("diary_dir"):
                self.reg.set_diary_backup_dir(body["diary_dir"])
        elif body.get("dir"):
            self.reg.set_backup_dir(body["dir"])
        return {"ok": True, "deduped": res.get("deduped", False),
                "kind": res["kind"], "kind_label": res["kind_label"],
                **{k: res[k] for k in ("path", "name", "bytes", "files", "trimmed")
                   if k in res},
                "spaces": len(self.reg.list(include_archived=True))}

    def backups(self) -> dict:
        items = backup.all_items(self.reg)
        g = backup.group(items)
        return {"dir": str(self.reg.backup_dir),
                "diary_dir": str(self.reg.diary_backup_dir),
                "items": items, "groups": g,
                "counts": {k: len(v) for k, v in g.items()},
                "diary_files": archive.files(self.reg),
                "last_daily": daily.last_run(self.reg),
                "due": daily.due(self.reg)}

    def run_daily(self, body: dict) -> dict:
        return daily.run(self.reg, dest=body.get("dir"), diary_dest=body.get("diary_dir"),
                         force=bool(body.get("force")))

    def do_restore(self, body: dict) -> dict:
        return backup.restore(body["file"], self.reg, self.reg.backup_dir)


def make_handler(api: Api, quiet=False):
    class Handler(BaseHTTPRequestHandler):
        server_version = "dsmem-ui/1.0"

        def log_message(self, fmt, *args):
            if not quiet:
                print(f"  {self.address_string()} {fmt % args}")

        def _send(self, code: int, body: bytes, ctype="application/json; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path in ("/", "/index.html"):
                    return self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
                if u.path == "/api/state":
                    return self._json(api.state())
                if u.path == "/api/overview":
                    return self._json(api.overview(q.get("id"), q.get("channel")))
                if u.path == "/api/month":
                    return self._json(api.month(q.get("id"), q.get("month") or
                                                datetime.now().strftime("%Y-%m"), q.get("channel")))
                if u.path == "/api/members":
                    return self._json(api.members(q.get("id")))
                if u.path == "/api/backups":
                    return self._json(api.backups())
                if u.path == "/api/fs":
                    return self._json(api.fs(q.get("path")))
                if u.path == "/api/timeline":
                    return self._json(api.timeline(q.get("id")))
                if u.path == "/favicon.ico":
                    return self._send(204, b"")
                return self._json({"error": "not found"}, 404)
            except KeyError as e:
                return self._json({"error": f"未找到：{e}"}, 404)
            except Exception as e:
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

        def do_POST(self):
            u = urlparse(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                body = {}
            try:
                if u.path == "/api/spaces/create":
                    return self._json(api.create_space(body))
                if u.path == "/api/spaces/active":
                    api.reg.set_active(body["id"])
                    return self._json({"ok": True, "active": api.reg.active()})
                if u.path == "/api/spaces/rename":
                    api.reg.rename(body["id"], body["name"])
                    return self._json({"ok": True})
                if u.path == "/api/spaces/archive":
                    return self._json({"ok": True, **api.reg.archive(body["id"], body.get("archived", True))})
                if u.path == "/api/spaces/delete":
                    return self._json({"ok": True, **api.reg.delete(body["id"])})
                if u.path == "/api/murmur":
                    return self._json(api.make_murmur(body.get("id"), body.get("count"),
                                                      body.get("channel")))
                if u.path == "/api/settings/murmurs":
                    return self._json(api.murmur_settings(body.get("id"), body))
                if u.path == "/api/member/add":
                    return self._json(api.add_member(body))
                if u.path == "/api/member/link":
                    return self._json(api._member_op(body, identity.link))
                if u.path == "/api/member/unlink":
                    return self._json(api._member_op(body, identity.unlink))
                if u.path == "/api/member/private":
                    return self._json(api._member_op(body, identity.set_private))
                if u.path == "/api/advise":
                    return self._json(api.advise(body))
                if u.path == "/api/backup":
                    return self._json(api.do_backup(body))
                if u.path == "/api/backup/daily":
                    return self._json(api.run_daily(body))
                if u.path == "/api/restore":
                    return self._json(api.do_restore(body))
                if u.path == "/api/verify":
                    return self._json(backup.verify(body["file"]))
                if u.path == "/api/bind":
                    return self._json({"ok": True, **api.reg.bind(body["chat_id"], body["space"])})
                if u.path == "/api/unbind":
                    return self._json({"ok": True, **api.reg.unbind(body["chat_id"])})
                if u.path == "/api/settings/backup_dir":
                    p = api.reg.set_backup_dir(body["path"])
                    return self._json({"ok": True, "backup_dir": str(p)})
                if u.path == "/api/settings/diary_dir":
                    p = api.reg.set_diary_backup_dir(body["path"])
                    return self._json({"ok": True, "diary_dir": str(p)})
                return self._json({"error": "not found"}, 404)
            except KeyError as e:
                return self._json({"error": f"缺少参数：{e}"}, 400)
            except Exception as e:
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    return Handler


def serve(root=None, port=8765, open_browser=True, quiet=False, scheduler=True):
    api = Api(root)
    if scheduler:
        daily.start(api.reg, interval=600, quiet=quiet)     # 界面开着就自动每日备份
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(api, quiet))
    url = f"http://127.0.0.1:{port}/"
    print(f"dsmem 管理界面：{url}")
    print(f"数据根目录：{api.reg.root}")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>dsmem · 记忆管理</title>
<style>
:root{--bg:#faf9f6;--card:#fff;--line:#e6e3dc;--tx:#2c2c2a;--sub:#6b6a64;--accent:#185fa5;--warn:#854f0b;--danger:#a32d2d}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.6 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
header{display:flex;align-items:center;gap:16px;padding:14px 24px;background:var(--card);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
h1{font-size:15px;font-weight:500;margin:0}
h2{font-size:14px;font-weight:500;margin:0 0 10px}
.sp{flex:1}
select,input,button{font:inherit;color:inherit;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px 10px;outline:none}
button{cursor:pointer}
button:hover{border-color:#b4b2a9}
button.primary{background:var(--accent);color:#fff;border-color:var(--accent)}
button.ghost{background:transparent}
button.danger{color:var(--danger);border-color:#e8c7c7}
nav{display:flex;gap:6px;padding:10px 24px 0}
nav button{border:none;background:transparent;color:var(--sub);padding:8px 12px;border-radius:8px}
nav button.on{background:#eef2f7;color:var(--accent);font-weight:500}
main{padding:18px 24px 60px;max-width:1180px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.k{font-size:12px;color:var(--sub)}
.v{font-size:24px;font-weight:500;margin-top:2px}
.v small{font-size:12px;color:var(--sub);font-weight:400}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:center}
.two{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:880px){.two{grid-template-columns:1fr}}
ul.plain{list-style:none;margin:0;padding:0}
ul.plain li{padding:8px 0;border-bottom:1px dashed var(--line);display:flex;gap:10px}
ul.plain li:last-child{border-bottom:0}
.tag{font-size:12px;color:var(--sub);white-space:nowrap}
.badge{display:inline-block;font-size:12px;padding:1px 8px;border-radius:999px;background:#eef2f7;color:var(--accent)}
.badge.warn{background:#faeeda;color:var(--warn)}
.badge.g{background:#eaf3de;color:#3b6d11}
.muted{color:var(--sub);font-size:13px}
pre.self{white-space:pre-wrap;margin:0;font:13px/1.7 ui-monospace,Consolas,monospace;color:#3a3a37}
.month{display:flex;justify-content:space-between;padding:10px 0;border-bottom:1px dashed var(--line)}
.murmur{background:#faf9f6;border-radius:8px;padding:8px 10px;margin:6px 0}
.tab{display:none}.tab.on{display:block}
#modal{position:fixed;inset:0;background:rgba(0,0,0,.28);display:none;align-items:center;justify-content:center;z-index:20}
#modal.on{display:flex}
#modal .box{background:#fff;border-radius:12px;width:min(560px,92vw);max-height:78vh;display:flex;flex-direction:column;overflow:hidden}
#modal .box header{position:static;border-bottom:1px solid var(--line);padding:12px 16px}
#modal .list{overflow:auto;padding:8px 8px;flex:1}
#modal .list div{padding:6px 10px;border-radius:6px;cursor:pointer}
#modal .list div:hover{background:#f1efE8}
 .hint{background:#eef2f7;border-left:3px solid #185fa5;padding:8px 10px;border-radius:6px;font-size:13px;color:#3a3a37;margin:0 0 12px}
.mgroup{border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-bottom:10px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.chip{display:flex;align-items:center;gap:6px;border:1px solid var(--line);border-radius:999px;padding:3px 10px;font-size:13px;cursor:pointer;background:#fff}
.chip.on{background:#eaf3de;border-color:#97c459}
.chip input{accent-color:#3b6d11;margin:0}
.mini{font-size:12px;color:var(--sub)}
#toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#2c2c2a;color:#fff;padding:8px 14px;border-radius:8px;font-size:13px;opacity:0;transition:.2s;pointer-events:none}
#toast.on{opacity:1}
</style></head><body>
<header>
  <h1>dsmem</h1>
  <span class="badge" id="rootTag">…</span>
  <div class="sp"></div>
  <label class="muted" for="spaceSel">分组</label>
  <select id="spaceSel" onchange="switchSpace(this.value)"></select>
  <button onclick="openCreate()">新建分组</button>
</header>
<nav id="nav">
  <button class="on" data-tab="ov">总览</button>
  <button data-tab="diary">日记 · 碎碎念</button>
  <button data-tab="backup">备份与恢复</button>
  <button data-tab="about">说明</button>
</nav>
<main>
  <section class="tab on" id="tab-ov">
    <div class="row" style="margin-bottom:12px">
      <label class="muted" for="chSel">记忆池</label>
      <select id="chSel" onchange="setChannel(this.value)"></select>
      <span class="badge" id="scopeTag">…</span>
    </div>
    <div class="grid" id="stats"></div>
    <div class="card" style="margin-top:16px">
      <h2>成员与记忆归属 <span class="muted">同一分组只有一个「我」，记忆按归属分池</span></h2>
      <p class="hint" id="mhint"></p>
      <div id="memb"></div>
      <div class="row" style="margin-top:10px">
        <button onclick="addMember('group')">+ 群聊</button>
        <button onclick="addMember('user')">+ 用户</button>
      </div>
    </div>
    <div class="two" style="margin-top:16px">
      <div class="card"><h2>最近重点</h2><ul class="plain" id="logs"></ul>
        <div id="acts" style="margin-top:10px"></div></div>
      <div class="card"><h2>碎碎念画像 <span class="muted" id="prof"></span></h2>
        <div id="selfbox"><pre class="self" id="self"></pre></div>
      </div>
    </div>
    <div class="card" style="margin-top:16px"><h2>连续性 · 月度章节</h2>
      <div class="muted" id="cov" style="margin-bottom:8px"></div>
      <div id="timeline"></div>
    </div>
  </section>

  <section class="tab" id="tab-diary">
    <div class="row" style="margin-bottom:14px">
      <input type="month" id="month" onchange="loadMonth()">
      <button class="primary" onclick="genMurmur()">生成碎碎念</button>
      <span class="muted" id="mprof"></span>
      <span class="badge" id="mScope">…</span>
    </div>
    <div class="card" style="margin-bottom:14px">
      <h2>碎碎念调节 <span class="muted">留 0 就跟随人格自动</span></h2>
      <div class="row">
        <label class="mini" for="mpd">每天条数</label>
        <input id="mpd" type="number" min="0" max="30" step="0.5" placeholder="0" style="width:96px">
        <label class="mini" for="mlen">单条字数上限</label>
        <input id="mlen" type="number" min="0" max="200" step="5" placeholder="0" style="width:96px">
        <button class="primary" onclick="saveMurmur()">保存</button>
        <button onclick="resetMurmur()">恢复自动</button>
      </div>
      <p class="muted" id="mpreview"></p>
    </div>
    <div class="two">
      <div class="card"><h2>日记</h2><div id="journals"></div></div>
      <div class="card"><h2>碎碎念</h2><div id="murmurs"></div></div>
    </div>
  </section>

  <section class="tab" id="tab-backup">
    <div class="card">
      <h2>备份位置</h2>
      <div class="row" style="margin-bottom:8px">
        <span class="mini" style="width:76px">记忆备份</span>
        <input id="bdir" style="flex:1;min-width:220px">
        <button onclick="browse('bdir')">浏览…</button>
      </div>
      <div class="row">
        <span class="mini" style="width:76px">日记备份</span>
        <input id="ddir" style="flex:1;min-width:220px">
        <button onclick="browse('ddir')">浏览…</button>
      </div>
      <p class="muted" id="dailystate">…</p>
      <div class="row" style="margin-top:6px">
        <button class="primary" onclick="doBackup('full')">备份完整记忆</button>
        <button onclick="doBackup('current')">备份当前记忆</button>
        <button onclick="doBackup('diary')">备份日记</button>
        <button onclick="doDaily()">立即执行今日备份</button>
      </div>
      <p class="muted">完整版不删任何东西；当前版只留此刻还记得的内容与此刻的人格；日记单独一摞、存在另一个目录，只追加不删。
      内容没变的版本不会重复生成，直接复用上一份。</p>
    </div>
    <div class="card" style="margin-top:16px">
      <h2>完整记忆 <span class="muted" id="cntFull"></span></h2>
      <div id="bkFull" class="muted">加载中…</div>
    </div>
    <div class="card" style="margin-top:16px">
      <h2>当前记忆 <span class="muted" id="cntCur"></span></h2>
      <div id="bkCur" class="muted">加载中…</div>
    </div>
    <div class="card" style="margin-top:16px">
      <h2>日记与碎碎念 <span class="muted" id="cntDiary"></span></h2>
      <div class="mini" style="margin-bottom:6px">文本归档（只追加，GC 和备份清理都不碰它）</div>
      <div id="dFiles" class="muted">加载中…</div>
      <div style="margin-top:12px" id="bkDiary" class="muted">加载中…</div>
    </div>
  </section>

  <section class="tab" id="tab-about">
    <div class="card">
      <h2>这套东西怎么存</h2>
      <ul class="plain">
        <li><span class="tag">分组</span><div>一个分组 = 一个独立 SQLite 文件，物理隔离，不可能串记忆。</div></li>
        <li><span class="tag">日记</span><div>每天一条，按月导出 <code>exports/&lt;分组&gt;/journal/2026-10.md</code>。</div></li>
        <li><span class="tag">碎碎念</span><div>一天可多条，按月导出 <code>murmurs/2026-10.md</code>；频率与长度由人格等级算出。</div></li>
        <li><span class="tag">连续性</span><div>片段被删前先折进月度章节，时间线不留空白。</div></li>
        <li><span class="tag">备份</span><div>zip，用 SQLite backup API 取一致快照，恢复前自动再备份一次。</div></li>
      </ul>
    </div>
  </section>
</main>

<div id="modal"><div class="box">
  <header style="display:flex;align-items:center;gap:8px">
    <b id="mtitle" style="font-weight:500">选择目录</b><div class="sp" style="flex:1"></div>
    <button class="ghost" onclick="closeModal()">关闭</button>
  </header>
  <div class="list" id="mlist"></div>
  <div style="padding:10px 16px;border-top:1px solid var(--line);display:flex;gap:8px">
    <span class="muted" id="mpath" style="flex:1;overflow:hidden;text-overflow:ellipsis"></span>
    <button class="primary" onclick="pickDir()">用这个目录</button>
  </div>
</div></div>
<div id="toast"></div>

<script>
const $=s=>document.querySelector(s), $$=s=>document.querySelectorAll(s);
let S={}, CUR=null, PICK=null, CH='', MEM=null;

function toast(t){const el=$('#toast');el.textContent=t;el.className='on';setTimeout(()=>el.className='',1800);}
async function api(path,opt){const r=await fetch(path,opt);const j=await r.json().catch(()=>({error:'返回不是 JSON'}));if(j.error)throw new Error(j.error);return j;}
const post=(p,b)=>api(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});

$$('#nav button').forEach(b=>b.onclick=()=>{
  $$('#nav button').forEach(x=>x.classList.remove('on'));b.classList.add('on');
  $$('.tab').forEach(t=>t.classList.remove('on'));$('#tab-'+b.dataset.tab).classList.add('on');
});

async function boot(){
  S=await api('/api/state'); CUR=S.active;
  $('#rootTag').textContent=S.root;
  $('#bdir').value=S.backup_dir;
  const sel=$('#spaceSel'); sel.innerHTML='';
  S.spaces.forEach(s=>{
    const o=document.createElement('option');
    o.value=s.id; o.textContent=(s.active?'● ':'')+s.name+(s.archived?'（已归档）':'');
    o.selected=s.id===CUR; sel.appendChild(o);
  });
  const n0=new Date(); $('#month').value=n0.getFullYear()+'-'+String(n0.getMonth()+1).padStart(2,'0');
  await loadOverview(); await loadMonth(); await loadBackups();
}
async function switchSpace(id){CUR=id;CH='';await post('/api/spaces/active',{id});S=await api('/api/state');await loadOverview();await loadMonth();}
function setChannel(v){CH=v;loadOverview();loadMonth();}
function chq(){return CH?('&channel='+encodeURIComponent(CH)):'';}

async function loadOverview(){
  const d=await api('/api/overview?id='+encodeURIComponent(CUR)+chq());
  MEM=d; renderChannels(d); renderMembers(d); renderMProf(d.profile);
  $('#scopeTag').textContent=d.scope_label||d.scope;
  $('#mScope').textContent=d.scope_label||d.scope;
  const s=d.stats;
  $('#stats').innerHTML=[
    ['空间占用',s.total_kb+' KB','不限容量'],
    ['L0 缓冲',s.L0+' KB','原始轮次'],
    ['L1 情节',s.L1+' KB','骨架+日志+日记'],
    ['L2 锚点',s.L2+' KB','完整原话'],
    ['碎碎念',s.murmurs+' 条','累计'],
    ['注入成本',d.prompt_tokens+' tok','每轮系统提示']
  ].map(([k,v,x])=>`<div class="card"><div class="k">${k}</div><div class="v">${v}<br><small>${x}</small></div></div>`).join('');
  $('#logs').innerHTML=d.logs.length?d.logs.map(l=>`<li><span class="tag">${l.day}</span><div>${esc(l.text)}</div></li>`).join(''):'<li class="muted">还没有日志</li>';
  $('#acts').innerHTML=(d.activities&&d.activities.length)
    ? `<div class="mini" style="margin-bottom:4px">插件今天做了这些（会写进日记和碎碎念）</div>`+
      d.activities.map(a=>`<div class="mini">· ${esc(a.title)} <span class="tag">${esc(a.plugin)}</span></div>`).join('')
    : '';
  const p=d.profile;
  $('#prof').textContent=`每天约 ${p.per_day} 条 · 每条约 ${p.length} 字 · ${p.tone}`;
  $('#self').textContent=d.self;
  const c=d.coverage;
  $('#cov').innerHTML=`有记录 ${c.months} 个月，跨度 ${c.span} 个月`+(c.gaps.length?` · <span class="badge warn">缺 ${c.gaps.length} 个月</span>`:' · <span class="badge g">连续</span>');
  $('#timeline').innerHTML=d.timeline.length?d.timeline.map(t=>`<div class="month"><div><b style="font-weight:500">${t.period}</b> <span class="muted">${t.journals} 篇日记 · ${t.murmurs} 条碎碎念</span></div><div class="muted">${t.count} 件事${t.deleted?' · 折入 '+t.deleted+' 条已删':''}</div></div>`).join(''):'<div class="muted">还没有章节</div>';
}

async function loadMonth(){
  const n1=new Date(); const m=$('#month').value||(n1.getFullYear()+'-'+String(n1.getMonth()+1).padStart(2,'0'));
  const d=await api('/api/month?id='+encodeURIComponent(CUR)+'&month='+m+chq());
  $('#journals').innerHTML=d.journals.length?d.journals.map(j=>`<div style="margin-bottom:14px"><div class="k">${j.day} · 心情 ${j.mood||'—'}</div><div>${esc(j.body)}</div></div>`).join(''):'<div class="muted">这个月还没有日记</div>';
  const days=Object.keys(d.murmurs).sort().reverse();
  $('#murmurs').innerHTML=days.length?days.map(day=>`<div style="margin-bottom:10px"><div class="k">${day}</div>${d.murmurs[day].map(x=>`<div class="murmur">${esc(x.text)} <span class="tag">${x.tone}</span></div>`).join('')}</div>`).join(''):'<div class="muted">这个月还没有碎碎念</div>';
}
async function genMurmur(){
  const r=await post('/api/murmur',{id:CUR,channel:CH});
  toast(r.created.length?`生成了 ${r.created.length} 条`:`今天已经够多了（上限 ${r.profile.daily_cap} 条，可在上面调）`);
  $('#mprof').textContent=`每天约 ${r.profile.per_day} 条 · 每条约 ${r.profile.length} 字 · ${r.profile.tone}`;
  renderMProf(r.profile);
  await loadMonth(); await loadOverview();
}
function renderMProf(p){
  if(!p) return;
  const pd=$('#mpd'), ml=$('#mlen');
  if(pd && document.activeElement!==pd) pd.value = p.per_day_source==='user' ? p.per_day : 0;
  if(ml && document.activeElement!==ml) ml.value = p.length_source==='user' ? p.length : 0;
  const kb=(p.est_bytes_per_day/1024).toFixed(1);
  $('#mpreview').innerHTML =
    `人格自动值：每天 ${p.auto_per_day} 条 · 每条约 ${p.auto_length} 字。<br>`+
    `当前生效：每天 <b>${p.per_day}</b> 条（硬上限 ${p.daily_cap} 条）· 单条最多 <b>${p.length}</b> 字 · 口吻 ${p.tone}。`+
    `按这个设定，一天大约 ${kb} KB。`;
}
async function saveMurmur(){
  const per=parseFloat($('#mpd').value||0), len=parseInt($('#mlen').value||0);
  const r=await post('/api/settings/murmurs',{id:CUR,per_day:per,length:len,channel:CH});
  renderMProf(r.profile);
  toast(`已保存：每天 ${r.profile.per_day} 条 · 单条最多 ${r.profile.length} 字`);
  await loadOverview();
}
async function resetMurmur(){
  $('#mpd').value=0; $('#mlen').value=0;
  await saveMurmur();
  toast('已恢复为跟随人格');
}
function renderBk(items, label){
  return items.length?items.map(b=>`<div class="month"><div><b style="font-weight:500">${b.name}</b>
    <div class="muted">${b.mtime} · ${b.kb} KB · ${b.spaces??'?'} 个分组${b.note?' · '+esc(b.note):''}${b.broken?' · <span class="badge warn">无法读取</span>':''}</div></div>
    <div class="row"><button onclick="doVerify('${jsq(b.path)}')">校验</button><button class="danger" onclick="doRestore('${jsq(b.path)}','${label}')">恢复</button></div></div>`).join(''):'<div class="muted">还没有这一类的备份</div>';
}
async function loadBackups(){
  const d=await api('/api/backups'); $('#bdir').value=d.dir; $('#ddir').value=d.diary_dir;
  $('#cntFull').textContent=(d.counts.full||0)+' 份';
  $('#cntCur').textContent=(d.counts.current||0)+' 份';
  $('#cntDiary').textContent=(d.counts.diary||0)+' 份';
  $('#dailystate').innerHTML = d.last_daily
    ? `每日自动备份：最近一次 ${d.last_daily} ` + (d.due?'<span class="badge warn">今天还没备</span>':'<span class="badge g">今天已备</span>')
    : '每日自动备份：还没跑过。界面开着时每 10 分钟检查一次。';
  $('#bkFull').innerHTML=renderBk(d.groups.full||[], '完整');
  $('#bkCur').innerHTML=renderBk(d.groups.current||[], '当前');
  $('#bkDiary').innerHTML=renderBk(d.groups.diary||[], '日记');
  $('#dFiles').innerHTML=(d.diary_files&&d.diary_files.length)
    ? d.diary_files.slice(0,12).map(f=>`<div class="mini">· ${esc(f.rel)} <span class="tag">${(f.bytes/1024).toFixed(1)} KB</span></div>`).join('')
    : '<div class="mini">还没有归档文件</div>';
}
async function saveDir(){
  const a=await post('/api/settings/backup_dir',{path:$('#bdir').value});
  const b=await post('/api/settings/diary_dir',{path:$('#ddir').value});
  toast('已保存：记忆 '+a.backup_dir+' ｜ 日记 '+b.diary_dir); await loadBackups();
}
async function doBackup(kind){
  const label={full:'完整记忆',current:'当前记忆',diary:'日记'}[kind]||kind;
  toast('备份'+label+'…');
  const r=await post('/api/backup',{dir:$('#bdir').value,diary_dir:$('#ddir').value,kind});
  toast(r.deduped?('内容没变，复用 '+r.name):('完成：'+r.name));
  await loadBackups();
}
async function doDaily(){
  toast('执行今日备份…');
  const r=await post('/api/backup/daily',{dir:$('#bdir').value,diary_dir:$('#ddir').value});
  if(r.skipped){toast(r.reason);}else{
    const made=Object.keys(r.backups||{}).length;
    const dup=(r.deduped||[]).length;
    toast(`生成 ${made} 份`+(dup?`，${dup} 类内容未变已跳过`:''));
  }
  await loadBackups();
}
async function doVerify(p){const r=await post('/api/verify',{file:p});toast(r.ok?`校验通过（${r.checked} 个文件 · ${r.kind_label||''}）`:'校验失败：'+JSON.stringify(r.bad||r.error));}
async function doRestore(p, label){
  if(!confirm(`恢复「${label||''}记忆」会覆盖现有分组数据库。\n恢复前会自动做一次完整备份。\n继续？`))return;
  toast('恢复中…');const r=await post('/api/restore',{file:p});
  if(!r.ok){toast('失败：'+(r.error||''));return;}
  toast(`已恢复 ${r.restored.length} 个分组（${r.kind_label||''}）`);S=await api('/api/state');boot();
}
function renderChannels(d){
  const sel=$('#chSel'); const cur=CH;
  let h='<option value="">默认（'+ (d.channels[0]?d.channels[0].label:'全部') +'）</option>';
  h+=d.channels.map(c=>`<option value="${esc(c.channel)}" ${cur===c.channel?'selected':''}>${c.kind==='group'?'群':'用户'} · ${esc(c.name)}（${c.shared?'共享':'独立'}）</option>`).join('');
  sel.innerHTML=h;
}
function renderMembers(d){
  const groups=d.members.filter(m=>m.kind==='group');
  const users=d.members.filter(m=>m.kind==='user');
  const loose=users.filter(u=>!u.groups.length);
  $('#mhint').innerHTML=groups.length
    ? `勾选进群的用户会与该群<b>共享记忆</b>；未勾选的用户只有自己的独立记忆。<br>建议：同时选择群聊和用户时，<b>优先从该群的成员里选人</b>，否则同一场对话会被记成两份。`
    : `还没有群聊。先加一个群聊，再把用户勾进去，才能共享记忆。`;
  let h='';
  if(!groups.length && !users.length) h='<div class="muted">还没有成员。点下面按钮添加群聊或用户。</div>';
  h+=groups.map(g=>{
    const inG=users.filter(u=>u.groups.includes(g.id));
    const outG=users.filter(u=>!u.groups.includes(g.id));
    return `<div class="mgroup">
      <div class="row" style="justify-content:space-between">
        <div><b style="font-weight:500">${esc(g.name)}</b>
          <span class="badge g">群共享 · ${inG.length} 人</span>
          <span class="mini">记忆池 ${esc(g.scope)}</span></div>
      </div>
      <div class="chips">${users.length?users.map(u=>`<label class="chip ${u.groups.includes(g.id)?'on':''}" title="${u.private?'该用户已设为私密，勾选也不会共享':''}">
        <input type="checkbox" ${u.groups.includes(g.id)?'checked':''} onchange="toggleLink('${jsq(g.id)}','${jsq(u.id)}',this.checked)">${esc(u.name)}</label>`).join(''):'<span class="mini">还没有用户</span>'}</div>
      ${outG.length?`<div class="mini" style="margin-top:6px">未加入：${outG.map(u=>esc(u.name)).join('、')} —— 他们此刻用的是独立记忆</div>`:''}
    </div>`;
  }).join('');
  if(loose.length){
    h+=`<p class="hint" style="margin:10px 0 6px">这些用户还没有加入任何群聊，只使用独立记忆：${loose.map(u=>esc(u.name)).join('、')}</p>`;
  }
  if(users.length){
    h+=`<div class="mini" style="margin-top:10px">用户记忆池：`+users.map(u=>{
      const tag=u.private?'<span class="badge warn">私密</span>':(u.shared?'<span class="badge g">共享</span>':'<span class="badge warn">独立</span>');
      return `${esc(u.name)} ${tag} <a href="#" onclick="togglePrivate('${jsq(u.id)}',${u.private?0:1});return false" style="color:#185fa5">${u.private?'取消私密':'设为私密'}</a>`;
    }).join(' · ')+`</div>`;
  }
  $('#memb').innerHTML=h;
}
async function toggleLink(gid,uid,on){
  const r=await post(on?'/api/member/link':'/api/member/unlink',{id:CUR,group:gid,user:uid});
  if(r.members) await loadOverview();
  toast(on?'已加入群，与群共享记忆':'已移出群，改为独立记忆');
}
async function togglePrivate(uid,v){
  await post('/api/member/private',{id:CUR,member:uid,private:!!v});
  await loadOverview(); toast(v?'已设为私密：即使入群也只用独立记忆':'已取消私密');
}
async function addMember(kind){
  const name=prompt(kind==='group'?'群聊名称（例如：技术群）':'用户名称（例如：小王）');
  if(!name)return;
  const r=await post('/api/member/add',{id:CUR,kind,name});
  await loadOverview();
  if(r.warnings&&r.warnings.length) toast(r.warnings[0]);
}

let BROWSE_TARGET='bdir';
function browse(target){BROWSE_TARGET=target||'bdir';PICK=null;openModal('选择备份目录');}
function openModal(t){$('#mtitle').textContent=t;$('#modal').classList.add('on');loadFs($('#'+BROWSE_TARGET).value||'');}
function closeModal(){$('#modal').classList.remove('on');}
async function loadFs(p){
  const d=await api('/api/fs?path='+encodeURIComponent(p));PICK=d.path;$('#mpath').textContent=d.path;
  let h='';
  if(d.drives.length)h+=d.drives.map(x=>`<div onclick="loadFs('${jsq(x)}')">${x}</div>`).join('');
  if(d.parent)h+=`<div onclick="loadFs('${jsq(d.parent)}')">↑ 上一级</div>`;
  h+=d.dirs.map(x=>`<div onclick="loadFs('${jsq(x.path)}')">[目录] ${x.name}</div>`).join('');
  $('#mlist').innerHTML=h||'<div class="muted" style="padding:10px">没有子目录</div>';
}
function pickDir(){ $('#'+BROWSE_TARGET).value=PICK; closeModal(); toast('已选择，记得点保存位置'); }
function openCreate(){
  const name=prompt('新分组名称（例如：技术群 / 家人群 / 项目 A）');if(!name)return;
  post('/api/spaces/create',{name}).then(r=>{toast('已创建');return post('/api/spaces/active',{id:r.space.id})}).then(()=>boot()).catch(e=>toast(e.message));
}
function esc(s){return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function jsq(s){return String(s||'').replace(/\\/g,'\\\\').replace(/'/g,"\\'");}
boot().catch(e=>{$('#stats').innerHTML='<div class="card">加载失败：'+esc(e.message)+'</div>';});
</script></body></html>"""


def main(argv=None):
    ap = argparse.ArgumentParser(description="dsmem 管理界面")
    ap.add_argument("--root", default=None, help="数据根目录，默认 ~/.dsmem")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-scheduler", action="store_true", help="关掉每日自动备份线程")
    args = ap.parse_args(argv)
    serve(args.root, args.port, open_browser=not args.no_browser,
          scheduler=not args.no_scheduler)


if __name__ == "__main__":
    main()
