#!/usr/bin/env python3
from __future__ import annotations

"""
s01_agent_loop.py - The Agent Loop

The entire secret of an AI coding agent in one pattern:

    while stop_reason == "tool_use":
        response = LLM(messages, tools)
        execute tools
        append results

    +----------+      +-------+      +---------+
    |   User   | ---> |  LLM  | ---> |  Tool   |
    |  prompt  |      |       | ---> | execute |
    +----------+      +---+---+      +----+----+
                          ^               |
                          |   tool_result |
                          +---------------+
                          (loop continues)

This is the core loop: feed tool results back to the model
until the model decides to stop. Production agents layer
policy, hooks, and lifecycle controls on top.

Usage:
    pip install anthropic openai python-dotenv
    ANTHROPIC_API_KEY=... MODEL_ID=... python practice/agent.py
    OPENAI_API_KEY=... OPENAI_BASE_URL=... MODEL_ID=... python practice/agent.py
"""

import json
import os
import subprocess
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from anthropic.types import MessageParam as AnthropicMessageParam
    from openai.types.chat import ChatCompletionMessageParam as OpenAIMessageParam
else:
    AnthropicMessageParam = object
    OpenAIMessageParam = object

try:
    import readline
    # macOS 的 libedit 在处理中文输入时有退格问题，这四行修复它
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
except ImportError:
    pass

try:
    from anthropic import Anthropic
except ImportError:
    Anthropic = None

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

from dotenv import load_dotenv

load_dotenv(override=True)

MODEL = os.environ["MODEL_ID"]
SYSTEM = f"You are a coding agent at {os.getcwd()}. Use bash to solve tasks. Act, don't explain."

BASH_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"command": {"type": "string"}},
    "required": ["command"],
}

ANTHROPIC_TOOLS = [{
    "name": "bash",
    "description": "Run a shell command.",
    "input_schema": BASH_INPUT_SCHEMA,
}]

OPENAI_TOOLS = [{
    "type": "function",
    "function": {
        "name": "bash",
        "description": "Run a shell command.",
        "parameters": BASH_INPUT_SCHEMA,
    },
}]

LABEL_COLORS = {
    "系统标题": "36",
    "使用说明": "36",
    "输入提示": "36",
    "需要执行的工具": "33",
    "需要执行的命令": "34",
    "工具结果": "32",
    "最终回复": "35",
    "调试": "90",
}

last_log_label: str | None = None


def format_label(label: str) -> str:
    color = LABEL_COLORS.get(label, "35")
    return f"\033[{color}m[{label}]\033[0m"


def next_label_prefix(label: str) -> str:
    global last_log_label
    if last_log_label is not None and last_log_label != label:
        print()
    last_log_label = label
    return format_label(label)


def print_labeled(label: str, content: object = "") -> None:
    prefix = next_label_prefix(label)
    if content != "":
        print(f"{prefix} {content}")
    else:
        print(prefix)


def print_debug(message: str) -> None:
    print_labeled("调试", message)


def input_labeled(label: str) -> str:
    return input(f"{next_label_prefix(label)} ")


def detect_provider() -> str:
    explicit_provider = os.getenv("PROVIDER", "").strip().lower()
    if explicit_provider:
        print_debug(f"检测到显式 PROVIDER={explicit_provider}")
        if explicit_provider in {"anthropic", "openai"}:
            return explicit_provider
        raise RuntimeError("PROVIDER 只支持 anthropic 或 openai")

    has_anthropic = bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))
    has_openai = bool(os.getenv("OPENAI_API_KEY"))
    print_debug(
        f"自动检测提供方: has_anthropic={has_anthropic}, has_openai={has_openai}"
    )

    if has_anthropic and not has_openai:
        print_debug("自动检测结果: 使用 Anthropic API")
        return "anthropic"
    if has_openai and not has_anthropic:
        print_debug("自动检测结果: 使用 OpenAI-compatible API")
        return "openai"
    if has_anthropic and has_openai:
        raise RuntimeError(
            "同时检测到 ANTHROPIC_* 和 OPENAI_* 凭证，自动检测无法判断。"
            + "请设置 PROVIDER=anthropic 或 PROVIDER=openai，或只保留一套凭证。"
        )
    raise RuntimeError(
        "未检测到可用凭证。请设置 ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN，"
        + "或设置 OPENAI_API_KEY。"
    )


def build_client(provider: str):
    if provider == "anthropic":
        if Anthropic is None:
            raise RuntimeError("未安装 anthropic，请执行: pip install anthropic")
        if os.getenv("ANTHROPIC_BASE_URL"):
            print_debug("检测到 ANTHROPIC_BASE_URL，清理 ANTHROPIC_AUTH_TOKEN 以避免双重认证冲突")
            _ = os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
        print_debug(
            f"初始化 Anthropic client: base_url={os.getenv('ANTHROPIC_BASE_URL') or '(default)'}"
        )
        return Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))

    if OpenAI is None:
        raise RuntimeError("未安装 openai，请执行: pip install openai")
    print_debug(
        f"初始化 OpenAI-compatible client: base_url={os.getenv('OPENAI_BASE_URL') or '(default)'}"
    )
    return OpenAI(
        api_key=os.environ["OPENAI_API_KEY"],
        base_url=os.getenv("OPENAI_BASE_URL"),
    )


PROVIDER = detect_provider()
client = build_client(PROVIDER)


