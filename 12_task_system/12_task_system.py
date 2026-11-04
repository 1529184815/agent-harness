"""
    任务系统（task system）：通过开关在"任务系统"和"todo_list"之间切换。

    TASK_SYSTEM_ENABLED = True  启用任务系统；False 退回 todo_list（前几章的简单待办列表）。

    任务系统（TaskSystem）：
      - 任务拆分（decompose）：把目标拆成子任务，支持依赖关系
      - 任务领用（claim）/ 完成（complete）两个动作
      - 三状态：pending（待领用）-> in_progress（已领用/执行中）-> done（已完成）
      - 持久化：任务状态写入磁盘 tasks.json，重启可恢复
      - 依赖管控：依赖未完成的任务不可领用
      - 互斥：同一时刻最多一个任务执行中，期间其他任务不能做任何操作
      - 加锁/释放锁：threading.Lock 串行化并发状态变更

    错误恢复、系统提示词、记忆、子代理、skill、上下文压缩均沿用前几章。
"""
import os
import sys
import re
import json
import yaml
import threading
from datetime import datetime
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from langchain.messages import HumanMessage, AIMessage, ToolMessage, SystemMessage, AnyMessage
from pathlib import Path
import subprocess


load_dotenv()

api_key = os.getenv("DEEPSEAK_API_KEY")

base_url = os.getenv("DEEPSEAK_BASE_URL")

model_name = os.getenv("DEEPSEAK_MODEL")
WORKDIR = Path.cwd()

# 危险命令黑名单：命中即 deny（拒绝） 正常情况不会直接这么做
DANGEROUS_COMMANDS = ["rm -rf /", "sudo", "shutdown", "reboot", "format ", "del /f /s /q c:", "del"]

def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path
@tool
def run_read(path, limit=None):
    """读取文件并返回内容
    :param path: 文件路径
    :param limit: 可选参数，返回的行数限制
    :return: 文件内容
    """
    lines = safe_path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    if limit:
        lines = lines[:limit]
    return "\n".join(lines)

@tool
def run_write(path, content):
    """写入文件
    :param path: 文件路径
    :param content: 要写入的内容
    :return: 提示信息
    """
    safe_path(path).write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} bytes to {path}"

@tool
def run_edit(path, old_text, new_text):
    """编辑文件
    :param path: 文件路径
    :param old_text: 要替换的旧文本
    :param new_text: 新文本
    :return: 提示信息
    """
    text = safe_path(path).read_text(encoding="utf-8")
    if old_text not in text:
        return "Error: text not found"
    safe_path(path).write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
    return f"Edited {path}"

@tool
def run_glob(pattern):
    """匹配文件
    :param pattern: 匹配模式
    :return: 匹配的文件列表
    """
    import glob as g
    return "\n".join(g.glob(pattern, root_dir=WORKDIR))


@tool
def run_cmd(command:str):
    """在 Windows 命令行(cmd.exe)中执行命令并返回输出。
    示例：'echo hello > hello.txt'、'dir'、'type hello.txt'、'del hello.txt'
    注意：这是 Windows CMD 语法，不是 bash/Linux 语法（没有 ls/cat/touch）。
    :param command: 要执行的 cmd 命令
    :return: 命令的输出
    """
    if any(d in command.lower() for d in DANGEROUS_COMMANDS):
        return "错误：危险命令被阻止"
    try:
        r = subprocess.run(
            command,
            shell=True,
            cwd=os.getcwd(),
            capture_output=True,
            text=True,
            timeout=120,
            encoding="gbk",       # Windows CMD 中文输出多为 GBK，避免乱码
            errors="replace",
        )
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "错误：超时（120秒）"
    except (FileNotFoundError, OSError) as e:
        return f"错误：{e}"

# ---- 待办列表（todo list）----
TODO_STATUS = ("pending", "in_progress", "complete")

class TodoList:
    """待办列表：每个任务有 pending / in_progress / complete 三种状态，按顺序推进。"""
    def __init__(self):
        self.items: list[dict] = []  # {"title": str, "status": str}

    def set(self, titles: list[str]):
        """用新计划替换整个列表，全部置为 pending。"""
        self.items = [{"title": t, "status": "pending"} for t in titles]

    def next(self):
        """把第一个 pending 置为 in_progress 并返回；无待办时返回 None。"""
        for it in self.items:
            if it["status"] == "pending":
                it["status"] = "in_progress"
                return it
        return None

    def complete(self):
        """把当前 in_progress 置为 complete 并返回；无进行中任务时返回 None。"""
        for it in self.items:
            if it["status"] == "in_progress":
                it["status"] = "complete"
                return it
        return None

    def is_done(self) -> bool:
        """是否所有任务都已完成。"""
        return bool(self.items) and all(it["status"] == "complete" for it in self.items)

    def render(self) -> str:
        marks = {"pending": "[ ]", "in_progress": "[>]", "complete": "[x]"}
        return "\n".join(f"{marks[it['status']]} {it['title']}" for it in self.items)


# 全局唯一的待办列表
todo = TodoList()


@tool
def todo_write(todos: list[str]):
    """制定待办计划：todos 为按顺序执行的任务标题列表，会替换整个列表。
    :param todos: 任务标题列表，例如 ['读取文件', '修改内容', '写回文件']
    """
    todo.set(todos)
    return todo.render()


