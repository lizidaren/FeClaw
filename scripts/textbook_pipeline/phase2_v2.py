#!/usr/bin/env python3
'''Phase 2: L2 Pro section-level chunking. Reads L1 JSONs from chunk_results_v2/,
calls V4 Pro for structural subdivision, saves to chunk_processed/ as JSONL.'''
import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from dotenv import load_dotenv; load_dotenv(os.path.join(os.path.dirname(__file__), '..', '..', '.env'))
import asyncio, json, zipfile, hashlib, time
import httpx

DS_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
DS_API = 'https://api.deepseek.com/chat/completions'
MAX_W = 32
MINERU_DIR = '/home/lch/data/textbooks/mineru_results'
CHUNK_DIR = '/home/lch/data/textbooks/chunk_results_v2'
PROC_DIR = '/home/lch/data/textbooks/chunk_processed'
os.makedirs(PROC_DIR, exist_ok=True)

# Raw API response checkpoint dir
RAW_DIR = '/home/lch/data/textbooks/raw_responses'

def save_path(tag, safe_id):
    p = os.path.join(RAW_DIR, tag, f'{safe_id}.json')
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p

DEEP_SUBJECTS = {'chemistry', 'math_rja', 'math_xj', 'physics', 'biology'}
SUBJECT_MAP = {'化学':'chemistry','数学-人教A版':'math_rja','数学-湘教版':'math_xj','物理':'physics','生物':'biology','英语':'english','语文':'chinese','地理':'geography','政治':'politics'}

L2_PROMPT = '你是一个教材章节结构分析专家。任务：将教材一章的内容精确切分为节(section)级别。每行有 L00001: 行号标记。注意科目差异：- 语文：节 = 课（每课含1或多篇课文），学习提示附在课内，单元导语和单元学习任务各自独立 - 化学/生物/数学/物理：节 = 1.1、1.2 等自然节 - 语文的整本书阅读/项目探究单元：不切，type=whole_unit。要求：节末练习题从范围排除。节内蓝色粗体子标题保留，标记 type="subsection"。输出JSON：{"sections": [{"id": "1.1", "title": "节标题", "line_start": 15, "line_end": 50, "type": "normal"}]}'
L3_PROMPT = '你是一个教材知识原子化专家。任务：将教材一节内容精确切分为原子级知识点。每行有 L00001: 行号标记。注意：- 化学/物理/数学/生物 常在一节内有 一、xxx / 二、xxx 的蓝色粗体子标题 - 每个这样的子标题是一个独立的知识点块 - 没有子标题的连续知识文本作为1个知识点 - 习题从范围排除。输出JSON：{"knowledge_points": [{"title": "一、氧化还原反应", "line_start": 10, "line_end": 35}]}'
SUMMARY_PROMPT = '你是一位教材内容精炼专家。为以下教材章节生成知识点摘要。保留所有核心知识点、定义、公式、定理、性质、典型例题思路框架。去冗余。数学公式保留LaTeX。图表用文字简要描述。输出纯文本。'

def extract_book_short(d):
    import re
    m = re.search(r'(必修[上下册一二三四五六七八九十\d]+)', d)
    return m.group(1) if m else d[:10]

def make_ref_id(subj, bs, ch, sec=0, kp=0):
    parts = [subj, bs, str(ch)]
    if sec: parts.append(str(sec))
    if kp: parts.append(str(kp))
    return '::'.join(parts)

def make_key(ref_id):
    return hashlib.md5(ref_id.encode()).hexdigest()

def read_md(zp):
    with zipfile.ZipFile(zp) as z:
        return z.read('full.md').decode('utf-8')

def add_numbers(text):
    lines = text.split('\n')
    return '\n'.join(f'L{i+1:05d}:{line}' for i, line in enumerate(lines)), lines

def extract_json(text):
    js_s = text.find('```json')
    if js_s >= 0:
        js_s = text.find('\n', js_s) + 1
        js_e = text.rfind('```')
    else:
        js_s = text.find('{')
        js_e = text.rfind('}') + 1
    return json.loads(text[js_s:js_e].strip())

def is_real_chapter(ch):
    if ch.get('skip') or ch.get('type') == 'appendix': return False
    title = ch.get('title', '')
    skip_kw = ['序言','绪言','前言','封面','目录','致同学']
    if any(k in title for k in skip_kw): return False
    if ch.get('line_end', 0) - ch.get('line_start', 0) < 50: return False
    return True

