#!/usr/bin/env python3
"""
交互式教材/高考向量检索测试脚本
输入查询 → 多源搜索 + Rerank → 显示 Top 5
输入 q 或 exit 退出
"""
import asyncio, sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))
from services.vector_search_service import VectorSearchService


def fmt(s, w=80):
    """截断长文本"""
    s = str(s)
    return s[:w] + '...' if len(s) > w else s


def pprint_results(results, label, show_subj=True):
    print(f'\n{"="*60}')
    print(f'  {label}')
    print(f'{"="*60}')
    if not results:
        print('  （无结果）')
        return
    for i, r in enumerate(results, 1):
        score = r.get('rerank_score', r.get('score', 0))
        text = r.get('text', r.get('metadata', {}).get('text', ''))
        subj = r.get('metadata', {}).get('subject', r.get('subject', '?'))
        book = r.get('metadata', {}).get('book', '')
        typ = r.get('metadata', {}).get('type', '')
        src = r.get('source', '?')
        print(f'\n  #{i}  [score={score:.3f}]  src={src}  subj={subj}')
        if book:
            print(f'      教材: {book}')
        if typ:
            print(f'      类型: {typ}')
        print(f'      {fmt(text, 200)}')


async def main():
    vs = VectorSearchService(agent_hash=None)

    print('\n' + '#'*60)
    print('#  教材/高考 向量检索测试')
    print('#  输入查询后自动搜索 + Rerank')
    print('#  输入 q / exit 退出')
    print('#'*60 + '\n')

    while True:
        try:
            q = input('>>> ').strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not q:
            continue
        if q.lower() in ('q', 'exit', 'quit'):
            break

        # 三种搜索模式
        pub, tb, gk = await asyncio.gather(
            vs.search_public(q, top_k=5),
            vs.search_textbook(q, top_k=3),
            vs.search_gaokao(q, top_k=3),
            return_exceptions=True,
        )

        if isinstance(pub, Exception):
            print(f'  search_public 失败: {pub}')
            pub = []
        else:
            pprint_results(pub, f'【多源融合 · Rerank】 Top 5  —  "{q}"', show_subj=True)

        if isinstance(tb, Exception):
            print(f'  search_textbook 失败: {tb}')
        else:
            pprint_results(tb, f'【教材知识库】 Top 3', show_subj=True)

        if isinstance(gk, Exception):
            print(f'  search_gaokao 失败: {gk}')
        else:
            pprint_results(gk, f'【高考题库】 Top 3', show_subj=True)

        print()


if __name__ == '__main__':
    asyncio.run(main())
