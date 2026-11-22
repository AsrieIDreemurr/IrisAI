"""Agent 循环：模型 → 工具 → 模型……直到通过结束核对，给出最终回答。

设计见 docs/Agent流程方案.md。要点：
- 系统提示词固定不变（利于缓存），会变的信息作为最后一条“当前状态”附上。
- 程序强制：写入后自动检查、改了要复验、验收要有证据、结束前核对、独立验收员。
- 每次运行是一个后台任务，事件先进缓冲区再推给网页，刷新页面后可以从头重放。
"""
import asyncio
import base64
import datetime as dt
import hashlib
import json
import logging
import mimetypes
import os
import re
import time
import uuid

from . import kb, llm, memory, paths, store, tools
from .config import CFG, M, ROLES, SANDBOX, DATA_DIR, load_settings
from .paths import display, resolve

MAX_STEPS = 100         # 一次运行最多调用模型几次（读一段代码、截一次图都算一步），按档位再定
STEPS = {"quick": 40, "standard": 80, "strict": 100}
LOG_KEEP = 40           # 工作记录最多留几条
HIST_TARGET = 0.75      # 历史超长时一次压到上限的多少（以前 65%，压得太狠，刚读的代码也被压掉，模型只好重读）
KEEP_STEPS = 3          # 压缩时最近几步原样保留
IDLE_WARN, IDLE_STOP = 10, 20   # 连续几步没有新进展：提醒一次 / 停下
CTX = {k: int(e["agent_ctx"]) for k, e in M.items() if e.get("agent_ctx")}   # Agent 自己用的上下文（模型清单的 agent_ctx，预留 8k 给验收员）
HISTORY_CHARS = {model: ctx * 5 for model, ctx in CTX.items()}  # hratio 最低 0.2 token/字；实际预算还扣除固定开销、回答和图片
IMAGE_TOKENS = 900      # 一张截图大约占多少 token
REPLY_RESERVE = 3500    # 给回答留的 token
SPILL_CHARS = 4000      # 工具结果超过这么长就存文件，上下文里只留头尾
GATE_MAX = 8            # 结束核对最多拦几次
GATE_STALL = 3          # 连续几次被拦都没有进展（没改文件、没做验证、问题没减少）就放行
REVIEW_MAX = 2          # 独立验收最多几轮
THINK_LIMIT = 8000
RUNS = {}               # run_id -> Run
ACTIVE = {}             # conv_id -> run_id


def prune_runs(conv_id=None):
    """只回收已结束的重放缓存；对话正文已在数据库中。"""
    ended = sorted((r for r in RUNS.values() if r.done), key=lambda r: getattr(r, "finished", r.started), reverse=True)
    chars = 0
    for i, run in enumerate(ended):
        chars += getattr(run, "event_chars", 0)
        if (run.conv == conv_id or i >= 20 or chars > 16_000_000 or
                time.time() - getattr(run, "finished", run.started) > 900):
            RUNS.pop(run.id, None)
            if ACTIVE.get(run.conv) == run.id:
                ACTIVE.pop(run.conv, None)
OUT_DIR = os.path.join(SANDBOX, ".运行", "输出")
SYSTEM = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts", "system.txt"), encoding="utf-8").read().strip()
# 轻量版（入口页的 9B 对话）：短的系统提示词、常用的十几个工具。9B 在 CPU 上读提示词约 130 token/秒，
# 完整版第一次要读 8000 多 token（约 65 秒），轻量版两三千 token；固定开头不变，之后都能用缓存
SYSTEM_LITE = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts", "system_lite.txt"), encoding="utf-8").read().strip()
LITE_MODEL = ROLES.get("fast")
LITE_TOOLS = {"web_search", "fetch_url", "search_knowledge", "read_norm", "read_file", "list_dir", "search_files", "write_file",
              "edit_file", "make_dir", "run_python", "translate", "update_plan", "ask_user", "propose_plan"}
LITE_STEPS = 15
MEMORY_TOOLS = {"remember", "recall_memory", "forget_memory"}   # 只在对话开了人格时提供（memory.py）
MEMORY_WAIT = 120       # 一轮结束后整理记忆最多等多久（秒）


def lite_schemas(web=True, persona=False):
    """随手问的工具；开了人格时再加记忆工具。"""
    names = LITE_TOOLS | (MEMORY_TOOLS if persona else set())
    return [s for s in tools.SCHEMAS if s["function"]["name"] in names and (web or s["function"]["name"] not in ("web_search", "fetch_url"))]
CODE_EXT = {".py", ".js", ".mjs", ".cjs", ".json", ".html", ".htm"}
LOOK_EXT = {".html", ".htm", ".png", ".jpg", ".jpeg", ".webp", ".gif"}
CHANGE_TOOLS = {"write_file", "edit_file", "move_path", "delete_path", "generate_image"}
AGENT_TOOLS = {"update_plan", "set_acceptance", "record_check", "ask_user"}
NO_VISION = {k for k, e in M.items() if not e["vision"]}      # 以下几个都来自模型清单（config/models.yaml）
# 要占满内存的模型：运行时先卸载内存常驻的 9B，标题也不调用 9B（否则它会被重新加载，专家被挤到硬盘上）
BIG_MODELS = {k for k, e in M.items() if e["big"]}
NO_TOKENIZE = {k for k, e in M.items() if e["no_tokenize"]}  # 没有兼容 tokenize 接口，直接估算固定部分
# 检查力度：用户在输入框下面选。越严越慢，但越不容易交出有问题的东西
LEVELS = {
    "quick": "快速：不写验收标准。写入后程序自动做语法检查，你自查一遍就汇报，汇报里说明没有实际运行验证",
    "standard": "标准：会产出文件时先写验收标准，做完逐条实际验证并登记，结束前程序核对",
    "strict": "严格：同标准，另外全部登记通过后，程序会请一位独立验收员只看证据再判一遍",
}
ACC_TOOLS = {"set_acceptance", "record_check"}
BOOKKEEP = {"update_plan", "set_acceptance", "record_check"}   # 这些步骤之后不用先思考


# ---------------------------------------------------------------- 运行对象
class Ctx:
    """工具执行时用来通知网页：哪些文件变了。"""

    def __init__(self, run):
        self.run = run

    def changed(self, path, lines, new):
        self.run.emit({"type": "file", "path": path, "show": display(path), "lines": lines, "new": new})

    def opened(self, path):
        pass

    def refresh(self):
        self.run.emit({"type": "refresh"})


class Run:
    def __init__(self, conv_id):
        self.id = uuid.uuid4().hex[:12]
        self.conv = conv_id
        self.events = []
        self.event_chars = 0
        self.cond = asyncio.Condition()
        self.done = False
        self.pending = {}        # 确认卡片 id -> Future
        self.denied_tools = set()  # 本次运行中被用户拒绝过的工具；再次需要确认时直接拒绝
        self.plan_ok = False
        self.task = None
        self.started = time.time()
        self.st = {}             # 对话的任务状态（存数据库）
        self.user_text = ""
        self.changed = set()     # 本次运行改过的文件
        self.said = []           # 本次运行已经说过的话（用来去重）
        self.images = []         # 下一次请求要附上的截图
        self.gates = 0
        self.gate_stall, self.gate_last = 0, None
        self.reviews = 0
        self.need_think = True
        self.rewrites = {}
        self.queue, self.notes = [], []   # 排队的提醒、正在显示的提醒（见 remind）
        self.trace = []          # 每次工具调用的指纹，用来发现重复（见 _detect_loop）
        self.reads = []          # 读过的文件段：{path, a, b, mtime, step, id}
        self.pinned = []         # 重读进来的结果：压缩时不再压它，免得读 A 挤掉 B、读 B 又挤掉 A
        self.gone = set()        # 已经被压缩或去掉的工具结果（tool_call_id）
        self.idle, self.step = 0, 0
        self.loop_warn = None    # (步数, 涉及的调用指纹, 第几级)：提醒过的重复
        self.blocked = None      # (暂停的工具名, 到第几步为止)：提醒过还在重复时，先收走重复用的工具
        self.trimmed = set()     # 上一轮被截短的工具结果（tool_call_id），重做不算重复
        self.traced = 0          # 一共记了多少次调用：这一步有没有真的执行了调用
        self.warned = set()
        self.level = "standard"
        self.max_steps = STEPS["standard"]

    def emit(self, ev):
        ev["seq"] = len(self.events)
        self.events.append(ev)
        self.event_chars += len(json.dumps(ev, ensure_ascii=False))
        asyncio.get_running_loop().create_task(self._notify())

    async def _notify(self):
        async with self.cond:
            self.cond.notify_all()

    async def ask(self, card):
        cid = uuid.uuid4().hex[:8]
        fut = asyncio.get_running_loop().create_future()
        self.pending[cid] = fut
        self.emit({"type": "confirm", "id": cid, **card})
        try:
            return await fut
        finally:
            self.pending.pop(cid, None)

    def answer(self, cid, decision, note=""):
        fut = self.pending.get(cid)
        if fut and not fut.done():
            fut.set_result((decision, note))
            self.emit({"type": "confirm_done", "id": cid, "decision": decision, "note": note})
            return True
        return False

    def save(self):
        store.set_state(self.conv, self.st)


def start(conv_id, text, model, think, web, level="standard", profile=None):
    prune_runs()
    if conv_id in ACTIVE and not RUNS[ACTIVE[conv_id]].done:
        raise RuntimeError("这个对话还在运行，等它结束或先停止。")
    run = Run(conv_id)
    run.profile = "lite" if profile == "lite" else None
    if run.profile:          # 轻量版：固定 9B、快速档、不思考、最多 15 步
        model, think, level = LITE_MODEL, False, "quick"
    run.level = level if level in LEVELS else "standard"
    run.max_steps = LITE_STEPS if run.profile else STEPS[run.level]
    RUNS[run.id] = run
    ACTIVE[conv_id] = run.id
    run.task = asyncio.get_running_loop().create_task(_main(run, text, model, think, web))
    def finished(task):
        if not task.cancelled():
            exc = task.exception()
            if exc:
                logging.getLogger("hub").error("运行收尾异常：%s", type(exc).__name__)
        if not run.done:  # 协程尚未开始就取消时，函数里的 finally 不会执行。
            run.done, run.finished = True, time.time()
            run.emit({"type": "notice", "text": "运行尚未开始，已停止。"})
            run.emit({"type": "done"})
            prune_runs()
    run.task.add_done_callback(finished)
    return run


