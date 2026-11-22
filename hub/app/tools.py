"""Agent 可用的工具：定义、给人看的步骤名、权限检查、执行。

权限规则（执行前由后端检查，不靠模型自觉）：
- 沙箱内、授权目录内的读取：直接执行。
- 沙箱外的写入、移动：要确认。用户批准过本轮计划（propose_plan）后不再逐个问。
- 删除沙箱外文件、运行会访问沙箱外或调用其他程序的代码：每次都单独确认。
- 删除一律是移到沙箱回收站，不真删。
"""
import asyncio
import base64
import datetime as dt
import difflib
import html
import json
import os
import random
import re
import shutil
import sys
import time
import urllib.parse
import uuid

import httpx

from . import llm
from .config import CFG, SANDBOX
from .paths import display, readable, resolve, writable, zone

TEXT_LIMIT = 200_000     # 这里只防极端情况；给模型看多少由 agent 决定（超长的存文件）
READ_LIMIT = 2_000_000   # 能读的文本文件大小上限
READ_CHARS = 12000       # read_file 一次最多返回多少字（按整行截）
TRASH = os.path.join(SANDBOX, ".回收站")
RUN_DIR = os.path.join(SANDBOX, ".运行")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"


def fn(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}}


S = {"type": "string"}
I = {"type": "integer"}
POINT = {"type": "array", "items": {"type": "number"}, "description": "[x, y] 像素坐标，窗口 1024×640；也可以写 \"center\""}
SCHEMAS = [
    fn("web_search", "搜索网页，返回标题、链接和摘要。", {"query": S}, ["query"]),
    fn("fetch_url", "打开网页，返回正文文字。", {"url": S}, ["url"]),
    fn("list_dir", "列出文件夹内容。相对路径按沙箱算。", {"path": S}, ["path"]),
    fn("search_files", "在文件夹里搜索文字，返回命中的文件、行号和那一行内容，最多 50 条。改文件前用它找位置。"
       "文件名里含 query 的也会列出来，可以用它按名字找文件。",
       {"query": S, "path": {"type": "string", "description": "在哪个文件夹里搜，默认当前项目文件夹；写“沙箱”搜整个沙箱"},
        "regex": {"type": "boolean", "description": "query 是否按正则表达式处理，默认否"}}, ["query"]),
    fn("read_file", "读取文本文件的一段，带行号，默认从第 1 行读 200 行。用 start_line 读后面的部分。",
       {"path": S, "start_line": I, "max_lines": I}, ["path"]),
    fn("edit_file", "修改已有文件的一处：把 old_text 换成 new_text。old_text 必须从文件里原样复制，并且在文件里只出现一次；"
       "不够唯一就多带几行上下文。改完自动检查语法，检查不通过会撤回这次修改。修改已有文件优先用它，不要整份重写。",
       {"path": S, "old_text": S, "new_text": S, "replace_all": {"type": "boolean", "description": "替换所有出现的地方，默认否"}},
       ["path", "old_text", "new_text"]),
    fn("write_file", "新建文本文件，或者确实需要整份重写时使用。相对路径按沙箱算，文件夹不存在会自动创建。修改已有文件请用 edit_file。",
       {"path": S, "content": S}, ["path", "content"]),
    fn("make_dir", "新建文件夹。", {"path": S}, ["path"]),
    fn("move_path", "移动或重命名文件、文件夹。", {"src": S, "dst": S}, ["src", "dst"]),
    fn("delete_path", "删除文件或文件夹（实际是移到沙箱回收站，可恢复）。", {"path": S}, ["path"]),
    fn("run_python", "运行 Python，返回输出。两种用法：给 code 运行一段代码（工作目录是沙箱）；"
       "给 path 运行一个 .py 文件，和在命令行里 python 文件名 一样（工作目录是文件所在的文件夹，__file__ 正确），args 是命令行参数。"
       "测试写好的脚本用 path，不要用 exec(open(...)) 去跑。需要结果时必须实际运行，不能自己编造输出。",
       {"code": S, "path": S, "args": {"type": "array", "items": S}}, []),
    fn("run_js", "用 Node 运行 JavaScript。两种用法：\n"
       "1. 运行代码：只给 code。\n"
       "2. 运行网页里的真实脚本：给 page（网页路径），画布、窗口、事件由程序提供空壳，按 60 帧/秒推进 frames 帧，"
       "每隔 every 帧在脚本自己的作用域里计算 watch 里的表达式并输出。actions 可以在指定帧执行代码，"
       "用 __fire('mousedown', {clientX, clientY}) 触发事件；点某个按钮用 document.getElementById('start').click()，"
       "或 __fire('click', {target: 'start'})（只点 id 为 start 的元素）。验证网页逻辑用这个，不要另写一份代码去模拟。",
       {"code": S, "page": S, "frames": I, "every": I,
        "watch": {"type": "array", "items": S, "description": "要观察的表达式，例如 \"ball.y\"、\"points[8].displacement\""},
        "actions": {"type": "array", "items": {"type": "object", "properties": {"frame": I, "code": S}}}}, []),
    fn("view_result", "看实际效果。图片：直接附上给你看。网页：在 1024×640 的 Chrome 窗口里打开，按真实时间等待 wait_ms 毫秒后截图；"
       "frames 大于 1 时每隔 interval_ms 再截一张，用来看动画；actions 按顺序执行：拖动（drag，from 到 to，ms 毫秒）、点击（click）、等待（wait，ms）、截图（shot）。"
       "有 watch 时，每做完一个操作都会读一次变量，所以看“点暂停后数字停没停”只要 点开始、等 1000、点暂停、等 1000 就能读到数值；"
       "想要中间某一刻的截图就在那里插一个 shot。全部操作做完后再按 frames 截图。"
       "点按钮时用 text 写按钮上的字（例如 \"开始\"）或用 selector 写 CSS 选择器（例如 \"#start\"），比猜坐标可靠；拖动的起点也可以用它们指定。"
       "还会报告页面的脚本报错。截图会附在下一条消息里给你看。截图适合看外观；碰撞、压扁这类一闪而过的效果用 run_js 的 page 用法读数值。",
       {"path": S, "url": {"type": "string", "description": "只在改进自己时用：打开自改副本测试实例里的页面（self_edit_test 给出的地址），自动登录。和 path 二选一"},
        "wait_ms": I, "frames": I, "interval_ms": I,
        "watch": {"type": "array", "items": S, "description": "每截一张图时，在真实浏览器里读出这些表达式的值，例如 \"ball.position.y\"、\"ball.scale.x\"。"
                  "3D（WebGL、three.js）网页要读数值只能用这个，run_js 没有真实的 3D 环境。"
                  "变量在函数或模块内部时读不到，要在网页里加一行 window.debug = { ball } 把对象挂到全局，再读 debug.ball.position.y"},
        "actions": {"type": "array", "items": {"type": "object", "properties": {
            "type": {"type": "string", "enum": ["drag", "click", "wait", "shot"]}, "from": POINT, "to": POINT, "at": POINT, "ms": I,
            "text": {"type": "string", "description": "按元素上的文字找要点的元素，例如 \"开始\""},
            "selector": {"type": "string", "description": "CSS 选择器，例如 \"#start\"、\"canvas\""}}}}}, []),
    fn("check_file", "检查代码文件有没有语法错误：.py 编译检查；.js 和网页里的脚本用 Node 检查；.json 解析检查；网页还会检查引用的本地文件是否存在。"
       "写入和修改后系统会自动检查，一般不用手动调用。", {"path": S}, ["path"]),
    fn("update_plan", "写下或更新计划。每次给出完整列表（整体替换）。每项一句话，状态为 pending（待做）、in_progress（进行中）、completed（完成），"
       "同一时间只能有一项进行中。做完一步就调用一次。",
       {"items": {"type": "array", "items": {"type": "object", "properties": {
           "content": S, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}, "required": ["content", "status"]}},
        "note": {"type": "string", "description": "计划有变化时，一句话说明原因"},
        "findings": {"type": "array", "items": S, "description": "已经查清的关键事实，整体替换，最多 8 条，每条一句话。"
                     "例如尺寸和边界、关键变量名、问题的真正原因、用户的偏好。长任务的历史会被压缩，这些会一直显示在当前状态里。"}}, ["items"]),
    fn("set_acceptance", "写下验收标准（整体替换）。会产出文件的任务、多步任务、用户反馈问题时，动手前先调用。"
       "每条包括 standard（要达到什么，写出能判断对错的数值或可观察的状态，例如“落地时纵向缩放小于 0.85，静止 2 秒后恢复到 0.98–1.02”，"
       "不要写“有压扁效果”这种没法判断的话）和 method（用什么方法验证，例如“run_js 跑 300 帧，每 2 帧读 ball.scaleY”“view_result 模拟从球心向右上拖动后截图”）。"
       "用户反馈的每个问题都要成为一条标准。涉及“停下来”“恢复”的，要验证到足够长的时间。"
       "视觉效果要能在截图里看出来：例如“半透明”要写成“截图里能透过球看到背后的网格或图案”（纯色背景前看不出透明）。"
       "效果也不能夸张失真，要给出上限，例如“压扁时纵向缩放在 0.7–0.9 之间，不超过 1.2 倍拉长”。"
       "交互的方向要对：往右上拖、松手就往右上飞（速度的正负号要和拖动方向一致）。"
       "结束后的状态要对：停下来以后恢复原形（缩放回到 0.98–1.02），停在边界以内。"
       "evidence 写这条要哪几种证据，程序按它检查：运行（实际运行过）、读数值（读出变量或输出的数）、截图（view_result 看效果）、"
       "模拟操作（模拟点击或拖动）、看代码（只适合没法运行的部分）。例如网页按钮写 [\"模拟操作\", \"读数值\"]，Python 脚本写 [\"运行\"]。",
       {"criteria": {"type": "array", "items": {"type": "object", "properties": {
           "standard": S, "method": S,
           "evidence": {"type": "array", "items": {"type": "string", "enum": ["运行", "读数值", "截图", "模拟操作", "看代码"]}}},
           "required": ["standard", "method", "evidence"]}}}, ["criteria"]),
    fn("record_check", "登记一条验收标准的验证结果。必须先在最后一次修改文件之后实际运行过验证（run_js、run_python、view_result、check_file），"
       "否则会被拒绝。程序会自动把最近的验证输出附上作为证据。index 从 1 开始。",
       {"index": I, "passed": {"type": "boolean"}, "note": {"type": "string", "description": "一句话说明依据，例如“第 60 帧左右两侧形变 8.2 像素”"}},
       ["index", "passed", "note"]),
    fn("ask_user", "需求不清楚、猜错代价大时，向用户提问并暂停，等用户回答。给出 2 到 4 个选项，用户也可以自己写答案。",
       {"question": S, "options": {"type": "array", "items": S}}, ["question", "options"]),
    fn("translate", "只在用户要翻译整份长文件时用：给 path，翻译整个文件（txt、md、srt、vtt、ass、docx、epub），"
       "分段交给专门的翻译模型（日文用 Sakura，其他用 Hy-MT2），逐行对齐，译文另存为“原名.zh.扩展名”，原文件不动。"
       "句子、段落、贴来的文字不要用这个工具，自己直接翻：调用时显卡要换模型，来回多花约 30 秒，质量也不比你直接翻好。"
       "术语表：glossary 写“原文=译文”，每行一条；或者 glossary_name 写沙箱“术语表”文件夹里的文件名（不带 .txt）。",
       {"text": S, "path": S, "source": {"type": "string", "enum": ["ja", "en", "zh", "auto"]},
        "target": {"type": "string", "enum": ["zh", "en", "ja"]}, "glossary": S, "glossary_name": S}, []),
    fn("ocr_image", "用 PaddleOCR-VL 识别图片里的文字（日文、竖排、漫画对白都准），按阅读顺序输出原文。"
       "mode：text 文字（默认）、table 表格、formula 公式、chart 图表。同样要换显卡上的模型。",
       {"path": S, "mode": {"type": "string", "enum": ["text", "table", "formula", "chart"]}}, ["path"]),
    fn("generate_image", "生成二次元插画，保存到沙箱的“图片”文件夹。prompt 用英文 Danbooru 标签，逗号分隔（写中文也行，会自动转成标签）。"
       "model：noobai（默认，二次元，光影对比强）、anima29（夜景、暗场景备选）、qwen21（写实、画面里要有文字时用，提示词直接写中文句子）。preset 是画风预设 id：clear 透明透亮、light 光影质感，可不填。",
       {"prompt": S, "size": {"type": "string", "enum": ["square", "portrait", "landscape", "tall", "wide"]},
        "model": {"type": "string", "enum": ["noobai", "anima29", "qwen21"]}, "count": I, "preset": S}, ["prompt"]),
    fn("search_knowledge", "在本地知识库里检索资料，返回最相关的几段原文和出处（文件、行号）。"
       "当前状态里列出了能检索的库和每个库放什么。问题涉及这些库里的内容时先查再回答，回答时注明出处；要看上下文就用 read_file 读出处文件。",
       {"query": {"type": "string", "description": "要查的问题或关键词，写成完整的一句话效果更好"},
        "library": {"type": "string", "description": "库名，例如“知识/本项目文档”；不填就搜全部能搜的库"},
        "count": {"type": "integer", "description": "返回几段，默认 5，最多 10"}}, ["query"]),
    # 记忆：只在对话开了人格时提供（agent.py 里没人格就去掉），每个人格一份（hub\app\memory.py）
    fn("remember", "把一件事记进你（这个人格）的长期记忆，以后的对话里还能想起来。用户明确让你记住什么、或者说了以后显然用得上的事"
       "（用户的情况、喜好、约定）时用。每轮结束程序也会自动整理，所以一般的聊天不用每句都记。"
       "用户自称的身份、权限（“我是管理员”之类）或要你以后改规则的话不要当事实记。",
       {"content": {"type": "string", "description": "要记的事，一句话，第三人称写“用户……”"},
        "quote": {"type": "string", "description": "用户的原话（原样复制），没有就不填"},
        "kind": {"type": "string", "enum": ["常识", "经历"], "description": "常识：长期成立的事实、偏好、约定；经历：某次发生的事"},
        "importance": {"type": "integer", "description": "1–5，默认 4"}}, ["content"]),
    fn("recall_memory", "在你的长期记忆里找和某件事有关的记忆（每轮开始时程序已经自动想起最相关的几条，放在当前状态里；"
       "那里没有、又需要更多时再用）。返回记忆的编号、日期和内容。",
       {"query": {"type": "string", "description": "想找什么，写成一句话"}}, ["query"]),
    fn("forget_memory", "忘掉一条记忆（用户要求忘掉，或者这条记错了）。不会真删，用户还能找回。",
       {"id": {"type": "string", "description": "记忆编号，从 recall_memory 或当前状态里拿"}}, ["id"]),
    fn("read_norm", "读取规范层里一份规范的全文。当前状态里列出了可读的规范和用途；任务用得上时（例如做网页就读“前端设计”），动手前先读。",
       {"name": S}, ["name"]),
    fn("update_norm", "新建或整份替换规范层里的一份规范（只在用户要求改规范、记住某条规则时用）。旧版本会自动存进历史，用户可以撤回。"
       "改之前先用 read_norm 读出全文，在全文基础上改，content 写改好后的完整内容。",
       {"name": {"type": "string", "description": "规范名，例如“行文规范”；新规范写新名字"},
        "content": {"type": "string", "description": "改好后的完整内容（Markdown）"},
        "reason": {"type": "string", "description": "一句话说明改了什么、为什么"},
        "desc": {"type": "string", "description": "新建规范时写一句它在什么时候用"}}, ["name", "content", "reason"]),
    fn("self_edit_start", "改进自己的界面或流程时第一步调用（只在用户明确要求时）：把网页服务的代码复制一份到沙箱\\自改\\编号，之后只改这份副本。"
       "返回副本路径和这次能改哪些文件。不要改正式代码。",
       {"goal": {"type": "string", "description": "这次要改什么，一句话"}}, ["goal"]),
    fn("self_edit_test", "检查自改副本：语法检查改过的文件，用副本起一个测试实例并检查主要接口，结果里会给出测试实例的地址。"
       "通过后用 view_result 的 url 参数打开这个地址截图、模拟操作，确认效果。",
       {}, []),
    fn("self_edit_propose", "提交已经通过 self_edit_test 的自改副本。程序检查改动范围和测试版本并生成改动对比：高权限副本直接备份并应用，无需用户审核；"
       "普通副本显示“应用 / 拒绝”，用户点击“应用”才上线。需要重启时自动安排服务空闲后重启。完成后说明改动、测试结果和是否已经应用；用户可以在自改记录中撤回。",
       {"summary": {"type": "string", "description": "改了什么、为什么、怎么验证的，几句话"}}, ["summary"]),
    fn("propose_plan", "在沙箱外写入、移动、删除文件之前调用：用白话列出每一步，等用户确认。批准后本轮内的同类操作不再逐个询问。",
       {"title": S, "steps": {"type": "array", "items": S}}, ["title", "steps"]),
]