# ── Tool execution ────────────────────────────────────────
def run_bash(command: str) -> str:
    print_debug(f"准备执行 bash 命令: {command}")
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        print_debug("命中危险命令拦截规则，拒绝执行")
        return "Error: Dangerous command blocked"

    try:
        print_debug(f"在目录 {os.getcwd()} 执行命令，超时=120s")
        result = subprocess.run(
            command,
            shell=True,
            cwd=os.getcwd(),
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = (result.stdout + result.stderr).strip()
        truncated = output[:50000] if output else "(no output)"
        print_debug(
            f"命令执行完成: returncode={result.returncode}, 输出长度={len(truncated)}"
        )
        return truncated
    except subprocess.TimeoutExpired:
        print_debug("命令执行超时，返回超时错误")
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as error:
        print_debug(f"命令执行发生系统错误: {error}")
        return f"Error: {error}"


# ── Provider-specific loops ───────────────────────────────
def anthropic_agent_loop(messages: list[object]) -> str:
    while True:
        print_debug(f"Anthropic 请求开始: 当前消息数={len(messages)}")
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=cast(list[AnthropicMessageParam], messages),
            tools=ANTHROPIC_TOOLS,
            max_tokens=8000,
        )
        print_debug(f"Anthropic 响应完成: stop_reason={response.stop_reason}")

        messages.append(cast(object, {"role": "assistant", "content": response.content}))
        print_debug("Anthropic assistant 内容已追加到消息历史")

        if response.stop_reason != "tool_use":
            final_text = "\n".join(
                block.text for block in response.content if block.type == "text"
            ).strip()
            print_debug(f"Anthropic 本轮无工具调用，最终回复长度={len(final_text)}")
            return final_text

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            command = block.input["command"]
            print_labeled("需要执行的工具", block.name)
            print_labeled("需要执行的命令", command)
            output = run_bash(command)
            print_labeled("工具结果", output[:200])
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": output,
            })

        if not results:
            print_debug("Anthropic 返回 tool_use 但未解析到工具块，直接结束以避免死循环")
            return ""

        messages.append(cast(object, {"role": "user", "content": results}))
        print_debug(f"Anthropic 工具结果已回填，本轮工具数={len(results)}")



def openai_agent_loop(messages: list[object]) -> str:
    while True:
        print_debug(f"OpenAI-compatible 请求开始: 当前消息数={len(messages)}")
        response = client.chat.completions.create(
            model=MODEL,
            messages=cast(
                list[OpenAIMessageParam],
                [{"role": "system", "content": SYSTEM}, *messages],
            ),
            tools=OPENAI_TOOLS,
            tool_choice="auto",
            max_tokens=8000,
        )

        message = response.choices[0].message
        tool_calls = list(message.tool_calls or [])
        print_debug(f"OpenAI-compatible 响应完成: tool_calls={len(tool_calls)}")

        assistant_message = {
            "role": "assistant",
            "content": message.content or "",
        }
        if tool_calls:
            assistant_message["tool_calls"] = [
                {
                    "id": tool_call.id,
                    "type": "function",
                    "function": {
                        "name": tool_call.function.name,
                        "arguments": tool_call.function.arguments,
                    },
                }
                for tool_call in tool_calls
            ]
        messages.append(cast(object, assistant_message))
        print_debug("OpenAI-compatible assistant 内容已追加到消息历史")

        if not tool_calls:
            final_text = message.content or ""
            print_debug(f"OpenAI-compatible 本轮无工具调用，最终回复长度={len(final_text)}")
            return final_text

        for tool_call in tool_calls:
            print_labeled("需要执行的工具", tool_call.function.name)

            if tool_call.function.name != "bash":
                print_debug(f"收到未知工具名: {tool_call.function.name}")
                output = f"Error: Unsupported tool: {tool_call.function.name}"
                print_labeled("工具结果", output)
                messages.append(cast(object, {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": output,
                }))
                continue

            try:
                args = json.loads(tool_call.function.arguments or "{}")
                print_debug(f"OpenAI-compatible 工具参数解析成功: keys={list(args.keys())}")
            except json.JSONDecodeError as error:
                print_debug(f"OpenAI-compatible 工具参数解析失败: {error}")
                output = f"Error: Invalid tool arguments JSON: {error}"
                print_labeled("工具结果", output)
                messages.append(cast(object, {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": output,
                }))
                continue

            command = args.get("command")
            if not isinstance(command, str) or not command:
                print_debug("OpenAI-compatible 工具参数缺少 command 字段")
                output = "Error: Missing command parameter"
                print_labeled("工具结果", output)
                messages.append(cast(object, {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": output,
                }))
                continue

            print_labeled("需要执行的命令", command)
            output = run_bash(command)
            print_labeled("工具结果", output[:200])
            messages.append(cast(object, {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": output,
            }))

        print_debug(f"OpenAI-compatible 工具结果已回填，本轮工具数={len(tool_calls)}")


# ── Dispatch loop ─────────────────────────────────────────
def agent_loop(messages: list[object]) -> str:
    print_debug(f"进入 agent_loop，provider={PROVIDER}, model={MODEL}")
    if PROVIDER == "anthropic":
        return anthropic_agent_loop(messages)
    return openai_agent_loop(messages)


# ── Entry point ──────────────────────────────────────────
if __name__ == "__main__":
    print_labeled("系统标题", "s01: Agent Loop")
    print_labeled(
        "使用说明",
        f"输入问题，回车发送。输入 q 退出。provider={PROVIDER}，model={MODEL}",
    )

    history: list[object] = []
    while True:
        try:
            query = input_labeled("输入提示")
        except (EOFError, KeyboardInterrupt):
            print_debug("检测到 EOF 或 KeyboardInterrupt，准备退出")
            break

        if query.strip().lower() in ("q", "exit", ""):
            print_debug("检测到退出指令或空输入，结束程序")
            break

        history.append({"role": "user", "content": query})
        print_debug(f"用户消息已追加，当前历史条数={len(history)}")
        final_text = agent_loop(history)
        if final_text:
            print_labeled("最终回复", final_text)
        else:
            print_debug("本轮最终回复为空字符串")
        print()
