#!/usr/bin/env python3
"""
教材重新切块 Pipeline v2（可靠性优先）

用法:
  python3 pipeline_rechunk_v2.py               # 跑全流程
  python3 pipeline_rechunk_v2.py --phase 1     # 只跑 Phase 1
  python3 pipeline_rechunk_v2.py --phase 2     # 只跑 Phase 2
  python3 pipeline_rechunk_v2.py --phase 3     # 只跑 Phase 3

Phase 1: DeepSeek V4 Flash 语义切分（排除习题）→ chunk_results_v2/
Phase 2: 文本提取 + 大块 Pro 总结 → chunk_processed/{book}.jsonl
Phase 3: Embedding + 分科入库 → idx-public-{subject}-textbook
"""
import asyncio, json, os, re, sys, time, zipfile, uuid, argparse
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from dotenv import load_dotenv
load_dotenv()
import httpx

DS_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
DS_API = 'https://api.deepseek.com/chat/completions'
MINERU_DIR = '/home/lch/data/textbooks/mineru_results'
CHUNK_DIR = '/home/lch/data/textbooks/chunk_results_v2'
PROC_DIR = '/home/lch/data/textbooks/chunk_processed'
MAX_W = 8
LARGE_THRESH = 2000
os.makedirs(CHUNK_DIR, exist_ok=True)
os.makedirs(PROC_DIR, exist_ok=True)

SUBJECT_MAP = {
    '化学': 'chemistry', '数学-人教A版': 'math_rja', '数学-湘教版': 'math_xj',
    '物理': 'physics', '生物': 'biology', '英语': 'english',
    '语文': 'chinese', '地理': 'geography', '政治': 'politics',
}

STRUCTURE_PROMPT = '''你是一个专业的教材结构分析专家。

任务：对 MinerU 输出的教材 Markdown 文本进行精确的逐行语义切块。

## 输入格式
每行前面有行号标记 `L00001:` 格式。

## 常见问题（请务必修正）
1. 节编号与节名拆成两行，应合并
2. OCR 伪影字符：如 ￥ 等
3. OCR 错字：如 儿->几、狗量->向量
4. 标题级别不准确：请根据语义判断
5. 封面/前言/目录等非正文内容标记为 skip=true

## 重要：习题排除
- 节末的"练习与应用""课后练习"等习题内容不属知识点
- 请从节的行号范围中排除（line_end 应在习题开始前）
- 各章的"整理与提升"是复习总结，属知识点，应保留

## 输出格式
```json
{
  "chapters": [
    {
      "title": "第1章 标题",
      "line_start": 1,
      "line_end": 100,
      "skip": false,
      "sections": [
        {"id": "1.1", "title": "节标题", "line_start": 15, "line_end": 50}
      ]
    }
  ]
}
```
行号范围必须精确对应原文行号。'''

SUMMARY_PROMPT = '''你是一位教材内容精炼专家。请为以下教材章节生成一个精炼的知识点摘要。

要求：
- 保留所有核心知识点、定义、公式、定理、性质
- 保留典型例题的解题思路框架（如果有）
- 去除冗余的重复叙述和过多例题
- 语言精炼、结构清晰
- 数学公式保留 LaTeX 格式
- 图表说明用文字简要描述
- 输出纯文本（不要 markdown 包装）'''


def discover_textbooks():
    books = []
    for subj_dir in sorted(os.listdir(MINERU_DIR)):
        spath = os.path.join(MINERU_DIR, subj_dir)
        if not os.path.isdir(spath):
            continue
        subject = SUBJECT_MAP.get(subj_dir)
        if not subject:
            continue
        for book_dir in sorted(os.listdir(spath)):
            zip_path = os.path.join(spath, book_dir, 'result.zip')
            if os.path.exists(zip_path):
                books.append({
                    'zip_path': zip_path, 'book_key': f'{subj_dir}/{book_dir}',
                    'subject': subject, 'subject_cn': subj_dir, 'book_dir': book_dir,
                })
    return books