# ---------------------------------------------------------------- 上下文
def _compact_args(name, args_json, latest_write):
    """历史里旧的写入内容换成一行说明，只保留每个文件最近一次写入的全文。"""
    try:
        a = json.loads(args_json or "{}")
    except ValueError:
        return args_json
    if name == "write_file" and not latest_write:
        a["content"] = f"<已省略：当时写入 {len(a.get('content', ''))} 字，现在以文件内容为准，需要时用 read_file 读>"
    elif name == "edit_file" and not latest_write:
        for k in ("old_text", "new_text"):
            if len(a.get(k, "")) > 300:
                a[k] = a[k][:300] + "……（已省略）"
    else:
        return args_json
    return json.dumps(a, ensure_ascii=False)


def history(run, max_chars):
    """历史原样组装，超过预算时统一交给 _fit 一次压缩到位。"""
    # 程序提醒仍只放当前状态；跨轮不再自行折叠工具调用、截短结果或改写旧写入参数。
    raw = [m for m in store.get_msgs(run.conv) if not (m.get("meta") or {}).get("system")]
    run.folded = 0
    msgs = [{k: m[k] for k in ("role", "content", "tool_calls", "tool_call_id") if k in m} for m in raw]
    # _fit 沿用既有预算、压缩目标和固定边界；未超预算时保持历史前缀不变。
    return _fit(run, raw, msgs, max_chars)


def _squeeze(m, tool=None):
    """压短一条较早的消息（工具结果、长参数、长回答）。tool：这条工具结果是哪个工具产生的。"""
    m = dict(m)
    if m["role"] == "tool" and len(m.get("content") or "") > 300:
        if tool == "read_file":
            tail = "……（这段读取结果已移出上下文。读过哪些段见当前状态的“这次读过的内容”；要用其中的细节，只重读要用的那几行）"
        elif tool in ("search_files", "list_dir", "web_search", "fetch_url", "search_knowledge"):
            tail = "……（这次查找的结果已移出上下文）"
        else:
            tail = "……（较早的结果已压缩）"
        m["content"] = m["content"][:200] + "\n" + tail
    if m.get("tool_calls"):
        m["tool_calls"] = [c if len(c["function"]["arguments"]) <= 600 else {**c, "function": {**c["function"], "arguments": json.dumps(
            {"已省略": f"较早的调用参数 {len(c['function']['arguments'])} 字"}, ensure_ascii=False)}} for c in m["tool_calls"]]
    if m["role"] == "assistant" and len(m.get("content") or "") > 600:
        m["content"] = m["content"][:400] + "……"
    return m


def _fit(run, raw, msgs, max_chars):
    """历史超长时从前面去掉整轮、再压短较早的消息。

    为了让模型那边的提示词缓存能复用：超长时一次压到上限的 65%，之后去掉和压缩的范围保持不动，
    直到再次超长。否则每一步的压缩边界都往后挪一点，前缀每步都变，缓存全部失效（3.8 每步要多读 20 秒）。
    """
    n = len(msgs)
    size = [len(json.dumps(m, ensure_ascii=False)) for m in msgs]
    small = {}
    cut, sq = getattr(run, "hist_cut", 0), getattr(run, "hist_sq", 0)
    if cut >= n or (cut and msgs[cut]["role"] != "user"):
        cut, sq = 0, 0
    tool_of = {c["id"]: c["function"]["name"] for m in msgs for c in m.get("tool_calls") or []}
    pinned = set(getattr(run, "pinned", []))
    # 最近几步原样保留（按步数，不按条数：一步并行读两个文件就有三条消息）
    asst = [i for i, m in enumerate(msgs) if m["role"] == "assistant"]
    keep = min(asst[-KEEP_STEPS] if len(asst) >= KEEP_STEPS else 0, n - 4)

    def squeezable(i):
        return cut < i < min(sq, n - 4) and msgs[i].get("tool_call_id") not in pinned

    def squeezed(i):
        return _squeeze(msgs[i], tool_of.get(msgs[i].get("tool_call_id")))

    def total():
        t = 0
        for i in range(cut, n):
            if squeezable(i):
                if i not in small:
                    small[i] = len(json.dumps(squeezed(i), ensure_ascii=False))
                t += small[i]
            else:
                t += size[i]
        return t

    if total() > max_chars:
        target = max_chars * HIST_TARGET
        # 这次运行的第一条用户消息之前的整轮可以去掉
        mine = next((i for i, m in enumerate(raw) if m["role"] == "user" and m["created"] >= run.started), n - 1)
        users = [i for i, m in enumerate(msgs) if m["role"] == "user" and cut < i <= mine]
        for u in users:
            if total() <= target:
                break
            cut = u
            sq = max(sq, cut)
        # 还是太长：从前往后压短，最近几步原样保留；仍然超过上限，再压到只留最后 4 条
        while total() > target and sq < keep:
            sq = max(sq, cut + 1) + 1
        while total() > max_chars and sq < n - 4:
            sq = max(sq, cut + 1) + 1
        run.hist_cut, run.hist_sq = cut, sq
    out = [(squeezed(i) if squeezable(i) else m) for i, m in enumerate(msgs) if i >= cut]
    run.gone = {m.get("tool_call_id") for i, m in enumerate(msgs) if m.get("tool_call_id") and
                (i < cut or (squeezable(i) and size[i] > 320))}
    if cut and out and isinstance(out[0].get("content"), str):
        out[0] = {**out[0], "content": f"（更早的 {cut} 条消息已省略，做过的事见当前状态里的工作记录）\n" + out[0]["content"]}
    return out


def _file_line(path, f):
    parts = []
    ext = os.path.splitext(path)[1].lower()
    if ext in CODE_EXT:
        parts.append("还没检查" if not f.get("checked") else (f"语法检查有 {f['problems']} 个问题" if f.get("problems") else "语法检查通过"))
    if ext in LOOK_EXT:
        parts.append("看过效果" if f.get("viewed") else "还没有看过效果（结束前必须用 view_result 看）")
    return f"  {display(path)}：" + "，".join(parts or ["已改动"])


def state_message(run, web, step, vision):
    st = run.st
    dirs = load_settings()["allowed_dirs"]
    lines = ["<当前状态>", f"任务需求（对话的第一句）：{st.get('requirement', '')}"]
    earlier = [a for a in st.get("asks", [])[:-1]]
    if earlier:
        lines.append("用户后来的要求（按时间）：")
        lines += [f"  - {a}" for a in earlier[-5:]]
    if run.user_text and run.user_text != st.get("requirement"):
        lines.append(f"用户这次说：{run.user_text[:600]}")
    if st.get("unfinished"):
        lines.append("上次结束时没做完的（先处理这些，除非用户这次说的是别的事）：")
        lines += [f"  - {u}" for u in st["unfinished"]]
    proj = st.get("project")
    lines += [f"日期：{dt.date.today():%Y-%m-%d}", f"沙箱：{SANDBOX}",
              (f"项目文件夹：{display(proj)}（这个对话的文件都放这里。写文件、运行代码时直接写文件名或相对路径，例如 index.html、数据\\a.csv，"
               f"不要加“沙箱\\”前缀；Python 代码的工作目录就是这个文件夹）" if proj and proj != SANDBOX
               else "项目文件夹：沙箱根目录（相对路径从沙箱算）"),
              "授权目录：" + ("；".join(f"{d['path']}（{'只读' if d.get('mode', 'ro') == 'ro' else '可写'}）" for d in dirs) or "无"),
              "联网：" + ("开" if web else "关，不要调用 web_search 和 fetch_url"),
              "检查力度：" + LEVELS[run.level]]
    if getattr(run, "recalled", None) or getattr(run, "core_mem", None):
        lines += memory.recall_block(run.recalled, run.core_mem)
    if st.get("findings"):
        lines.append("已确认的事实（你之前记下的）：")
        lines += [f"  - {f}" for f in st["findings"]]
    if st.get("plan"):
        mark = {"completed": "完成", "in_progress": "进行中", "pending": "待做"}
        lines.append("计划：")
        lines += [f"  [{mark.get(i.get('status'), '待做')}] {i.get('content', '')}" for i in st["plan"]]
    if st.get("criteria") and run.level != "quick":
        lines.append("验收标准：")
        for i, c in enumerate(st["criteria"], 1):
            note = f"（{c['note']}）" if c.get("note") else ""
            need = f"；要的证据：{'、'.join(c['need'])}" if c.get("need") else ""
            lines.append(f"  {i}. [{c['status']}] {c['standard']}；验证方法：{c['method']}{need}{note}")
    if st.get("prev_criteria") and not st.get("criteria") and run.level != "quick":
        lines.append("上一个任务的验收标准（已结束，供参考；这次的修改可能影响它们时，把相关的写进新的验收标准）：")
        lines += [f"  - [{c['status']}] {c['standard']}" for c in st["prev_criteria"]]
    log = st.get("log") or []
    if log and (getattr(run, "hist_cut", 0) or getattr(run, "hist_sq", 0) or any(e[0] < run.started for e in log)):
        lines.append("工作记录（程序自动记的，较早的历史已压缩，以这里为准）：")
        lines += [f"  {e[1]}" for e in log[-15:]]
    reads = _read_lines(run)
    if reads:
        lines.append("这次读过的内容（较早的读取结果已移出上下文；标了“之后改过”的要重读）：")
        lines += reads
    files = {p: f for p, f in st.get("files", {}).items() if p in run.changed}
    if files:
        lines.append("本次运行改过的文件：")
        lines += [_file_line(p, f) for p, f in files.items()]
    if st.get("verify_seq", 0) > st.get("change_seq", 0) and st.get("change_seq"):
        lines.append("最后一次修改之后已经做过验证，可以用 record_check 登记。")
    elif st.get("change_seq"):
        lines.append("最后一次修改之后还没有验证。")
    try:
        lines += kb.state_lines(st)
    except Exception:
        pass
    try:
        from . import selfedit
        lines += selfedit.state_lines(st)
    except Exception:
        pass
    notes = [n["text"] for n in getattr(run, "notes", []) if n["until"] >= step]
    if notes:
        lines.append("<系统提醒>")
        lines += notes
        lines.append("</系统提醒>")
    lines.append(f"已用步数：{step} / {run.max_steps}")
    lines.append("</当前状态>")
    text = "\n".join(lines)
    if not run.images:
        return {"role": "user", "content": text}
    if not vision:
        run.images = []
        return {"role": "user", "content": text + "\n（当前模型不能看图，截图已保存，换用 Qwen3.6 才能看）"}
    parts = [{"type": "text", "text": text + "\n下面是 view_result 的截图，按顺序对应工具结果里的说明："}]
    for p in run.images[:run.max_images]:
        mime = mimetypes.guess_type(p)[0] or "image/jpeg"
        parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64," + base64.b64encode(open(p, "rb").read()).decode()}})
    run.images = []
    return {"role": "user", "content": parts}