async def call_ds(model, prompt, user_content, thinking=True, timeout=310, raw_output=False, save_to=None):
    """Call DeepSeek API.
    raw_output=True: return raw text (no JSON parsing).
    save_to: path to save full raw response JSON (disk checkpoint).
    """
    payload = {'model': model,
        'messages': [{'role': 'system', 'content': prompt}, {'role': 'user', 'content': user_content}],
        'max_tokens': 16384, 'temperature': 0.2}
    if thinking:
        payload['thinking'] = {'type': 'enabled'}
    for attempt in range(1, 4):
        try:
            t0 = time.time()
            async with httpx.AsyncClient(timeout=timeout) as cli:
                r = await cli.post(DS_API, json=payload,
                    headers={'Authorization': f'Bearer {DS_KEY}', 'Content-Type': 'application/json'})
            el = time.time() - t0
            if r.status_code == 429:
                wait = 15 * attempt
                print(f'  [RATE_LIMIT] attempt {attempt}/3, waiting {wait}s')
                await asyncio.sleep(wait)
                continue
            if r.status_code != 200:
                print(f'  [HTTP {r.status_code}] attempt {attempt}/3')
                if attempt < 3:
                    await asyncio.sleep(5 * attempt)
                continue
            resp_json = r.json()
            txt = resp_json['choices'][0]['message']['content']
            if save_to:
                _dir = os.path.dirname(save_to)
                if _dir:
                    os.makedirs(_dir, exist_ok=True)
                with open(save_to, 'w', encoding='utf-8') as _f:
                    json.dump(resp_json, _f, ensure_ascii=False)
            if raw_output:
                return txt, el
            return extract_json(txt), el
        except Exception as e:
            if attempt < 3:
                await asyncio.sleep(5 * attempt)
    return None, 0

async def save_original_to_cos(text, ref_id):
    try:
        from config import settings
        from qcloud_cos import CosConfig, CosS3Client
        config = CosConfig(Region=settings.TENCENT_COS_REGION, SecretId=settings.TENCENT_COS_SECRET_ID, SecretKey=settings.TENCENT_COS_SECRET_KEY)
        cli = CosS3Client(config)
        cos_key = (settings.TENCENT_COS_PREFIX or '') + f'textbook_originals/{ref_id}.txt'
        cli.put_object(Bucket=settings.TENCENT_COS_BUCKET, Key=cos_key, Body=text.encode('utf-8'))
        return cos_key
    except Exception as e:
        print(f'WARN: COS save {ref_id}: {e}')
        return None

# ===== Phase 2: L2 Pro =====
async def phase2():
    print(f'Phase 2: L2 Pro section chunking')
    sem = asyncio.Semaphore(MAX_W)
    total_chunks = 0
    
    async def process_book(fn):
        nonlocal total_chunks
        with open(os.path.join(CHUNK_DIR, fn), encoding='utf-8') as f:
            l1 = json.load(f)
        meta = l1.get('_meta', {})
        safe = fn.replace('.json', '')
        out = os.path.join(PROC_DIR, f'{safe}.jsonl')
        if os.path.exists(out) and os.path.getsize(out) > 100:
            with open(out) as f:
                for line in f:
                    if line.strip(): total_chunks += 1
            print(f'  [SKIP] {safe}')
            return
        
        md = read_md(meta['zip_path'])
        lines = md.split('\n')
        bs = extract_book_short(meta.get('book_dir', ''))
        subj = meta.get('subject', '')
        new_chunks = []
        ch_idx = 0
        
        for ch in l1.get('chapters', []):
            if not is_real_chapter(ch):
                continue
            ch_idx += 1
            ct = ch.get('title', '')
            cs, ce = ch.get('line_start', 0), ch.get('line_end', 0)
            text = '\n'.join(lines[cs-1:ce])
            numbered, _ = add_numbers(text)
            
            # L2 Pro
            l2_res, el = await call_ds('deepseek-v4-pro', L2_PROMPT, numbered, timeout=180)
            secs = l2_res.get('sections', []) if l2_res else ch.get('sections', [])
            sec_idx = 0
            for s in secs:
                sec_idx += 1
                ss = cs + s.get('line_start', 0) - 1
                se = cs + s.get('line_end', 0) - 1
                st = '\n'.join(lines[ss-1:se])
                ref = make_ref_id(subj, bs, ch_idx, sec_idx)
                new_chunks.append({
                    'key': make_key(ref), 'ref_id': ref, 'subject': subj,
                    'book': meta.get('book_dir', ''), 'chapter': ct,
                    'section': s.get('id', ''), 'section_title': s.get('title', ''),
                    'chunk_type': 'section', 'sec_type': s.get('type', 'normal'),
                    'raw_text': st, 'raw_len': len(st),
                    'needs_L3': subj in DEEP_SUBJECTS and s.get('type') == 'normal'
                })
            print(f'  [P2] {safe} - {ch_idx}/{len(l1.get("chapters",[]))} ch: {ct[:30]} -> {len(secs)} sec, {el:.0f}s')
        
        with open(out, 'w', encoding='utf-8') as f:
            for c in new_chunks:
                f.write(json.dumps(c, ensure_ascii=False) + '\n')
        total_chunks += len(new_chunks)
        print(f'  [P2 OK] {safe}: {len(new_chunks)} chunks')
    
    tasks = []
    for fn in sorted(os.listdir(CHUNK_DIR)):
        if fn.endswith('.json'):
            tasks.append(process_book(fn))
    await asyncio.gather(*tasks)
    print(f'Phase 2 done: {total_chunks} total chunks')
    return total_chunks

