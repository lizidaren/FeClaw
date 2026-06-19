#!/usr/bin/env python3
import sys, json, httpx, time
sys.path.insert(0, '/home/lch/Projects/FeClaw')

import os
from config import settings
from services.vector_search_service import VectorSearchService, VECTOR_BUCKET, VECTOR_ENDPOINT
from qcloud_cos import CosConfig, CosVectorsClient

QWEN_API_KEY = settings.QWEN_API_KEY or os.environ.get('QWEN_API_KEY')
if not QWEN_API_KEY:
    # fallback: read from .env
    import dotenv
    dotenv.load_dotenv('/home/lch/Projects/FeClaw/.env')
    QWEN_API_KEY = os.environ.get('QWEN_API_KEY') or os.environ.get('DASHSCOPE_API_KEY')

RERANK_URL = 'https://dashscope.aliyuncs.com/compatible-api/v1/reranks'

# ====== 15 test queries ======
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

def truncate_text(text, max_chars=3000):
    if len(text) <= max_chars:
        return text
    head = text[:400]
    tail = text[-(max_chars - 400):]
    return head + '...' + tail

def rerank(query, documents, top_n=5):
    texts = [truncate_text(d.get('text','')[:4000], 3000) for d in documents]
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
    print(f'Rerank API error: {json.dumps(data, ensure_ascii=False)[:300]}')
    return []

async def run_tests():
    vs = VectorSearchService()
    config = CosConfig(Region='ap-guangzhou', SecretId=settings.TENCENT_COS_SECRET_ID, SecretKey=settings.TENCENT_COS_SECRET_KEY, Endpoint=VECTOR_ENDPOINT, Scheme='https')
    cvc = CosVectorsClient(config)
    
    report = []
    for i, q in enumerate(QUERIES):
        print(f'\n{"="*60}')
        print(f'[{i+1}/15] {q}')
        print('='*60)
        
        # Pure vector search top 5
        vec_top5 = await vs.search(q, index='idx-public-kb', top_k=5)
        vec_scores = [r.get('score',0) for r in vec_top5]
        vec_keys = [r.get('key','')[:50] for r in vec_top5]
        
        # Vector search top 50 for rerank
        vec_top50 = await vs.search(q, index='idx-public-kb', top_k=50)
        
        if len(vec_top50) < 2:
            print('  Too few results, skipping rerank')
            report.append({'query': q, 'vec_top5_keys': vec_keys, 'vec_scores': [round(s,4) for s in vec_scores], 'rerank_top5_keys': vec_keys, 'rerank_scores': [round(s,4) for s in vec_scores], 'note': 'too few results'})
            continue
        
        # Rerank
        docs_for_rerank = [{'text': r.get('metadata',{}).get('text',''), 'key': r.get('key','')} for r in vec_top50]
        
        t0 = time.time()
        rr = rerank(q, docs_for_rerank, top_n=5)
        t1 = time.time()
        
        rerank_scores = [r['relevance_score'] for r in rr]
        rerank_indices = [r['index'] for r in rr]
        rerank_keys = [docs_for_rerank[idx]['key'][:50] for idx in rerank_indices]
        
        overlap = len(set(vec_keys) & set(rerank_keys))
        
        print(f'  {"Rank":<6} {"Vector Top5":<52} {"Score":<8} | {"Rerank Top5":<52} {"Score":<8}')
        print(f'  {"-"*120}')
        for j in range(max(len(vec_keys), len(rerank_keys))):
            vk = vec_keys[j] if j < len(vec_keys) else ''
            vs_ = f'{vec_scores[j]:.4f}' if j < len(vec_scores) else ''
            rk = rerank_keys[j] if j < len(rerank_keys) else ''
            rs_ = f'{rerank_scores[j]:.4f}' if j < len(rerank_scores) else ''
            print(f'  {j+1:<6} {vk:<52} {vs_:<8} | {rk:<52} {rs_:<8}')
        
        print(f'  Rerank time: {t1-t0:.2f}s, Overlap: {overlap}/5')
        
        report.append({
            'query': q,
            'vec_top5_keys': vec_keys,
            'vec_scores': [round(s,4) for s in vec_scores],
            'rerank_top5_keys': rerank_keys,
            'rerank_scores': [round(s,4) for s in rerank_scores],
            'overlap': overlap,
            'rerank_time_s': round(t1-t0, 2),
            'total_vec_results': len(vec_top50),
        })
    
    # Summary
    print(f'\n\n{"="*70}')
    print('RESULT COMPARISON SUMMARY')
    print('='*70)
    print(f'{"Query (truncated)":<35} {"Overlap/5":<10} {"Vec #1 source":<25} {"Rerank #1 source":<25}')
    print('-'*95)
    
    total_overlap = 0
    changed_first = 0
    from collections import Counter
    subj_changes = Counter()
    
    for r in report:
        v_top_key = r['vec_top5_keys'][0] if r['vec_top5_keys'] else ''
        r_top_key = r['rerank_top5_keys'][0] if r['rerank_top5_keys'] else ''
        q_short = r['query'][:33]
        ov = r.get('overlap', 0)
        total_overlap += ov
        
        v_is_gaokao = 'gaokao' in v_top_key
        r_is_gaokao = 'gaokao' in r_top_key
        if v_top_key != r_top_key:
            changed_first += 1
            if v_is_gaokao and not r_is_gaokao:
                subj_changes['textbook取代gaokao'] += 1
            elif not v_is_gaokao and r_is_gaokao:
                subj_changes['gaokao取代textbook'] += 1
            elif v_is_gaokao and r_is_gaokao:
                subj_changes['gaokao→gaokao(不同)'] += 1
            else:
                subj_changes['textbook→textbook(不同)'] += 1
        
        print(f'{q_short:<35} {f"{ov}/5":<10} {v_top_key[:25]:<25} {r_top_key[:25]:<25}')
    
    print('-'*95)
    print(f'Average overlap: {total_overlap/len(report):.1f}/5')
    print(f'First result changed: {changed_first}/{len(report)}')
    print(f'\nSource change analysis:')
    for k, v in sorted(subj_changes.items(), key=lambda x: -x[1]):
        print(f'  {k}: {v}')
    print(f'\nAverage rerank time: {sum(r["rerank_time_s"] for r in report if r["rerank_time_s"])/len(report):.2f}s')

if __name__ == '__main__':
    import asyncio
    asyncio.run(run_tests())