@tool
def todo_read():
    """查看当前待办列表及各项状态。"""
    return todo.render() or "(空列表)"


@tool
def todo_next():
    """开始下一项：把第一个 pending 置为 in_progress 并返回该任务。"""
    it = todo.next()
    return f"当前任务: {it['title']}" if it else "没有待处理任务"


@tool
def todo_done():
    """完成当前 in_progress 任务，置为 complete；全部完成时提示汇总。"""
    it = todo.complete()
    if it is None:
        return "没有进行中的任务"
    msg = f"已完成: {it['title']}"
    if todo.is_done():
        msg += "\n全部任务完成，请汇总结果。"
    return msg + "\n" + todo.render()


# ---- 任务系统（task system）----
TASK_SYSTEM_ENABLED = True                              # 开关：True 用任务系统，False 退回 todo_list
TASKS_FILE = Path(__file__).resolve().parent / "tasks.json"


class TaskSystem:
    """任务系统：拆分 / 领用 / 完成 / 依赖管控 / 持久化 / 加锁释放锁。

    三状态：pending（待领用）-> in_progress（已领用/执行中）-> done（已完成）
    互斥：同一时刻最多一个任务处于 in_progress（执行中），期间其他任务不能做任何操作。
    """

    def __init__(self, path: Path = TASKS_FILE):
        self.path = Path(path)                 # 兼容传入字符串路径
        self._lock = threading.Lock()          # 并发锁：串行化所有状态变更（加锁/释放锁）
        self.goal = ""                          # 顶层目标
        self.tasks: list[dict] = []             # 每个任务：{id, title, status, deps}
        self.current_id: str | None = None      # 当前执行中的任务 id
        self.load()

    # ---- 持久化 ----
    def load(self):
        """从磁盘恢复任务状态。"""
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.goal = data.get("goal", "")
                self.tasks = data.get("tasks", [])
                self.current_id = data.get("current_id")
            except Exception:
                self.goal, self.tasks, self.current_id = "", [], None

    def _save(self):
        """把任务状态持久化到磁盘。"""
        self.path.write_text(
            json.dumps({"goal": self.goal, "tasks": self.tasks, "current_id": self.current_id},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _find(self, task_id: str):
        return next((t for t in self.tasks if t["id"] == task_id), None)

    # ---- 任务拆分 ----
    def decompose(self, goal: str, subtasks: list[str], deps: dict | None = None) -> list[dict]:
        """把目标拆成若干子任务；deps 形如 {子任务标题: [它依赖的子任务标题...]}。"""
        with self._lock:
            self.goal = goal
            start = len(self.tasks)
            new_tasks = [
                {"id": f"t{start + i + 1}", "title": t, "status": "pending", "deps": []}
                for i, t in enumerate(subtasks)
            ]
            self.tasks.extend(new_tasks)
            title2id = {t["title"]: t["id"] for t in self.tasks}
            for t in new_tasks:
                for dep_title in (deps or {}).get(t["title"], []):
                    dep_id = title2id.get(dep_title)
                    if dep_id and dep_id != t["id"] and dep_id not in t["deps"]:
                        t["deps"].append(dep_id)
            self._save()
            return new_tasks

    # ---- 领用 / 完成 ----
    def claim(self, task_id: str) -> str:
        """领用一个任务：依赖完成、且当前无执行中任务时才允许。"""
        with self._lock:
            t = self._find(task_id)
            if t is None:
                return f"任务 {task_id} 不存在"
            if self.current_id is not None:
                return f"当前已有任务 {self.current_id} 在执行，其他任务不能做任何操作"
            if t["status"] != "pending":
                return f"任务 {task_id} 状态为 {t['status']}，不能领用"
            undone = [d for d in t["deps"]
                      if (dep := self._find(d)) and dep["status"] != "done"]
            if undone:
                return f"任务 {task_id} 的依赖尚未完成：{', '.join(undone)}"
            t["status"] = "in_progress"
            self.current_id = task_id      # 加锁：锁定当前任务
            self._save()
            return f"已领用任务 {task_id}（{t['title']}），已加锁，其他任务暂不可操作"

    def complete(self, task_id: str) -> str:
        """完成当前执行中的任务，并释放锁。"""
        with self._lock:
            t = self._find(task_id)
            if t is None:
                return f"任务 {task_id} 不存在"
            if t["status"] != "in_progress":
                return f"任务 {task_id} 状态为 {t['status']}，不能完成"
            t["status"] = "done"
            self.current_id = None        # 释放锁
            self._save()
            return f"已完成任务 {task_id}（{t['title']}），已释放锁"

    # ---- 展示 ----
    def render(self) -> str:
        if not self.tasks:
            return "(无任务)"
        marks = {"pending": "[ ]", "in_progress": "[>]", "done": "[x]"}
        lines = [f"目标: {self.goal}"] if self.goal else []
        for t in self.tasks:
            dep = f" (依赖: {', '.join(t['deps'])})" if t["deps"] else ""
            cur = " <- 当前执行" if t["id"] == self.current_id else ""
            lines.append(f"{marks[t['status']]} {t['id']} {t['title']}{dep}{cur}")
        return "\n".join(lines)


# 全局唯一的任务系统实例
task_system = TaskSystem(TASKS_FILE)


@tool
def task_split(goal: str, subtasks: list[str], dependencies: dict | None = None):
    """任务拆分：把目标拆成若干子任务并持久化到磁盘，可声明依赖 dependencies={子任务标题: [依赖的标题...]}。

    :param goal: 顶层目标描述
    :param subtasks: 子任务标题列表，按顺序执行
    :param dependencies: 可选，任务间依赖关系
    :return: 拆分后的任务列表
    """
    task_system.decompose(goal, subtasks, dependencies)
    return task_system.render()


@tool
def task_claim(task_id: str):
    """任务领用：领用一个 pending 任务开始执行。依赖未完成或已有执行中任务时会被拒绝。

    :param task_id: 要领用的任务 id（见 task_list）
    :return: 领用结果
    """
    return task_system.claim(task_id)


@tool
def task_complete(task_id: str):
    """任务完成：完成当前执行中的任务并释放锁，之后才能操作其他任务。

    :param task_id: 要完成的任务 id
    :return: 完成结果
    """
    return task_system.complete(task_id)


@tool
def task_list():
    """查看任务系统的任务列表（含状态、依赖、当前执行中任务）。"""
    return task_system.render()


def get_task_tools() -> list:
    """按开关返回任务相关工具：启用返回任务系统工具，未启用退回 todo_list 工具。"""
    if TASK_SYSTEM_ENABLED:
        return [task_split, task_claim, task_complete, task_list]
    return [todo_write, todo_read, todo_next, todo_done]


def check_permission(tool_name: str, args: dict) -> tuple[str, str]:
    """调用工具前的三层权限校验，返回 (决策, 原因)。

    deny  → 危险操作直接拒绝
    ask   → 写文件 / 执行命令需要用户确认
    allow → 只读操作直接放行
    """
    # 第一层 deny：命中危险命令黑名单
    if tool_name == "run_cmd":
        command = str(args.get("command", "")).lower()
        if any(d in command for d in DANGEROUS_COMMANDS):
            return "deny", "危险命令被阻止"

    # 第二层 allow：只读/计划/任务管理类工具直接放行
    if tool_name in ("run_read", "run_glob") or tool_name.startswith("todo_") or tool_name.startswith("task_"):
        return "allow", "只读、计划或任务管理操作"

    # 第三层 ask：其余修改/执行类操作需人工确认
    return "ask", "写文件或执行命令"

class ToolHook:
    """工具钩子基类。

    before: 工具调用前执行，返回 None 表示放行；返回字符串表示拦截（该字符串直接作为结果返回）。
    after:  工具调用后执行，可改写结果或仅记录日志，返回值作为最终结果。
    """
    def before(self, tool_name: str, args: dict):
        return None

    def after(self, tool_name: str, args: dict, result):
        return result


class PermissionHook(ToolHook):
    """权限校验钩子：把 deny/ask/allow 三层校验接到 before 阶段。"""
    def before(self, tool_name: str, args: dict):
        decision, reason = check_permission(tool_name, args)
        if decision == "deny":
            return f"权限拒绝：{reason}"
        if decision == "ask":
            ans = input(f"⚠️ 是否允许执行 {tool_name}？({reason}) [y/N]: ").strip().lower()
            if ans != "y":
                return "权限拒绝：用户未确认"
        return None


class LogHook(ToolHook):
    """日志钩子：带共享状态（call_count）。

    同一个实例可以同时放进主 Agent 和子代理的钩子链——所有工具调用都累加到同一个计数，
    这就是"共用钩子"的意义：共享的是钩子实例的状态，而不只是相同的代码。
    """
    def __init__(self):
        self.call_count = 0

    def before(self, tool_name: str, args: dict):
        self.call_count += 1
        print(f"[日志] #{self.call_count} 工具调用: {tool_name}; 参数: {args};")

    def after(self, tool_name: str, args: dict, result):
        print(f"[日志] 工具返回: {result}")
        return result


# 全局共享的日志钩子：主 Agent 和子代理的钩子链里放的是同一个实例
LOG_HOOK = LogHook()


def run_tool(tools_map: dict, tool_name: str, args: dict, hooks: list[ToolHook]):
    """agent 循环唯一入口：先跑 before 钩子决定是否放行，再执行工具，最后跑 after 钩子。

    钩子链由调用方传入 —— 主 Agent 和子代理可以各用各的链，也可以把同一个钩子实例放进两条链里共用。
    """
    result = None
    # before：任一钩子拦截则跳过工具执行
    for hook in hooks:
        blocked = hook.before(tool_name, args)
        if blocked is not None:
            result = blocked
            break
    else:
        result = tools_map[tool_name].invoke(args)
    # after：统一收尾（日志、改写结果等）
    for hook in hooks:
        result = hook.after(tool_name, args, result)
    return result


# ---- 上下文压缩管线 ----
CONTEXT_WINDOW = 64000       # 模型上下文窗口（token），按实际模型调整
KEEP_TOKENS = 2000           # 全量压缩时保留开头的 token 数
SNIP_MAX_TOKENS = 1500       # Snip 阶段单条消息保留的最大 token 数

_CJK_RE = re.compile(r'[一-鿿　-〿＀-￯]')


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数：CJK 字符≈1 token，其余≈每 4 字符 1 token。"""
    cjk = len(_CJK_RE.findall(text))
    return cjk + (len(text) - cjk) // 4


def _message_text(msg) -> str:
    """取一条消息的"计费文本"：正文 + 工具调用参数。"""
    content = msg.content
    if isinstance(content, list):
        content = " ".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    text = content if isinstance(content, str) else str(content)
    tc = getattr(msg, "tool_calls", None)
    if tc:
        text += "\n" + json.dumps(tc, ensure_ascii=False, default=str)
    return text


def _with_content(msg, content):
    """复制一条消息并把 content 替换掉（保留类型、tool_call_id 等字段）。"""
    try:
        return msg.model_copy(update={"content": content})  # pydantic v2
    except Exception:
        try:
            return msg.copy(update={"content": content})    # pydantic v1 / langchain
        except Exception:
            msg.content = content                            # 兜底：原地改
            return msg


class ContextCompactor:
    """上下文压缩管线：按使用率分四档处理。

    <45%        不压缩
    45%~60%     Snip：截断过长的消息内容（保留头部）
    60%~85%     清理工具调用结果：把 ToolMessage 结果替换成占位符
    >=85%       全量压缩：LLM 生成摘要，只保留「摘要 + 开头 KEEP_TOKENS token」
    """

    def __init__(self, client, window: int = CONTEXT_WINDOW, keep_tokens: int = KEEP_TOKENS):
        self.client = client
        self.window = window
        self.keep_tokens = keep_tokens

    def used_tokens(self, messages) -> int:
        return sum(estimate_tokens(_message_text(m)) for m in messages)

    def ratio(self, messages) -> float:
        return self.used_tokens(messages) / self.window

    def compact(self, messages: list) -> list:
        """按当前使用率就地压缩，返回同一列表（方便调用方继续 append）。"""
        r = self.ratio(messages)
        before = self.used_tokens(messages)
        if r < 0.45:
            print(f"[上下文] 使用率 {r:.1%}（{before}t）< 45%，不压缩")
        elif r < 0.60:
            messages[:] = self._snip(messages)
            print(f"[上下文] 使用率 {r:.1%}（{before}t），Snip 截取 -> {self.used_tokens(messages)}t")
        elif r < 0.85:
            messages[:] = self._clean_tool_results(messages)
            print(f"[上下文] 使用率 {r:.1%}（{before}t），清理工具结果 -> {self.used_tokens(messages)}t")
        else:
            messages[:] = self._full_compress(messages)
            print(f"[上下文] 使用率 {r:.1%}（{before}t），全量压缩 -> {self.used_tokens(messages)}t")
        return messages

    # ---- 四档策略 ----
    def _snip(self, messages):
        """Snip：截断过长的消息内容，保留头部。"""
        out = []
        for m in messages:
            if estimate_tokens(_message_text(m)) > SNIP_MAX_TOKENS:
                head = m.content if isinstance(m.content, str) else str(m.content)
                out.append(_with_content(m, head[: SNIP_MAX_TOKENS * 2] + "\n…(已截断)"))
            else:
                out.append(m)
        return out

    def _clean_tool_results(self, messages):
        """清理工具调用结果：把 ToolMessage 的详细结果替换成占位符。"""
        out = []
        for m in messages:
            if isinstance(m, ToolMessage):
                n = estimate_tokens(_message_text(m))
                out.append(_with_content(m, f"[工具结果已清理，原 {n} token]"))
            else:
                out.append(m)
        return out

    def _full_compress(self, messages):
        """全量压缩：LLM 生成摘要，只保留「摘要 + 开头 keep_tokens」。"""
        summary = self._summarize(messages)
        head = self._first_tokens(messages, self.keep_tokens)
        return [SystemMessage(content=f"[历史摘要]\n{summary}")] + head

    def _summarize(self, messages) -> str:
        history = "\n".join(f"{type(m).__name__}: {_message_text(m)}" for m in messages)
        prompt = (
            "请把下面的对话历史压缩成一段简洁的中文摘要，"
            "必须保留：任务目标、已完成的步骤、关键结论、以及未完成的待办。\n\n"
            f"{history}"
        )
        resp = self.client.invoke([HumanMessage(content=prompt)])
        return resp.content

    def _first_tokens(self, messages, max_tokens):
        """取开头累计不超过 max_tokens 的消息。"""
        out = []
        used = 0
        for m in messages:
            t = estimate_tokens(_message_text(m))
            if used + t > max_tokens and out:
                break
            out.append(m)
            used += t
        return out


# ---- 错误恢复（error recovery）----
MAX_TOKENS_INITIAL = 8000     # 初始单次输出 token 上限
MAX_TOKENS_LIMIT = 32000      # 输出 token 上限（被截断后逐级扩大，不超过此值）

# 尝试导入 OpenAI 异常类，用于区分"服务商问题"和"上下文过长"
try:
    import openai as _openai
    _PROVIDER_ERROR_CLASSES = (
        _openai.APIConnectionError,
        _openai.APITimeoutError,
        _openai.AuthenticationError,
        _openai.RateLimitError,
        _openai.PermissionDeniedError,
        _openai.InternalServerError,
    )
except ImportError:  # 没有 openai 包时退化为空，只靠消息关键词判断
    _PROVIDER_ERROR_CLASSES = ()


def _make_client(max_tokens: int) -> ChatOpenAI:
    """按给定的输出 token 上限创建模型客户端。"""
    return ChatOpenAI(
        api_key=api_key,
        base_url=base_url,
        model_name=model_name,
        max_tokens=max_tokens,
    )


def _finish_reason(response) -> str:
    """取模型回复的 finish_reason（'stop' / 'tool_calls' / 'length' 等）。"""
    meta = getattr(response, "response_metadata", {}) or {}
    addl = getattr(response, "additional_kwargs", {}) or {}
    return str(meta.get("finish_reason") or addl.get("finish_reason") or "")


def is_truncated(response) -> bool:
    """输出是否因 max_tokens 被截断（finish_reason == 'length'）。"""
    return _finish_reason(response) == "length"


def is_context_error(e: Exception) -> bool:
    """判断异常是否为"上下文过长"（靠消息关键词匹配，避免绑定具体异常类）。"""
    msg = str(e).lower()
    keys = ("context length", "maximum context", "context is too", "too long",
            "exceed", "超出", "上下文", "太长")
    return any(k in msg for k in keys)


def is_provider_error(e: Exception) -> bool:
    """判断异常是否为"服务商问题"（连接/超时/鉴权/限流/服务器错误）。"""
    if _PROVIDER_ERROR_CLASSES and isinstance(e, _PROVIDER_ERROR_CLASSES):
        return True
    msg = str(e).lower()
    keys = ("connection", "timed out", "timeout", "rate limit", "unauthorized", "authentication",
            "internal server", "service unavailable", "bad gateway", "connect")
    return any(k in msg for k in keys)


def agent_loop(messages: list[AnyMessage], tools: list, hooks: list[ToolHook]) -> str:
    """通用 agent 循环：绑定给定工具、走给定钩子链，返回最终的文本回复。

    内置三级错误恢复：
      1) 输出截断 -> 扩大 max_tokens 重试
      2) 长上下文 -> 压缩 -> 摘要压缩 -> 退出
      3) 服务商问题 -> 直接退出
    """
    tools_map = {tool.name: tool for tool in tools}
    max_tokens = MAX_TOKENS_INITIAL
    client = _make_client(max_tokens)
    client_with_tools = client.bind_tools(tools)
    compactor = ContextCompactor(client)
    context_retries = 0   # 长上下文重试计数（0=压缩，1=摘要压缩，>=2=退出）

    while True:
        compactor.compact(messages)   # 每轮调用模型前先做上下文压缩

        try:
            response = client_with_tools.invoke(messages)
        except Exception as e:
            # 2) 长上下文：先压缩 -> 再摘要 -> 再退出
            if is_context_error(e):
                if context_retries == 0:
                    print("[错误恢复] 上下文过长：先调用压缩方法")
                    compactor.compact(messages)
                elif context_retries == 1:
                    print("[错误恢复] 压缩后仍过长：用模型生成摘要压缩")
                    try:
                        messages[:] = compactor._full_compress(messages)
                    except Exception as e2:
                        print(f"[错误恢复] 摘要生成失败，退出：{e2}")
                        sys.exit(1)
                else:
                    print("[错误恢复] 摘要后仍过长，退出")
                    sys.exit(1)
                context_retries += 1
                continue
            # 3) 服务商问题：直接退出
            if is_provider_error(e):
                print(f"[错误恢复] 服务商问题，直接退出：{e}")
                sys.exit(1)
            # 其它未知异常也按服务商问题处理，直接退出
            print(f"[错误恢复] 未知异常，直接退出：{e}")
            sys.exit(1)

        context_retries = 0   # 本轮成功，重置长上下文重试计数

        # 1) 输出被截断：扩大 max_tokens 后重试（不追加截断的回复）
        if is_truncated(response):
            if max_tokens < MAX_TOKENS_LIMIT:
                max_tokens = min(max_tokens * 2, MAX_TOKENS_LIMIT)
                print(f"[错误恢复] 输出被截断，扩大 max_tokens 到 {max_tokens} 重试")
                client = _make_client(max_tokens)
                client_with_tools = client.bind_tools(tools)
                compactor.client = client   # 摘要用的客户端同步更新
                continue
            print(f"[错误恢复] 输出被截断但已达上限 {MAX_TOKENS_LIMIT}，接受当前结果")

        # 如果调用工具 则使用 否则结束
        if response.tool_calls:
            messages.append(response)
            for tool_call in response.tool_calls:
                tool_name = tool_call["name"]
                args = tool_call["args"]
                # 通过钩子链执行工具（权限校验、日志等都已封装在钩子里）
                result = run_tool(tools_map, tool_name, args, hooks)
                messages.append(ToolMessage(content=str(result), tool_call_id=tool_call["id"]))
        else:
            messages.append(AIMessage(content=response.content))
            return response.content


# ---- 子代理（subagent）----
class SubAgent:
    """子代理：一个拥有自己消息历史、工具子集和钩子链的迷你 agent。

    与主 Agent 的区别只在"喂给它什么"：
      - tools 传入子集 → 它的能力边界更窄（比如只读）；
      - hooks 传入自己的链 → 链里可放与主 Agent 共用的钩子实例（如 LOG_HOOK），也可只放自己的；
      - messages 每次 run 都是全新的 → 它看不到主 Agent 的上下文。
    """

    def __init__(self, name: str, tools: list, hooks: list[ToolHook], system_prompt: str = ""):
        self.name = name
        self.tools = tools
        self.hooks = hooks
        self.system_prompt = system_prompt

    def run(self, task: str) -> str:
        """给子代理一个任务，跑完它的循环，把最终结果作为字符串交回。"""
        messages = []
        if self.system_prompt:
            messages.append(SystemMessage(content=self.system_prompt))
        messages.append(HumanMessage(content=task))
        return agent_loop(messages, self.tools, self.hooks)


@tool
def spawn_subagent(task: str):
    """把一个独立、可并行的子任务交给"只读研究子代理"去办，并拿回它的结论。

    适用于：搜索/阅读多个文件并归纳信息这类"只读调研"工作。
    子代理拥有自己的消息历史、只读工具（run_read/run_glob），并与主 Agent 共用 LOG_HOOK 日志钩子；
    因此它不会要求写文件，也不会打断主流程向你索要权限。
    :param task: 交给子代理的调研任务描述，越具体越好
    :return: 子代理最终的文字结论
    """
    # 子代理的工具子集：只读，不给 run_write / run_edit / run_cmd
    research = SubAgent(
        name="research",
        tools=[run_read, run_glob],
        hooks=[LOG_HOOK],   # 与主 Agent 共用同一个日志钩子实例
        system_prompt=(
            "你是一个只读的研究子代理。只能使用 run_read 和 run_glob 收集信息，"
            "不要尝试写文件或执行命令。最后用简洁的中文总结你的发现。"
        ),
    )
    return research.run(task)


# ---- Skill ----
# skills 在项目根目录（本文件的上一级），每个 skill 是一个子目录，定义在其中的 SKILL.md
SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 SKILL.md 的 YAML frontmatter（支持多行 description 等），返回 (元信息, 正文)。"""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)  # ['', frontmatter, 正文]
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except Exception:
        meta = {}
    return meta, parts[2].lstrip("\n")


