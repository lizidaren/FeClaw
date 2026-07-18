#!/usr/bin/env python3
"""
选必教材知识库向量化脚本（跳过已处理的必修教材）
- 读取所有选必 MinerU 结果 full.md
- 按章节分细节块 + 缩略重构块（导语+LLM摘要）
- Embedding + 上传到 COS idx-public-textbook-kb
"""
import os, sys, json, re, time, logging, asyncio, zipfile, hashlib, httpx, threading
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('textbook_vectorize')

sys.path.insert(0, '/home/lch/Projects/FeClaw')
from dotenv import load_dotenv
load_dotenv('/home/lch/Projects/FeClaw/.env')
from config import settings
from qcloud_cos import CosConfig, CosS3Client

# DeepSeek
DS_KEY = settings.DEEPSEEK_API_KEY
DS_MODEL = 'deepseek-chat'
DS_URL = 'https://api.deepseek.com/v1/chat/completions'

# COS
COS_REGION = settings.TENCENT_COS_REGION
COS_SECRET_ID = settings.TENCENT_COS_SECRET_ID
COS_SECRET_KEY = settings.TENCENT_COS_SECRET_KEY
COS_BUCKET = settings.TENCENT_COS_BUCKET
COS_PREFIX = settings.STORAGE_PREFIX or ''

VECTOR_BUCKET = COS_BUCKET  

SEMAPHORE = asyncio.Semaphore(3)

# Find all result.zips
RESULT_DIR = Path('/home/lch/data/textbooks/mineru_results')

# Chapter/Section heading patterns
CHAPTER_PATTERNS = [
    re.compile(r'#+\s*第[一二三四五六七八九十百]+章'),
    re.compile(r'#+\s*第[1-9]+章'),
    re.compile(r'#+\s*Chapter\s+\d+', re.I),
    re.compile(r'#+\s*Unit\s+\d+', re.I),
]
SECTION_PATTERNS = [
    re.compile(r'#+\s*第[一二三四五六七八九十百]+节'),
    re.compile(r'#+\s*第[1-9]+节'),
    re.compile(r'#+\s*Section\s+\d+', re.I),
    # 数学教材节：1.1 集合的概念
    re.compile(r'#+\s*[0-9]+(\.[0-9]+)+\s+'),
]

# 非内容标题（跳过）
SKIP_HEADINGS = {
    '目录', '人民教育出版社', '人民都育出版社', '后 记', '后记', '名词索引',
    '元素周期表', '附录I', '附录Ⅱ', '附录Ⅲ', '附录IV',
    '附 录', '教材中设置的主要栏目及说明',
    '谨向为本书提供照片的单位和人士致谢',
    '致谢', '编后记', '编委会',
    '部分酸、碱和盐的溶解性表',
    '一些常见元素中英文名称对照表',
    '相对原子质量表',
    '实验室突发事件的应对措施和常见废弃物的处理方法',
    '一些化学品安全使用标识',
    '主编寄语',
    '本册导引',
    '扉页',
    '版权',
    '编者',
    '出版说明',
}

def is_skip_heading(text: str) -> bool:
    t = text.lstrip('#').strip()
    for skip in SKIP_HEADINGS:
        if skip in t:
            return True
    return False

def clean_book_name(book_dir: str) -> str:
    """从目录名提取干净的书名"""
    book = re.sub(r'_part\d+_p\d+-\d+', '', book_dir)
    return book

def is_section_heading(text: str) -> bool:
    """判断是否为一个节的标题"""
    text_stripped = text.lstrip('#').strip()
    for pat in SECTION_PATTERNS:
        if pat.match(text):
            return True
    return False

def is_chapter_heading(text: str) -> bool:
    """判断是否为一个章的标题"""
    text_stripped = text.lstrip('#').strip()
    for pat in CHAPTER_PATTERNS:
        if pat.match(text):
            return True
    return False

def strip_images(text: str) -> str:
    """去图片行"""
    lines = []
    for l in text.split("\n"):
        stripped = l.strip()
        if not stripped or stripped.startswith('![]('):
            continue
        lines.append(l)
    return "\n".join(lines)

def extract_intro(lines: List[str], section_start_idx: int) -> str:
    """提取章节引言（节标题后的第一段有效文字）"""
    intro_lines = []
    for i in range(section_start_idx + 1, min(section_start_idx + 40, len(lines))):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith('![]('):
            continue
        if stripped.startswith('#'):
            break
        if stripped.startswith('<table'):
            break
        intro_lines.append(line)
    intro = "\n".join(intro_lines).strip()
    return intro