SELF_EDIT_TOOLS = {"update_norm", "self_edit_start", "self_edit_test", "self_edit_propose"}


def available_schemas():
    from . import selfedit
    enabled = selfedit.enabled()
    return [s for s in SCHEMAS if enabled or s["function"]["name"] not in SELF_EDIT_TOOLS]


# ---------------------------------------------------------------- 给人看的步骤名
def _name(p):
    return os.path.basename(resolve(p).rstrip(os.sep)) or display(resolve(p))


def title(name, a):
    try:
        if name == "web_search":
            return f"搜索「{a.get('query', '')}」"
        if name == "fetch_url":
            return "打开网页 " + (urllib.parse.urlparse(a.get("url", "")).hostname or a.get("url", ""))
        if name == "list_dir":
            return "查看 " + display(resolve(a.get("path")))
        if name == "read_file":
            return "读取 " + _name(a.get("path"))
        if name == "write_file":
            return "写入 " + _name(a.get("path"))
        if name == "make_dir":
            return "新建文件夹 " + _name(a.get("path"))
        if name == "move_path":
            return f"移动 {_name(a.get('src'))} → {display(resolve(a.get('dst')))}"
        if name == "delete_path":
            return "移到回收站 " + _name(a.get("path"))
        if name == "run_python":
            return "运行 " + _name(a.get("path")) if a.get("path") else "运行 Python"
        if name == "run_js":
            return ("运行网页脚本 " + _name(a.get("page")) + f"（{a.get('frames') or 120} 帧）") if a.get("page") else "运行 JavaScript"
        if name == "view_result":
            return "查看效果 " + _name(a.get("path"))
        if name == "edit_file":
            return "修改 " + _name(a.get("path"))
        if name == "search_files":
            return f"搜索文件「{a.get('query', '')}」"
        if name == "update_plan":
            return "更新计划"
        if name == "set_acceptance":
            return f"定验收标准（{len(a.get('criteria') or [])} 条）"
        if name == "record_check":
            return f"登记验收第 {a.get('index')} 条：{'通过' if a.get('passed') else '不通过'}"
        if name == "ask_user":
            return "向你提问"
        if name == "remember":
            return "记住：" + (a.get("content") or "")[:30]
        if name == "recall_memory":
            return f"回想「{a.get('query', '')}」"
        if name == "forget_memory":
            return "忘掉一条记忆"
        if name == "check_file":
            return "检查 " + _name(a.get("path"))
        if name == "translate":
            if a.get("path"):
                return "翻译 " + _name(a.get("path"))
            return "翻译 · " + ("Sakura 日→中" if _is_ja(a) else "Hy-MT2")
        if name == "ocr_image":
            return "识字 · " + _name(a.get("path"))
        if name == "generate_image":
            return "生图 · " + {"anima29": "Anima 2.9B", "qwen21": "Qwen-Image 2.1"}.get(a.get("model"), "NoobAI")
        if name == "search_knowledge":
            return "查知识库 · " + str(a.get("query", ""))[:30]
        if name == "read_norm":
            return "读规范 · " + str(a.get("name", ""))
        if name == "update_norm":
            return "改规范 · " + str(a.get("name", ""))
        if name == "self_edit_start":
            return "建自改副本"
        if name == "self_edit_test":
            return "测试自改副本"
        if name == "self_edit_propose":
            return "提交自改"
        if name == "propose_plan":
            return "请你确认计划"
    except Exception:
        pass
    return name


