"""
    skill（技能）：把"某类任务怎么做"沉淀成 skills/ 目录下的一个子目录（内含 SKILL.md）。
    Agent 执行前先扫描 skills 目录，提取每个 skill 的 frontmatter 摘要（name + description）注入上下文；
    需要时用 load_skill 工具按名字加载完整指令，若声明了脚本则先执行脚本、把输出一并作为上下文。
"""
import os
import sys
import re
import yaml
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

    # 第二层 allow：只读/计划类工具直接放行
    if tool_name in ("run_read", "run_glob") or tool_name.startswith("todo_"):
        return "allow", "只读或计划操作"

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


def agent_loop(messages: list[AnyMessage], tools: list, hooks: list[ToolHook]) -> str:
    """通用 agent 循环：绑定给定工具、走给定钩子链，返回最终的文本回复。

    messages / tools / hooks 都由调用方提供，因此主 Agent 和子代理共用同一套循环逻辑，
    却各自持有独立的状态。
    """
    tools_map = {tool.name: tool for tool in tools}
    client = ChatOpenAI(
        api_key=api_key,
        base_url=base_url,
        model_name=model_name,
        max_tokens=8000
    )
    client_with_tools = client.bind_tools(tools)
    while True:
        response = client_with_tools.invoke(messages)
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


# 启动时扫描一次，构建 skill 字典
SKILLS = scan_skills(SKILLS_DIR)


@tool
def load_skill(name: str):
    """按名字加载一个 skill，返回完整指令（含可用的脚本/参考文档清单；frontmatter 声明 script 则会执行）。

    先在上下文里的"可用 skill 列表"中确认要调用哪个 skill，再传它的 name。
    :param name: skill 名称（见可用 skill 列表）
    :return: skill 完整内容 + 脚本输出（若有）+ 可用的脚本/参考文档清单
    """
    skill = SKILLS.get(name)
    if skill is None:
        avail = ", ".join(SKILLS.keys()) or "(无)"
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


# 主 Agent 的工具集与钩子链：比子代理更全，且包含 spawn_subagent 和 load_skill 两个"能力扩展"工具。
MAIN_TOOLS = [run_read, run_write, run_edit, run_glob, run_cmd,
              todo_write, todo_read, todo_next, todo_done, spawn_subagent, load_skill]
MAIN_HOOKS: list[ToolHook] = [LOG_HOOK, PermissionHook()]


if __name__ == '__main__':
    print("7_skill")
    print("可用 skill：")
    print(skill_summary(SKILLS))
    print("\n输入问题，回车发送。输入 q 退出。\n")

    # Agent 执行前：把 skill 摘要注入系统上下文，让 Agent 知道有哪些 skill 可用
    system_prompt = (
        "你是主 Agent。下面是当前可用的 skill（技能）列表：\n"
        f"{skill_summary(SKILLS)}\n\n"
        "当任务与某个 skill 匹配时，先调用 load_skill 工具按名字加载该 skill，"
        "拿到完整指令（含可能的脚本输出）后再执行。"
    )
    messages = [SystemMessage(content=system_prompt)]
    while True:
        user_input = input("你: ").strip()
        if user_input.lower() == 'q':
            print("退出程序。")
            break
        if not user_input:
            continue
        messages.append(HumanMessage(content=user_input))
        try:
            agent_loop(messages, MAIN_TOOLS, MAIN_HOOKS)
            print(f"\nAgent: {messages[-1].content}\n")
        except Exception as e:
            print(f"\n错误: {e}\n")
            messages = [SystemMessage(content=system_prompt)]