# ===== Phase 3: L3 Pro =====
async def phase3():
    print('Phase 3: L3 Pro knowledge point chunking')
    sem = asyncio.Semaphore(MAX_W)
    all_chunks = []
    for fn in sorted(os.listdir(PROC_DIR)):
        if fn.endswith('.jsonl') and '_l3' not in fn and '_ready' not in fn:
            with open(os.path.join(PROC_DIR, fn)) as f:
                for line in f:
                    if line.strip(): all_chunks.append(json.loads(line))
    
    targets = [c for c in all_chunks if c.get('needs_L3')]
    if not targets:
        print('  No targets for L3')
        return all_chunks
    print(f'  {len(targets)} sections need L3')
    
    async def process_one(idx, chunk):
        async with sem:
            text = chunk.get('raw_text', '')
            if len(text) < 100:
                chunk['needs_L3'] = False
                chunk['content'] = text
                return [chunk]
            numbered, _ = add_numbers(text)
            l3_res, el = await call_ds('deepseek-v4-pro', L3_PROMPT, numbered, timeout=180, save_to=save_path('l3', chunk['ref_id'].replace('::', '_')))
            kps = l3_res.get('knowledge_points', []) if l3_res else []
            if not kps:
                chunk['needs_L3'] = False
                chunk['content'] = text
                return [chunk]
            ref = chunk['ref_id']
            new = []
            for ki, kp in enumerate(kps):
                ks = kp.get('line_start', 1)
                ke = kp.get('line_end', len(text.split('\n')))
                kt = '\n'.join(text.split('\n')[ks-1:ke])
                kref = f'{ref}::{ki+1}'
                new.append({
                    'key': make_key(kref), 'ref_id': kref, 'subject': chunk['subject'],
                    'book': chunk['book'], 'chapter': chunk['chapter'],
                    'section': chunk['section'], 'section_title': chunk['section_title'],
                    'chunk_type': 'knowledge_point', 'kp_title': kp.get('title', ''),
                    'raw_text': kt, 'raw_len': len(kt), 'needs_L3': False
                })
            print(f'  [P3] {ref}: {len(kps)} KPs, {el:.0f}s')
            return new
    
    # Build final BEFORE processing (targets still have needs_L3=True, excluded from final)
    final = [c for c in all_chunks if not c.get('needs_L3')]
    
    results = await asyncio.gather(*[process_one(i, c) for i, c in enumerate(targets)], return_exceptions=True)
    
    l3_ok = 0
    l3_err = 0
    for r in results:
        if isinstance(r, Exception):
            l3_err += 1
            print(f'  [P3 ERR] {r}')
            continue
        final.extend(r)
        l3_ok += 1
    print(f'Phase 3 done: {len(final)} chunks (L3: {l3_ok} ok, {l3_err} err, from {len(all_chunks)})')
    # Persist to disk for checkpoint continuation
    ready_path = os.path.join(PROC_DIR, '_ready.jsonl')
    with open(ready_path, 'w', encoding='utf-8') as f:
        for c in final:
            f.write(json.dumps(c, ensure_ascii=False) + '\n')
    print(f'Phase 3 checkpoint: {len(final)} chunks saved to _ready.jsonl')
    return final

