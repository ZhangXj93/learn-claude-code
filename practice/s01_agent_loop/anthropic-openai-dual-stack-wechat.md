# 我把一个 Anthropic 版 Agent Loop，改成了 Anthropic 和 OpenAI 双栈，这里面真正难的不是换 SDK

上一篇我刚把最小 Agent Loop 跑通。

也就是那种最朴素的版本，你给模型一句话，它决定要不要调 Bash，调了之后把结果喂回去，再继续下一轮。

代码不长。

但骨架已经有了。

如果你已经看过上一篇，应该会有印象，我当时一直在强调一件事，很多所谓的 Agent，先别急着谈长期记忆、工作流、多 Agent 协作，先把那条最小执行回路亲手跑通。

因为那条回路一旦通了，很多东西就不神秘了。

结果刚跑通没多久，真实问题就来了。

原脚本只支持 Anthropic 的 API Key。
如果你现在手里只有 OpenAI 兼容的 key，它就直接废了。

很多人第一反应会是，换个 base_url，不就完了。

结果并不是。

## 先讲结论

如果你手里的是 OpenAI 兼容 key，而脚本内部写的是 Anthropic SDK 的 `client.messages.create(...)`，那它不是小改，是两层不兼容。

第一层是请求协议不一样。
第二层是工具调用的返回结构不一样。

所以这个兼容改造，真正要做的不是换一行初始化代码。

## 先看原始脚本到底在做什么

原来的 agent.py 很干净，核心其实就三块。

第一块，定义 system prompt 和工具。
这里只有一个工具，就是 bash。

第二块，调用模型。
用的是 Anthropic SDK 的 `client.messages.create(...)`。

第三块，进入 agent loop。
如果 `stop_reason == "tool_use"`，就把工具执行掉，然后把 `tool_result` 回填给模型，继续下一轮。

整个逻辑，长这样。

```python
while True:
    response = client.messages.create(
        model=MODEL,
        system=SYSTEM,
        messages=messages,
        tools=TOOLS,
        max_tokens=8000,
    )

    messages.append({"role": "assistant", "content": response.content})

    if response.stop_reason != "tool_use":
        return

    results = []
    for block in response.content:
        if block.type == "tool_use":
            output = run_bash(block.input["command"])
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": output,
            })

    messages.append({"role": "user", "content": results})
```

## 为什么不能直接把 Anthropic key 换成 OpenAI 兼容 key

表面上看，这两种服务都在做一件事，给你一个大模型接口，然后支持 tools。
但代码层面，它们不是同一套输入输出结构。

Anthropic 这边，你写的是这样。

```python
response = client.messages.create(...)
```

OpenAI 兼容这边，通常写的是这样。

```python
response = client.chat.completions.create(...)
```

它连 API 的形状都不是一个形状。

再往下看，工具定义也不一样。

Anthropic 的工具定义更像这样。

```python
{
    "name": "bash",
    "description": "Run a shell command.",
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {"type": "string"}
        },
        "required": ["command"]
    }
}
```

OpenAI 兼容的 function calling，更像这样。

```python
{
    "type": "function",
    "function": {
        "name": "bash",
        "description": "Run a shell command.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string"}
            },
            "required": ["command"]
        }
    }
}
```

再往下，模型返回的 tool call 也不是一回事。

Anthropic 一般是 content block。

```python
for block in response.content:
    if block.type == "tool_use":
        command = block.input["command"]
```

OpenAI 兼容一般是 `message.tool_calls`。

```python
tool_calls = message.tool_calls or []
for tool_call in tool_calls:
    args = json.loads(tool_call.function.arguments)
    command = args["command"]
```

一个是结构化 block。
一个是函数调用列表，参数还是 JSON 字符串。

## 这个兼容改造里，我们先小改

因为本教程是聚焦在让读者学习 Agent Loop ，而不是学到一团工程包装，所以这次的架构原则很简单。

**保留一个脚本，保留一个主入口，保留一个 `agent_loop`，只把 provider 差异压在最薄的一层。**

也就是这几个点分离出来。

- `detect_provider()`，决定走 Anthropic 还是 OpenAI
- `build_client(provider)`，创建对应 client
- `anthropic_agent_loop(...)`，跑 Anthropic 分支
- `openai_agent_loop(...)`，跑 OpenAI 分支
- `agent_loop(...)`，只做总分发

这个结构的好处很直接。

第一，主流程还是看得懂。
第二，差异都放在 provider 分支里。
第三，如果你以后再接第三个 provider，也知道该往哪加。
第四，这个复杂度还没高到把教学脚本搞废。

最后脚本的骨架，大概是这样的。

```python
PROVIDER = detect_provider()
client = build_client(PROVIDER)

def agent_loop(messages: list[object]) -> str:
    if PROVIDER == "anthropic":
        return anthropic_agent_loop(messages)
    return openai_agent_loop(messages)
```

这个脚本只有两个 provider。
Anthropic 一条分支。
OpenAI 一条分支。
共享的东西只有这些。

- `SYSTEM`
- `MODEL`
- `run_bash()`
- 打印日志
- 顶层交互循环

这就够了。

## Provider 自动检测

这看起来简单，其实很坑。

因为真实环境里，经常会同时存在多套环境变量。
你本机可能之前配过 `ANTHROPIC_API_KEY`。
今天又加了 `OPENAI_API_KEY`。
如果检测规则不清楚，最后你根本不知道脚本到底走了哪边。

所以我最后的思路是，自动检测可以有，但冲突时必须报错。

逻辑大概是这样。