def find_result_zips(compulsory_only: bool = False) -> List[Tuple[str, str, str]]:
    """查找所有 result.zip，compulsory_only=False 时排除已处理的 必修 教材"""
    results = []
    for f in sorted(RESULT_DIR.rglob('result.zip')):
        rel = f.relative_to(RESULT_DIR)
        parts = rel.parts
        subject = parts[0]
        book_dir = parts[1]
        book = clean_book_name(book_dir)
        # 排除已经处理过的必修教材（只处理选必）
        if not compulsory_only and '必修' in book_dir:
            continue
        results.append((subject, book, str(f)))
    return results

def parse_sections(md_text: str) -> List[Dict]:
    """解析 full.md，返回章节列表"""
    lines = md_text.split("\n")
    
    # Find all heading lines
    sections = []
    current_section_start = None
    current_section_heading = None
    current_section_level = None
    current_chapter = None
    
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith('#') or len(stripped) < 3:
            continue
        
        level = len(line) - len(line.lstrip('#'))
        text = stripped.lstrip('#').strip()
        
        is_chap = is_chapter_heading(stripped)
        is_sec = is_section_heading(stripped)
        
        if is_chap:
            # 章节标题也作为 section 边界
            if current_section_start is not None:
                sections.append({
                    'chapter': current_chapter,
                    'heading': current_section_heading,
                    'start': current_section_start,
                    'end': i,
                })
            current_chapter = text
            current_section_start = i
            current_section_heading = text
            current_section_level = level
            continue
        
        if is_skip_heading(stripped):
            continue
        
        if is_sec:
            if current_section_start is not None:
                sections.append({
                    'chapter': current_chapter,
                    'heading': current_section_heading,
                    'start': current_section_start,
                    'end': i,
                })
            current_section_start = i
            current_section_heading = text
            current_section_level = level
            continue
        
        # Non-section # headings (like 整理与提升, 复习与提高)
        # Check if it's a top-level heading that should start a new section
        if level == 0 or level == 1:
            if current_section_start is not None:
                sections.append({
                    'chapter': current_chapter,
                    'heading': current_section_heading,
                    'start': current_section_start,
                    'end': i,
                })
            current_section_start = i
            current_section_heading = text
            current_section_level = level
    
    # Last section
    if current_section_start is not None:
        sections.append({
            'chapter': current_chapter,
            'heading': current_section_heading,
            'start': current_section_start,
            'end': len(lines),
        })
    
    return sections

def chunk_section_text(text_lines: List[str], max_chars: int = 2000) -> List[Dict]:
    """把一节内容切成若干个细节块（按子标题）"""
    chunks = []
    current_chunk_lines = []
    current_chunk_heading = None
    
    for line in text_lines:
        stripped = line.strip()
        if stripped.startswith('##') or stripped.startswith('###'):
            # Save current chunk
            if current_chunk_lines:
                text = "\n".join(current_chunk_lines).strip()
                if text and len(text) > 20:  # Skip trivial chunks
                    chunks.append({
                        'heading': current_chunk_heading or 'intro',
                        'text': strip_images(text),
                    })
            current_chunk_lines = [line]
            current_chunk_heading = stripped.lstrip('#').strip()
        else:
            current_chunk_lines.append(line)
    
    # Last chunk
    if current_chunk_lines:
        text = "\n".join(current_chunk_lines).strip()
        if text and len(text) > 20:
            chunks.append({
                'heading': current_chunk_heading or 'intro',
                'text': strip_images(text),
            })
    
    # Further split oversized chunks
    final_chunks = []
    for c in chunks:
        if len(c['text']) > max_chars:
            sub_texts = split_long_text(c['text'], max_chars)
            for i, st in enumerate(sub_texts):
                final_chunks.append({
                    'heading': f"{c['heading']} (续{i+1})" if i > 0 else c['heading'],
                    'text': st,
                })
        else:
            final_chunks.append(c)
    
    return final_chunks

def split_long_text(text: str, max_chars: int) -> List[str]:
    """在段落边界拆分长文本"""
    paragraphs = text.split("\n"*2)
    result = []
    current = []
    current_len = 0
    for p in paragraphs:
        if current_len + len(p) > max_chars and current:
            result.append("\n\n".join(current))
            current = [p]
            current_len = len(p)
        else:
            current.append(p)
            current_len += len(p)
    if current:
        result.append("\n\n".join(current))
    return result