def read_md(zip_path):
    with zipfile.ZipFile(zip_path) as z:
        return z.read('full.md').decode('utf-8')


# ==================== Phase 1 ====================

async def phase1():
    print('\n=== Phase 1: DeepSeek V4 Flash 语义切分 ===')
    books = discover_textbooks()
    print(f'发现 {len(books)} 本教材')
    sem = asyncio.Semaphore(MAX_W)

    async def chunk_one(book):
        safe_key = book['book_key'].replace('/', '_')
        out = os.path.join(CHUNK_DIR, f'{safe_key}.json')
        if os.path.exists(out):
            with open(out) as f:
                return json.load(f)
        async with sem:
            for attempt in range(1, 4):
                try:
                    md = read_md(book['zip_path'])
                    lines = md.split('\n')
                    numbered = '\n'.join(f'L{i+1:05d}:{line}' for i, line in enumerate(lines))
                    if len(numbered) > 900_000:
                        print(f'  [SKIP] {book["book_key"]}: >900K chars')
                        return None
                    t0 = time.time()
                    async with httpx.AsyncClient(timeout=310) as cli:
                        r = await cli.post(DS_API, json={
                            'model': 'deepseek-v4-flash',
                            'thinking': {'type': 'enabled'},
                            'messages': [
                                {'role': 'system', 'content': STRUCTURE_PROMPT},
                                {'role': 'user', 'content': numbered}
                            ],
                            'max_tokens': 16384, 'temperature': 0.2,
                        }, headers={'Authorization': f'Bearer {DS_KEY}', 'Content-Type': 'application/json'})
                    result = r.json()['choices'][0]['message']['content']
                    elap = time.time() - t0
                    js_s = result.find('```json')
                    if js_s >= 0:
                        js_s = result.find('\n', js_s) + 1
                        js_e = result.rfind('```')
                    else:
                        js_s = result.find('{')
                        js_e = result.rfind('}') + 1
                    parsed = json.loads(result[js_s:js_e].strip())
                    parsed['_meta'] = {
                        'book': book['book_key'], 'subject': book['subject'],
                        'subject_cn': book['subject_cn'], 'book_dir': book['book_dir'],
                        'zip_path': book['zip_path'], 'chars': len(md),
                        'lines': len(lines), 'elapsed_s': round(elap, 1),
                    }
                    with open(out, 'w', encoding='utf-8') as f:
                        json.dump(parsed, f, ensure_ascii=False, indent=2)
                    sec_cnt = sum(len(ch.get('sections', [])) for ch in parsed.get('chapters', []))
                    print(f'  [P1] {book["book_key"]}: {elap:.0f}s {sec_cnt}sec')
                    return parsed
                except Exception as e:
                    if attempt < 3:
                        await asyncio.sleep(5 * attempt)
            print(f'  [P1 FAIL] {book["book_key"]}')
            return None

    tasks = [chunk_one(b) for b in books]
    results = await asyncio.gather(*tasks)
    ok = sum(1 for r in results if r)
    fail = sum(1 for r in results if r is None)
    secs = sum(sum(len(ch.get('sections', [])) for ch in (r.get('chapters', []) if r else [])) for r in results if r)
    print(f'Phase 1 完成: {ok} OK, {fail} FAIL, {secs} 节')
    return results


# ==================== Phase 2 ====================

