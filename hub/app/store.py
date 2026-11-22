"""对话存储（SQLite）。消息按 OpenAI 格式存，额外的界面信息放在 meta 里。"""
import json
import os
import sqlite3
import threading
import time
import uuid

from .config import DATA_DIR

_DB = os.path.join(DATA_DIR, "hub.db")
_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(_DB, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


_db = _conn()
_db.executescript("""
CREATE TABLE IF NOT EXISTS convs (
  id TEXT PRIMARY KEY, title TEXT, model TEXT, created REAL, updated REAL, archived INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS msgs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conv TEXT, data TEXT, meta TEXT, created REAL);
CREATE INDEX IF NOT EXISTS msgs_conv ON msgs(conv, id);
CREATE TABLE IF NOT EXISTS conv_state (conv TEXT PRIMARY KEY, data TEXT);
""")
# 归档里的分组（例如“测试”）。老数据库没有这一列，补上
if "folder" not in [r[1] for r in _db.execute("PRAGMA table_info(convs)")]:
    _db.execute("ALTER TABLE convs ADD COLUMN folder TEXT DEFAULT ''")
_db.commit()


def get_state(cid):
    """对话的任务状态：原始需求、计划、验收标准、文件检查情况。跨轮次保留。"""
    r = _db.execute("SELECT data FROM conv_state WHERE conv=?", (cid,)).fetchone()
    return json.loads(r[0]) if r else {}


def set_state(cid, state):
    with _lock:
        _db.execute("INSERT OR REPLACE INTO conv_state VALUES (?,?)", (cid, json.dumps(state, ensure_ascii=False)))
        _db.commit()


def mark_interrupted():
    """服务启动时调用：上次没跑完的运行补一条“被中断”记录。返回处理的对话数。"""
    n = 0
    for (cid,) in _db.execute("SELECT id FROM convs WHERE archived=0").fetchall():
        last = _db.execute("SELECT data, meta FROM msgs WHERE conv=? ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
        if not last:
            continue
        d, m = json.loads(last[0]), json.loads(last[1] or "{}")
        unfinished = d["role"] in ("user", "tool") or (d["role"] == "assistant" and d.get("tool_calls"))
        if unfinished:
            add_msg(cid, {"role": "assistant", "content": "（这次运行被中断了：网页服务重启。说“继续”可以接着做。）"}, {"interrupted": True})
            n += 1
    return n


def new_conv(model, title="新对话"):
    cid = uuid.uuid4().hex[:12]
    now = time.time()
    with _lock:
        _db.execute("INSERT INTO convs (id, title, model, created, updated, archived, folder) VALUES (?,?,?,?,?,0,'')", (cid, title, model, now, now))
        _db.commit()
    return get_conv(cid)


def get_conv(cid):
    r = _db.execute("SELECT * FROM convs WHERE id=?", (cid,)).fetchone()
    return dict(r) if r else None


def list_convs(limit=100, archived=0):
    rows = _db.execute("SELECT * FROM convs WHERE archived=? ORDER BY updated DESC LIMIT ?", (archived, limit)).fetchall()
    return [dict(r) for r in rows]


def delete_conv(cid):
    """彻底删除对话：标题、消息、任务状态都删掉。项目文件夹里的文件不动。"""
    with _lock:
        for t, col in (("msgs", "conv"), ("conv_state", "conv"), ("convs", "id")):
            _db.execute(f"DELETE FROM {t} WHERE {col}=?", (cid,))
        _db.commit()


def update_conv(cid, **kw):
    if not kw or not set(kw) <= {"title", "archived", "folder"}:     # 空参数调用用于更新活动时间
        kw["updated"] = time.time()
    keys = ", ".join(f"{k}=?" for k in kw)
    with _lock:
        _db.execute(f"UPDATE convs SET {keys} WHERE id=?", (*kw.values(), cid))
        _db.commit()


def add_msg(cid, data, meta=None):
    with _lock:
        cur = _db.execute("INSERT INTO msgs (conv, data, meta, created) VALUES (?,?,?,?)",
                          (cid, json.dumps(data, ensure_ascii=False), json.dumps(meta or {}, ensure_ascii=False), time.time()))
        _db.execute("UPDATE convs SET updated=? WHERE id=?", (time.time(), cid))
        _db.commit()
        return cur.lastrowid


def update_meta(mid, meta):
    with _lock:
        _db.execute("UPDATE msgs SET meta=? WHERE id=?", (json.dumps(meta, ensure_ascii=False), mid))
        _db.commit()


def get_msgs(cid):
    rows = _db.execute("SELECT id, data, meta, created FROM msgs WHERE conv=? ORDER BY id", (cid,)).fetchall()
    return [{"id": r["id"], **json.loads(r["data"]), "meta": json.loads(r["meta"] or "{}"), "created": r["created"]} for r in rows]


def relocate_paths(src, dst, current=None):
    """更新持久任务状态中的路径键和值；历史对话仍保留当时的文字。"""
    prefix = os.path.normcase(src).rstrip(os.sep)

    def change(value):
        if isinstance(value, str):
            path = os.path.normcase(value)
            if path == prefix or path.startswith(prefix + os.sep):
                return dst + value[len(src):]
        elif isinstance(value, dict):
            return {change(k): change(v) for k, v in value.items()}
        elif isinstance(value, list):
            return [change(v) for v in value]
        elif isinstance(value, set):
            return {change(v) for v in value}
        return value

    with _lock, _db:
        for cid, raw in _db.execute("SELECT conv, data FROM conv_state").fetchall():
            state = current.st if current and cid == current.conv else json.loads(raw)
            updated = change(state)
            if updated != state:
                _db.execute("UPDATE conv_state SET data=? WHERE conv=?", (json.dumps(updated, ensure_ascii=False), cid))
    if current:
        current.st = change(current.st)
        current.project["path"] = change(current.project["path"])
        for key in ("reads", "images", "changed"):
            setattr(current, key, change(getattr(current, key)))


def move_tracked(src, dst, current=None):
    """活动项目禁止外部移动；文件移动与任务状态更新失败时恢复原位置。"""
    import shutil
    from . import agent, image
    from .fileops import inside
    for run in agent.RUNS.values():
        if run is current:
            continue
        project = run.st.get("project")
        if not run.done and project and (inside(src, project) or inside(project, src) or inside(dst, project)):
            raise ValueError("这个项目正在运行，停止或等本次运行结束后再移动文件")
    move = os.rename if os.path.normcase(src) == os.path.normcase(dst) else shutil.move
    move(src, dst)
    try:
        image.relocate(src, dst)
        relocate_paths(src, dst, current)
    except Exception:
        move(dst, src)
        image.relocate(dst, src)
        raise