def generate_deepseek_summary(section_text: str, intro_text: str, subject: str, book: str, chapter: str, section: str) -> Optional[str]:
    """调用 DeepSeek 生成章节摘要"""
    prompt = f"""你是一位教材分析专家。请为以下教材章节生成结构化摘要。

教材：{book}
科目：{subject}
章节：{chapter} / {section}

章节完整内容：
{strip_images(section_text)[:6000]}

请以如下格式输出（只输出摘要内容，不要额外解释）：

## 核心知识点
- 知识点1：简要说明
- 知识点2：简要说明
...

## 关键概念
- 概念1：定义
...

## 重要公式/规律
（如有）"""
    
    try:
        resp = httpx.post(DS_URL, headers={
            'Authorization': f'Bearer {DS_KEY}',
            'Content-Type': 'application/json'
        }, json={
            'model': DS_MODEL,
            'messages': [{'role': 'user', 'content': prompt}],
            'max_tokens': 1024,
        }, timeout=60.0)
        if resp.status_code != 200:
            log.error(f'  DS API error: {resp.status_code} {resp.text[:200]}')
            return None
        data = resp.json()
        content = data.get('choices', [{}])[0].get('message', {}).get('content', '')
        return content.strip()
    except Exception as e:
        log.error(f'  DS API exception: {e}')
        return None

def get_cos_file_client():
    config = CosConfig(Region=COS_REGION, SecretId=COS_SECRET_ID, SecretKey=COS_SECRET_KEY)
    return CosS3Client(config)

def get_cos_vector_client():
    config = CosConfig(Region=COS_REGION, SecretId=COS_SECRET_ID, SecretKey=COS_SECRET_KEY)
    return CosS3Client(config)

def upload_to_file_bucket(key: str, data: dict) -> bool:
    """上传完整数据到 COS 文件桶"""
    try:
        client = get_cos_file_client()
        cos_path = f'{COS_PREFIX}public/kb/{key}.json'
        client.put_object(
            Bucket=COS_BUCKET,
            Key=cos_path,
            Body=json.dumps(data, ensure_ascii=False),
            ContentType='application/json',
        )
        return True
    except Exception as e:
        log.error('Failed to upload %s: %s', key, e)
        return False

def safe_key(s: str) -> str:
    """安全的 COS key"""
    return re.sub(r'[\\/:*?"<>|\s]+', '_', s).strip('_')

def process_section(sec: dict, lines: List[str], subject: str, book: str, book_key: str, subject_key: str):
    """处理单个章节（用于线程池并行）"""
    heading = sec['heading']
    chapter = sec['chapter'] or heading
    section_key = safe_key(heading)
    
    section_lines = lines[sec['start']:sec['end']]
    if not section_lines:
        return [], []
    
    intro_text = extract_intro(lines, sec['start'])
    section_text = "\n".join(section_lines)
    clean_text = strip_images(section_text)
    if len(clean_text.strip()) < 50:
        return [], []
    
    detail_chunks = chunk_section_text(section_lines)
    
    summary = generate_deepseek_summary(section_text, intro_text, subject, book, chapter, heading)
    
    items = []
    uploads = []
    chap_key = safe_key(chapter) if chapter else 'intro'
    
    if summary:
        summary_lines = [f'{book}', f'{heading} （摘要）', '', intro_text if intro_text else '(无导语)', '', summary]
        summary_text = "\n".join(summary_lines)
        summary_key = f'textbooks/{subject_key}/{book_key}/{chap_key}_{section_key}_summary'
        meta = {'text': summary_text, 'subject': subject, 'book': book, 'chapter': chapter, 'section': heading, 'type': 'summary', 'source': 'textbook', 'injection_preview': summary_text[:300]}
        items.append({'key': summary_key, 'text': summary_text, 'metadata': meta})
        uploads.append((summary_key, {'text': summary_text, 'metadata': {k:v for k,v in meta.items() if k in ['subject','book','chapter','section','type','source']}}))
    
    for dc_idx, dc in enumerate(detail_chunks):
        dc_text = dc['text'].strip()
        if len(dc_text) < 30:
            continue
        dc_content_hash = hashlib.md5(dc_text.encode()).hexdigest()[:8]
        detail_key = f'textbooks/{subject_key}/{book_key}/{chap_key}_{section_key}_{dc_content_hash}_detail_{dc_idx:03d}'
        meta = {'text': dc_text, 'subject': subject, 'book': book, 'chapter': chapter, 'section': heading, 'subsection': dc['heading'], 'type': 'detail', 'source': 'textbook', 'injection_preview': dc_text[:300]}
        items.append({'key': detail_key, 'text': dc_text, 'metadata': meta})
        uploads.append((detail_key, {'text': dc_text, 'metadata': {k:v for k,v in meta.items() if k in ['subject','book','chapter','section','subsection','type','source']}}))
    
    return items, uploads