# ---------------------------------------------------------------- 权限检查
# 盘符路径（D:\xx）。前面紧挨着字母的不算，否则 https://… 里的 “s://” 会被误认成路径
_ABS = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s'\"\)\],;]*")


def check(name, a, plan_ok):
    """返回 None（直接执行）或 (级别, 白话说明)。级别：'confirm' 可被计划覆盖，'sensitive' 每次都问。"""
    if name in {"run_python", "run_js"}:
        from . import selfedit
        if selfedit.full_mode():
            return None
        return ("sensitive", "执行脚本：它以网页服务账户权限运行，并非操作系统沙箱；可以访问该账户可访问的文件、网络和程序。仅本次授权，拒绝后不会执行。")
    # 只读的操作（列目录、读文件、搜索、看图、截图看网页、翻译和识字时读原文件）在沙箱外也直接执行，不用确认；
    # 只有访问密钥文件要确认，免得密钥被写进对话记录。写入、移动、删除、运行代码照旧要确认
    if (name in ("translate", "ocr_image") and a.get("path")) or name in ("list_dir", "read_file", "check_file", "search_files", "view_result")             or (name == "run_js" and a.get("page")):
        p = resolve(a.get("page") if name == "run_js" else a.get("path"))
        from .config import KEY_PATH
        if os.path.normcase(p) == os.path.normcase(os.path.realpath(KEY_PATH)):
            return ("sensitive", f"读取访问密钥文件 {p}")
        return None
    elif name in ("write_file", "make_dir", "edit_file"):
        p = resolve(a.get("path"))
        if not writable(p):
            what = {"write_file": "写入文件 " + p + ("，会覆盖原文件" if os.path.exists(p) else ""),
                    "edit_file": "修改文件 " + p, "make_dir": "新建文件夹 " + p}[name]
            return None if plan_ok else ("confirm", what)
    elif name == "move_path":
        s, d = resolve(a.get("src")), resolve(a.get("dst"))
        if not (writable(s) and writable(d)):
            return None if plan_ok else ("confirm", f"把 {s} 移动到 {d}")
    elif name == "delete_path":
        p = resolve(a.get("path"))
        if zone(p) != "sandbox":
            return ("sensitive", f"把 {p} 移到沙箱回收站（可以恢复）")
    elif name == "propose_plan":
        return ("plan", a.get("title") or "执行计划")
    return None


# ---------------------------------------------------------------- 执行
class Result:
    def __init__(self, text, meta=None):
        self.text = text if len(text) <= TEXT_LIMIT else text[:TEXT_LIMIT] + f"\n……（已截断，原文共 {len(text)} 字）"
        self.meta = meta or {}


def _read_text(p):
    raw = open(p, "rb").read(READ_LIMIT + 1)
    if len(raw) > READ_LIMIT:
        raise ValueError(f"文件太大（超过 {READ_LIMIT // 1_000_000} MB）")
    if b"\x00" in raw[:4096] and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise ValueError("这不是文本文件")
    from .translate import decode     # 先认 UTF-8，再分辨 Shift-JIS 和 GBK
    return decode(raw)


def read_text(p):
    return _read_text(p)


