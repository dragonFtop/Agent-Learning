from config import MODEL, SYSTEM, client
from tools import PARENT_TOOLS, TOOLS_HANDLERS

def agent_loop(messages: list):
    while True:
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=PARENT_TOOLS,
            max_tokens=8000,
        )

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return

        results = []
        for block in response.content:
            if block.type == "tool_use":
                if block.name=="task":
                    desc=block.input.get("description","subtask")
                    print(f">task ({desc}):{block.input['prompt'][:80]}")
                    output = run_subagent(block.input["prompt"])
                else:
                    handler = TOOLS_HANDLERS.get(block.name)
                    output = handler(**block.input) if handler else f"UnKnown tool:{block.name}"
                print(f"{str(output)[:200]}")
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": output}
                )
        messages.append({"role": "user", "content": results})
