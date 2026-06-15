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

QUERY_PAIRS = [
    ('课本例题：二次函数顶点坐标公式', '高考真题：二次函数顶点坐标公式'),
    ('课本：光合作用的过程和化学方程式', '高考真题：光合作用的化学方程式'),
    ('课本：辛亥革命的历史背景', '高考真题：辛亥革命的时间和意义'),
    ('课本习题：牛顿第二定律的应用', '高考真题：牛顿第二定律的考查'),
    ('课本：氧化还原反应的本质和判断', '高考真题：氧化还原反应的题目'),
    ('课本：细胞有丝分裂各时期特征', '高考真题：细胞有丝分裂过程'),
    ('课本：English reading comprehension skills', '高考真题：英语阅读理解主旨大意题'),
    ('课本内容：地球自转的地理意义', '高考真题：地球自转的地理意义考查'),
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
    return []

async def run_tests():
    vs = VectorSearchService()
    report = []
    
    for i, (q_textbook, q_gaokao) in enumerate(QUERY_PAIRS):
        topic = q_textbook.split('：',1)[1] if '：' in q_textbook else q_textbook
        print(f'\n{"="*70}')
        print(f'[Pair {i+1}/8] {topic}')
        print('='*70)
        
        row = {'topic': topic}
        
        for label, query in [('textbook', q_textbook), ('gaokao', q_gaokao)]:
            vec_top5 = await vs.search(query, index='idx-public-kb', top_k=5)
            vec_keys = [r.get('key','') for r in vec_top5]
            vec_scores = [r.get('score',0) for r in vec_top5]
            v_gk = sum(1 for k in vec_keys if 'gaokao' in k)
            v_tb = sum(1 for k in vec_keys if 'textbooks' in k)
            
            vec_top50 = await vs.search(query, index='idx-public-kb', top_k=50)
            docs = [{'text': r.get('metadata',{}).get('text',''), 'key': r.get('key','')} for r in vec_top50]
            rr = rerank(query, docs, top_n=5) if len(vec_top50) >= 2 else []
            rk_keys = [docs[r['index']]['key'] for r in rr] if rr else []
            rk_scores = [r['relevance_score'] for r in rr] if rr else []
            r_gk = sum(1 for k in rk_keys if 'gaokao' in k)
            r_tb = sum(1 for k in rk_keys if 'textbooks' in k)
            overlap = len(set(vec_keys) & set(rk_keys))
            
            row[f'vec_gk_{label}'] = v_gk
            row[f'vec_tb_{label}'] = v_tb
            row[f'rerank_gk_{label}'] = r_gk
            row[f'rerank_tb_{label}'] = r_tb
            row[f'overlap_{label}'] = overlap
            
            print(f'  [{label}] query={query[:40]}')
            print(f'    Vector: {v_gk} gaokao + {v_tb} textbook = {v_gk+v_tb}/5  |  Rerank: {r_gk} gaokao + {r_tb} textbook = {r_gk+r_tb}/5')
            print(f'    Overlap: {overlap}/5')
            
            # Print individual results
            for j in range(5):
                vk = vec_keys[j][:50] if j < len(vec_keys) else ''
                vs_ = f'{vec_scores[j]:.4f}' if j < len(vec_scores) else ''
                rk = rk_keys[j][:50] if j < len(rk_keys) else ''
                rs_ = f'{rk_scores[j]:.4f}' if j < len(rk_scores) else ''
                v_tag = 'G' if 'gaokao' in vk else 'T' if 'textbooks' in vk else '?'
                r_tag = 'G' if 'gaokao' in rk else 'T' if 'textbooks' in rk else '?'
                print(f'    V{j+1}: [{v_tag}] {vs_:<7} {vk}')
                print(f'    R{j+1}: [{r_tag}] {rs_:<7} {rk}')
        
        report.append(row)
    
    # Print summary
    print(f'\n\n{"="*70}')
    print('FINAL SUMMARY: 课本前缀 vs 高考前缀')
    print('='*70)
    print(f'{"Topic":<35} {"课本前缀-Vec":<14} {"课本前缀-Rerank":<14} {"高考前缀-Vec":<14} {"高考前缀-Rerank":<14}')
    print(f'{"":<35} {"G":>4} {"T":>4} {"G":>4} {"T":>4} {"G":>4} {"T":>4} {"G":>4} {"T":>4}')
    print('-'*90)
    
    for r in report:
        t = r['topic'][:33]
        print(f'{t:<35} {r["vec_gk_textbook"]:>4} {r["vec_tb_textbook"]:>4} {r["rerank_gk_textbook"]:>4} {r["rerank_tb_textbook"]:>4} {r["vec_gk_gaokao"]:>4} {r["vec_tb_gaokao"]:>4} {r["rerank_gk_gaokao"]:>4} {r["rerank_tb_gaokao"]:>4}')
    
    # Bottom line
    print('-'*90)
    print('G = gaokao results, T = textbook results')
    print()
    
    # Key insights
    print('Key observations:')
    imp_vec = sum(1 for r in report if r['vec_gk_textbook'] < r['vec_gk_gaokao'] and r['vec_tb_textbook'] > r['vec_tb_gaokao'])
    imp_rr = sum(1 for r in report if r['rerank_gk_textbook'] < r['rerank_gk_gaokao'] and r['rerank_tb_textbook'] > r['rerank_tb_gaokao'])
    print(f'  Vector search correctly responded to prefix: {imp_vec}/8 pairs')
    print(f'  Rerank correctly responded to prefix: {imp_rr}/8 pairs')

if __name__ == '__main__':
    import asyncio
    asyncio.run(run_tests())