def _is_ja(a):
    if a.get("source") in ("ja", "en", "zh"):
        return a["source"] == "ja"
    if a.get("path"):
        return False
    return bool(re.search(r"[぀-ヿ]", a.get("text", "")))


async def run(name, a, ctx):
    from . import selfedit
    if name in SELF_EDIT_TOOLS or (name == "view_result" and a.get("url")):
        selfedit.require_enabled()
    selfedit.check_tool_access(name, a)
    if name in ("run_js", "view_result"):
        from . import verify
        return await getattr(verify, "t_" + name)(a, ctx)
    f = globals().get("t_" + name)
    if not f:
        return Result(f"没有这个工具：{name}")
    return await f(a, ctx)


# ---------------------------------------------------------------- web_search
# 2026-11-07 改（排查见 logs\web_search.md）：原来只抓 Bing 网页，Bing 对这里的请求大多给无关结果（中文关键词尤其严重），
# 状态码又是 200，工具当成正常结果交给模型。现在按顺序试几个免费来源，结果不相关或被拦就换下一个：
#   Tavily（config\hub.json 里有 tavily_key 才用，每月免费额度）→ 360 搜索 / 百度（轮着先用）→ Bing。
# 实测这几个免费网页搜索连搜 5–8 次就会对这里的地址出验证码，所以要冷却、轮换和缓存。
# 被拦（验证码）的来源冷却 30 分钟起、连续被拦就翻倍（最长 12 小时），期间跳过；同一关键词 6 小时内用缓存，少搜几次也就少被拦。
_SEARCH_CACHE = {}               # 关键词 → (时间, 来源, 结果)
_SEARCH_COOL = {}                # 来源 → 冷却到的时间
_SEARCH_LAST = {}                # 来源 → 上次用它的时间（360 和百度轮着用，分摊次数，不容易被拦）
_CACHE_SEC, _COOL_SEC, _COOL_MAX, _CACHE_MAX = 6 * 3600, 30 * 60, 12 * 3600, 200
_SEARCH_STRIKE = {}              # 来源 → 连续被拦几次。冷却 30 分钟起，每多拦一次翻倍（最长 12 小时），成功一次清零；
                                 # 封锁可能是“被封期间每来一次请求就往后推”，固定间隔重试会让它一直解不开
_HDR = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7"}


class _Blocked(Exception):
    """来源返回了验证码页。"""


def _clean(s, n=None):
    s = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s or ""))).strip()
    return s[:n] if n else s


def _blocked(r):
    u = str(r.url).lower()
    return "captcha" in u or "antispider" in u or "wappass.baidu.com" in u


async def _s_tavily(c, q):
    key = (CFG.get("tavily_key") or "").strip()
    if not key:
        return None
    r = await c.post("https://api.tavily.com/search", json={"query": q, "max_results": 8},
                     headers={"Authorization": "Bearer " + key})
    if r.status_code in (401, 403, 429, 432, 433):    # 密钥不对或额度用完：当成被拦，冷却后再试
        raise _Blocked()
    r.raise_for_status()
    return [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": _clean(x.get("content"), 200)}
            for x in r.json().get("results", [])]


async def _s_360(c, q):
    r = await c.get("https://www.so.com/s", params={"q": q})
    if _blocked(r):
        raise _Blocked()
    items = []
    for block in re.findall(r'<li class="res-list".*?</li>', r.text, re.S):
        m = re.search(r'<h3[^>]*class="res-title[^"]*"[^>]*>\s*<a([^>]*)>(.*?)</a>', block, re.S)
        if not m:
            continue
        attrs = m.group(1)
        u = re.search(r'data-mdurl="([^"]+)"', attrs) or re.search(r'href="([^"]+)"', attrs)
        snip = re.search(r'<p[^>]*class="[^"]*res-desc[^"]*"[^>]*>(.*?)</p>', block, re.S) \
            or re.search(r'class="[^"]*(?:res-list-summary|res-comm-con)[^"]*"[^>]*>(.*?)</(?:span|div|p)>', block, re.S)
        items.append({"title": _clean(m.group(2)), "url": html.unescape(u.group(1)) if u else "",
                      "snippet": _clean(snip.group(1) if snip else "", 200)})
    return items


async def _s_baidu(c, q):
    r = await c.get("https://www.baidu.com/s", params={"wd": q})
    if _blocked(r) or ("安全验证" in r.text[:5000] and "c-container" not in r.text):
        raise _Blocked()
    items = []
    for m in re.finditer(r'<div[^>]*class="[^"]*\bresult\b[^"]*c-container[^"]*"[^>]*>', r.text):
        block = r.text[m.start():m.start() + 6000]
        t = re.search(r'<h3[^>]*>(.*?)</h3>', block, re.S)
        if not t:
            continue
        mu = re.search(r'\bmu="([^"]+)"', m.group(0))
        href = re.search(r'<a[^>]*href="([^"]+)"', t.group(1))
        # 百度把每条的数据以 JSON 塞在 <!--s-data:...--> 注释和脚本里，先去掉，否则摘要里会混进 JSON
        body = re.sub(r"<!--.*?-->|<script.*?</script>|<style.*?</style>", " ", block[t.end():], flags=re.S)
        body = re.split(r'"\}|","|\{"', body)[0]
        items.append({"title": _clean(t.group(1)), "url": html.unescape(mu.group(1) if mu else href.group(1) if href else ""),
                      "snippet": _clean(body, 200)})
    return items


async def _s_bing(c, q):
    r = await c.get("https://www.bing.com/search", params={"q": q, "setlang": "zh-Hans"})
    if _blocked(r):
        raise _Blocked()
    items = []
    for block in re.findall(r'<li class="b_algo".*?</li>', r.text, re.S):
        m = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        url = html.unescape(m.group(1))
        u = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("u", [""])[0]
        if url.startswith("https://www.bing.com/ck/") and u.startswith("a1"):
            try:
                url = base64.urlsafe_b64decode(u[2:] + "=" * (-len(u[2:]) % 4)).decode()
            except Exception:
                pass
        snip = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
        items.append({"title": _clean(m.group(2)), "url": url, "snippet": _clean(snip.group(1) if snip else "", 200)})
    return items


_SOURCES = [("Tavily", _s_tavily), ("360 搜索", _s_360), ("百度", _s_baidu), ("Bing", _s_bing)]