def scan_skills(skills_dir: Path) -> dict:
    """扫描 skills 目录：每个子目录是一个 skill（定义在 SKILL.md 里），返回 {name: {...}}。

    这就是"字典"：load_skill 靠它按名字匹配 skill。
    """
    skills = {}
    if not skills_dir.exists():
        return skills
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        text = skill_md.read_text(encoding="utf-8")
        meta, body = parse_frontmatter(text)
        name = str(meta.get("name") or skill_md.parent.name)
        skills[name] = {
            "name": name,
            # 摘要只保留单行并截断，避免注入的上下文过长
            "description": " ".join(str(meta.get("description", "")).split())[:200],
            "script": str(meta.get("script") or ""),
            "body": body.strip(),
            "dir": skill_md.parent,   # skill 所在目录，找 scripts/ references/ 用
            "file": str(skill_md),
        }
    return skills


def skill_summary(skills: dict) -> str:
    """把每个 skill 的 frontmatter 摘要拼成一段文本，用于注入 Agent 上下文。"""
    if not skills:
        return "(没有可用的 skill)"
    return "\n".join(f"- {name}: {s['description']}" for name, s in skills.items())


def list_dir(skill_dir: Path, sub: str) -> list[str]:
    """列出 skill 目录下某个子目录（如 scripts/ references/）里的文件名。"""
    d = skill_dir / sub
    if not d.is_dir():
        return []
    return [p.name for p in sorted(d.iterdir()) if p.is_file()]