def process_one_book(subject: str, book: str, zip_path: str, max_sections: Optional[int] = None, max_workers: int = 8):
    """处理一本教材（并行）"""
    log.info(f'\n===== {subject} / {book} =====')
    
    z = zipfile.ZipFile(zip_path)
    md_text = z.read('full.md').decode('utf-8')
    z.close()
    
    lines = md_text.split("\n")
    sections = parse_sections(md_text)
    log.info(f'  Found {len(sections)} sections')
    
    book_key = safe_key(book)
    subject_key = safe_key(subject)
    to_process = sections[:max_sections] if max_sections else sections
    
    all_items = []
    file_uploads = []
    lock = threading.Lock()
    completed = 0
    total = len(to_process)
    
    def _worker(sec):
        nonlocal completed
        try:
            items, uploads = process_section(sec, lines, subject, book, book_key, subject_key)
            with lock:
                all_items.extend(items)
                file_uploads.extend(uploads)
                completed += 1
                if completed % 5 == 0 or completed == total:
                    log.info(f'  Progress: {completed}/{total} sections')
            return len(items), len(uploads)
        except Exception as e:
            log.error(f'  Section "{sec["heading"][:30]}" failed: {e}')
            return 0, 0
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        list(executor.map(_worker, to_process))
    
    log.info(f'  Done: {len(all_items)} items, {len(file_uploads)} uploads')
    return all_items, file_uploads

async def process_all(max_sections_per_book: Optional[int] = None, dry_run: bool = False, subjects_filter: Optional[List[str]] = None):
    """处理所有教材"""
    zips = find_result_zips()
    if subjects_filter:
        zips = [z for z in zips if z[0] in subjects_filter]
        log.info(f'Filtered to {len(zips)} textbooks: {subjects_filter}')
    log.info(f'Found {len(zips)} textbooks')
    
    vs = None
    if not dry_run:
        from services.vector_search_service import VectorSearchService
        vs = VectorSearchService(agent_hash=None)
    
    total_items = 0
    total_files = 0
    
    for subject, book, zip_path in zips:
        try:
            items, uploads = process_one_book(subject, book, zip_path, max_sections=max_sections_per_book)
            total_items += len(items)
            total_files += len(uploads)
            
            if not dry_run and items:
                # 去重（按 key）
                seen = set()
                deduped = []
                for it in items:
                    if it['key'] not in seen:
                        seen.add(it['key'])
                        deduped.append(it)
                if len(deduped) < len(items):
                    log.warning(f'  Removed {len(items)-len(deduped)} duplicates')
                items = deduped
                # Upload files to COS
                uploaded = 0
                for key, data in uploads:
                    if upload_to_file_bucket(key, data):
                        uploaded += 1
                log.info(f'  Uploaded {uploaded}/{len(uploads)} files')
                
                # Index vectors
                index_name = 'idx-public-textbook-kb'
                await vs.index_batch(items, index_name)
                log.info(f'  Indexed {len(items)} vectors to {index_name}')
            else:
                log.info(f'  DRY RUN: would index {len(items)} items, upload {len(uploads)} files')
                # Print first summary as example
                for it in items[:3]:
                    if it['metadata']['type'] == 'summary':
                        log.info(f'  Example summary chunk:\n{it["text"][:200]}...')
        except Exception as e:
            log.error(f'FAILED {book}: {e}')
            import traceback
            traceback.print_exc()
    
    log.info(f'\n===== COMPLETE =====')
    log.info(f'Total items: {total_items}, files: {total_files}')
    return total_items

async def test_search(query: str):
    """测试检索"""
    from services.vector_search_service import VectorSearchService
    vs = VectorSearchService(agent_hash=None)
    results = await vs.search(query, top_k=5)
    print(f'\n===== SEARCH: {query} =====')
    for r in results:
        src = r['metadata'].get('source', '?')
        typ = r['metadata'].get('type', '?')
        text = r['metadata'].get('text', '')[:200]
        print(f'  [{src}/{typ}] score={r["score"]:.3f}')
        print(f'    {text}...')
    return results

if __name__ == '__main__':
    import sys
    args = sys.argv[1:]
    
    subjects = None
    max_sec = None
    
    if '--test' in args:
        query = args[args.index('--test') + 1] if '--test' in args and len(args) > args.index('--test') + 1 else '物质分类'
        asyncio.run(test_search(query))
        sys.exit(0)
    
    if '--subjects' in args:
        idx = args.index('--subjects')
        subjects = args[idx+1].split(',') if idx+1 < len(args) else None
    if '--max' in args:
        idx = args.index('--max')
        max_sec = int(args[idx+1]) if idx+1 < len(args) else None
    
    if '--dry-run' in args:
        asyncio.run(process_all(max_sections_per_book=max_sec or 2, dry_run=True, subjects_filter=subjects))
    else:
        asyncio.run(process_all(max_sections_per_book=max_sec, subjects_filter=subjects))