# ---------------------------------------------------------------- 主流程
async def _main(run, text, model, think, web):
    conv = None
    state_ready = False
    try:
        conv = store.get_conv(run.conv)
        first = not store.get_msgs(run.conv)
        run.st = store.get_state(run.conv)
        state_ready = True
        run.st.setdefault("files", {})
        if first or not run.st.get("requirement"):
            run.st["requirement"] = text[:800]
        else:
            # 用户后来的每次要求按时间记下（“任务需求”只是第一句）
            run.st.setdefault("asks", []).append(text[:300])
            run.st["asks"] = run.st["asks"][-6:]
        # 上一个任务的验收标准都登记过了：归档，不让这次的改动把它们变回“待复验”
        crit = run.st.get("criteria") or []
        if crit and all(c["status"] in ("通过", "不通过") for c in crit):
            run.st["prev_criteria"] = [{"standard": c["standard"], "status": c["status"]} for c in crit]
            run.st["criteria"], run.st["unfinished"] = [], []
            run.st.pop("reviewed_seq", None)
        # 项目文件夹：新对话按第一句话建一个；以前已经写过文件的老对话保持按沙箱算，免得历史里的相对路径对不上
        proj = run.st.get("project")
        title = None
        if first:
            try:
                title = await asyncio.wait_for(_title(run, text, model), 15)
            except Exception:
                title = None
        if not proj or not os.path.isdir(proj):
            proj = paths.new_project(title or text) if (first or not run.st.get("files")) else SANDBOX
            run.st["project"] = proj
        run.project = {"path": proj}
        paths.PROJECT.set(run.project)
        run.user_text = text
        run.st["level"] = run.level
        run.save()
        store.add_msg(run.conv, {"role": "user", "content": text}, {"run": run.id, "loop_events": 1})
        store.update_conv(run.conv, model=model)
        run.emit({"type": "start", "model": model, "level": run.level})
        run.emit({"type": "project", "path": proj, "show": display(proj)})
        run.emit({"type": "state", "plan": run.st.get("plan", []), "criteria": run.st.get("criteria", []), "level": run.level,
                  "findings": run.st.get("findings", [])})
        if model in BIG_MODELS:
            try:
                await llm.post(f"/api/models/unload/{CFG['models']['fast']}")
            except Exception:
                pass
        # 人格的记忆：开了人格的对话（随手问也算），一轮开始时用用户这句话回想，结果放进“当前状态”
        run.persona = kb.conv_persona(run.st)
        run.recalled, run.core_mem, run.remembered, run.user_text = [], [], [], text
        if run.persona:
            try:
                run.recalled, run.core_mem = await asyncio.wait_for(memory.recall_for_turn(run.persona, text), 30)
                if run.recalled or run.core_mem:
                    run.emit({"type": "recall", "persona": run.persona,
                              "items": list({x["id"]: {k: x.get(k) for k in ("id", "text", "time", "via")}
                                             for x in run.core_mem + run.recalled}.values())})     # 核心记忆和这轮想起的可能重复
            except Exception as e:
                logging.getLogger("hub").warning("回想失败：%s", e)
        await _loop(run, model, think, web)
        if run.persona and run.said:
            try:       # 一轮结束：用当前已加载的模型挑出值得记的事
                run.emit({"type": "status", "text": "整理记忆"})
                saved = await asyncio.wait_for(memory.extract_and_save(run.persona, model, text, run.said[-1], run.conv, run.recalled,
                                                                       run.remembered), MEMORY_WAIT)
                if saved:
                    run.emit({"type": "memory", "persona": run.persona, "items": saved})
            except Exception as e:
                logging.getLogger("hub").warning("整理记忆失败：%s", e)
    except asyncio.CancelledError:
        run.emit({"type": "notice", "text": "已停止。"})
        store.add_msg(run.conv, {"role": "assistant", "content": "（已停止）"}, {"stopped": True})
    except Exception as e:
        run.emit({"type": "error", "text": f"出错了：{e}"})
        store.add_msg(run.conv, {"role": "assistant", "content": f"（出错：{e}）"}, {"error": True})
    finally:
        for fut in run.pending.values():
            if not fut.done():
                fut.cancel()
        try:
            if state_ready:
                run.save()
        finally:
            run.done = True
            run.finished = time.time()
            run.emit({"type": "done"})
            prune_runs()
        if conv:
            store.update_conv(run.conv)


def remind(run, text, key=None, prio=1):
    """程序插入的提醒。先排队，下一步开始时挑一条放进“当前状态”，显示两步后去掉，不进历史。
    key 相同的每次运行只提醒一次。prio：1 普通建议，2 步数、没进展，3 重复、结束核对、参数写坏（要马上看到）。"""
    if key is not None:
        if key in run.warned:
            return
        run.warned.add(key)
    run.queue.append({"text": text, "prio": prio, "at": run.step})


def _flush_reminder(run, step):
    """每步开始时调用：从排队的提醒里挑一条显示。一步最多一条；上一步刚出过提醒的，普通提醒再等一步。
    排队超过 3 步的不要了（情况多半已经变了）。界面照常显示，数据库里也记一条（标记为系统，组装历史时跳过）。"""
    run.notes = [n for n in run.notes if n["until"] >= step]
    run.queue = [q for q in run.queue if step - q["at"] <= 3]
    if not run.queue:
        return
    q = max(run.queue, key=lambda x: (x["prio"], x["at"]))
    if q["prio"] < 3 and any(n["from"] >= step - 1 for n in run.notes):
        return
    run.queue.remove(q)
    run.notes.append({"text": q["text"], "from": step, "until": step + 1})
    store.add_msg(run.conv, {"role": "user", "content": f"<系统提醒>{q['text']}</系统提醒>"}, {"system": True, "reminder": True})
    run.emit({"type": "reminder", "text": q["text"]})
    run.need_think = True


class _Overflow(Exception):
    pass


class _BadTool(Exception):
    pass


async def _stream(model, msgs, schemas, think, params):
    """包一层：超出上下文的错误变成一个 ("overflow", …) 事件，其他错误照常抛出。"""
    try:
        async for item in llm.stream_chat(model, msgs, tools=schemas, think=think, **params):
            yield item
    except RuntimeError as e:
        if "exceed" in str(e) and "context" in str(e):
            yield "overflow", str(e)
            return
        if "Failed to parse tool call" in str(e):
            yield "badtool", str(e)
            return
        raise


