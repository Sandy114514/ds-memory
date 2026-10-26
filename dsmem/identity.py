"""成员与记忆归属：谁和谁共享记忆。

一条规则：
    用户在某群里  → 该用户的记忆写进这个群的共享池，和群聊共通
    用户不在群里  → 只有自己的私密池

于是"群聊 + 个人用户"可以同处一个分组（同一个人格），但记忆池按归属分流：
    群里所有人 + 群聊本身  →  scope = g:<群 id>
    不在任何群里的用户      →  scope = u:<用户 id>

人格不受 scope 影响 —— 同一个分组里只有一个"我"，被所有渠道的相处共同影响。
"""
import re
import time

from .store import Store

DEFAULT_SCOPE = "main"


def _slug(text: str) -> str:
    base = re.sub(r"[^\w]+", "-", (text or "").strip().lower(), flags=re.UNICODE).strip("-")
    return base or "item"


def add_member(store: Store, kind: str, name: str, ext: str = "", note: str = "",
               private: bool = False, now: float | None = None) -> dict:
    kind = "group" if kind in ("group", "群聊", "g") else "user"
    name = (name or "").strip()
    if not name:
        raise ValueError("名称不能为空")
    mid = f"{'g' if kind == 'group' else 'u'}:{_slug(name)}"
    if store.q1("SELECT 1 FROM members WHERE id=?", (mid,)):
        raise ValueError(f"已存在：{name}")
    now = now or time.time()
    store.x("INSERT INTO members(id,kind,name,ext,note,private,home_group,created_at) "
            "VALUES(?,?,?,?,?,?,NULL,?)",
            (mid, kind, name, ext or "", note, 1 if private else 0, now))
    return {"id": mid, "kind": kind, "name": name, "ext": ext, "private": int(private)}


def list_members(store: Store) -> list[dict]:
    rows = store.q("SELECT * FROM members ORDER BY kind DESC, name")
    out = []
    for r in rows:
        groups = [x["group_id"] for x in
                  store.q("SELECT group_id FROM membership WHERE user_id=? ORDER BY joined_at", (r["id"],))]
        item = {"id": r["id"], "kind": r["kind"], "name": r["name"], "ext": r["ext"],
                "private": r["private"], "home_group": r["home_group"], "groups": groups}
        if r["kind"] == "user":
            sc = resolve(store, r["id"])
            item["scope"] = sc["write"]
            item["shared"] = sc["shared"]
            item["scope_label"] = describe_scope(store, sc["write"])
            item["warnings"] = sc["warnings"]
        else:
            item["scope"] = r["id"]          # 群成员 id 本身就是记忆池 id
            item["shared"] = True
            item["scope_label"] = f"{r['name']}（群共享）"
            item["warnings"] = []
            item["user_count"] = len(users_of(store, r["id"]))
        out.append(item)
    return out


def get_member(store: Store, member_id: str) -> dict | None:
    row = store.q1("SELECT * FROM members WHERE id=?", (member_id,))
    return dict(row) if row else None


def find(store: Store, key: str) -> dict | None:
    """按 id 或外部 id 找成员。"""
    row = store.q1("SELECT * FROM members WHERE id=? OR ext=?", (key, key))
    return dict(row) if row else None


def set_private(store: Store, member_id: str, private: bool) -> dict:
    store.x("UPDATE members SET private=? WHERE id=?", (1 if private else 0, member_id))
    return {"ok": True, "id": member_id, "private": int(bool(private))}


def set_home_group(store: Store, member_id: str, group_id: str | None) -> dict:
    store.x("UPDATE members SET home_group=? WHERE id=?", (group_id or None, member_id))
    return {"ok": True, "id": member_id, "home_group": group_id}


def link(store: Store, group_id: str, user_id: str, now: float | None = None) -> dict:
    g = get_member(store, group_id)
    u = get_member(store, user_id)
    if not g or g["kind"] != "group":
        raise ValueError(f"不是群聊：{group_id}")
    if not u or u["kind"] != "user":
        raise ValueError(f"不是用户：{user_id}")
    store.x("INSERT OR IGNORE INTO membership(group_id,user_id,joined_at) VALUES(?,?,?)",
            (group_id, user_id, now or time.time()))
    return {"ok": True, "group": group_id, "user": user_id}