def run_skill_script(script_path: Path) -> str:
    """执行一个 Python 脚本并返回输出。"""
    if not script_path.exists():
        return f"(脚本不存在: {script_path.name})"
    try:
        r = subprocess.run(
            [sys.executable, str(script_path)],  # 用当前解释器执行，避免依赖系统 PATH 里的 python
            cwd=WORKDIR,                         # 在工作目录下跑，收集的是项目上下文
            capture_output=True,
            text=True,
            timeout=60,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},  # 强制脚本 UTF-8 输出，避免中文乱码
        )
        out = (r.stdout + r.stderr).strip()
        return out[:20000] if out else "(脚本无输出)"
    except subprocess.TimeoutExpired:
        return "错误：脚本超时（60秒）"
    except Exception as e:
        return f"错误：脚本执行失败 {e}"


# skill 字典：懒加载（第一次调用 get_skills 才真正扫描 skills/ 目录）
_SKILLS_CACHE: dict | None = None


def get_skills() -> dict:
    """按需扫描 skill 并缓存：只有第一次调用时才读 skills/ 目录。"""
    global _SKILLS_CACHE
    if _SKILLS_CACHE is None:
        _SKILLS_CACHE = scan_skills(SKILLS_DIR)
    return _SKILLS_CACHE


@tool
def load_skill(name: str):
    """按名字加载一个 skill，返回完整指令（含可用的脚本/参考文档清单；frontmatter 声明 script 则会执行）。

    先在上下文里的"可用 skill 列表"中确认要调用哪个 skill，再传它的 name。
    :param name: skill 名称（见可用 skill 列表）
    :return: skill 完整内容 + 脚本输出（若有）+ 可用的脚本/参考文档清单
    """
    skills = get_skills()
    skill = skills.get(name)
    if skill is None:
        avail = ", ".join(skills.keys()) or "(无)"
        return f"未找到 skill '{name}'。可用: {avail}"

    parts = []

    # 若 frontmatter 声明了 script（相对 skill 目录），先执行并注入输出
    if skill["script"]:
        parts.append(f"【脚本输出】\n{run_skill_script(skill['dir'] / skill['script'])}")

    parts.append(skill["body"])

    # 补充 skill 目录下 scripts/ 与 references/ 里的可用资源
    extra = []
    scripts = list_dir(skill["dir"], "scripts")
    refs = list_dir(skill["dir"], "references")
    if scripts:
        extra.append("可用脚本: " + ", ".join(scripts))
    if refs:
        extra.append("参考文档: " + ", ".join(refs))
    if extra:
        parts.append("\n".join(extra))

    return "\n\n".join(parts)


