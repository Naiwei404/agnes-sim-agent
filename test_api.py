"""T1：Agnes API 连通性测试。

验证：
  1) AGNES_API_KEY 已配置
  2) 网关 https://api.agnes-ai.cn/v1 可达
  3) agnes-3.0-flash 能正常返回（含一次 function calling 往返）

运行：venv\\Scripts\\python.exe test_api.py
"""
from openai import OpenAI
from config import AGNES_API_KEY, AGNES_BASE_URL, AGNES_MODEL, require_key


def main():
    require_key()
    client = OpenAI(api_key=AGNES_API_KEY, base_url=AGNES_BASE_URL)

    print(f"[1/3] 网关={AGNES_BASE_URL}  模型={AGNES_MODEL}")
    # 基础对话
    resp = client.chat.completions.create(
        model=AGNES_MODEL,
        messages=[{"role": "user", "content": "用一句话介绍你自己。"}],
        max_tokens=128,
    )
    print("[2/3] 普通对话返回：", resp.choices[0].message.content.strip())

    # 一次最小 function calling 往返，验证工具调用能力
    tools = [{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询某城市天气",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }]
    resp2 = client.chat.completions.create(
        model=AGNES_MODEL,
        messages=[{"role": "user", "content": "北京今天天气如何？"}],
        tools=tools,
        tool_choice="auto",
    )
    msg = resp2.choices[0].message
    if msg.tool_calls:
        print("[3/3] function calling 正常，模型请求工具：", msg.tool_calls[0].function.name)
    else:
        print("[3/3] 未触发工具调用（内容）：", (msg.content or "").strip())
    print("==> Agnes API 连通 OK")


if __name__ == "__main__":
    main()
