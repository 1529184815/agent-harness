"""
    没有计划的 agent 走哪算哪，— 先列步骤再动手，长任务更不容易漏项
"""
import os
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from openai import api_key
from langchain_core.tools import tool
from langchain.messages import HumanMessage,AIMessage,ToolMessage,AnyMessage
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
    lines = safe_path(path).read_text().splitlines()
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
    safe_path(path).write_text(content)
    return f"Wrote {len(content)} bytes to {path}"

@tool
def run_edit(path, old_text, new_text):
    """编辑文件
    :param path: 文件路径
    :param old_text: 要替换的旧文本
    :param new_text: 新文本
    :return: 提示信息
    """
    text = safe_path(path).read_text()
    if old_text not in text:
        return "Error: text not found"
    safe_path(path).write_text(text.replace(old_text, new_text, 1))
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
            encoding="gbk",      # Windows CMD 中文输出多为 GBK，避免乱码
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
    """日志钩子：后续要加日志/审计，只需新增这类钩子并注册，无需改 agent 循环。"""
    def before(self, tool_name: str, args: dict):
        print(f"[日志] 工具调用: {tool_name}; 参数: {args};")

    def after(self, tool_name: str, args: dict, result):
        print(f"[日志] 工具返回: {result}")
        return result


# 钩子链：按顺序执行。新增钩子只需 append 到这里。
HOOKS: list[ToolHook] = [LogHook(), PermissionHook()]


def run_tool(tools_map: dict, tool_name: str, args: dict):
    """agent 循环唯一入口：先跑 before 钩子决定是否放行，再执行工具，最后跑 after 钩子。"""
    result = None
    # before：任一钩子拦截则跳过工具执行
    for hook in HOOKS:
        blocked = hook.before(tool_name, args)
        if blocked is not None:
            result = blocked
            break
    else:
        result = tools_map[tool_name].invoke(args)
    # after：统一收尾（日志、改写结果等）
    for hook in HOOKS:
        result = hook.after(tool_name, args, result)
    return result


def agent_loop(messages:list[AnyMessage]):
    while True:
        client = ChatOpenAI(
            api_key=api_key,
            base_url=base_url,
            model_name=model_name,
            messages=messages,
            max_tokens=8000
        )
        # 绑定工具
        tools = [run_read, run_write, run_edit, run_glob, run_cmd,
                 todo_write, todo_read, todo_next, todo_done]
        tools_map = {tool.name: tool for tool in tools}
        client_with_tools = client.bind_tools(tools)
        response = client_with_tools.invoke(messages)
        # 如果调用工具 则使用 否则结束
        if response.tool_calls:
            messages.append(response)
            for tool_call in response.tool_calls:
                tool_name = tool_call["name"]
                args = tool_call["args"]
                # 通过钩子链执行工具（权限校验、日志等都已封装在钩子里）
                result = run_tool(tools_map, tool_name, args)
                messages.append(ToolMessage(content=str(result), tool_call_id=tool_call["id"]))
        else:
            messages.append(AIMessage(content=response.content))
            break


if __name__ == '__main__':
    print("5_todo_list")
    print("输入问题，回车发送。输入 q 退出。\n")
    messages = []
    while True:
        user_input = input("你: ").strip()
        if user_input.lower() == 'q':
            print("退出程序。")
            break
        if not user_input:
            continue
        messages.append(HumanMessage(content=user_input))
        try:
            agent_loop(messages)
            print(f"\nAgent: {messages[-1].content}\n")
        except Exception as e:
            print(f"\n错误: {e}\n")
            messages = []
