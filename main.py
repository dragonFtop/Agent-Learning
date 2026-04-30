from agent import agent_loop


def main():
    history = []
    while True:
        try:
            query = input("\033[36mDragonfeng >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break

        # 退出条件：输入 q、exit 或者直接回车都可以退出程序
        if query.strip().lower() in ("q", "exit", ""):
            break

        history.append({"role": "user", "content": query})
        agent_loop(history)

        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()


if __name__ == "__main__":
    main()