async def _loop(run, model, allow_think, web):
    skip = ({"web_search", "fetch_url"} if not web else set()) | (ACC_TOOLS if run.level == "quick" else set())
    if not getattr(run, "persona", None):
        skip |= MEMORY_TOOLS          # 记忆工具只在开了人格时给
    ctx = Ctx(run)
    vision = model not in NO_VISION
    run.need_think = allow_think
    ctx_tokens = CTX.get(model, 16384)
    run.max_images = 4 if ctx_tokens >= 32768 else 2
    run.hratio = getattr(run, "hratio", 0.5)    # 历史部分每个字大约多少 token，每次请求后按实际用量校准
    run.think_limit = int((ctx_tokens and M.get(model, {}).get("think_limit")) or 2500)   # 清单的 think_limit   # 第一次请求前还不知道速度
    shrink, overflow = 1.0, 0
    try:
        _seed_trace(run)
    except Exception:
        pass
    for step in range(1, run.max_steps + 1):
        schemas = (lite_schemas(web, bool(getattr(run, "persona", None))) if getattr(run, "profile", None) == "lite"
                   else [s for s in tools.available_schemas() if s["function"]["name"] not in skip])
        run.step = step
        if step == int(run.max_steps * 0.8):
            remind(run, f"还剩 {run.max_steps - step} 步。优先完成最重要的部分，然后汇报进度和剩下的事。", key="steps", prio=2)
        _flush_reminder(run, step)
        think = allow_think and run.need_think
        # 按模型的上下文大小定历史长度：总量 = 上下文 − 给回答留的 − 截图占的
        n_img = min(len(run.images), run.max_images) if vision else 0
        budget_tokens = (ctx_tokens - REPLY_RESERVE - n_img * IMAGE_TOKENS) * shrink
        # 固定开销：系统提示词 + 工具说明（约 8000 字，每次请求都要发） + 当前状态
        # 对话附带的规范（默认带行文规范）接在系统提示后面；内容不变时前缀不变，缓存照样能复用
        # 对话选了人格（knowledge\人格）时，人格卡接在工作规则之后、规范之前；随手问（轻量版）也能选人格，但不带规范
        # 系统提示词在知识库（knowledge\系统提示词\），每次读文件：用户改完从下一条消息起生效
        system = kb.system_prompt("精简版") + kb.persona_block(run.persona) if getattr(run, "profile", None) == "lite" else (
            kb.system_prompt("完整版") + kb.persona_block(kb.conv_persona(run.st)) + kb.norms_block(kb.conv_norms(run.st)))
        if (load_settings().get("look") or {}).get("lang") == "en":       # 界面是英文：默认用英文回答（放最后，前面的缓存照样能用）
            system += ("\n\n# Language\nThe user's interface is in English. Reply in English unless the user writes in another language; "
                       "this overrides the Chinese-language rules above. File contents keep whatever language the task needs.")
        # 固定部分（系统提示词 + 工具说明）用模型自己的分词器数一次 token；以前按字数估，英文 JSON 被高估，历史只剩一半
        fixed_tok = await _fixed_tokens(model, system, schemas)
        max_chars = int(min(HISTORY_CHARS.get(model, ctx_tokens * 5), max(3000, (budget_tokens - fixed_tok - STATE_TOKENS) / run.hratio)))
        hist = history(run, max_chars)
        state = state_message(run, web, step, vision)
        msgs = [{"role": "system", "content": system}] + hist + [state]
        hist_chars = (len(json.dumps(hist, ensure_ascii=False)) +
                      len(json.dumps(state["content"] if isinstance(state["content"], str) else state["content"][0], ensure_ascii=False)))
        run.emit({"type": "llm", "model": model, "think": think})
        t0 = time.time()
        result, last_prog, reasoning, checked, looped = None, 0, [], 0, None
        emo = EmoteFilter() if getattr(run, "persona", None) else None   # 开了人格：回答里的〔表情〕换成 emote 事件

        def out(text, faces=()):
            for f in faces:
                run.emit({"type": "emote", "face": f})
            if text:
                run.emit({"type": "delta", "text": text})
        # Qwen 官方建议：思考模式 0.6 / 0.95；不思考 0.7 / 0.8。量化模型开思考时加重复惩罚
        params = ({"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0, "presence_penalty": 1.5} if think
                  else {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0})
        try:
            async for kind, val in _stream(model, msgs, schemas, think, params):
                if kind == "overflow":
                    raise _Overflow()
                if kind == "badtool":
                    raise _BadTool()
                if kind == "content":
                    out(*emo.feed(val)) if emo else out(val)
                elif kind == "think":
                    run.emit({"type": "think", "text": val})
                    reasoning.append(val)
                    n = sum(map(len, reasoning))
                    if n - checked > 200:          # 每多 200 字检查一次有没有在重复
                        checked = n
                        looped = _looping("".join(reasoning), getattr(run, "think_limit", THINK_LIMIT))
                        if looped:
                            break                  # 断开连接，模型那边也随之停止生成
                elif kind == "tool":
                    if time.time() - last_prog > 0.4:
                        last_prog = time.time()
                        run.emit(_progress(val))
                else:
                    result = val
        except _BadTool:
            # 模型写工具参数时陷入重复，参数不是合法的 JSON，服务端拒收。提醒它重写这一步，最多两次
            run.badtool = getattr(run, "badtool", 0) + 1
            if run.badtool > 2:
                raise RuntimeError("模型连续几次写出无法解析的工具参数（重复同一段内容），先停下。说“继续”可以接着做。")
            run.emit({"type": "notice", "text": "模型写工具参数时陷入重复，参数无法解析。让它重写这一步。"})
            remind(run, "上一步的工具参数写坏了：同一段内容一直重复，超长而且没有结束，系统无法解析。"
                        "重写这一步，参数要短：重复的操作用循环或 repeat，不要逐条复制。", prio=3)
            continue
        except _Overflow:
            # 超出上下文：把历史再压短重试，最多两次
            overflow += 1
            if overflow > 2:
                raise RuntimeError("历史太长，压缩后仍然超出模型的上下文。可以开一个新对话继续。")
            shrink *= 0.65
            run.images = run.images[:1]
            run.emit({"type": "notice", "text": "历史太长，超出了模型的上下文，压短后重试。"})
            continue
        if emo:
            out(*emo.flush())
        usage = (result or {}).get("usage") or {}
        if usage.get("prompt_tokens") and hist_chars:
            run.hratio = max(0.2, min(1.2, (usage["prompt_tokens"] - fixed_tok - n_img * IMAGE_TOKENS) / hist_chars))
        if looped:
            _log_loop_event(run, model, step, "thinking_loop", sum(map(len, reasoning)), looped)
            run.emit({"type": "notice", "text": f"思考{looped}，已中止。这一步改为不思考直接做。"})
            run.need_think = False
            continue
        msg, finish = result["message"], result["finish"]
        if emo and msg.get("content"):
            msg["content"] = strip_emotes(msg["content"])
        if think and finish == "length" and not msg.get("content") and not msg.get("tool_calls"):
            _log_loop_event(run, model, step, "thinking_limit", len(result.get("reasoning") or ""), "finish=length")
            run.emit({"type": "notice", "text": "思考太长，用完了额度。关掉思考重试一次。"})
            run.need_think = False
            continue
        calls = msg.get("tool_calls") or []
        if not calls and msg.get("content"):
            cleaned = _dedupe(msg["content"], run.said, result["reasoning"])
            if cleaned != msg["content"]:
                msg["content"] = cleaned
                run.emit({"type": "final", "text": cleaned})
        tm = result.get("timings") or {}
        meta = {"sec": round(time.time() - t0, 1), "model": model, "think_chars": len(result["reasoning"]), "think": result["reasoning"][:20000],
                "gen_tps": round(tm.get("predicted_per_second", 0), 1), "finish": finish, "ctx": ctx_tokens,
                "prompt_n": tm.get("prompt_n"), "cache_n": tm.get("cache_n"), "pp_tps": round(tm.get("prompt_per_second") or 0)}
        # 思考上限按速度定：大约“想 90 秒”能写的字数，2000–8000。慢模型（3.8 每秒 12–17 字）不再白想 4 分钟
        if meta["gen_tps"] > 0:
            run.think_limit = int(max(2000, min(THINK_LIMIT, meta["gen_tps"] * 90)))
        store.add_msg(run.conv, msg, meta)
        run.emit({"type": "llm_done", **{k: v for k, v in meta.items() if k != "think"}})
        if msg.get("content"):
            run.said.append(msg["content"])
        if calls:
            # 下一步默认先思考：不思考时模型常把上一步的话和调用照着再来一遍（统计里重复率 18%，思考时 4%）。
            # 只在纯记账的步骤之后省掉；工具出错、验证结果、提醒会在 _call 里再把它打开
            run.need_think = not all(c["function"]["name"] in BOOKKEEP for c in calls)
            run.step_progress = False
            traced0 = run.traced
            for c in calls:
                await _call(run, ctx, c)
            run.save()
            # 进展按步算（并行调用只算一步）：改了文件、登记了验收、读到或查到新东西都算
            run.idle = 0 if run.step_progress else run.idle + 1
            # 这一步的调用全被拒绝（暂停中的工具、参数坏了）就没有新记录，不再拿上一步的记录重复判断
            stop = _check_loop(run, msg.get("content") or "") if run.traced > traced0 else None
            if not stop and run.idle >= IDLE_STOP:
                stop = (f"连续 {run.idle} 步没有新进展（没改文件、没登记验收，读到和查到的也都是已经有的），程序先停下。"
                        "说明你的想法，或者说“继续”可以接着做。")
            elif run.idle == IDLE_WARN:
                recent = "、".join(dict.fromkeys(t["title"] for t in run.trace[-6:]))
                remind(run, f"最近 {IDLE_WARN} 步没有新进展：没改文件、没登记验收，读到和查到的也都是已经有的内容。最近做的是：{recent}。"
                            "先说清楚现在卡在哪，再决定下一步；缺用户才知道的信息就用 ask_user 问。", key="idle", prio=2)
            if stop:
                run.emit({"type": "notice", "text": stop})
                store.add_msg(run.conv, {"role": "assistant", "content": f"（{stop}）"}, {"stuck": True})
                _publish_acceptance(run, unfinished=[stop])
                return
            continue
        # 想结束：先过结束核对
        if await _gate(run, model, vision, msg.get("content") or ""):
            continue
        return
    run.emit({"type": "notice", "text": f"已经执行了 {run.max_steps} 步，先停下来。说“继续”可以接着做。"})
    _publish_acceptance(run, unfinished=["步数用完，没有完成全部验证"])


# ---------------------------------------------------------------- 结束核对和验收
def _gate_problems(run, final_text):
    st, probs = run.st, []
    files = {p: f for p, f in st.get("files", {}).items() if p in run.changed and os.path.exists(p)}
    for p, f in files.items():
        ext = os.path.splitext(p)[1].lower()
        if ext in CODE_EXT and (not f.get("checked") or f.get("problems")):
            probs.append(f"{display(p)} " + ("还没有通过语法检查" if f.get("problems") else "还没有做语法检查"))
        if ext in LOOK_EXT and not f.get("viewed") and run.level != "quick":
            probs.append(f"{display(p)} 还没有用 view_result 看过实际效果")
    if run.level == "quick":
        return probs
    if files and not st.get("criteria"):
        probs.append("这次产出了文件，但还没有验收标准。先用 set_acceptance 写下标准（用户反馈的每个问题各一条），再逐条验证、用 record_check 登记")
    todo = [f"第 {i} 条（{c['standard']}）" for i, c in enumerate(st.get("criteria", []), 1) if c["status"] in ("待验证", "待复验")]
    if todo and files:
        probs.append("这些验收标准还没在最后一次修改之后验证登记：" + "、".join(todo))
    failed = [i for i, c in enumerate(st.get("criteria", []), 1) if c["status"] == "不通过"]
    if failed and files and not re.search(r"没通过|未通过|不通过|没有解决|未解决|没解决|没做到|未完成", final_text):
        probs.append(f"第 {'、'.join(map(str, failed))} 条验收没有通过，要么继续修，要么在汇报里明确说出没通过")
    return probs


async def _gate(run, model, vision, final_text):
    """返回 True 表示拦下了，继续干活；False 表示可以结束。"""
    probs = _gate_problems(run, final_text)
    if probs:
        # 上次被拦之后有没有进展：改过文件或做过验证（seq 变了），或者问题变少了
        last = run.gate_last
        moved = last is None or run.st.get("seq") != last[0] or len(probs) < last[1]
        run.gate_stall = 0 if moved else run.gate_stall + 1
        run.gate_last = (run.st.get("seq"), len(probs))
        if run.gates < GATE_MAX and run.gate_stall < GATE_STALL:
            run.gates += 1
            tail = ("\n上次被拦之后没有进展。做不到的，在汇报里如实写出哪几项没做完、卡在哪里。" if run.gate_stall else "")
            remind(run, "还不能结束：\n" + "\n".join(f"- {p}" for p in probs) + "\n处理完再汇报。" + tail, prio=3)
            return True
        # 放行，但把没做完的事明确写进回答，下次说“继续”时放在状态最前面
        why = "连续几次被拦都没有进展" if run.gate_stall >= GATE_STALL else f"结束核对已经拦了 {run.gates} 次"
        note = f"（程序核对：{why}，先结束。下面几项没有做完，说“继续”可以接着处理：\n" + "\n".join(f"- {p}" for p in probs) + "）"
        run.emit({"type": "final", "text": (final_text + "\n\n" if final_text else "") + note})
        store.add_msg(run.conv, {"role": "assistant", "content": note}, {"unfinished": True})
        _publish_acceptance(run, unfinished=probs)
        return False
    st = run.st
    passed_all = st.get("criteria") and all(c["status"] in ("通过", "不通过") for c in st["criteria"])
    if run.level == "strict" and run.changed and passed_all and st.get("reviewed_seq") != st.get("seq") and run.reviews < REVIEW_MAX:
        run.reviews += 1
        issues = await _review(run, model, vision)
        if issues:
            remind(run, "独立验收员看了证据，认为还有问题：\n" + "\n".join(f"- {x}" for x in issues) +
                   "\n逐条处理：先复现，再修改，再验证登记。验收员说错了的，用证据说明。", prio=3)
            return True
        st["reviewed_seq"] = st.get("seq")
    _publish_acceptance(run)
    return False


def _publish_acceptance(run, unfinished=None):
    st = run.st
    if not st.get("criteria") and not unfinished:
        return
    st["unfinished"] = unfinished or []
    run.save()
    run.emit({"type": "acceptance", "criteria": st.get("criteria", []), "unfinished": st["unfinished"], "final": True})


REVIEW_PROMPT = """你是验收员。执行者的结论没有给你，你只能根据下面给出的证据（运行输出、截图）判断。
对每条标准，先在 reason 里写证据里实际看到了什么（具体数值、截图里的样子），再下结论；证据里找不到的，就当没有做到。
判为不通过的情况：
- 证据里没有标准要求的操作。例如标准要求拖动、点击，证据里却没有模拟拖动或点击。
- 标准要求最终状态（停下来、恢复原样、回到圆形），证据里却没有足够长的运行、拿不出最后几帧的数值，或者最后几帧的数值并没有达到。
- 数值和标准对不上，或者执行者把“趋势对了”当成“做到了”。
- 截图里看不出标准说的效果。例如标准说半透明，但球背后是纯色背景、看不到任何被遮挡的东西，这张截图证明不了半透明。
- 效果明显夸张失真（例如数值一直顶在上下限、形变超过一半），即使标准没写上限，也写在 other 里。
- 标准本身写得含糊、没法判断对错的，在 reason 里指出来，并判为不通过。
另外看任务需求里有没有标准没覆盖、但证据显示有问题的地方，写在 other 里。重点看：
- 方向对不对：模拟往右上拖动后，速度的正负号是不是也朝右上；
- 停下后的状态对不对：缩放有没有回到 1、形状有没有恢复、有没有越过边界；
- 页面有没有报错。
判断坐标、边界时，以证据里写明的尺寸为准；证据没给尺寸时不要自己假设，写“证据缺少尺寸，无法判断”。
只输出一个 JSON，不要输出其他文字：
{"results": [{"index": 1, "pass": true, "reason": "一句话依据"}], "other": "标准没覆盖的问题，没有就写空字符串"}"""


async def _review(run, model, vision):
    st = run.st
    run.emit({"type": "review", "status": "running"})
    lines = [f"对话的第一句：{st.get('requirement', '')}"]
    for x in st.get("asks", [])[:-1][-3:]:
        lines.append(f"用户后来的要求：{x}")
    if run.user_text != st.get("requirement"):
        lines.append(f"用户最新的话（这次的任务以它为准）：{run.user_text[:600]}")
    images = []
    for i, c in enumerate(st.get("criteria", []), 1):
        # 不给执行者登记的结论和说明：同一个模型看到自己的结论容易顺着判，只看证据
        lines.append(f"\n标准 {i}：{c['standard']}\n验证方法：{c['method']}")
        for e in c.get("evidence", [])[-2:]:
            ops = "，模拟操作：" + "、".join(e["actions"]) if e.get("actions") else "，没有模拟操作"
            lines.append(f"证据（{e['title']}{ops}）：\n{e['summary']}")
            images += [p for p in e.get("images", []) if os.path.exists(p)]
    content = "\n".join(lines)
    images = list(dict.fromkeys(images))[-3:]
    if vision and images:
        content = [{"type": "text", "text": content + "\n\n截图按顺序附在下面。"}] + [
            {"type": "image_url", "image_url": {"url": f"data:{mimetypes.guess_type(p)[0] or 'image/jpeg'};base64," +
                                                      base64.b64encode(open(p, 'rb').read()).decode()}} for p in images]
    try:
        out, _ = await llm.complete(model, [{"role": "system", "content": REVIEW_PROMPT}, {"role": "user", "content": content}],
                                    max_tokens=1500, temperature=0.2, top_p=0.8, chat_template_kwargs={"enable_thinking": False})
        m = re.search(r"\{.*\}", out, re.S)
        data = json.loads(m.group(0)) if m else {}
    except Exception as e:
        run.emit({"type": "review", "status": "error", "text": f"验收员没有给出结果：{e}"})
        return []
    issues = []
    for r in data.get("results", []):
        i = int(r.get("index", 0))
        if 1 <= i <= len(st.get("criteria", [])) and not r.get("pass"):
            c = st["criteria"][i - 1]
            c["status"], c["note"] = "待复验", f"验收员：{r.get('reason', '')}"
            issues.append(f"第 {i} 条（{c['standard']}）：{r.get('reason', '')}")
    if (data.get("other") or "").strip():
        issues.append("标准之外：" + data["other"].strip())
    run.save()
    run.emit({"type": "review", "status": "done", "results": data.get("results", []), "other": data.get("other", ""), "issues": issues})
    run.emit({"type": "acceptance", "criteria": st.get("criteria", []), "unfinished": []})
    return issues


# ---------------------------------------------------------------- 由 Agent 自己处理的工具
async def _agent_tool(run, name, a, cid):
    st = run.st
    if name == "update_plan":
        items = [{"content": str(i.get("content", ""))[:120], "status": i.get("status", "pending")} for i in a.get("items") or []][:12]
        doing = [i for i in items if i["status"] == "in_progress"]
        for extra in doing[1:]:
            extra["status"] = "pending"
        old_done = sum(i.get("status") == "completed" for i in st.get("plan") or [])
        st["plan"] = items
        if isinstance(a.get("findings"), list):
            st["findings"] = [str(f)[:150] for f in a["findings"] if str(f).strip()][:8]
        run.emit({"type": "plan", "items": items, "note": a.get("note", ""), "findings": st.get("findings", [])})
        if sum(i["status"] == "completed" for i in items) > old_done:
            run.need_think = True        # 推进到下一步之前想一想
        done = sum(i["status"] == "completed" for i in items)
        return f"计划已更新：{len(items)} 项，完成 {done} 项。" + ("（同一时间只能有一项进行中，多出来的已改为待做）" if len(doing) > 1 else ""), {}
    if name == "set_acceptance":
        crit = [{"standard": str(c.get("standard", ""))[:200], "method": str(c.get("method", ""))[:200], "status": "待验证", "note": "",
                 "evidence": [], "need": [k for k in (c.get("evidence") or []) if k in KINDS]}
                for c in a.get("criteria") or [] if c.get("standard")][:10]
        if not crit:
            return "没有收到验收标准。每条要有 standard 和 method。", {"status": "error"}
        lazy = [i for i, c in enumerate(crit, 1) if c["need"] == ["看代码"]]
        st["criteria"] = crit
        st.pop("reviewed_seq", None)
        run.emit({"type": "acceptance", "criteria": crit, "unfinished": []})
        warn = (f"第 {'、'.join(map(str, lazy))} 条只靠看代码验证。能运行的程序要实际运行，看代码只适合没法运行的部分。" if lazy else "")
        return f"已记下 {len(crit)} 条验收标准。做完后逐条验证（run_js / run_python / view_result），再用 record_check 登记。{warn}", {}
    if name == "record_check":
        crit = st.get("criteria") or []
        i = int(a.get("index") or 0)
        if not crit:
            return "还没有验收标准，先用 set_acceptance 写下标准。", {"status": "error"}
        if not 1 <= i <= len(crit):
            return f"index 要在 1 到 {len(crit)} 之间。", {"status": "error"}
        if st.get("change_seq") and st.get("verify_seq", 0) <= st.get("change_seq", 0):
            return ("最后一次修改文件之后还没有做过验证，不能登记。先用 run_js、run_python 或 view_result 实际验证，再登记。"), {"status": "error"}
        if not st.get("verifications"):
            return "还没有做过任何验证，不能登记。先实际运行或查看效果。", {"status": "error"}
        c = crit[i - 1]
        miss = _evidence_gap(c, run)
        if miss and a.get("passed"):
            run.rejects = getattr(run, "rejects", 0) + 1
            hint = ("如果是验证方法写得不合适（例如这是 Python 脚本，不需要截图），用 set_acceptance 改写这条标准的验证方法。"
                    if run.rejects >= 2 else "")
            return f"第 {i} 条不能登记为通过：{miss}。按这条标准写的验证方法去验证，再登记。{hint}", {"status": "error"}
        c["status"] = "通过" if a.get("passed") else "不通过"
        c["note"] = str(a.get("note", ""))[:200]
        c["evidence"] = _pick_evidence(c, run)
        run.emit({"type": "acceptance", "criteria": crit, "unfinished": []})
        left = sum(x["status"] in ("待验证", "待复验") for x in crit)
        return f"已登记第 {i} 条：{c['status']}。证据已附上（{'；'.join(v['title'] for v in c['evidence'])}）。还有 {left} 条待验证。", {}
    if name == "ask_user":
        opts = [str(o)[:80] for o in (a.get("options") or [])][:4]
        card = {"title": str(a.get("question", ""))[:300], "steps": [], "options": opts, "kind": "ask"}
        decision, note = await run.ask(card)
        return (f"用户回答：{note}" if note else "用户没有回答。"), {"confirm": {**card, "decision": decision, "note": note}}
    return f"没有这个工具：{name}", {"status": "error"}


# ---------------------------------------------------------------- 普通工具
async def _call(run, ctx, c):
    name = c["function"]["name"]
    try:
        args = json.loads(c["function"]["arguments"] or "{}")
    except ValueError:
        out = "参数不是合法的 JSON，没有执行。检查引号和转义后重试。"
        run.emit({"type": "step", "id": c["id"], "title": name, "status": "error", "tool": name, "result": out})
        store.add_msg(run.conv, {"role": "tool", "tool_call_id": c["id"], "content": out}, {"title": name, "tool": name, "status": "error"})
        run.need_think = True
        return
    title = tools.title(name, args)
    blk = run.blocked
    if blk and run.step <= blk[1] and name in blk[0]:
        out = (f"{name} 这两步暂时停用：提醒过之后还在重复同样的操作。需要的内容都在上面了，"
               "直接动手改；拿不准用户要什么就用 ask_user 问；已经做完了就登记验收、汇报。")
        run.emit({"type": "step", "id": c["id"], "title": title, "status": "error", "tool": name, "result": out})
        store.add_msg(run.conv, {"role": "tool", "tool_call_id": c["id"], "content": out}, {"title": title, "tool": name, "status": "error"})
        run.need_think = True
        return
    detail = _detail(name, args)
    run.emit({"type": "step", "id": c["id"], "title": title, "status": "running", "tool": name, "detail": detail})
    meta = {"title": title, "tool": name}
    t0 = time.time()

    if name in AGENT_TOOLS:
        out, extra = await _agent_tool(run, name, args, c["id"])
        meta.update(extra)
        status = meta.pop("status", "ok")
        if status == "ok" and name != "update_plan":     # 登记验收、写标准、用户回答了问题
            run.step_progress = True
        _trace(run, c["id"], name, args, title, out, status)
        meta.update(status=status, sec=round(time.time() - t0, 1))
        run.emit({"type": "step", "id": c["id"], "title": title, "status": status, "sec": meta["sec"], "tool": name, "detail": detail, "result": out})
        store.add_msg(run.conv, {"role": "tool", "tool_call_id": c["id"], "content": out}, meta)
        if status == "error":
            run.need_think = True
        return

    # 没有验收标准就写代码：以前直接拦下，但模型已经把整个文件生成完了，被拦后要重新生成一遍（3.8 一次要几分钟）。
    # 现在照常写入，由 _nudge 立刻提醒先补标准；结束前的核对仍然要求有标准。

    chk = tools.check(name, args, run.plan_ok)
    if chk:
        if name in run.denied_tools:
            out = f"已拦截 {name}：你刚被拒绝过这一步，换个不需要确认的做法，或者在回答里说明做不了。"
            meta["status"] = "denied"
            run.emit({"type": "step", "id": c["id"], "title": title, "status": "denied", "tool": name, "result": out})
            store.add_msg(run.conv, {"role": "tool", "tool_call_id": c["id"], "content": out}, meta)
            run.need_think = True
            return
        level, desc = chk
        if level == "plan":
            card = {"title": args.get("title") or "执行计划", "steps": list(args.get("steps") or []), "kind": "plan"}
        else:
            card = {"title": "以下操作在沙箱外，需要你确认" if level == "confirm" else "这一步需要你单独确认", "steps": [desc], "kind": level}
        run.emit({"type": "step", "id": c["id"], "title": title, "status": "waiting"})
        decision, note = await run.ask(card)
        meta["confirm"] = {**card, "decision": decision, "note": note}
        if decision != "ok":
            if decision == "no":
                run.denied_tools.add(name)
            out = "用户拒绝了这一步，没有执行。用一句话告诉用户哪一步没有执行，不要道歉，不要再次请求。" if decision == "no" else \
                  f"用户想修改：{note}。按用户的意见调整后，重新调用 propose_plan。"
            meta["status"] = "denied"
            run.step_progress = True             # 用户给了意见，方向会变
            run.emit({"type": "step", "id": c["id"], "title": title, "status": "denied", "tool": name})
            store.add_msg(run.conv, {"role": "tool", "tool_call_id": c["id"], "content": out}, meta)
            run.need_think = True
            return
        if level == "plan":
            run.plan_ok = True
    try:
        res = await tools.run(name, args, ctx)
        out, extra, status = res.text, res.meta, "ok"
    except Exception as e:
        out, extra, status = f"执行失败：{e}", {}, "error"
    meta.update(extra)
    meta.pop("results", None)
    if extra.get("edit_failed"):
        status = "error"
    meta.update(status=status, sec=round(time.time() - t0, 1))
    _track(run, name, args, out, meta)
    _log(run, name, title, status, out, meta)
    out = _nudge(run, name, args, out, meta)
    if meta.get("verify") and name != "check_file" and run.level != "quick":
        run.need_think = True            # 拿到验证结果先想清楚对不对，再登记或修改
    if len(out) > SPILL_CHARS and name != "read_file":     # read_file 自己按字数截，整段留在上下文里
        out = _spill(name, out)
    run.emit({"type": "step", "id": c["id"], "title": title, "status": status, "sec": meta["sec"], "tool": name, "detail": detail,
              "result": out[:3000], **{k: meta[k] for k in ("image", "images", "file", "exit", "problems", "page_errors", "selfedit") if k in meta}})
    store.add_msg(run.conv, {"role": "tool", "tool_call_id": c["id"], "content": out}, meta)
    if status == "error" or meta.get("problems") or meta.get("edit_failed") or meta.get("page_errors") or meta.get("exit") not in (None, 0):
        run.need_think = True
    # 进展：改了文件；或者读到、查到了这次运行里还没见过的东西
    if name in CHANGE_TOOLS and status == "ok" and not meta.get("unchanged") and not meta.get("edit_failed"):
        run.step_progress = True
    elif name == "read_file" and status == "ok":
        if not _note_read(run, c["id"], args, out):
            run.step_progress = True
    if _trace(run, c["id"], name, args, title, out, status) and name not in CHANGE_TOOLS and name != "read_file":
        run.step_progress = True
    if name == "edit_file" and meta.get("unchanged"):
        run.noop = getattr(run, "noop", 0) + 1
        if run.noop == 2:
            remind(run, "edit_file 连着两次“替换前后内容一样”：你写的 new_text 和 old_text 相同，文件没有任何变化。"
                        "先 read_file 看文件现在的样子，确认到底要改哪几行，再写不同的 new_text。", prio=2)
    elif name in CHANGE_TOOLS:
        run.noop = 0


def _log(run, name, title, status, out, meta):
    """工作记录：每次改文件、验证、登记各记一行。历史被压缩后，模型靠它知道做过什么。"""
    if name in ("update_plan", "ask_user", "list_dir", "read_file", "search_files", "web_search", "fetch_url"):
        return
    first = (out or "").strip().splitlines()[0][:90] if (out or "").strip() else ""
    if name in CHANGE_TOOLS:
        extra = f"，自动检查发现 {meta['problems']} 个问题" if meta.get("problems") else ""
        line = f"{title}：{'成功' if status == 'ok' else '失败'}{extra}"
    elif meta.get("verify"):
        bad = []
        if meta.get("page_errors"):
            bad.append(f"页面报错 {meta['page_errors']} 个")
        if meta.get("exit") not in (None, 0):
            bad.append(f"退出码 {meta['exit']}")
        if meta.get("problems"):
            bad.append(f"{meta['problems']} 个问题")
        line = f"{title}：" + ("；".join(bad) if bad else first)
    else:
        line = f"{title}：{first}"
    log = run.st.setdefault("log", [])
    log.append([time.time(), f"[{dt.datetime.now():%H:%M}] {line}"])
    run.st["log"] = log[-LOG_KEEP:]


def _track(run, name, args, out, meta):
    """记录文件改动和验证，给结束核对和验收登记用。"""
    st = run.st
    files = st.setdefault("files", {})
    if meta.get("status") != "ok":
        return
    if name in CHANGE_TOOLS and not meta.get("unchanged") and not meta.get("edit_failed"):
        path = meta.get("image") or meta.get("file") or resolve(args.get("path") or args.get("src") or "")
        st["seq"] = st.get("seq", 0) + 1
        st["change_seq"] = st["seq"]
        st["verifications"] = []
        f = files.setdefault(path, {})
        f.update(viewed=False)
        if "checked" in meta:
            f.update(checked=True, problems=meta.get("problems", 0))
        elif os.path.splitext(path)[1].lower() in CODE_EXT:
            f.update(checked=False)
        run.changed.add(path)
        for c in st.get("criteria", []):
            if c["status"] == "通过":
                c["status"], c["note"] = "待复验", "文件改过，需要重新验证"
        if any(c["status"] == "待复验" for c in st.get("criteria", [])):
            run.emit({"type": "acceptance", "criteria": st["criteria"], "unfinished": []})
        if name == "write_file" and meta.get("rewrite"):
            run.rewrites[path] = run.rewrites.get(path, 0) + 1
            if run.rewrites[path] >= 3 and ("rw", path) not in run.warned:
                run.warned.add(("rw", path))
                remind(run, f"{display(path)} 已经整份重写 {run.rewrites[path]} 次。用 edit_file 只改出问题的地方。", prio=2)
    if meta.get("verify"):
        st["seq"] = st.get("seq", 0) + 1
        st["verify_seq"] = st["seq"]
        imgs = meta.get("images") or []
        # 证据要包含最后的状态：长输出保留开头和结尾（“停下来没有”“恢复没有”都在结尾）
        summary = out if len(out) <= 2400 else out[:1000] + "\n……（中间省略）……\n" + out[-1400:]
        acts = [a.get("type") or "代码" for a in (args.get("actions") or [])]
        st.setdefault("verifications", []).append({"title": meta["title"], "tool": name, "summary": summary, "images": imgs, "actions": acts,
                                                   "watch": bool(args.get("watch"))})
        st["verifications"] = st["verifications"][-6:]
        if name == "check_file":
            f = files.get(resolve(args.get("path")))
            if f is not None:
                f.update(checked=True, problems=meta.get("problems", 0))
        if name == "view_result":
            p = resolve(args.get("path"))
            if p in files:
                files[p]["viewed"] = True
            run.images = imgs
    if name in ("edit_file", "write_file") and meta.get("checked"):
        f = files.get(meta.get("file"))
        if f is not None:
            f.update(checked=True, problems=meta.get("problems", 0))


# 验证方法里出现这些词，就必须有对应的证据。最后一项为 True 的只对网页、图片任务生效
# （Python 脚本“显示平均分”“点击运行”不该要求截图和模拟点击）
_RUN = ("run_python", "run_js")
_NEEDS = [
    (r"截图|view_result|外观|看效果|看起来|颜色|透明|显示", lambda v: v.get("tool") == "view_result",
     "验证方法要求截图看效果，但最后一次修改之后没有 view_result 的截图", True),
    (r"拖|甩|drag|点击|click|鼠标|按钮", lambda v: bool(v.get("actions")),
     "验证方法要求模拟拖动或点击，但最后一次修改之后的验证里没有模拟操作（view_result 的 actions 或 run_js 的 actions）", True),
    (r"run_js|帧|数值|逐帧|watch|变量", lambda v: v.get("tool") in _RUN or v.get("watch"),
     "验证方法要求读出数值，但最后一次修改之后没有实际运行过（run_python、run_js，或 view_result 的 watch）", False),
    (r"run_python|运行|输出|打印", lambda v: v.get("tool") in _RUN,
     "验证方法要求实际运行程序，但最后一次修改之后没有运行过", False),
]


# set_acceptance 里每条标准声明的证据种类 → 怎样的验证记录算数。最后一项为 True 的只在这次改过网页或图片时要求
KINDS = {
    "运行": (lambda v: v.get("tool") in _RUN + ("view_result",), "验证方法要求实际运行，但最后一次修改之后没有运行过（run_python、run_js 或 view_result）", False),
    "读数值": (lambda v: v.get("tool") in _RUN or v.get("watch"), "验证方法要求读出数值，但最后一次修改之后没有读过（run_python、run_js，或 view_result 的 watch）", False),
    "截图": (lambda v: v.get("tool") == "view_result", "验证方法要求截图看效果，但最后一次修改之后没有 view_result 的截图", True),
    "模拟操作": (lambda v: bool(v.get("actions")), "验证方法要求模拟点击或拖动，但最后一次修改之后的验证里没有模拟操作（view_result 或 run_js 的 actions）", True),
    "看代码": (lambda v: True, "", False),
}


def _needs(c, run):
    """这条标准要求哪些证据。网页、图片类的要求只在这次运行改过网页或图片时才算。"""
    page = any(os.path.splitext(p)[1].lower() in LOOK_EXT for p in run.changed)
    if c.get("need"):
        # 模型自己声明了要哪种证据：按声明来，不再从文字里猜
        return [(ok, msg) for k in c["need"] for ok, msg, page_only in [KINDS[k]] if msg and (page or not page_only)]
    text = c.get("method", "") + c.get("standard", "")
    return [(ok, msg) for pat, ok, msg, page_only in _NEEDS if re.search(pat, text, re.I) and (page or not page_only)]


def _evidence_gap(c, run):
    """标准写的验证方法，在最后一次修改之后的验证记录里有没有对应的证据。返回缺什么，没缺返回 None。"""
    for ok, msg in _needs(c, run):
        if not any(ok(v) for v in run.st.get("verifications", [])):
            return msg
    return None


def _pick_evidence(c, run):
    """挑和这条标准的验证方法对得上的证据，最多两条。"""
    verifs = run.st.get("verifications", [])
    picked = [v for ok, _ in _needs(c, run) for v in verifs if ok(v)]
    picked = list({id(v): v for v in picked}.values()) or verifs
    return [dict(v) for v in picked[-2:]]


def _nudge(run, name, args, out, meta):
    """根据测试里看到的坏习惯，及时提醒（不等到结束核对）。返回可能追加了提示的工具结果。"""
    st = run.st
    if run.level == "quick":
        return out
    pages = [p for p in run.changed if os.path.splitext(p)[1].lower() in (".html", ".htm")]
    # 改了文件还没有验收标准：第一次改完就提醒，不等到最后
    if name in CHANGE_TOOLS and meta.get("status") == "ok" and run.changed and not st.get("criteria"):
        remind(run, key="need_criteria", text="已经开始产出文件，但还没有验收标准。现在先用 set_acceptance 写下标准（每条写清楚要达到什么、用什么方法验证），再继续做。")
    # 网页改过，却另写代码去“模拟”：结果直接标明不可信
    if name == "run_js" and not args.get("page") and pages:
        out += ("\n注意：这是你另写的代码，不是网页里的真实脚本，结果不能证明网页是对的。"
                f"验证网页请用 run_js 的 page 参数（例如 page=\"{display(pages[0]).replace('沙箱' + os.sep, '')}\"），"
                "用 watch 读出脚本里的真实变量。")
    # 同一个网页截图两次还没读过数值：动画、碰撞这类一闪而过的效果截图抓不到
    if name == "run_js" and args.get("page"):
        run.page_runs = getattr(run, "page_runs", 0) + 1
    if name == "view_result" and os.path.splitext(args.get("path", ""))[1].lower() in (".html", ".htm"):
        key = ("views", resolve(args.get("path")))
        n = run.rewrites.get(key, 0) + 1
        run.rewrites[key] = n
        if n >= 2 and not getattr(run, "page_runs", 0) and not args.get("watch") and ("num", key) not in run.warned:
            run.warned.add(("num", key))
            try:
                is3d = bool(re.search(r"THREE\.|WebGLRenderer|getContext\(\s*['\"]webgl", open(key[1], encoding="utf-8", errors="ignore").read()))
            except OSError:
                is3d = False
            if is3d:
                remind(run, "动画、碰撞、形变这类一闪而过的效果，截图很容易错过。这是 3D 网页，run_js 跑不了；"
                            "在网页里加一行 window.debug = { ball } 把对象挂到全局，再用 view_result 的 watch 读变量（例如 debug.ball.position.y、debug.ball.scale.y），"
                            "配合 frames 连续截图和 actions 模拟拖动，拿到数值再判断。")
            else:
                remind(run, "截图只能看到某一瞬间。要确认随时间变化的东西（计时、动画、碰撞、点击后的变化），"
                            "改用 run_js 的 page 参数读出相关变量的数值，或者在 view_result 里加 watch 读变量。截图只用来看外观。"
                            "点按钮时用 actions 的 text（按钮上的字）或 selector，不要猜坐标。")
    return out


def _spill(name, out):
    os.makedirs(OUT_DIR, exist_ok=True)
    f = os.path.join(OUT_DIR, f"{dt.datetime.now():%H%M%S}_{name}.txt")
    with open(f, "w", encoding="utf-8") as fh:
        fh.write(out)
    return (out[:2500] + f"\n……（结果太长，共 {len(out)} 字，完整内容已存到 {display(f)}，需要时用 read_file 分段读）……\n" + out[-800:])


# ---------------------------------------------------------------- 重复检测
STATE_TOKENS = 1500     # 给“当前状态”留的 token
_FIXED = {}             # (模型, 系统提示词和工具说明的摘要) -> token 数


async def _fixed_tokens(model, system, schemas):
    text = system + json.dumps(schemas, ensure_ascii=False)
    if model in NO_TOKENIZE:
        return int(len(text) * 0.55)
    key = (model, hashlib.md5(text.encode("utf-8")).hexdigest())
    if key not in _FIXED:
        try:
            r = await llm.post(f"/upstream/{model}/tokenize", json={"content": text})
            _FIXED[key] = len(r.json()["tokens"]) + 200          # 聊天模板给工具说明加的标记
        except Exception:
            return int(len(text) * 0.55)
    return _FIXED[key]


_FAIL = re.compile(r"没有找到|找不到|没有相关|不存在|失败|出错|错误|没有修改|没有写入|不能|无法")
_NUM_NOISE = re.compile(r"\d{6}_\d+\.(?:jpg|png)|\d+(?:\.\d+)?\s*(?:秒|毫秒|ms)|「[^」]*」")


def _sig(run, name, args):
    """调用的指纹：工具名 + 参数，再加上“最后一次改文件”的序号。中间改过文件，同样的调用就不算重复。"""
    a = json.dumps(args, sort_keys=True, ensure_ascii=False)
    return f"{name}|{re.sub(r'\s+', ' ', a)}|{run.st.get('change_seq', 0)}"


def _trace(run, cid, name, args, title, out, status):
    """记下这次调用。结果在这次运行里第一次出现（对这个工具而言）返回 True，算读到或查到了新东西。"""
    res = hashlib.md5(_NUM_NOISE.sub("", out or "").encode("utf-8")).hexdigest()
    sig = _sig(run, name, args)
    # 和之前某次调用一模一样、结果也一样，而且那次的结果还在上下文里：这次是白做
    # 那次的结果被压缩、去掉或截短了，重做是正当的，不算重复
    dup = next((t for t in reversed(run.trace) if t["sig"] == sig and t["res"] == res
                and t["id"] not in run.gone and t["id"] not in run.trimmed), None)
    if name == "read_file" and not dup:
        # 读文件按行段判断：这次读的行全在之前某次读过的范围里（参数不同也算，例如先读 180 起 60 行、再读 180 起 50 行），
        # 文件没改过、那次的结果也还在上下文里，就是重读
        cur = next((r for r in reversed(run.reads) if r["id"] == cid), None)
        cover = cur and next((r for r in reversed(run.reads) if r["id"] != cid and r["path"] == cur["path"] and r["mtime"] == cur["mtime"]
                              and r["a"] <= cur["a"] and cur["b"] <= r["b"] and r["id"] not in run.gone and r["id"] not in run.trimmed), None)
        if cover:
            dup = {"step": cover["step"]}
    run.traced += 1
    new = not any(t["name"] == name and t["res"] == res for t in run.trace)
    first = (out or "").strip().splitlines()[0][:80] if (out or "").strip() else ""
    run.trace.append({"id": cid, "name": name, "sig": sig, "res": res, "title": title, "step": run.step, "first": first,
                      "dup": dup["step"] if dup else None, "seq": run.st.get("change_seq", 0),
                      "fail": status != "ok" or bool(_FAIL.search(first))})
    run.trace = run.trace[-40:]
    return new


def _note_read(run, cid, args, out):
    """记下读过的文件段。返回 True 表示这段之前读过、文件也没改过（重读）；重读进来的结果固定，压缩时不再压它。"""
    p = resolve(args.get("path") or "")
    start = max(int(args.get("start_line") or 1), 1)
    n = min(int(args.get("max_lines") or 200), 1000)
    m = re.search(r"（(\d+) 行）", (out or "").split("\n", 1)[0])
    end = min(start + n - 1, int(m.group(1))) if m else start + n - 1
    got = re.search(r"这次读了第 \d+–(\d+) 行", out or "")      # 结果太长被截短时，以实际返回到的行为准
    if got:
        end = min(end, int(got.group(1)))
    try:
        mt = os.path.getmtime(p)
    except OSError:
        return False
    again = any(r["path"] == p and r["mtime"] == mt and r["a"] <= start and end <= r["b"] for r in run.reads)
    run.reads.append({"path": p, "a": start, "b": end, "mtime": mt, "step": run.step, "id": cid})
    run.reads = run.reads[-60:]
    if again:
        run.pinned = (run.pinned + [cid])[-8:]
    return again


def _read_lines(run):
    """当前状态里的“这次读过的内容”：历史压缩过才列（没压缩时读取结果都还在上下文里）。"""
    if not run.reads or not (getattr(run, "hist_cut", 0) or getattr(run, "hist_sq", 0)):
        return []
    by = {}
    for r in run.reads:
        by.setdefault(r["path"], []).append(r)
    out = []
    for p, rs in list(by.items())[-10:]:
        spans = []
        for a, b in sorted((r["a"], r["b"]) for r in rs):
            if spans and a <= spans[-1][1] + 1:
                spans[-1][1] = max(spans[-1][1], b)
            else:
                spans.append([a, b])
        try:
            changed = os.path.getmtime(p) != rs[-1]["mtime"]
        except OSError:
            changed = True
        out.append(f"  {display(p)}：第 " + "、".join(f"{a}–{b}" for a, b in spans) + " 行" + ("（之后改过）" if changed else ""))
    return out


def _detect_loop(run, text):
    """看最近 6 次调用有没有在打转。返回 (说明, 涉及的调用指纹)，没有返回 None。
    两种情况：同样的调用和结果又做了一遍（A A、A B A B 这类，至少两次）；
    参数换了、结果都一样（例如搜三个不同的名字都没找到、edit_file 连着三次同一个错误），期间没改过文件。"""
    t = run.trace[-6:]
    if not t:
        return None
    last = t[-1]
    dups = [x for x in t if x["dup"] is not None]
    if last["dup"] is not None and len(dups) >= 2:
        titles = "、".join(dict.fromkeys(x["title"] for x in dups))
        steps = "、".join(dict.fromkeys("上一轮" if not x["dup"] else f"第 {x['dup']} 步" for x in dups))
        if all(x["name"] == "read_file" for x in dups):
            why = (f"{titles}：这些内容{steps}刚读过，文件没改过，结果还在上面。直接用上面的内容往下做，不要再读。"
                   "要找某个名字在哪，用 search_files。")
        else:
            why = (f"{titles}：和{steps}做的一模一样，结果也一样，期间没改过文件，再做一次结果也不会变。"
                   "根据已有的结果决定下一步。")
        info = (why, {x["sig"] for x in dups})
    else:
        # 只看失败或没结果的（连着检查三个文件都“没有问题”、连着更新计划，结果相同是正常的）
        same = [x for x in t if x["name"] == last["name"] and x["res"] == last["res"] and x["seq"] == last["seq"] and x["fail"]]
        if not last["fail"] or len(same) < 3 or len({x["sig"] for x in same}) < 3:
            return None
        titles = "、".join(dict.fromkeys(x["title"] for x in same))
        if last["name"] in ("search_files", "list_dir", "search_knowledge") and re.search(r"没有找到|没有相关|0 项", last["first"]):
            why = (f"{titles}：都没有结果。要是在确认东西已经删干净、改掉了，这就是结论，不用换说法再搜，直接登记或往下做；"
                   "要是在找东西，说明代码里没有这些名字，别再猜，先用 list_dir 看有哪些文件，或者搜界面上显示的中文、已经读到的变量名和函数名。")
        elif last["name"] == "edit_file":
            why = (f"edit_file 连着 {len(same)} 次同一个错误：{last['first']}。先用 read_file 读要改的那几行，"
                   "把原文原样复制成 old_text；中文引号、全角符号、缩进最容易对不上。")
        else:
            why = f"{titles}：换了参数，结果都一样（{last['first']}）。再换参数解决不了，先弄清楚为什么总是这个结果。"
        info = (why, {x["sig"] for x in same})
    # 说的话也和最近几步一模一样
    said = re.sub(r"\s+", "", text or "")
    if len(said) >= 8:
        prev = [re.sub(r"\s+", "", s) for s in run.said[:-1][-4:]]
        if said in prev:
            info = (info[0] + f"你这一步说的“{(text or '').strip()[:40]}”也和前几步一字不差。", info[1])
    return info


def _check_loop(run, text):
    """有重复就点名提醒；提醒过之后同样的调用还在重复，返回停下的说明。"""
    found = _detect_loop(run, text)
    if not found:
        return None
    why, sigs = found
    lw = run.loop_warn
    if lw and run.step > lw[0] and sigs & lw[1]:
        if lw[2] >= 2:
            return f"提醒过、也暂停过工具，还在重复同样的操作，程序先停下。{why}说说你的想法，或者说“继续”可以接着做。"
        # 第二次：先不停，把它在重复用的工具收走两步，逼它动手或者问用户
        names = {s.split("|", 1)[0] for s in sigs}
        run.blocked = (names, run.step + 2)
        run.loop_warn = (run.step, lw[1] | sigs, 2)
        remind(run, f"{why}提醒过之后还在重复，接下来两步暂时不能用 {'、'.join(sorted(names))}。需要的内容都在上面了："
                    "先用一两句话写清楚接下来具体做什么（改哪个文件的哪几行、改成什么），然后直接做；"
                    "拿不准用户要什么就用 ask_user 问；已经做完了就登记验收、汇报。", prio=3)
        return None
    if not lw or not sigs & lw[1]:
        run.loop_warn = (run.step, sigs, 1)
        remind(run, why, prio=3)
    return None


def _seed_trace(run):
    """说“继续”之后是新的一次运行：把上一轮做过、结果还在上下文里的调用记进来（步数记 0，提醒里显示“上一轮”），
    接着重读也能发现。只记上一轮最后一次改文件之后的调用；读文件的还要文件之后没改过。"""
    raw = store.get_msgs(run.conv)
    starts = [i for i, m in enumerate(raw) if m["role"] == "user" and not (m.get("meta") or {}).get("system")]
    mine = next((i for i in starts if raw[i]["created"] >= run.started), None)
    prev = max([i for i in starts if mine is not None and i < mine], default=None)
    if prev is None:
        return
    seg = raw[prev + 1:mine]
    results = {m.get("tool_call_id"): m for m in seg if m["role"] == "tool"}
    calls = []
    for m in seg:
        for c in m.get("tool_calls") or []:
            r = results.get(c["id"])
            try:
                args = json.loads(c["function"]["arguments"] or "{}")
            except ValueError:
                continue
            if r:
                calls.append((c["id"], c["function"]["name"], args, r, m["created"]))
    last = max([i for i, x in enumerate(calls) if x[1] in CHANGE_TOOLS and (x[3].get("meta") or {}).get("status") == "ok"], default=-1)
    for cid, name, args, r, created in calls[last + 1:]:
        out, status = r.get("content") or "", (r.get("meta") or {}).get("status", "ok")
        if name == "read_file":
            try:
                if os.path.getmtime(resolve(args.get("path") or "")) > created:
                    continue
            except OSError:
                continue
            _note_read(run, cid, args, out)
        _trace(run, cid, name, args, tools.title(name, args), out, status)
    for t in run.trace:          # 上一轮自己的重复不带进来：只作参照，这一轮重复了才算
        t["dup"] = None


# ---------------------------------------------------------------- 小工具
def _log_loop_event(run, model, step, event_type, think_chars, reason):
    """只落事件计数和长度，不存思考正文。单次追加完整 JSON 行。"""
    event = {"time": time.time(), "conv": run.conv, "run": run.id, "step": step,
             "model": model, "type": event_type, "think_chars": think_chars, "reason": reason}
    payload = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        fd = os.open(os.path.join(DATA_DIR, "loop_events.jsonl"), os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0), 0o600)
        try:
            if os.write(fd, payload) != len(payload):
                raise OSError("打转事件未完整写入")
        finally:
            os.close(fd)
    except OSError as exc:
        # 统计不是执行任务的前提；写盘失败只留运行日志，仍按原分支继续。
        logging.getLogger(__name__).warning("打转统计写入失败，已跳过：%s", exc)


