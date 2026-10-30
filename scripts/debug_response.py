"""手动调试脚本：查看 DeepSeek Anthropic 端点返回的 response 真实属性。

运行方式（项目根目录，需先启动 LiteLLM 网关）：py -m scripts.debug_response
"""
from config import client, MODEL

# 1) 普通对话调用
r1 = client.messages.create(
    model=MODEL,
    max_tokens=100,
    messages=[{"role": "user", "content": "用一句话打招呼"}],
)
print("=== 普通调用 ===")
print("字段列表 =", list(r1.to_dict().keys()))
print("stop_reason =", repr(r1.stop_reason))
print("content 块类型 =", [b.type for b in r1.content])
for b in r1.content:
    if b.type == "text":
        print("text =", b.text[:60])

# 2) 带工具的调用
TOOL = {
    "name": "get_weather",
    "description": "查询一个城市的天气",
    "input_schema": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    },
}
r2 = client.messages.create(
    model=MODEL,
    max_tokens=200,
    tools=[TOOL],
    messages=[{"role": "user", "content": "北京今天天气怎么样？请调用工具查询。"}],
)
print("\n=== 带工具调用 ===")
print("stop_reason =", repr(r2.stop_reason))
print("content 块类型 =", [b.type for b in r2.content])
for b in r2.content:
    if b.type == "tool_use":
        print("tool name =", b.name, "| input =", b.input)