# ---- 记忆系统 ----
MEMORY_DIR = Path(__file__).resolve().parent / "memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
MEMORY_CATEGORIES = ["main", "plan", "tool"]     # 主 Agent / 计划 / 工具 三个目录
MAX_FILES_PER_CATEGORY = 10                       # 单目录超过 10 个文件就整理合并
MEMORY_TOP_K = 3                                  # 每次 query 注入的记忆条数


def _category_dir(category: str) -> Path:
    d = MEMORY_DIR / category
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slug(name: str) -> str:
    """把记忆名转成安全的文件名片段。"""
    s = re.sub(r'[^a-zA-Z0-9一-鿿]+', '-', name).strip('-').lower()
    return s or "memory"


def _write_memory_file(category: str, name: str, description: str, content: str) -> Path:
    """写一个记忆文件（md + YAML frontmatter），文件名带时间戳避免覆盖。"""
    d = _category_dir(category)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = d / f"{stamp}-{_slug(name)}.md"
    meta = {"name": name, "category": category, "description": description}
    fm = "---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + "---\n\n" + content.strip() + "\n"
    path.write_text(fm, encoding="utf-8")
    return path


def load_memories() -> list[dict]:
    """扫描三个分类目录，解析 frontmatter，返回所有记忆。"""
    memories = []
    for category in MEMORY_CATEGORIES:
        d = MEMORY_DIR / category
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.md")):
            meta, body = parse_frontmatter(f.read_text(encoding="utf-8"))
            memories.append({
                "category": str(meta.get("category") or category),
                "name": str(meta.get("name") or f.stem),
                "description": str(meta.get("description") or ""),
                "content": body.strip(),
                "file": str(f),
            })
    return memories