def _looping(text, limit=THINK_LIMIT):
    """思考是否在打转。返回原因（给人看），没问题返回 None。"""
    if len(text) > limit:
        return f"超过 {limit} 字"
    if len(text) < 600:
        return None
    tail = text[-150:]
    if text.count(tail) >= 3:
        return "开始重复同样的内容"
    lines = [l.strip() for l in text.splitlines() if len(l.strip()) >= 15]
    if lines:
        top = max(set(lines), key=lines.count)
        if lines.count(top) >= 4:
            return "开始重复同样的内容"
    return None


EMOTE_RE = re.compile(r"〔([^〔〕\n]{1,6})〕[ \t]*")


def strip_emotes(text):
    """去掉回答里的表情标记（kb.EMOTE_RULE），存进历史的是干净的话。"""
    return EMOTE_RE.sub("", text or "")


class EmoteFilter:
    """流式回答里拿掉〔表情〕标记：遇到〔先攒着，凑齐〕再判断。feed 返回 (要显示的字, [表情帧名])。
    〔里超过 6 个字还没合上，就当普通文字放出去。"""

    def __init__(self):
        self.buf = ""

    def feed(self, s):
        self.buf += s
        out, faces = "", []
        while self.buf:
            i = self.buf.find("〔")
            if i < 0:
                out, self.buf = out + self.buf, ""
                break
            out, self.buf = out + self.buf[:i], self.buf[i:]
            m = EMOTE_RE.match(self.buf)
            if m:
                if m.end() == len(self.buf) and not self.buf.endswith("〕"):
                    break                       # 标记后面的空格可能还没来完，等下一段
                face = kb.EMOTES.get(m.group(1).strip())
                if face:
                    faces.append(face)
                self.buf = self.buf[m.end():]
                continue
            j = self.buf.find("〕")
            if j < 0 and len(self.buf) <= 8 and "\n" not in self.buf:
                break                           # 还没合上，等下一段
            out, self.buf = out + self.buf[0], self.buf[1:]   # 不是标记：〔照常输出
        return out, faces

    def flush(self):
        rest, self.buf = self.buf, ""
        faces = [kb.EMOTES[m.group(1).strip()] for m in EMOTE_RE.finditer(rest) if m.group(1).strip() in kb.EMOTES]
        return strip_emotes(rest), faces