async def phase2(chunk_results=None):
    print('\n=== Phase 2: 文本提取 + 大块 Pro 总结 ===')
    if chunk_results is None:
        # 从磁盘读取
        chunk_results = []
        for fn in sorted(os.listdir(CHUNK_DIR)):
            if fn.endswith('.json') and fn != '_summary.json':
                with open(os.path.join(CHUNK_DIR, fn)) as f:
                    chunk_results.append(json.load(f))
        print(f'从磁盘读取 {len(chunk_results)} 个切分结果')

    # 第一步：提取所有 chunks，保存 raw_text (快，无API调用)
    all_chunks = []
    for cr in chunk_results:
        if cr is None:
            continue
        meta = cr['_meta']
        md = read_md(meta['zip_path'])
        lines = md.split('\n')
        safe_key = meta['book'].replace('/', '_')
        out_path = os.path.join(PROC_DIR, f'{safe_key}.jsonl')
        if os.path.exists(out_path):
            with open(out_path) as f:
                for line in f:
                    if line.strip():
                        all_chunks.append(json.loads(line))
            continue

        new_chunks = []
        for ch in cr.get('chapters', []):
            if ch.get('skip'):
                continue
            secs = ch.get('sections', [])
            if not secs:
                text = '\n'.join(lines[ch['line_start'] - 1:ch['line_end']])
                new_chunks.append({
                    'key': str(uuid.uuid4()), 'book_key': meta['book'],
                    'subject': meta['subject'], 'book': meta['book_dir'],
                    'chapter': ch.get('title', ''), 'section': '', 'section_title': '',
                    'needs_summary': len(text) > LARGE_THRESH,
                    'chunk_type': 'raw' if len(text) <= LARGE_THRESH else None,
                    'raw_text': text, 'content': text if len(text) <= LARGE_THRESH else '',
                })
            else:
                for sec in secs:
                    s, e = sec.get('line_start', 0), sec.get('line_end', 0)
                    if s <= 0 or e <= s:
                        continue
                    text = '\n'.join(lines[s - 1:e])
                    new_chunks.append({
                        'key': str(uuid.uuid4()), 'book_key': meta['book'],
                        'subject': meta['subject'], 'book': meta['book_dir'],
                        'chapter': ch.get('title', ''), 'section': sec.get('id', ''),
                        'section_title': sec.get('title', ''),
                        'needs_summary': len(text) > LARGE_THRESH,
                        'raw_text': text, 'content': text if len(text) <= LARGE_THRESH else '',
                    })
        # 保存到 JSONL（含 raw_text，Pro 阶段会读取）
        with open(out_path, 'w', encoding='utf-8') as f:
            for c in new_chunks:
                f.write(json.dumps(c, ensure_ascii=False) + '\n')
        all_chunks.extend(new_chunks)
        lc = sum(1 for c in new_chunks if c['needs_summary'])
        print(f'  [P2] {meta["book"]}: {len(new_chunks)}chunks ({lc}large)')

    # 第二步：大块 Pro 总结（直接读 raw_text）
    needs_pro = [c for c in all_chunks if c['needs_summary'] and not c['content']]
    if needs_pro:
        print(f'\nPro 总结 ({len(needs_pro)} 个大块)...')
        sem_pro = asyncio.Semaphore(MAX_W)

        async def get_summary(chunk):
            async with sem_pro:
                for attempt in range(1, 4):
                    try:
                        t0 = time.time()
                        async with httpx.AsyncClient(timeout=190) as cli:
                            r = await cli.post(DS_API, json={
                                'model': 'deepseek-v4-pro',
                                'thinking': {'type': 'enabled'},
                                'messages': [
                                    {'role': 'system', 'content': SUMMARY_PROMPT},
                                    {'role': 'user', 'content': f'请为以下教材章节生成知识点摘要：\n\n{chunk["raw_text"]}'}
                                ],
                                'max_tokens': 4096, 'temperature': 0.3,
                            }, headers={'Authorization': f'Bearer {DS_KEY}', 'Content-Type': 'application/json'})
                            return chunk['key'], r.json()['choices'][0]['message']['content']
                    except Exception as e:
                        if attempt < 3:
                            await asyncio.sleep(5 * attempt)
                return chunk['key'], chunk['raw_text'][:2000] + '...'

        done = 0
        for i in range(0, len(needs_pro), MAX_W):
            batch = needs_pro[i:i + MAX_W]
            results = await asyncio.gather(*[get_summary(c) for c in batch])
            for key, summary in results:
                for c in all_chunks:
                    if c['key'] == key:
                        c['content'] = summary
                        c['needs_summary'] = False
                        c['chunk_type'] = 'summary'
                        break
            done += len(batch)
            # 每批完成后写回磁盘
            by_file = {}
            for c in all_chunks:
                if c.get('book_key'):
                    by_file.setdefault(c['book_key'], []).append(c)
            for bkey, chunks in by_file.items():
                sk = bkey.replace('/', '_')
                fp = os.path.join(PROC_DIR, f'{sk}.jsonl')
                with open(fp, 'w', encoding='utf-8') as f:
                    for c in chunks:
                        out = {k: c[k] for k in ['key', 'book_key', 'subject', 'book', 'chapter', 'section', 'section_title', 'needs_summary', 'chunk_type', 'content'] if k in c}
                        f.write(json.dumps(out, ensure_ascii=False) + '\n')
            print(f'  Pro: {done}/{len(needs_pro)}  (已写盘)')

    # 清理 raw_text 字段（不再需要）
    for c in all_chunks:
        c.pop('raw_text', None)
        c.pop('book_key', None)

    print(f'Phase 2 完成: {len(all_chunks)} chunks')
    return all_chunks


