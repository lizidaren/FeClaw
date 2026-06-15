#!/usr/bin/env python3
"""测试 GAOKAO-Bench 向量搜索"""
import asyncio, sys
sys.path.insert(0, '.')
from services.vector_search_service import VectorSearchService

async def test():
    vs = VectorSearchService()
    
    queries = [
        "已知集合 A={x∈R||x|≤2}, B={x∈Z|√x≤4}，求A∩B",  # 数学题
        "中国考古学的发展成就",  # 语文现代文
        "cats behavior animal language",  # 英语阅读
        "skiing in Beijing",  # 英语阅读
        "勾股定理",  # 不相关的，看会不会匹配
    ]
    
    for q in queries:
        print(f"\n{'='*60}")
        print(f"查询: {q}")
        print('='*60)
        results = await vs.search(q, top_k=3)
        if results:
            for r in results:
                text = r.get('metadata', {}).get('text', '')[:200]
                print(f"  [{r['score']:.3f}] {r['key']}")
                print(f"  {text}...")
                print()
        else:
            print("  无结果")

asyncio.run(test())