```python
def detect_provider() -> str:
    explicit_provider = os.getenv("PROVIDER", "").strip().lower()
    if explicit_provider:
        if explicit_provider in {"anthropic", "openai"}:
            return explicit_provider
        raise RuntimeError("PROVIDER 只支持 anthropic 或 openai")

    has_anthropic = bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))
    has_openai = bool(os.getenv("OPENAI_API_KEY"))

    if has_anthropic and not has_openai:
        return "anthropic"
    if has_openai and not has_anthropic:
        return "openai"
    if has_anthropic and has_openai:
        raise RuntimeError("同时检测到两套凭证，请显式设置 PROVIDER")

    raise RuntimeError("未检测到可用凭证")
```

一个是，**自动检测只在不含糊的时候自动。**
一个是，**一旦环境模糊，就强制用户显式声明。**

Agent 这种东西，一旦接上真实工具，走错 provider 不是小问题。
你可能调了一个完全不同的模型，拿到完全不同的 tool calling 行为，然后还以为是脚本逻辑坏了。

排查起来会很烦。

所以这一步千万别偷懒。

## Anthropic 分支

请求入口还是 `client.messages.create(...)`。
工具定义还是 Anthropic 的 `input_schema`。
最重要的是，模型返回里，工具调用混在 `response.content` 里。

所以 Anthropic 这边核心流程没变，还是这几步。

```python
response = client.messages.create(
    model=MODEL,
    system=SYSTEM,
    messages=messages,
    tools=ANTHROPIC_TOOLS,
    max_tokens=8000,
)

messages.append({"role": "assistant", "content": response.content})

if response.stop_reason != "tool_use":
    return final_text

results = []
for block in response.content:
    if block.type == "tool_use":
        output = run_bash(block.input["command"])
        results.append({
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": output,
        })

messages.append({"role": "user", "content": results})
```

这里你注意一下，Anthropic 的好处是它的工具回填逻辑非常直观。
模型告诉你它要调哪个 tool。
你执行。
把结果作为 `tool_result` 喂回去。
然后继续跑。

## OpenAI 分支

OpenAI 兼容这边，变化最大的其实不是请求入口，而是**工具调用的消息组织方式**。

它不像 Anthropic 那样把工具调用直接长在 content blocks 里。
它一般会把工具调用挂在 `message.tool_calls` 上。

而且参数不是 dict。
是字符串。

所以这一段必须单独处理。

```python
response = client.chat.completions.create(
    model=MODEL,
    messages=[
        {"role": "system", "content": SYSTEM},
        *messages,
    ],
    tools=OPENAI_TOOLS,
    tool_choice="auto",
    max_tokens=8000,
)

message = response.choices[0].message
tool_calls = list(message.tool_calls or [])
```

然后如果有工具调用，要把 assistant 这一轮先塞回历史。

```python
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
```

再往下，执行工具。

```python
args = json.loads(tool_call.function.arguments or "{}")
command = args["command"]
output = run_bash(command)

messages.append({
    "role": "tool",
    "tool_call_id": tool_call.id,
    "content": output,
})
```

你看，Anthropic 那边是 `tool_result`。
OpenAI 这边是 `role = tool`。

Anthropic 那边你遍历 `response.content`。
OpenAI 这边你遍历 `tool_calls`。

Anthropic 那边参数已经是结构化对象。
OpenAI 这边还得 `json.loads(...)`。

## 这里有个很容易漏掉的点，system prompt 放哪

Anthropic 和 OpenAI 兼容接口，在 system prompt 的组织方式上也不完全一样。

Anthropic 这边，你可以这么传。

```python
client.messages.create(
    system=SYSTEM,
    messages=messages,
    ...
)
```

OpenAI 兼容这边，一般得把 system prompt 塞进 messages 的第一项。

```python
messages=[
    {"role": "system", "content": SYSTEM},
    *messages,
]
```

这个差异不大，但很容易漏。

如果你强行用一套统一消息结构去喂两边，最后就会在这种地方不断打补丁。
一会儿这个 provider 需要独立 `system` 字段。
一会儿那个 provider 只认 messages 里的 system role。

所以还是那句话，分支写开，反而更清楚。

## 如果你也要自己改一版，建议你按这个顺序来

### 第一步，先确认原始 Anthropic 版本是能跑的

也就是你要先确认这个 loop 是活的。
不是一开始就在坏代码上做兼容。

至少要能完成这些动作。

- 用户输入一句话
- 模型发出一个 bash 工具调用
- 本地命令被执行
- 工具结果被回填
- 模型给出最终回复

如果这一步还没通，就别往下了。

### 第二步，只改 provider 检测和 client 初始化

先把这两块拆出来。

```python
PROVIDER = detect_provider()
client = build_client(PROVIDER)
```

这一步不要急着碰 tool call 解析。
只先把入口分流搭起来。

### 第三步，再分叉两个 agent loop

Anthropic 保持原结构。
OpenAI 单独写一份。

这个阶段你会有一点重复代码。
没关系。
重复比错误抽象强。

### 第四步，再处理工具调用差异

Anthropic 的 `tool_use`。
OpenAI 的 `tool_calls`。
Anthropic 的 `tool_result`。
OpenAI 的 `role = tool`。

这一块是兼容核心。
要一条条对齐。

### 第五步，最后再看类型检查和日志

很多人会反过来做。
一上来就先把类型写得特别漂亮。

结果逻辑还没通，已经把自己绕晕了。

我的建议是，先跑通，再把调用边界的类型和日志补上。
效率高很多。

以上，既然看到这里了，如果觉得有用，随手点个赞、在看、转发三连吧，如果想第一时间收到推送，也可以给我个星标⭐～

谢谢你看我的文章，我们，下次再见。

> / 作者，同学小张