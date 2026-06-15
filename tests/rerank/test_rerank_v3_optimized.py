#!/usr/bin/env python3
import sys, json, httpx, time
sys.path.insert(0, '/home/lch/Projects/FeClaw')

import os
from config import settings
from services.vector_search_service import VectorSearchService, VECTOR_BUCKET, VECTOR_ENDPOINT, EMBEDDING_API_URL
from qcloud_cos import CosConfig, CosVectorsClient

QWEN_API_KEY = settings.QWEN_API_KEY or os.environ.get('QWEN_API_KEY')
if not QWEN_API_KEY:
    import dotenv
    dotenv.load_dotenv('/home/lch/Projects/FeClaw/.env')
    QWEN_API_KEY = os.environ.get('QWEN_API_KEY') or os.environ.get('DASHSCOPE_API_KEY')

RERANK_URL = 'https://dashscope.aliyuncs.com/compatible-api/v1/reranks'

# ====== 15 queries ======
QUERIES = [
    '二次函数顶点坐标公式',
    '三角函数诱导公式推导过程',
    '牛顿第二定律的应用场景',
    '什么是匀速圆周运动',
    '氧化还原反应的本质',
    '化学平衡移动原理',
    '光合作用的化学方程式',
    '细胞有丝分裂过程',
    '辛亥革命的历史意义',
    '抗日战争重要战役',
    '鲁迅《祝福》祥林嫂人物形象',
    '文言文断句技巧',
    '英语阅读理解主旨大意题解题方法',
    '地球自转的地理意义',
    '人民代表大会制度的特点',
]

# ====== Better truncation ======
# Qwen tokenizer: ~1.8 chars/token for Chinese-dominant text
# Rerank max: 4000 tokens per doc, leave ~500 for query
# Safe limit: 3500 tokens ≈ 6300 chars
MAX_TOKENS = 3500
CHARS_PER_TOKEN = 1.8
MAX_CHARS = int(MAX_TOKENS * CHARS_PER_TOKEN)  # ~6300

