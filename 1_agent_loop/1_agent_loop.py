"""
    一个简单的Agent+Tool循环
    缺点：当前工具要执行所有命令，读、写、删除，每一次都要AI先生成命令再执行工具，非常消耗token，而且容易拼错
"""
import os
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from openai import api_key
from langchain_core.tools import tool
from langchain.messages import HumanMessage,AIMessage,ToolMessage,AnyMessage
import subprocess

from pyexpat.errors import messages

load_dotenv()

api_key = os.getenv("DEEPSEAK_API_KEY")

base_url = os.getenv("DEEPSEAK_BASE_URL")

model_name = os.getenv("DEEPSEAK_MODEL")

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
        tools = [run_cmd]
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
    print("s01: Agent Loop")
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