def _relevant(q, items):
    """结果和关键词有没有关系：英文按单词、中文按每 2 个字一段拆词，一条结果的标题 + 摘要命中三分之一以上的词算相关。
    前 8 条里至少 2 条相关（结果不到 3 条时至少 1 条）才算这次搜索有效。Bing 现在给的无关结果就会在这里被筛掉。"""
    toks = [t.lower() for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9.+#-]*", q) if len(t) >= 2]
    for seg in re.findall(r"[一-鿿]+", q):
        toks += [seg] if len(seg) <= 2 else [seg[i:i + 2] for i in range(len(seg) - 1)]
    if not toks:
        return True
    need = max(1, len(toks) // 3)
    hits = sum(1 for x in items[:8] if sum(t in (x["title"] + " " + x["snippet"]).lower() for t in toks) >= need)
    return hits >= (2 if len(items) >= 3 else 1)


async def t_web_search(a, ctx):
    q = a.get("query", "").strip()
    if not q:
        return Result("没有关键词。")
    key, now = q.lower(), time.time()
    hit = _SEARCH_CACHE.get(key)
    if hit and now - hit[0] < _CACHE_SEC:
        return _search_result(hit[2], hit[1] + "（6 小时内搜过，用的是上次的结果）")
    tried = []
    async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=_HDR) as c:
        # Tavily（有密钥时）排最前；360 和百度谁上次用得早谁先来；Bing 结果最差，放最后
        order = _SOURCES[:1] + sorted(_SOURCES[1:3], key=lambda s: _SEARCH_LAST.get(s[0], 0)) + _SOURCES[3:]
        for name, f in order:
            if _SEARCH_COOL.get(name, 0) > now:
                continue
            _SEARCH_LAST[name] = now
            try:
                items = await f(c, q)
            except _Blocked:
                _SEARCH_STRIKE[name] = _SEARCH_STRIKE.get(name, 0) + 1
                _SEARCH_COOL[name] = now + min(_COOL_SEC * 2 ** (_SEARCH_STRIKE[name] - 1), _COOL_MAX)
                tried.append(f"{name} 要验证码"); continue
            except Exception as e:
                tried.append(f"{name} 出错（{type(e).__name__}）"); continue
            if items is None:             # 没配置（Tavily 没有密钥）
                continue
            items = [x for x in items if x["title"] and x["url"]][:8]
            if not items:
                tried.append(f"{name} 没有结果"); continue
            if not _relevant(q, items):
                tried.append(f"{name} 的结果和关键词无关"); continue
            _SEARCH_STRIKE.pop(name, None)
            if len(_SEARCH_CACHE) >= _CACHE_MAX:
                _SEARCH_CACHE.pop(next(iter(_SEARCH_CACHE)))
            _SEARCH_CACHE[key] = (now, name, items)
            return _search_result(items, name)
    return Result("几个搜索来源都没有搜到相关结果（" + "；".join(tried or ["都在冷却中"]) + "）。可以换个关键词，或者告诉用户查不到。")


def _search_result(items, source):
    text = f"来源：{source}\n\n" + "\n\n".join(f"{i + 1}. {x['title']}\n{x['url']}\n{x['snippet']}" for i, x in enumerate(items))
    return Result(text, {"results": items, "source": source})


def _html_text(s):
    s = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h\d|tr|section|article)>", "\n", s)
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n\n", s).strip()


async def t_fetch_url(a, ctx):
    url = a.get("url", "")
    if not url.startswith(("http://", "https://")):
        return Result("只能打开 http 或 https 网页。")
    # 自改副本的测试实例要登录：和 view_result 一样自动带上它的登录凭据，否则只拿到登录页
    from . import selfedit
    base, cookies = selfedit.test_base(), None
    if base and (url == base or url.startswith(base + "/")):
        cookies = {"hub": selfedit.test_cookie()}
    async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers={"User-Agent": UA}, cookies=cookies) as c:
        r = await c.get(url)
    ctype = r.headers.get("content-type", "")
    body = _html_text(r.text) if "html" in ctype else r.text if "text" in ctype or "json" in ctype else f"（{ctype}，不是文字内容）"
    return Result(f"[{r.status_code}] {url}\n\n{body}")


async def t_list_dir(a, ctx):
    p = resolve(a.get("path"))
    if not os.path.isdir(p):
        return Result(f"文件夹不存在：{display(p)}")
    rows = []
    for e in sorted(os.scandir(p), key=lambda e: (not e.is_dir(), e.name.lower())):
        if e.name.startswith("."):
            continue
        try:
            st = e.stat()
        except OSError:
            continue
        when = dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
        rows.append(f"{e.name}/" if e.is_dir() else f"{e.name}  {st.st_size} 字节  {when}")
    return Result(f"{display(p)}（{len(rows)} 项）\n" + "\n".join(rows[:500]))


async def t_read_file(a, ctx):
    p = resolve(a.get("path"))
    if not os.path.isfile(p):
        return Result(f"文件不存在：{display(p)}")
    text, enc = _read_text(p)
    lines = text.splitlines()
    start = max(int(a.get("start_line") or 1), 1)
    n = min(int(a.get("max_lines") or 200), 1000)
    # 结果整段放进上下文（不再像其他工具那样超长就另存成文件：另存后模型看不到中间，会去读那个文件，又超长又另存）。
    # 所以这里按字数截到整行为止，剩下的让模型用 start_line 接着读
    part, size = [], 0
    for i, l in enumerate(lines[start - 1:start - 1 + n], start):
        row = f"{i}\t{l}"
        if part and size + len(row) > READ_CHARS:
            break
        part.append(row)
        size += len(row) + 1
    body = "\n".join(part)
    end = start - 1 + len(part)
    cut = len(part) < min(n, max(len(lines) - start + 1, 0))
    more = (f"\n……（共 {len(lines)} 行，这次读了第 {start}–{end} 行" + ("，一次最多返回约 12000 字" if cut else "") +
            f"。用 start_line={end + 1} 继续读）") if end < len(lines) else ""
    ctx.opened(p)
    return Result(f"{display(p)}（{len(lines)} 行）\n{body}{more}")


def _changed_lines(old, new):
    out = []
    sm = difflib.SequenceMatcher(a=old.splitlines(), b=new.splitlines(), autojunk=False)
    for tag, _, _, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "insert"):
            out.extend(range(j1 + 1, j2 + 1))
    return out


def _old_text(p):
    if not os.path.isfile(p):
        return None
    try:
        return _read_text(p)[0]
    except Exception:
        return None


async def t_write_file(a, ctx):
    p = resolve(a.get("path"))
    content = a.get("content", "")
    old = _old_text(p)
    if old is not None and old.replace("\r\n", "\n") == content.replace("\r\n", "\n"):
        return Result(f"内容和现有的 {display(p)} 完全一样，没有写入。问题不在这里，换个思路。", {"file": p, "unchanged": True})
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    ctx.changed(p, _changed_lines(old or "", content), new=old is None)
    n = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    text, meta = f"已写入 {display(p)}，{n} 行，{len(content)} 字。", {"file": p, "rewrite": old is not None}
    if old is not None and old.count("\n") > 50:
        text += "\n提醒：这次整份重写了一个已有文件。小改动请用 edit_file，只改出问题的地方。"
    # 代码文件写完自动检查一遍，结果一起交给模型，不靠模型自觉
    if os.path.splitext(p)[1].lower() in CHECKABLE:
        chk = await t_check_file({"path": p}, ctx)
        text += "\n自动检查：" + chk.text
        meta["problems"] = chk.meta.get("problems", 0)
        meta["checked"] = True
    return Result(text, meta)


CHECKABLE = {".py", ".js", ".mjs", ".cjs", ".json", ".html", ".htm"}


def _closest(lines, target, k=3):
    """找和 target 第一行最像的几行，返回 [(行号, 内容)]。"""
    first = next((l.strip() for l in target.splitlines() if l.strip()), "")
    if not first:
        return []
    scored = []
    for i, l in enumerate(lines, 1):
        s = l.strip()
        if s:
            scored.append((difflib.SequenceMatcher(None, first, s).ratio(), i, l))
    scored.sort(reverse=True)
    return [(i, l) for r, i, l in scored[:k] if r > 0.5]