def smart_truncate(text):
    """Keep head + proportional middle + tail, more generous"""
    if len(text) <= MAX_CHARS:
        return text
    head_len = 1000
    tail_len = 800
    middle_max = MAX_CHARS - head_len - tail_len
    middle_start = head_len
    middle_end = len(text) - tail_len
    if middle_end <= middle_start:
        return text[:head_len] + text[-tail_len:]
    middle_text = text[middle_start:middle_end]
    if len(middle_text) > middle_max:
        mid_head = middle_text[:middle_max // 2]
        mid_tail = middle_text[-(middle_max - middle_max // 2):]
        middle_text = mid_head + '\n...(snip)...\n' + mid_tail
    result = text[:head_len] + '\n...\n' + middle_text + '\n...\n' + text[-tail_len:]
    return result

def token_estimate(text):
    return len(text) / CHARS_PER_TOKEN

def rerank(query, documents, top_n=5):
    texts = [smart_truncate(d.get('text','')) for d in documents]
    payload = {
        'model': 'qwen3-rerank',
        'query': query,
        'documents': texts,
        'top_n': min(top_n, len(texts)),
    }
    headers = {
        'Authorization': f'Bearer {QWEN_API_KEY}',
        'Content-Type': 'application/json',
    }
    resp = httpx.post(RERANK_URL, json=payload, headers=headers, timeout=30)
    data = resp.json()
    if 'output' in data and 'results' in data['output']:
        return data['output']['results']
    elif 'results' in data:
        return data['results']
    return []

async def run_tests():
    vs = VectorSearchService()
    report = []
    total_tokens_usage = 0
    
    for i, query in enumerate(QUERIES):
        print(f'\n{"="*60}')
        print(f'[{i+1}/15] {query}')
        print('='*60)
        
        # Vector search top 5 (current production)
        vec_top5 = await vs.search(query, index='idx-public-kb', top_k=5)
        vec_keys = [r.get('key','') for r in vec_top5]
        vec_scores = [r.get('score',0) for r in vec_top5]
        
        # Vector search top 100 for rerank (COS API max)
        vec_top100 = await vs.search(query, index='idx-public-kb', top_k=100)
        print(f'  Vector search returned {len(vec_top100)} candidates for rerank')
        
        if len(vec_top100) < 2:
            print(f'  Too few results, skipping')
            continue
        
        # Rerank
        docs = [{'text': r.get('metadata',{}).get('text',''), 'key': r.get('key','')} for r in vec_top100]
        
        # Stats about truncation impact
        total_chars = sum(len(d.get('text','')) for d in docs)
        est_tokens = total_chars / CHARS_PER_TOKEN
        total_tokens_usage += est_tokens
        truncated_count = sum(1 for d in docs if len(d.get('text','')) > MAX_CHARS)
        
        t0 = time.time()
        rr = rerank(query, docs, top_n=5)
        t1 = time.time()
        
        if not rr:
            print(f'  Rerank failed, skipping')
            continue
            
        rerank_scores = [r['relevance_score'] for r in rr]
        rerank_indices = [r['index'] for r in rr]
        rerank_keys = [docs[idx]['key'] for idx in rerank_indices]
        
        overlap = len(set(vec_keys) & set(rerank_keys))
        
        print(f'  Docs truncated (>{MAX_CHARS} chars): {truncated_count}/{len(docs)}')
        print(f'  Avg doc: {total_chars//len(docs)} chars, ~{est_tokens//len(docs):.0f} tok/doc')
        print(f'  Total est tokens: {est_tokens:.0f} (limit 120,000)')
        print(f'  Rerank time: {t1-t0:.2f}s | Overlap: {overlap}/5')
        print()
        print(f'  {"Rank":<6} {"Vector Source":<10} {"Score":<8} | {"Rerank Source":<10} {"Score":<8}')
        print(f'  {"-"*50}')
        for j in range(5):
            vk = 'gaokao' if 'gaokao' in (vec_keys[j] if j < len(vec_keys) else '') else ('textbook' if 'textbooks' in (vec_keys[j] if j < len(vec_keys) else '') else '?')
            vs_ = f'{vec_scores[j]:.4f}' if j < len(vec_scores) else ''
            rk = 'gaokao' if 'gaokao' in (rerank_keys[j] if j < len(rerank_keys) else '') else ('textbook' if 'textbooks' in (rerank_keys[j] if j < len(rerank_keys) else '') else '?')
            rs_ = f'{rerank_scores[j]:.4f}' if j < len(rerank_scores) else ''
            print(f'  {j+1:<6} {vk:<10} {vs_:<8} | {rk:<10} {rs_:<8}')
        
        # Show detailed changes for 1st result
        if vec_keys[0] != rerank_keys[0]:
            print(f'  #1 changed: {vec_keys[0][:45]} -> {rerank_keys[0][:45]}')
        
        report.append({
            'query': query,
            'vec_gaokao': sum(1 for k in vec_keys if 'gaokao' in k),
            'vec_textbook': sum(1 for k in vec_keys if 'textbooks' in k),
            'rerank_gaokao': sum(1 for k in rerank_keys if 'gaokao' in k),
            'rerank_textbook': sum(1 for k in rerank_keys if 'textbooks' in k),
            'overlap': overlap,
            'rerank_time': round(t1-t0, 2),
            'total_candidates': len(vec_top100),
            'truncated_docs': truncated_count,
        })
    
    # Summary
    print(f'\n\n{"="*70}')
    print('SUMMARY: top-100 vs top-5 rerank (generous truncation)')
    print('='*70)
    print(f'{"Query":<35} {"Vec G":<6}{"Vec T":<6}{"RR G":<6}{"RR T":<6}{"Ovlp":<6}{"Trunc":<6}')
    print('-'*70)
    for r in report:
        q = r['query'][:33]
        print(f'{q:<35} {r["vec_gaokao"]:<6} {r["vec_textbook"]:<6} {r["rerank_gaokao"]:<6} {r["rerank_textbook"]:<6} {r["overlap"]}/5  {r["truncated_docs"]}')
    
    print('-'*70)
    avg_ovlp = sum(r['overlap'] for r in report) / len(report)
    changed = sum(1 for r in report if r['overlap'] < 5)
    total_trunc = sum(r['truncated_docs'] for r in report)
    print(f'Average overlap: {avg_ovlp:.1f}/5')
    print(f'Changed (not identical): {changed}/{len(report)}')
    print(f'Total truncated docs across all queries: {total_trunc}')
    print(f'Avg truncation per query: {total_trunc/len(report):.1f} docs')
    print(f'COST: est {total_tokens_usage:.0f} tokens, ~${total_tokens_usage/1e6*0.1:.4f}')
    print()
    print('G=gaokao, T=textbook, Ovlp=overlap(5) Trunc=docs_truncated')

if __name__ == '__main__':
    import asyncio
    asyncio.run(run_tests())
