"""
    一个简单的Agent+多Tool循环
    解决问题：每个工具负责一个任务，AI可以同时使用多个工具，提高效率，避免token浪费
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

def safe_path(p: str) -> Path:
    """确保路径在工作目录内"""
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
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "format ", "del /f /s /q c:","del"]
    if any(d in command.lower() for d in dangerous):
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
        tools = [run_read, run_write, run_edit, run_glob,run_cmd]
        tools_map = {tool.name: tool for tool in tools}
        client_with_tools = client.bind_tools(tools)
        response = client_with_tools.invoke(messages)
        # 如果调用工具 则使用 否则结束
        if response.tool_calls:
            messages.append(response)
            for tool_call in response.tool_calls:
                tool_name = tool_call["name"]
                print(f"工具调用: {tool_name}; 参数: {tool_call['args']};")
                result = tools_map[tool_name].invoke(tool_call["args"])
                print(f"工具返回: {result}")
                messages.append(ToolMessage(content=str(result), tool_call_id=tool_call["id"]))
        else:
            messages.append(AIMessage(content=response.content))
            break


if __name__ == '__main__':
    print("2_tool_use")
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
