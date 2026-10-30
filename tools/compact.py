"""
压缩工具：主要压缩tool调用的结果
"""


def estimate_tokens(messages: list) -> int:
    """粗略估计token数量"""
    return len(str(messages)) // 4

# 压缩第一层微压缩
def micro_compact(messages: list) -> list:
    tool_result = []
    for msg_idx,msg in enumerate(messages):
        if msg["role"] =="user" and isinstance(msg.get["content"],list):
