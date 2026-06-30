"""
测试 Qwen 内置工具 —— DashScope 原生 API 格式
直接调原生 API endpoint，不走 SDK
"""
import json, sys, os, httpx, asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from config import settings

API_KEY = settings.QWEN_API_KEY
HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}


async def native_chat(model: str, messages: list, **params):
    """调 DashScope 原生 text-generation API"""
    url = "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation"
    body = {
        "model": model,
        "input": {
            "messages": messages
        },
        "parameters": {
            "result_format": "message",
            **params
        }
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, headers=HEADERS, json=body)
        return resp


async def test_native_params():
    """DashScope 原生 API — 试各种参数格式"""
    msgs = [{"role": "user", "content": "帮我搜一下植物细胞结构的示意图"}]

    # 方式1: parameters.tools = [{name: "t2i_search"}]
    print("\n=== 方式1: parameters.tools ===")
    r1 = await native_chat("qwen3.6-flash", msgs, tools=[{"name": "t2i_search"}])
    print(f"  status: {r1.status_code}")
    if r1.status_code == 200:
        d = r1.json()
        print(f"  content: {d['output']['choices'][0]['message'].get('content','')[:500]}")
        if "tool_calls" in d["output"]["choices"][0]["message"]:
            print(f"  🛠️ 有 tool_calls")
    else:
        print(f"  body: {r1.text[:300]}")

    # 方式2: parameters.enable_search = True
    print("\n=== 方式2: enable_search=True ===")
    r2 = await native_chat("qwen3.6-flash", msgs, enable_search=True)
    print(f"  status: {r2.status_code}")
    if r2.status_code == 200:
        d = r2.json()
        content = d['output']['choices'][0]['message'].get('content','')[:500]
        print(f"  content: {content}")
    else:
        print(f"  body: {r2.text[:300]}")

    # 方式3: parameters.search_options
    print("\n=== 方式3: search_options ===")
    r3 = await native_chat("qwen3.6-flash", msgs, search_options={"enable_source": True})
    print(f"  status: {r3.status_code}")
    if r3.status_code == 200:
        d = r3.json()
        print(f"  content: {d['output']['choices'][0]['message'].get('content','')[:500]}")
    else:
        print(f"  body: {r3.text[:300]}")

    # 方式4: input.messages + tools 放 input 里
    print("\n=== 方式4: tools 放 input ===")
    body = {
        "model": "qwen3.6-flash",
        "input": {
            "messages": msgs,
            "tools": [{"name": "t2i_search"}]
        },
        "parameters": {
            "result_format": "message"
        }
    }
    async with httpx.AsyncClient(timeout=60) as client:
        r4 = await client.post(
            "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation",
            headers=HEADERS, json=body
        )
        print(f"  status: {r4.status_code}")
        if r4.status_code == 200:
            d = r4.json()
            print(f"  content: {d['output']['choices'][0]['message'].get('content','')[:500]}")
        else:
            print(f"  body: {r4.text[:300]}")

    # 方式5: 看看官方 qwen-agent 是怎么调的
    print("\n=== 方式5: enable_search + t2i_search 同时 ===")
    r5 = await native_chat("qwen3.6-flash", msgs,
                           enable_search=True,
                           tools=[{"name": "t2i_search"}, {"name": "web_search"}])
    print(f"  status: {r5.status_code}")
    if r5.status_code == 200:
        d = r5.json()
        msg = d['output']['choices'][0]['message']
        finish = d['output']['choices'][0].get('finish_reason', '')
        print(f"  finish_reason: {finish}")
        print(f"  content: {msg.get('content','')[:500]}")
        if "tool_calls" in msg:
            print(f"  🛠️ tool_calls")
            for tc in msg["tool_calls"]:
                print(f"     {tc['function']['name']}: {tc['function']['arguments']}")
        else:
            print(f"  无 tool_calls（服务端执行了？）")
            # 看看是不是直接返回了搜索图片结果
            if msg.get('content'):
                print(f"  可能服务端已执行搜索")


async def test_sdk_version():
    """查看 dashscope SDK 版本"""
    import dashscope
    print(f"\ndashscope version: {dashscope.__version__}")


async def test_qwen_agent_style():
    """Qwen-Agent 风格的调用: function_list 参数"""
    import dashscope
    print("\n=== Qwen-Agent 风格: parameters.function_list ===")
    resp = dashscope.Generation.call(
        model='qwen3.6-flash',
        messages=[{"role": "user", "content": "帮我搜一下关于细胞分裂的图片"}],
        result_format='message',
        function_list=["t2i_search"],
    )
    print(f"  status: {resp.status_code}")
    if resp.status_code == 200:
        msg = resp.output.choices[0].message
        print(f"  content: {str(msg.content)[:500]}")
    else:
        print(f"  Error: {resp}")


if __name__ == "__main__":
    asyncio.run(test_native_params())
    asyncio.run(test_sdk_version())
    asyncio.run(test_qwen_agent_style())
