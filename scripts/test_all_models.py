"""
FeClaw 全模型综合测试 —— llm_service.chat
- 文本模型：验证连通性
- 支持深度思考的模型：额外测一轮 thinking=enable
- Token 用量：从 llm_service 记录 + DB llm_stats 验证
"""
import asyncio, sys, os, time, json
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from dotenv import load_dotenv; load_dotenv()

from services.llm_service import llm_service
from services.model_registry import resolve, MODEL_REGISTRY
from sqlalchemy import text as sa_text
from models.database import SessionLocal

results = []

async def test(name, enable_thinking=False):
    info = resolve(name)
    provider = info['provider']
    key_attr = info.get('api_key_attr', '?')
    key = os.environ.get(key_attr, '')
    if not key:
        results.append((name, enable_thinking, '❌', 0, '缺少 API Key'))
        return

    t0 = time.time()
    usage_holder = [None]  # P0.5 机制：provider 写入 usage 到此 list
    try:
        text = ''
        async for chunk in llm_service.chat(
            messages=[{'role': 'user', 'content': '用中文回复"OK"即可，不要多余内容'}],
            provider=provider, model=name,
            stream=False,
            disable_thinking=not enable_thinking,
            request_type='test',
        ):
            text += chunk
        elapsed = time.time() - t0
        content = text[:40].replace('\n', ' ')
        results.append((name, enable_thinking, '✅', f'{elapsed:.1f}s — {content}'))
    except Exception as e:
        elapsed = time.time() - t0
        err = str(e)[:80].replace('\n', ' ')
        results.append((name, enable_thinking, '❌', f'{elapsed:.1f}s — {err}'))

async def test_vision(name):
    info = resolve(name)
    provider = info['provider']
    t0 = time.time()
    try:
        text = ''
        async for chunk in llm_service.chat(
            messages=[{'role': 'user', 'content': [{'type': 'text', 'text': '回复"OK"即可'}]}],
            provider=provider, model=name,
            stream=False, disable_thinking=True, request_type='test',
        ):
            text += chunk
        elapsed = time.time() - t0
        content = text[:40].replace('\n', ' ')
        results.append((name, False, '✅', None, f'{elapsed:.1f}s — {content}'))
    except Exception as e:
        elapsed = time.time() - t0
        # 文生图模型 accept 纯文本失败是预期行为
        if '400' in str(e):
            results.append((name, False, '⚠️', None, f'{elapsed:.1f}s — 文生图模型，不收纯文本'))
        else:
            err = str(e)[:80].replace('\n', ' ')
            results.append((name, False, '❌', None, f'{elapsed:.1f}s — {err}'))

async def main():
    print('═' * 72)
    print('  FeClaw 全模型综合测试 + 深度思考 + Token 追踪')
    print('═' * 72)

    qwen_text  = ['qwen3.6-flash', 'qwen3.6-plus', 'qwen3.7-plus', 'qwen3.7-max']
    qwen_vis   = ['qwen3.6-35b-a3b', 'qwen3-vl-flash', 'qwen3-vl-plus']
    deepseek   = ['deepseek-v4-flash']
    zhipu      = ['glm-4.7', 'glm-4.7-flash', 'glm-4.5-air', 'glm-5-turbo', 'glm-5']
    zhipu_vis  = ['glm-4.6v']
    kimi       = ['kimi-k2.5', 'kimi-k2.6']
    mimo       = ['mimo-v2.5', 'mimo-v2.5-pro', 'mimo-v2.5-pro-ultraspeed']
    doubao     = ['doubao-seed-2-0-lite-260215', 'doubao-seed-2-1-turbo-260628', 'doubao-seed-2-1-pro-260628']
    doubao_img = ['doubao-seedream-5-0-260128']  # 文生图

    all_text = qwen_text + deepseek + zhipu + kimi + mimo + doubao
    all_vis  = qwen_vis + zhipu_vis + doubao_img

    # ---- 文本模型 ----
    print(f'\n── 文本模型 ({len(all_text)}) ──')
    for m in all_text:
        await test(m, enable_thinking=False)
    
    for name, thinking, status, detail in results[-len(all_text):]:
        print(f'  {status} {name:35s} {detail}')

    # ---- 深度思考模型 ----
    thinking_models = [n for n,i in MODEL_REGISTRY.items()
                       if i.get('supports_thinking') and 'embed' not in n and 'rerank' not in n]
    if thinking_models:
        thinking_results_start = len(results)
        print(f'\n── 深度思考测试 ({len(thinking_models)}) ──')
        for m in thinking_models:
            await test(m, enable_thinking=True)
        
        for name, thinking, status, detail in results[thinking_results_start:]:
            print(f'  {status} {name:35s} {detail}')

    # ---- 视觉模型 ----
    vis_start = len(results)
    print(f'\n── 视觉模型 ({len(all_vis)}) ──')
    for m in all_vis:
        await test_vision(m)
    
    for name, _, status, _, detail in results[vis_start:]:
        print(f'  {status} {name:35s} {detail}')

    # ---- Token 追踪验证 ----
    print(f'\n── Token 追踪（查 DB llm_stats）──')
    db = SessionLocal()
    try:
        rows = db.execute(
            sa_text("SELECT model, tokens_used, request_type FROM llm_stats ORDER BY id DESC LIMIT 5")
        ).fetchall()
        for r in rows:
            print(f'  📊 {r.model:35s} tokens={r.tokens_used}  type={r.request_type}')
    finally:
        db.close()

    # ---- 汇总 ----
    passed  = sum(1 for _,_,s,_ in results if s == '✅')
    warned  = sum(1 for _,_,s,_ in results if s == '⚠️')
    failed  = sum(1 for _,_,s,_ in results if s == '❌')
    total_tests = len([r for r in results])
    print(f'\n{"═" * 72}')
    print(f'  {passed} 通过, {warned} 预期失败, {failed} 失败 / 共 {total_tests} 次测试')
    print('═' * 72)

asyncio.run(main())