def unlink(store: Store, group_id: str, user_id: str) -> dict:
    store.x("DELETE FROM membership WHERE group_id=? AND user_id=?", (group_id, user_id))
    return {"ok": True, "group": group_id, "user": user_id}


def groups_of(store: Store, user_id: str) -> list[str]:
    return [r["group_id"] for r in
            store.q("SELECT group_id FROM membership WHERE user_id=? ORDER BY joined_at", (user_id,))]


def users_of(store: Store, group_id: str) -> list[str]:
    return [r["user_id"] for r in
            store.q("SELECT user_id FROM membership WHERE group_id=? ORDER BY joined_at", (group_id,))]


# ---------------------------------------------------------------- 归属解析

def resolve(store: Store, channel: str) -> dict:
    """给一个渠道（群聊或用户）算出它读写的记忆池。"""
    m = find(store, channel)
    warnings = []
    if not m:
        return {"channel": channel, "kind": "unknown", "shared": False,
                "write": f"u:{_slug(channel)}", "read": [f"u:{_slug(channel)}"],
                "group": None, "warnings": ["该渠道未登记，按独立记忆处理"]}

    if m["kind"] == "group":
        sc = m["id"]
        return {"channel": m["id"], "kind": "group", "shared": True, "write": sc, "read": [sc],
                "group": m["id"], "warnings": []}

    groups = groups_of(store, m["id"])
    if m["private"]:
        sc = m["id"]
        return {"channel": m["id"], "kind": "user", "shared": False, "write": sc, "read": [sc],
                "group": None, "warnings": ["已标记为私密：即使在群里也使用独立记忆"]}
    if not groups:
        sc = m["id"]
        return {"channel": m["id"], "kind": "user", "shared": False, "write": sc, "read": [sc],
                "group": None, "warnings": []}

    gid = m["home_group"] if m["home_group"] in groups else groups[0]
    if len(groups) > 1:
        warnings.append(f"同时属于 {len(groups)} 个群，默认用「{gid}」；可在成员设置里指定")
    sc = gid
    return {"channel": m["id"], "kind": "user", "shared": True, "write": sc, "read": [sc],
            "group": gid, "warnings": warnings}


def describe_scope(store: Store, scope: str) -> str:
    m = get_member(store, scope)
    name = m["name"] if m else scope.split(":", 1)[-1]
    if scope.startswith("g:"):
        return f"{name}（群共享）"
    if scope.startswith("u:"):
        return f"{name}（独立）"
    return "默认池"


# ---------------------------------------------------------------- 选择提醒

def advise(store: Store, group_ids: list[str] | None = None,
           user_ids: list[str] | None = None) -> dict:
    """用户同时勾了群聊和用户时的检查与建议。

    核心提示：**优先从所选群聊的成员里选人**，否则这些人只能拿到独立记忆，
    既享受不到群的共通记忆，也让同一场对话在两边各记一份。
    """
    group_ids = group_ids or []
    user_ids = user_ids or []
    warnings, notes, suggest = [], [], []
    in_group: list[str] = []
    for gid in group_ids:
        in_group += users_of(store, gid)
    in_group = list(dict.fromkeys(in_group))
    suggest = in_group

    for uid in user_ids:
        u = get_member(store, uid)
        name = u["name"] if u else uid
        if not group_ids:
            groups = groups_of(store, uid)
            if not groups:
                notes.append(f"「{name}」不在任何群聊里，将使用独立记忆池")
        elif uid in in_group:
            notes.append(f"「{name}」已在所选群聊中，与群共享记忆")
        else:
            warnings.append(
                f"「{name}」不在所选群聊里，只会得到独立记忆。"
                f"建议改选群内成员，或先把「{name}」加入该群。")
    if group_ids and in_group:
        notes.append(f"该群已有 {len(in_group)} 位成员可共享记忆：" +
                     "、".join((get_member(store, x) or {}).get("name", x) for x in in_group))
    return {"ok": not warnings, "warnings": warnings, "notes": notes, "suggest": suggest}
