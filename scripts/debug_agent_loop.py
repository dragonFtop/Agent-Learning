"""端到端手动测试：agent_loop 完整工具循环（工具执行 + 结果回传 + 子代理）。

运行方式（项目根目录）：py -m scripts.debug_agent_loop
"""
from agent import agent_loop


def run(prompt: str, label: str):
    print(f"\n===== {label} =====")
    msgs = [{"role": "user", "content": prompt}]
    agent_loop(msgs)
    for b in msgs[-1]["content"]:
        if hasattr(b, "text") and b.text.strip():
            print(">>>", b.text[:300])


run("请调用 read_file 工具读取 config.py 文件的前 3 行，然后告诉我里面 import 了什么。",
    "测试1：直接工具调用")
run("请创建一个子代理（task 工具），让它数一下 tools 目录里有几个 .py 文件，然后把结果告诉我。",
    "测试2：子代理")