# ==================== Phase 3 ====================

async def phase3():
    print('\n=== Phase 3: Embedding + 分科入库 ===')
    from services.vector_search_service import VectorSearchService
    vs = VectorSearchService()

    # 从磁盘读取所有 processed chunks
    by_subject = {}
    for fn in sorted(os.listdir(PROC_DIR)):
        if not fn.endswith('.jsonl'):
            continue
        with open(os.path.join(PROC_DIR, fn)) as f:
            for line in f:
                if not line.strip():
                    continue
                c = json.loads(line)
                if not c.get('content'):
                    continue
                by_subject.setdefault(c['subject'], []).append(c)

    print(f'共 {sum(len(v) for v in by_subject.values())} chunks')
    for subject, chunks in sorted(by_subject.items()):
        idx_name = f'idx-public-{subject}-textbook'
        print(f'\n[{subject}] → {idx_name} ({len(chunks)} chunks)')
        items = []
        for c in chunks:
            prefix = f'# [{c["book"]}] {c["chapter"]}'
            if c.get('section'):
                prefix += f' — {c["section"]} {c.get("section_title", "")}'
            items.append({
                'key': c['key'],
                'text': f'{prefix}\n\n{c["content"]}',
                'metadata': {
                    'source': 'textbook', 'subject': c['subject'],
                    'book': c['book'], 'chapter': c['chapter'],
                    'section': c.get('section', ''),
                    'section_title': c.get('section_title', ''),
                    'chunk_type': c.get('chunk_type', 'raw'),
                }
            })
        # 分批入库
        for i in range(0, len(items), 20):
            batch = items[i:i + 20]
            try:
                await vs.index_batch(batch, idx_name)
            except Exception as e:
                print(f'    [FAIL] batch {i//20+1}: {e}')
            await asyncio.sleep(0.3)
        print(f'  {len(items)} 条入库完成')
    print('Phase 3 完成')


# ==================== 测试 ====================

async def test():
    print('\n=== 测试：湘教版必修二 复数章节 ===')
    from services.vector_search_service import VectorSearchService
    vs = VectorSearchService()
    for q in ['复数的三角形式', '复数的几何意义', '复数 概念 湘教版']:
        print(f'\n--- "{q}" ---')
        try:
            rs = await vs.search(q, index='idx-public-math-xj-textbook', top_k=3)
            for r in rs:
                m = r.get('metadata', {})
                print(f'  [{r["score"]:.3f}] {m.get("book","")} → {m.get("chapter","")} → {m.get("section","")}')
        except Exception as e:
            print(f'  [ERR] {e}')


# ==================== 入口 ====================

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--phase', type=int, default=0, help='1=only P1, 2=only P2, 3=only P3, 0=all')
    args = parser.parse_args()
    t0 = time.time()

    if args.phase == 0 or args.phase == 1:
        await phase1()
    if args.phase == 0 or args.phase == 2:
        await phase2()
    if args.phase == 0 or args.phase == 3:
        await phase3()

    if args.phase == 0:
        await test()

    print(f'\n总耗时 {(time.time()-t0)/60:.1f} 分钟')

if __name__ == '__main__':
    asyncio.run(main())