def rebuild_index() -> Path:
    """重建 MEMORY.md 索引：每个记忆一行。"""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    memories = load_memories()
    lines = ["# Memory Index", ""]
    for category in MEMORY_CATEGORIES:
        items = [m for m in memories if m["category"] == category]
        if not items:
            continue
        lines.append(f"## {category}")
        for m in items:
            lines.append(f"- [{m['name']}]({category}/{Path(m['file']).name}) — {m['description']}")
        lines.append("")
    MEMORY_INDEX.write_text("\n".join(lines), encoding="utf-8")
    return MEMORY_INDEX


def _tokenize(text: str) -> set:
    """中文按字、英文/数字按词，统一小写，用于轻量检索。"""
    return set(re.findall(r'[a-z0-9]+|[一-鿿]', text.lower()))


def query_memories(query: str, top_k: int = MEMORY_TOP_K) -> list[dict]:
    """轻量级 query：按词重叠打分返回最相关的记忆（不调 LLM）。"""
    memories = load_memories()
    if not memories:
        return []
    qterms = _tokenize(query)
    if not qterms:
        return []
    scored = []
    for m in memories:
        doc = f"{m['name']} {m['description']} {m['content']}"
        hits = len(qterms & _tokenize(doc))
        if hits:
            scored.append((hits, m))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [m for _, m in scored[:top_k]]