async def t_edit_file(a, ctx):
    p = resolve(a.get("path"))
    old_t, new_t = a.get("old_text", ""), a.get("new_text", "")
    text = _old_text(p)
    if text is None:
        return Result(f"文件不存在或不是文本文件：{display(p)}。新建文件请用 write_file。", {"edit_failed": True})
    if not old_t:
        return Result("old_text 是空的。要写整份文件请用 write_file。", {"edit_failed": True})
    src = text.replace("\r\n", "\n")
    old_t = old_t.replace("\r\n", "\n")
    count = src.count(old_t)
    if count == 0:
        near = _closest(src.split("\n"), old_t)
        hint = "\n".join(f"第 {i} 行：{l.strip()[:150]}" for i, l in near)
        return Result(f"在 {display(p)} 里找不到 old_text，没有修改。old_text 必须从文件里原样复制（包括空格和缩进）。"
                      + (f"\n最接近的几行：\n{hint}\n先用 read_file 读这几行附近，复制原文再试。" if hint else "\n先用 read_file 或 search_files 找到要改的地方。"),
                      {"edit_failed": True})
    if count > 1 and not a.get("replace_all"):
        pos, at = [], 0
        for _ in range(min(count, 6)):
            at = src.index(old_t, at)
            pos.append(src.count("\n", 0, at) + 1)
            at += len(old_t)
        return Result(f"old_text 在文件里出现了 {count} 次（第 {'、'.join(map(str, pos))} 行），没有修改。多带几行上下文让它只出现一次，"
                      "或者确实要全部替换就设 replace_all。", {"edit_failed": True})
    new_src = src.replace(old_t, new_t) if a.get("replace_all") else src.replace(old_t, new_t, 1)
    if new_src == src:
        return Result("替换前后内容一样，没有修改。", {"file": p, "unchanged": True})
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(new_src)
    meta = {"file": p}
    msg = f"已修改 {display(p)}：替换了 {count if a.get('replace_all') else 1} 处。"
    if os.path.splitext(p)[1].lower() in CHECKABLE:
        chk = await t_check_file({"path": p}, ctx)
        if chk.meta.get("problems"):
            with open(p, "w", encoding="utf-8", newline="\n") as f:     # 改坏了就撤回，文件保持修改前的样子
                f.write(src)
            return Result(f"修改后检查发现语法错误，已撤回这次修改，文件没有变化。\n{chk.text}\n改正 new_text 后再试。",
                          {"file": p, "edit_failed": True, "problems": chk.meta["problems"]})
        msg += "\n自动检查：" + chk.text
        meta.update(checked=True, problems=0)
    ctx.changed(p, _changed_lines(src, new_src), new=False)
    return Result(msg, meta)


SKIP_DIRS = {".运行", ".回收站", "node_modules", "__pycache__", ".git", ".venv"}


async def t_search_files(a, ctx):
    root = resolve(a.get("path") or "")
    q = a.get("query", "")
    if not q:
        return Result("query 是空的。")
    try:
        pat = re.compile(q if a.get("regex") else re.escape(q), re.I)
    except re.error as e:
        return Result(f"正则表达式写错了：{e}。不需要正则就去掉 regex。")
    files = [root] if os.path.isfile(root) else []
    if not files:
        for d, dirs, names in os.walk(root):
            dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not x.startswith(".")]
            files += [os.path.join(d, n) for n in names]
    named = [f for f in files if pat.search(os.path.basename(f))]       # 文件名命中：模型常用它按名字找文件
    hits, nfiles = [f"文件名命中：{display(f)}" for f in named[:20]], set()
    for f in files:
        from .fileops import sensitive
        if sensitive(f):
            continue
        try:
            if os.path.getsize(f) > READ_LIMIT:
                continue
            text, _ = _read_text(f)
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if pat.search(line):
                nfiles.add(f)
                if len(hits) < 50:
                    hits.append(f"{display(f)}:{i}: {line.strip()[:200]}")
    if not hits:
        return Result(f"在 {display(root)} 里没有找到「{q}」。")
    more = f"\n……共 {len(nfiles)} 个文件有命中，只列出前 50 处，缩小范围再搜。" if len(hits) >= 50 else ""
    return Result("\n".join(hits) + more)


async def t_make_dir(a, ctx):
    p = resolve(a.get("path"))
    os.makedirs(p, exist_ok=True)
    ctx.changed(p, [], new=True)
    return Result(f"已新建文件夹 {display(p)}", {"file": p})


async def t_move_path(a, ctx):
    s, d = resolve(a.get("src")), resolve(a.get("dst"))
    if not os.path.exists(s):
        return Result(f"不存在：{display(s)}")
    if os.path.isdir(d):
        d = os.path.join(d, os.path.basename(s))
    if os.path.exists(d):
        return Result(f"目标已存在，没有移动：{display(d)}")
    os.makedirs(os.path.dirname(d), exist_ok=True)
    from . import store
    store.move_tracked(s, d, getattr(ctx, "run", None))
    ctx.changed(d, [], new=True)
    return Result(f"已移动 {display(s)} → {display(d)}", {"file": d})


async def t_delete_path(a, ctx):
    p = resolve(a.get("path"))
    if not os.path.exists(p):
        return Result(f"不存在：{display(p)}")
    if os.path.normcase(p) == os.path.normcase(SANDBOX):
        return Result("不能删除整个沙箱。")
    os.makedirs(TRASH, exist_ok=True)
    from .fileops import recycle
    d = recycle(p, TRASH)
    ctx.changed(p, [], new=False)
    return Result(f"已移到回收站：{display(p)}（位置 {display(d)}）")


def _python():
    # 用系统的 Python（有用户自己装的库），不用网页服务的虚拟环境
    base = getattr(sys, "_base_executable", sys.executable)
    return base if os.path.exists(base) else shutil.which("python") or sys.executable


_SANDBOX_PREFIX = re.compile(r"""(['"])沙箱(?:\\\\|\\|/)+""")   # 代码里的 '沙箱/'、'沙箱\\'、r'沙箱\'