# ===== Phase 4: Summarize + COS =====
async def phase4(chunks):
    print(f'Phase 4: Summarize large + COS save ({len(chunks)} chunks)')
    large = [c for c in chunks if c.get('raw_len', 0) > 2000 and not c.get('content') and c.get('chunk_type') not in ('appendix', 'whole_unit')]
    
    if large:
        print(f'  Pro summary: {len(large)} large chunks')
        sem = asyncio.Semaphore(MAX_W)
        async def summarize_one(chunk):
            async with sem:
                text = chunk.get('raw_text', '')
                if not text:
                    chunk['content'] = ''
                    return
                ref_safe = chunk.get('ref_id', chunk.get('key', 'unknown')).replace('::', '_')
                summary, _ = await call_ds('deepseek-v4-pro', SUMMARY_PROMPT,
                    f'\u8bf7\u4e3a\u4ee5\u4e0b\u6559\u6750\u7ae0\u8282\u751f\u6210\u77e5\u8bc6\u70b9\u6458\u8981\uff1a\n\n{text}',
                    timeout=180, raw_output=True, save_to=save_path('summary', ref_safe))
                if summary and isinstance(summary, str):
                    chunk['content'] = summary[:8000]
                else:
                    chunk['content'] = text[:8000]
        for i in range(0, len(large), MAX_W):
            batch_results = await asyncio.gather(*[summarize_one(c) for c in large[i:i+MAX_W]], return_exceptions=True)
            errs = sum(1 for r in batch_results if isinstance(r, Exception))
            if errs:
                print(f'    [WARN] {errs} summary errors in batch {i//MAX_W+1}')
            print(f'    {min(i+MAX_W, len(large))}/{len(large)}')
    
    # COS save (skipped for speed — run separately if needed)
    cos_done = 0
    print(f'  COS save skipped ({cos_done} saved)')
    
    # Finalize: remove raw_text, ensure content exists
    for c in chunks:
        if not c.get('content'):
            c['content'] = c.get('raw_text', '')[:8000]
    return chunks

# ===== Phase 5: Embed + Index =====
async def phase5(chunks):
    from services.vector_search_service import VectorSearchService
    vs = VectorSearchService()
    
    by_subject = {}
    for c in chunks:
        if not c.get('content'): continue
        by_subject.setdefault(c['subject'], []).append(c)
    
    for subject, c_list in sorted(by_subject.items()):
        idx = f'idx-public-{subject.replace("_", "-")}-textbook'
        print(f'[{subject}] -> {idx} ({len(c_list)} chunks)')
        items = []
        for c in c_list:
            pref = f'# [{c["book"]}] {c["chapter"]}'
            if c.get('section'):
                pref += f' -- {c["section"]} {c.get("section_title","")}'
            items.append({
                'key': c['key'],
                'text': f'{pref}\n\n{c["content"]}',
                'metadata': {
                    'source': 'textbook', 'subject': subject,
                    'book': c['book'], 'chapter': c['chapter'],
                    'section': c.get('section', ''), 'section_title': c.get('section_title', ''),
                    'chunk_type': c.get('chunk_type', 'section'),
                    'ref_id': c.get('ref_id', ''),
                    'original_cos_key': c.get('original_cos_key', '')
                }
            })
        for i in range(0, len(items), 10):
            try:
                await vs.index_batch(items[i:i+10], idx)
            except Exception as e:
                print(f'  FAIL batch {i//10+1}: {e}')
            await asyncio.sleep(0.3)
        print(f'  {len(items)} indexed')
    print('Phase 5 done')

# ===== Test =====
async def test():
    from services.vector_search_service import VectorSearchService
    vs = VectorSearchService()
    for q in ['复数的三角形式', '复数的几何意义', '复数概念', '氧化还原反应']:
        print(f'  --- "{q}" ---')
        rs = await vs.search(q, index='idx-public-math-xj-textbook', top_k=3)
        for r in rs:
            m = r.get('metadata', {})
            print(f'  [{r["score"]:.3f}] {m.get("book","")} -> {m.get("chapter","")} -> {m.get("section","")}')
            print(f'    {r.get("text","")[:60]}')

# ===== Main =====
async def main():
    import time
    t0 = time.time()
    
    # Checkpoint: if _ready.jsonl exists, skip Phase 2+3
    ready_path = os.path.join(PROC_DIR, '_ready.jsonl')
    if os.path.exists(ready_path):
        chunks = []
        with open(ready_path) as f:
            for line in f:
                if line.strip():
                    chunks.append(json.loads(line))
        print(f'Loaded {len(chunks)} chunks from checkpoint, skipping Phase 2+3')
    else:
        print('='*60)
        print('Phase 2: L2 Pro section chunking')
        print('='*60)
        total = await phase2()
        
        print()
        print('='*60)
        print('Phase 3: L3 Pro knowledge points')
        print('='*60)
        chunks = await phase3()
    
    print()
    print('='*60)
    print('Phase 4: Summarize + COS')
    print('='*60)
    chunks = await phase4(chunks)
    
    print()
    print('='*60)
    print('Phase 5: Embed + Index')
    print('='*60)
    await phase5(chunks)
    
    print()
    print('='*60)
    print('Test')
    print('='*60)
    await test()
    
    print(f'\nTotal: {(time.time()-t0)/60:.1f} min')

if __name__ == '__main__':
    asyncio.run(main())