def consolidate(category: str) -> bool:
    """某目录记忆文件超过 MAX_FILES_PER_CATEGORY 时，把最旧的若干条合并成一个文件。"""
    d = MEMORY_DIR / category
    if not d.is_dir():
        return False
    files = sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime)
    if len(files) <= MAX_FILES_PER_CATEGORY:
        return False
    to_merge = files[: len(files) - MAX_FILES_PER_CATEGORY + 1]
    parts, names = [], []
    for f in to_merge:
        meta, body = parse_frontmatter(f.read_text(encoding="utf-8"))
        names.append(str(meta.get("name") or f.stem))
        parts.append(f"## {names[-1]}\n{body.strip()}")
        f.unlink()
    merged = "\n\n".join(parts)
    _write_memory_file(category, f"consolidated-{len(names)}", f"{category} 记忆合并（{len(names)} 条）", merged)
    return True


def _parse_json_list(text: str) -> list:
    """容错地从一个（可能是带代码块的）字符串里解析 JSON 数组。"""
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else []
    except Exception:
        pass
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start:end + 1])
            return data if isinstance(data, list) else []
        except Exception:
            pass
    return []


def extract_memories(messages: list) -> list[dict]:
    """Agent 完成后，调 LLM 从对话里提取值得长期记住的信息，写入对应分类目录。"""
    history = "\n".join(f"{type(m).__name__}: {_message_text(m)}" for m in messages)
    prompt = (
        "从下面的对话中提取值得长期记住的信息，以 JSON 数组返回。"
        "每个元素包含 category、name、description、content 四个字符串字段。\n"
        "category 只能是 main / plan / tool 之一：\n"
        "  main = 用户偏好、工作方式（关于主 Agent）\n"
        "  plan = 任务计划、目标、进度\n"
        "  tool = 工具用法、经验、坑\n"
        "description 用一句话概括（用于检索）。没有值得记的内容就返回 []。\n\n"
        f"对话：\n{history}"
    )
    client = ChatOpenAI(api_key=api_key, base_url=base_url, model_name=model_name, max_tokens=2000)
    resp = client.invoke([HumanMessage(content=prompt)])
    items = _parse_json_list(resp.content)
    saved = []
    for it in items:
        cat = it.get("category") if it.get("category") in MEMORY_CATEGORIES else "main"
        name = str(it.get("name") or "memory")
        desc = str(it.get("description") or "")
        content = str(it.get("content") or "")
        if not content.strip():
            continue
        path = _write_memory_file(cat, name, desc, content)
        saved.append({"category": cat, "name": name, "description": desc, "file": str(path)})
    if saved:
        rebuild_index()
        for cat in MEMORY_CATEGORIES:
            consolidate(cat)
    return saved