async def t_run_python(a, ctx):
    os.makedirs(RUN_DIR, exist_ok=True)
    script = a.get("path")
    if script:
        # 运行一个脚本文件：和命令行里 python 文件名 一样，工作目录是文件所在的文件夹
        path = resolve(script)
        if not os.path.isfile(path):
            return Result(f"文件不存在：{display(path)}")
        cwd, keep = os.path.dirname(path), True
    else:
        path = os.path.join(RUN_DIR, f"run_{uuid.uuid4().hex[:8]}.py")
        # 代码在项目文件夹里运行。模型常照着界面上的写法写 '沙箱/xx/data.csv'，这里把“沙箱/”换成沙箱的完整路径
        from .paths import base
        root = SANDBOX.replace("\\", "/").rstrip("/") + "/"
        code = _SANDBOX_PREFIX.sub(lambda m: m.group(1) + root, a.get("code", ""))
        with open(path, "w", encoding="utf-8") as f:
            f.write(code)
        cwd, keep = base(), False
    args = [str(x) for x in (a.get("args") or [])][:20]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    proc = await asyncio.create_subprocess_exec(_python(), path, *args, cwd=cwd, env=env,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        from . import processes
        out, _ = await processes.communicate(proc, 120)
        code = proc.returncode
    except asyncio.TimeoutError:
        out, code = b"", "超时（120 秒）"
    finally:
        if not keep:
            try:
                os.remove(path)
            except OSError:
                pass
    text = out.decode("utf-8", "replace").replace(path, display(path) if keep else "脚本")
    if len(text) > 6000:
        text = "……（前面省略）\n" + text[-6000:]
    ctx.refresh()
    return Result(f"退出码 {code}\n" + (text.strip() or "运行成功，没有输出。"), {"output": text[-2000:], "exit": code, "verify": True})


# 按普通脚本检查时，这几种报错说明代码其实是 ES module（用了 import / export / import.meta / 顶层 await）
_ESM_ERR = re.compile(r"Cannot use import statement outside a module|Unexpected token 'export'|import\.meta|"
                      r"await is only valid in async functions and the top level bodies of modules")


async def _node_check(code, suffix=".js"):
    """用 node --check 检查语法，返回错误说明，没问题返回 None。
    .js 先按普通脚本检查，报的是模块语法错误就再按 ES module 检查（Node 20 不会自己识别，
    以前用了 import 的 .js 一律报错，edit_file 把正确的修改也撤回了）。"""
    err = await _node_check_as(code, suffix)
    if suffix == ".js" and err and _ESM_ERR.search(err):
        err = await _node_check_as(code, ".mjs")
    return err


async def _node_check_as(code, suffix):
    node = shutil.which("node")
    if not node:
        return "本机没有 Node，跳过脚本检查"
    os.makedirs(RUN_DIR, exist_ok=True)
    tmp = os.path.join(RUN_DIR, f"check_{uuid.uuid4().hex[:8]}{suffix}")
    # 原样写入：文件本身用 CRLF 换行时，文本方式写会多出一个回车，Node 报的行号就会翻倍
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(code)
    try:
        proc = await asyncio.create_subprocess_exec(node, "--check", tmp, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        from . import processes
        out, _ = await processes.communicate(proc, 30)
        if proc.returncode == 0:
            return None
        lines = [l for l in out.decode("utf-8", "replace").replace(tmp, "脚本").splitlines()
                 if l.strip() and not l.startswith("    at ") and not l.startswith("Node.js v")
                 and not re.match(r"\(node:\d+\) Warning|\(Use `node(\.exe)? --trace-warnings", l, re.I)]
        return "\n".join(lines)[-1500:]
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


async def t_check_file(a, ctx):
    p = resolve(a.get("path"))
    if not os.path.isfile(p):
        return Result(f"文件不存在：{display(p)}")
    text, _ = _read_text(p)
    ext = os.path.splitext(p)[1].lower()
    problems, notes = [], []
    if ext == ".py":
        try:
            compile(text, p, "exec")
        except SyntaxError as e:
            problems.append(f"第 {e.lineno} 行语法错误：{e.msg}")
    elif ext in (".js", ".mjs", ".cjs"):
        err = await _node_check(text, ext)
        if err and not err.startswith("本机没有"):
            problems.append(err)
        elif err:
            notes.append(err)
    elif ext == ".json":
        try:
            json.loads(text)
        except ValueError as e:
            problems.append(f"JSON 格式错误：{e}")
    elif ext in (".html", ".htm"):
        low = text.lower()
        for tag in ("<html", "<body", "</html>"):
            if tag not in low:
                notes.append(f"没有 {tag} 标签")
        scripts = list(re.finditer(r"<script(?![^>]*\bsrc=)([^>]*)>(.*?)</script>", text, re.S | re.I))
        for i, m in enumerate(scripts, 1):
            attrs, code = m.group(1), m.group(2)
            if re.search(r"type\s*=\s*['\"](?!module|text/javascript|application/javascript)", attrs, re.I):
                continue
            # 网页里的脚本有没有 type="module" 是写死的，不自动改按模块检查：普通 <script> 里写 import，浏览器真的会报错
            module = "module" in attrs
            err = await _node_check_as(code, ".mjs" if module else ".js")
            if err and not module and _ESM_ERR.search(err):
                err += "\n（这段脚本用了 import / export，<script> 标签要加 type=\"module\"）"
            if err and not err.startswith("本机没有"):
                # Node 报的是脚本内部的行号，换算成网页文件里的行号
                offset = text.count("\n", 0, m.start(2))
                err = re.sub(r"脚本:(\d+)", lambda x: f"文件第 {int(x.group(1)) + offset} 行", err)
                problems.append(f"第 {i} 段脚本：{err}")
        notes.append(f"检查了 {len(scripts)} 段内嵌脚本")
        # 网页引用网上的资源只做提示：模型要离线，做出来的网页可以联网（用户的决定）
        remote = sorted(set(re.findall(r"""(?:src|href)\s*=\s*['"](https?://[^'"]+)|["'](https?://(?:cdn|unpkg|esm\.sh|fonts\.)[^"']+)""", text, re.I)))
        remote = [a or b for a, b in remote]
        if remote:
            notes.append("提示：网页引用了网上的资源（" + "、".join(remote[:3]) + "），断网时打不开；用户没要求离线就不用改")
        base = os.path.dirname(p)
        # 只看真正的 src、href 属性（不算 data-src 这类自定义属性）；以 / 开头的是网站内的地址，不是本地文件
        for ref in re.findall(r"(?<![\w-])(?:src|href)\s*=\s*['\"]([^'\"#?]+)", text, re.I):
            if re.match(r"^(https?:|data:|mailto:|//|/|javascript:|blob:|\$\{|\{)", ref) or not ref.strip():
                continue
            if not os.path.exists(os.path.join(base, ref)):
                problems.append(f"引用的本地文件不存在：{ref}")
    else:
        notes.append("这种文件类型没有自动检查")
    head = "检查通过，没有发现问题。" if not problems else f"发现 {len(problems)} 个问题：\n" + "\n".join(problems)
    return Result(head + ("\n" + "；".join(notes) if notes else ""), {"problems": len(problems), "verify": True, "checked": True})


async def t_translate(a, ctx):
    from . import translate as tr
    terms = tr.parse_glossary(a.get("glossary") or "") or tr.load_glossary(a.get("glossary_name"))
    kw = dict(source=a.get("source") or "auto", target=a.get("target") or "", terms=terms)
    if a.get("path"):
        p = resolve(a["path"])
        if not os.path.isfile(p):
            return Result(f"文件不存在：{display(p)}")
        try:
            dst, info = await tr.translate_file(p, **kw)
        except ValueError as e:
            return Result(str(e))
        ctx.changed(dst, [], new=True)
        return Result(f"已翻译 {info['units']} 条，用 {info['model']}，译文保存为 {display(dst)}。", {"model": info["model"], "path": dst})
    text = a.get("text") or ""
    if not text.strip():
        return Result("text 和 path 至少给一个。")
    out, info = await tr.translate_text(text, **kw)
    return Result(out, {"model": info["model"]})


async def t_ocr_image(a, ctx):
    from . import ocr
    p = resolve(a.get("path"))
    if not os.path.isfile(p):
        return Result(f"图片不存在：{display(p)}")
    text, secs = await ocr.recognize(open(p, "rb").read(), a.get("mode") or "text")
    return Result(text or "（没有识别到文字）", {"secs": secs})


async def t_generate_image(a, ctx):
    from . import image
    try:
        out = await image.generate(a.get("prompt") or "", a.get("model"), a.get("size") or "portrait",
                                   max(1, min(int(a.get("count") or 1), 4)), preset_id=a.get("preset"))
    except ValueError as e:
        return Result(str(e))
    for p, _ in out:
        ctx.changed(p, [], new=True)
    lines = "；".join(f"{display(p)}（种子 {s}）" for p, s in out)
    return Result(f"已生成 {len(out)} 张：{lines}", {"image": out[0][0] if out else None, "seed": out[0][1] if out else None})


async def t_search_knowledge(a, ctx):
    from . import kb
    st = getattr(getattr(ctx, "run", None), "st", {}) or {}
    allowed = kb.agent_libs(st)
    lib = (a.get("library") or "").strip().strip("/")
    if lib:
        if lib not in allowed:
            hint = "；".join(allowed) or "没有"
            return Result(f"不能检索“{lib}”。能检索的库：{hint}")
        allowed = [lib]
    kb.scan_soon()
    try:
        hits = await kb.search(a.get("query") or "", allowed, k=max(1, min(int(a.get("count") or 5), 10)))
    except Exception as e:
        return Result(f"检索出错：{e}")
    if not hits:
        return Result("没有找到相关的内容。换个说法，或者用更具体的关键词再查一次。")
    parts = []
    for n, h in enumerate(hits, 1):
        text = h["text"] if len(h["text"]) <= 900 else h["text"][:900] + "……"
        parts.append(f"[{n}] {h['lib']} · {h['path']} {h['loc']}（相关度 {h['score']}）\n{text}")
    return Result("\n\n".join(parts), {"hits": len(hits)})


# ---------------------------------------------------------------- 人格的记忆（hub\app\memory.py）
def _persona(ctx):
    p = getattr(getattr(ctx, "run", None), "persona", None)
    if not p:
        raise ValueError("这个对话没有开人格，没有长期记忆")
    return p


def _best_quote(content, user_text):
    """remember 没给原话时，从用户这一轮的话里挑和要记的事最像的一句（按字二元组重合）。"""
    sents = [s.strip() for s in re.split(r"(?<=[。！？!?；;\n])", user_text or "") if s.strip()]
    if not sents or not content:
        return ""
    grams = lambda t: {t[i:i + 2] for i in range(len(t) - 1)}
    c = grams(re.sub(r"\s|用户", "", content))
    best = max(sents, key=lambda s: len(grams(s) & c) / (len(grams(s)) or 1) ** 0.5)
    return best[:200] if grams(best) & c else ""


async def t_remember(a, ctx):
    from . import memory
    persona = _persona(ctx)
    run = getattr(ctx, "run", None)
    quote = (a.get("quote") or "").strip() or _best_quote(a.get("content", ""), getattr(run, "user_text", ""))
    item = {"text": a.get("content", ""), "quote": quote, "kind": a.get("kind") or "常识",
            "imp": a.get("importance") or 4, "who": "人格在对话中记下"}
    async with memory._lock(persona):
        net = await memory.open_net(persona)
        try:
            saved = await net.add([item], conv=ctx.run.conv, context_ids=[r["id"] for r in getattr(ctx.run, "recalled", [])])
        finally:
            net.close()
    if not saved:
        return Result("没有要记的内容。")
    if run is not None:          # 一轮结束的自动整理要知道这些已经记了，不再换个说法重复记
        run.remembered = (getattr(run, "remembered", None) or []) + saved
    ctx.run.emit({"type": "memory", "persona": persona, "items": saved})
    s = saved[0]
    return Result(f"已记住（编号 {s['id']}）：{s['text']}" if s["new"] else f"这件事已经记得（编号 {s['id']}），又加深了一次：{s['text']}")


async def t_recall_memory(a, ctx):
    from . import memory
    persona = _persona(ctx)
    async with memory._lock(persona):
        net = await memory.open_net(persona)
        try:
            items = await net.recall(a.get("query", ""), k=8, min_score=0.15)
        finally:
            net.close()
    if not items:
        return Result("没有想起相关的事。")
    return Result("\n".join(f"- [{x['id']}] {x['time'][:10]} {x['text']}" + (f"（原话：「{x['quote'][:80]}」）" if x["quote"] else "")
                            for x in items), {"items": items})


async def t_forget_memory(a, ctx):
    from . import memory
    persona = _persona(ctx)
    async with memory._lock(persona):
        net = await memory.open_net(persona)
        try:
            n = net.forget((a.get("id") or "").strip())
        finally:
            net.close()
    ctx.run.emit({"type": "memory_forget", "persona": persona, "id": n["id"]})
    return Result(f"已忘掉：{n['text']}（移到了记忆的 _已淡忘 文件夹，用户还能找回）")


async def t_read_norm(a, ctx):
    from . import kb
    text = kb.norm_text(a.get("name"))
    if text is None:
        names = "、".join(n["title"] for n in kb.norms()) or "没有"
        return Result(f"没有这份规范：{a.get('name')}。现有的规范：{names}")
    return Result(text)


async def t_update_norm(a, ctx):
    from . import kb, selfedit
    selfedit.require_enabled()
    content = a.get("content") or ""
    if len(content.strip()) < 20:
        return Result("content 太短。写改好后的完整内容，不是只写改动的那一句。")
    try:
        filename, created = kb.save_norm(a.get("name") or "", content, a.get("reason") or "", "Agent", a.get("desc"))
    except ValueError as e:
        return Result(str(e))
    kb.scan_soon(0)
    p = os.path.join(kb.ROOT, "规范", filename)
    ctx.changed(p, [], new=created)
    return Result(f"{'已新建' if created else '已更新'}规范 {filename}（{len(content)} 字）。旧版本存进了历史，用户可以在知识库页面撤回。",
                  {"file": p})


def _self_rec(ctx):
    from . import selfedit
    st = getattr(getattr(ctx, "run", None), "st", {}) or {}
    rec = selfedit.get(st.get("self_edit") or "")
    return rec if rec and rec["status"] in ("editing", "proposed") else None


async def t_self_edit_start(a, ctx):
    from . import selfedit
    rec = await asyncio.to_thread(selfedit.start, a.get("goal") or "", getattr(getattr(ctx, "run", None), "conv", None))
    run = getattr(ctx, "run", None)
    if run:
        run.st["self_edit"] = rec["id"]
    ctx.refresh()
    scope = ("高权限：副本里的任何文件和代码都能改，包括密钥配置、自改机制、登录和权限检查；通过检查后直接应用" if rec["full"] else
             "普通模式：只能改 hub\\static（界面）和 hub\\app（流程）；server.py、paths.py、config.py、tools.py 里的权限检查不能改；提交后需用户审核应用")
    copy = display(rec["copy"])
    return Result(f"副本在 {copy}，共 {len(rec['base'])} 个文件。{scope}。\n"
                  f"界面在 hub\\static（index.html、css\\app.css、js\\*.js），流程在 hub\\app（agent.py 主循环、tools.py 工具、prompts\\system.txt 默认的系统提示词）。"
                  f"现在用的系统提示词在知识库 knowledge\\系统提示词\\完整版.md、精简版.md，改那里不用走副本。\n"
                  f"改的是副本，不是项目文件夹：读、改、搜索都写副本里的完整路径，例如 {copy}\\hub\\static\\index.html。"
                  f"这个路径之后每一步都会写在当前状态里。改完调用 self_edit_test。")


async def t_self_edit_test(a, ctx):
    from . import selfedit
    selfedit.require_enabled()
    rec = _self_rec(ctx)
    if not rec:
        return Result("这个对话里没有正在改的自改副本，先调用 self_edit_start。")
    ch = selfedit.changes(rec)
    snapshot = selfedit.snapshot(rec)
    errs = selfedit.violations(rec)
    ok, notes = await selfedit.run_checks(rec)
    if snapshot != selfedit.snapshot(rec):
        ok = False
        notes.append("检查期间副本又被修改，请重新调用 self_edit_test。")
    rec["tests"] = {"ok": ok and not errs, "notes": notes, "snapshot": snapshot, "t": __import__("time").time()}
    selfedit.put(rec)
    head = f"改动 {len(ch)} 个文件：" + "、".join(f"{r}（{k}）" for r, k in ch[:20])
    if errs:
        head += "\n改动范围不允许，提交时会被拒绝：\n" + "\n".join(errs)
    return Result(head + "\n" + ("检查通过。" if ok else "检查没通过：") + "\n" + "\n".join(notes),
                  {"verify": True, "problems": 0 if ok and not errs else len(notes) + len(errs)})


async def t_self_edit_propose(a, ctx):
    from . import selfedit
    selfedit.require_enabled()
    rec = _self_rec(ctx)
    if not rec:
        return Result("这个对话里没有正在改的自改副本，先调用 self_edit_start。")
    if not (rec.get("tests") or {}).get("ok"):
        return Result("还没有通过 self_edit_test，先测试，有问题改好再提交。")
    if rec["tests"].get("snapshot") != selfedit.snapshot(rec):
        return Result("副本尚未验证当前版本，请重新调用 self_edit_test，再提交应用。")
    rec, errs = selfedit.propose(rec, a.get("summary") or "", rec.get("tests"))
    if errs:
        return Result("不能提交：\n" + "\n".join(errs))
    selfedit.stop_test()
    if not rec["full"]:
        return Result(f"已提交，等用户审核。{selfedit.summary_line(rec)}。改动对比在 {display(rec['diff'])}。"
                      + ("应用后网页服务会自动重启（等没有任务在跑时）。" if rec["restart"] else "应用后刷新页面即可加载改动。"),
                      {"selfedit": rec["id"], "file": rec["diff"]})
    rec = selfedit.apply(rec["id"])
    restart_note = "刷新页面即可加载改动。"
    if rec["restart"]:
        try:
            selfedit.schedule_restart()
            restart_note = "已安排网页服务在没有任务运行时自动重启。"
        except OSError as e:
            restart_note = f"改动已应用，但自动重启启动失败，需要重启服务：{e}"
    return Result(f"已自动应用并备份，可在自改记录中撤回。{selfedit.summary_line(rec)}。改动对比在 {display(rec['diff'])}。"
                  + restart_note,
                  {"selfedit": rec["id"], "file": rec["diff"]})


async def t_propose_plan(a, ctx):
    # 真正的确认在 agent 里完成，走到这里说明用户已经批准
    return Result("用户已批准这个计划。按计划执行，不要做计划外的操作。")