def _dedupe(text, said, reasoning):
    """去掉最终回答里和之前说过的话、思考内容重复的段落。全部重复时保留原文。"""
    seen = "\n".join(said) + "\n" + (reasoning or "")
    paras = [p for p in re.split(r"\n\s*\n", text)]
    kept = [p for p in paras if len(p.strip()) < 12 or p.strip() not in seen]
    return "\n\n".join(kept).strip() if kept and len(kept) < len(paras) else text


_PATH = re.compile(r'"(?:path|page)"\s*:\s*"((?:[^"\\]|\\.)*)')


def _progress(slot):
    """模型正在生成工具参数（比如整个网页的代码）时，告诉界面写到哪了。"""
    name, args = slot["function"]["name"], slot["function"]["arguments"]
    m = _PATH.search(args)
    path = os.path.basename(m.group(1).replace("\\\\", "\\").replace("/", "\\")) if m else ""
    what = {"write_file": f"正在写 {path or '文件'}", "edit_file": f"正在改 {path or '文件'}", "run_python": "正在写代码",
            "run_js": "正在写脚本", "translate": "正在整理要翻译的文本", "generate_image": "正在写生图提示词",
            "propose_plan": "正在写计划", "update_plan": "正在写计划", "set_acceptance": "正在写验收标准"}.get(name, "正在准备下一步")
    return {"type": "writing", "title": what, "chars": len(args)}


def _detail(name, args):
    """给界面展开看的参数。写入的整份内容不放这里，界面会直接在右边打开文件。"""
    d = {}
    for k, v in args.items():
        if name == "write_file" and k == "content":
            d["lines"] = v.count("\n") + 1
            d["chars"] = len(v)
            continue
        d[k] = v[:6000] if isinstance(v, str) else v
    return d


async def _title(run, text, model=""):
    """用内存常驻的小模型起标题，失败就用前 20 个字。"""
    fallback = text.strip().splitlines()[0][:20] if text.strip() else "新对话"
    store.update_conv(run.conv, title=fallback)
    run.emit({"type": "title", "title": fallback})
    if model in BIG_MODELS:
        return fallback
    try:
        t, _ = await asyncio.wait_for(llm.complete(CFG["models"]["fast"], [{"role": "user", "content":
            "给下面这句话起一个不超过 12 个字的中文标题，只输出标题本身：\n" + text[:500]}], max_tokens=40, temperature=0.3), 30)
        t = t.strip().strip("《》\"'“”。 ")[:24]
        if t:
            store.update_conv(run.conv, title=t)
            run.emit({"type": "title", "title": t})
            return t
    except Exception:
        pass
    return fallback