# ---- 系统提示词（按需加载）----
def tool_definitions(tools: list) -> str:
    """工具定义：从 tool 对象抽取 name + 第一行描述（摘要，跳过 :param/:return 等参数说明）。"""
    lines = []
    for t in tools:
        summary = next((ln.strip() for ln in (t.description or "").splitlines() if ln.strip()), "")
        lines.append(f"- {t.name}：{summary}")
    return "\n".join(lines) if lines else "(无工具)"


def skill_definitions() -> str:
    """skill 定义：懒扫描 skills/ 目录，返回 frontmatter 摘要列表。"""
    return skill_summary(get_skills())


def memory_definitions(query: str, top_k: int = MEMORY_TOP_K) -> str:
    """记忆定义：按当前问题做轻量级检索，返回相关记忆文本（无相关记忆返回空串）。"""
    mems = query_memories(query, top_k)
    if not mems:
        return ""
    return "\n".join(f"- [{m['category']}] {m['name']}：{m['content']}" for m in mems)


def get_system_prompt(tools: list, query: str = "") -> str:
    """系统提示词获取方法：把工具 / skill / 记忆三类定义封装到一起，按需组装。

    传 query 时额外注入与当前问题相关的记忆；不传 query 只返回静态部分（工具 + skill）。
    这样每轮对话按需刷新记忆，而不重复列出工具/skill 定义。
    """
    sections = [
        "你是主 Agent，一个能调用工具完成任务的中文助手。",
        "## 可用工具\n" + tool_definitions(tools),
        "## 可用技能（skill）\n" + skill_definitions(),
    ]
    mem_text = memory_definitions(query) if query else ""
    if mem_text:
        sections.append("## 相关记忆\n" + mem_text)
    sections.append(
        "使用说明：任务与某个 skill 匹配时，先调用 load_skill 按名字加载完整指令；"
        "复杂任务先用 todo_write 列计划再逐步执行。"
    )
    return "\n\n".join(sections)


# 主 Agent 的工具集与钩子链：能力扩展工具 + 按开关选择的任务工具（任务系统或 todo_list）。
MAIN_TOOLS = [run_read, run_write, run_edit, run_glob, run_cmd,
              spawn_subagent, load_skill, *get_task_tools()]
MAIN_HOOKS: list[ToolHook] = [LOG_HOOK, PermissionHook()]


if __name__ == '__main__':
    print("12_task_system")
    rebuild_index()   # 启动时构建/刷新 MEMORY.md 索引
    print(f"任务系统：{'已启用' if TASK_SYSTEM_ENABLED else '未启用（使用 todo_list）'}")
    print("可用 skill：")
    print(skill_definitions())
    print(f"记忆条数：{len(load_memories())}")
    print("\n输入问题，回车发送。输入 q 退出。\n")

    # 系统提示词：工具 + skill 定义按需组装（记忆每轮按 query 动态注入）
    system_prompt = get_system_prompt(MAIN_TOOLS)
    messages = [SystemMessage(content=system_prompt)]
    while True:
        user_input = input("你: ").strip()
        if user_input.lower() == 'q':
            print("退出程序。")
            break
        if not user_input:
            continue
        # 每轮按需加载相关记忆，注入到上下文（轻量检索，不调 LLM）
        mem_text = memory_definitions(user_input)
        if mem_text:
            messages.append(SystemMessage(content=f"[相关记忆]\n{mem_text}"))
        messages.append(HumanMessage(content=user_input))
        try:
            agent_loop(messages, MAIN_TOOLS, MAIN_HOOKS)
            print(f"\nAgent: {messages[-1].content}\n")
        except Exception as e:
            print(f"\n错误: {e}\n")
            messages = [SystemMessage(content=system_prompt)]
            continue
        # 记忆提取：Agent 完成后从对话提取并保存记忆
        try:
            saved = extract_memories(messages)
            if saved:
                print(f"[记忆] 已保存 {len(saved)} 条")
        except Exception as e:
            print(f"[记忆] 提取失败: {e}")
