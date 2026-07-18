#!/usr/bin/env python3
"""
选必教材 MinerU 批量处理 + 本地分块/向量化
Phase 0: 提取PDF → 上传COS
Phase 1: 提交URL → MinerU
Phase 2: 轮询结果
Phase 3: 分块 + DeepSeek 摘要 + 向量化（存本地不上传）
"""
import os, sys, json, re, time, logging, zipfile, hashlib, httpx, tempfile, shutil
from pathlib import Path
from typing import List, Dict, Optional

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

# === Config ===
ZIP_PATH = '/home/lch/data/textbooks/high_school_textbooks.zip'
OUT_DIR = '/home/lch/data/textbooks/mineru_results/'
LOCAL_VEC_DIR = '/home/lch/data/textbooks/local_vectors/'
TMP_DIR = '/tmp/xuanbi/'

# Load env
def load_env(path):
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    os.environ[k.strip()] = v.strip()

for ep in ['/home/lch/Projects/FeClaw/.env', os.path.expanduser('~/.secrets/deepseek_api_key'), os.path.expanduser('~/.openclaw/secrets/deepseek_api_key')]:
    if os.path.exists(ep):
        try:
            if ep.endswith('.env'):
                load_env(ep)
            elif ep.endswith('secrets/'):
                pass
        except:
            pass

# Also load FeClaw config for COS
sys.path.insert(0, '/home/lch/Projects/FeClaw')
from dotenv import load_dotenv
load_dotenv('/home/lch/Projects/FeClaw/.env')
from config import settings

# === COS Client ===
from qcloud_cos import CosConfig, CosS3Client
cos_config = CosConfig(Region=settings.TENCENT_COS_REGION, SecretId=settings.TENCENT_COS_SECRET_ID, SecretKey=settings.TENCENT_COS_SECRET_KEY)
cos_client = CosS3Client(cos_config)
COS_BUCKET = settings.TENCENT_COS_BUCKET
COS_PREFIX = settings.STORAGE_PREFIX or ''
COS_DOMAIN = 'https://firstentrance-gz01-1257148458.cos.ap-guangzhou.myqcloud.com'

# API keys
MINERU_TOKEN = os.environ.get('MINERU_TOKEN', '')
DS_API_KEY = os.environ.get('DEEPSEEK_API_KEY', '') or ''
if not DS_API_KEY:
    for p in [os.path.expanduser(x) for x in ['~/.secrets/deepseek_api_key', '~/.openclaw/secrets/deepseek_api_key']]:
        if os.path.exists(p):
            DS_API_KEY = open(p).read().strip()
            break

MINERU_API = 'https://mineru.net/api/v4/extract/task'

# === Helpers ===
def safe_key(s):
    return re.sub(r'[\\/:*?"<>|\s]+', '_', s).strip('_')

# Processed list (skip these)
PROCESSED = {
    '高中化学必修一', '高中化学必修二',
    '高中地理必修一', '高中政治必修一',
    '高中数学人教A版必修一', '高中数学人教A版必修二',
    '高中数学湘教版必修一', '高中数学湘教版必修二',
    '高中物理必修一', '高中物理必修二', '高中物理必修三',
    '高中生物学必修一', '高中生物学必修二',
    '高中英语外研社版必修一', '高中英语外研社版必修二', '高中英语外研社版必修三',
    '高中语文必修上', '高中语文必修下'
}

def resolve_subject_book(pdf_name):
    """From zip internal path to (subject, book_name)"""
    parts = pdf_name.replace('电子教材/', '', 1).split('/')
    if len(parts) >= 3 and parts[0] == '数学':
        # 数学/人教A版/xxx.pdf or 数学/湘教版/xxx.pdf
        subject = f'数学-{parts[1]}'
        book = parts[2].replace('.pdf', '')
    elif len(parts) >= 2:
        subject = parts[0]
        book = parts[1].replace('.pdf', '')
    else:
        subject = parts[0]
        book = os.path.basename(pdf_name).replace('.pdf', '')
    return subject, book

# === Phase 0: Upload to COS ===
def upload_to_cos(subject, book, pdf_path):
    key = f'{COS_PREFIX}public/textbooks/{safe_key(subject)}/{book}.pdf'
    try:
        with open(pdf_path, 'rb') as f:
            cos_client.put_object(Bucket=COS_BUCKET, Key=key, Body=f)
        url = f'{COS_DOMAIN}/{key}'
        log.info(f'  Uploaded to COS: {url[:80]}...')
        return url
    except Exception as e:
        log.error(f'  COS upload failed: {e}')
        return None

def upload_existing_textbooks():
    """Upload PDFs from zip to COS if not already there"""
    with zipfile.ZipFile(ZIP_PATH) as z:
        pdfs = [n for n in z.namelist() if n.endswith('.pdf')]
    targets = [p for p in pdfs if os.path.basename(p).replace('.pdf','') not in PROCESSED]
    log.info(f'Need to upload {len(targets)} PDFs to COS')
    
    os.makedirs(TMP_DIR, exist_ok=True)
    with zipfile.ZipFile(ZIP_PATH) as z:
        for pdf_name in targets:
            subject, book = resolve_subject_book(pdf_name)
            cos_key = f'{COS_PREFIX}public/textbooks/{safe_key(subject)}/{book}.pdf'
            
            # Check if already in COS
            try:
                cos_client.head_object(Bucket=COS_BUCKET, Key=cos_key)
                log.info(f'  Already in COS: {subject}/{book}')
                continue
            except:
                pass
            
            log.info(f'  Uploading: {subject}/{book}')
            data = z.read(pdf_name)
            tmp = os.path.join(TMP_DIR, f'{book}.pdf')
            with open(tmp, 'wb') as f:
                f.write(data)
            url = upload_to_cos(subject, book, tmp)
            os.unlink(tmp)
    log.info('Phase 0 complete')

# === Phase 1: Submit URLs to MinerU ===
def submit_to_mineru(url):
    resp = httpx.post(
        MINERU_API,
        headers={'Authorization': f'Bearer {MINERU_TOKEN}', 'Content-Type': 'application/json'},
        json={'url': url},
        timeout=30
    )
    if resp.status_code == 200:
        j = resp.json()
        if j.get('code') != 0:
            log.error(f'  API error: {j.get("msg","?")}')
            return None
        task_id = j.get('data', {}).get('task_id')
        if task_id:
            log.info(f'  Submitted: task_id={task_id}')
            return task_id
    log.error(f'  Submit failed: {resp.status_code} {resp.text[:200]}')
    return None

def submit_all():
    log.info('\n--- Phase 1: Submitting URLs to MinerU ---')
    
    with zipfile.ZipFile(ZIP_PATH) as z:
        pdfs = [n for n in z.namelist() if n.endswith('.pdf')]
    targets = [p for p in pdfs if os.path.basename(p).replace('.pdf','') not in PROCESSED]
    
    tasks = []
    for pdf_name in targets:
        subject, book = resolve_subject_book(pdf_name)
        cos_url = f'{COS_DOMAIN}/{COS_PREFIX}public/textbooks/{safe_key(subject)}/{book}.pdf'
        
        # Check if already processed
        result_dir = os.path.join(OUT_DIR, safe_key(subject), safe_key(book))
        if os.path.exists(os.path.join(result_dir, 'result.zip')):
            log.info(f'  Already processed: {subject}/{book}')
            tasks.append((subject, book, result_dir, 'DONE'))
            continue
        
        log.info(f'  Submitting: {subject}/{book}')
        task_id = submit_to_mineru(cos_url)
        if task_id:
            tasks.append((subject, book, result_dir, task_id))
        time.sleep(12)
    
    log.info(f'Submitted {len(tasks)} tasks')
    with open(os.path.join(OUT_DIR, 'xuanbi_tasks.json'), 'w') as f:
        json.dump(tasks, f)
    return tasks

# === Phase 2: Poll ===
def poll_result(task_id):
    for attempt in range(30):
        try:
            resp = httpx.get(f'{MINERU_API}/{task_id}',
                headers={'Authorization': f'Bearer {MINERU_TOKEN}'}, timeout=30)
            if resp.status_code == 200:
                data = resp.json().get('data', {})
                state = data.get('state')
                if state == 'done':
                    zip_url = data.get('full_zip_url')
                    log.info(f'    Done!')
                    return zip_url
                elif state in ('failed', 'error'):
                    log.error(f'    Failed: {data}')
                    return None
        except Exception as e:
            log.error(f'    Poll error: {e}')
        time.sleep(10) if attempt < 10 else time.sleep(30)
    log.error('Timed out')
    return None

def download_zip(zip_url, target_dir):
    try:
        resp = httpx.get(zip_url, timeout=600)
        if resp.status_code == 200:
            os.makedirs(target_dir, exist_ok=True)
            with open(os.path.join(target_dir, 'result.zip'), 'wb') as f:
                f.write(resp.content)
            log.info(f'    Saved to {target_dir}/result.zip')
            return True
    except Exception as e:
        log.error(f'    Download failed: {e}')
    return False

def poll_all(tasks):
    log.info('\n--- Phase 2: Polling results (concurrent) ---')
    from concurrent.futures import ThreadPoolExecutor, as_completed
    
    def _poll_one(args):
        subject, book, out_dir, task_id = args
        if task_id == 'DONE':
            return subject, book, True
        zip_url = poll_result(task_id)
        if zip_url and download_zip(zip_url, out_dir):
            return subject, book, True
        return subject, book, False
    
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(_poll_one, t): t for t in tasks}
        for f in as_completed(futures):
            s, b, ok = f.result()
            if ok:
                log.info(f'  ✅ {s}/{b}')
            else:
                log.error(f'  ❌ {s}/{b}')

# === Phase 3: Local chunk + DeepSeek + vectorize (save locally) ===
def strip_images(text):
    lines = text.split('\\n')
    res = []
    for l in lines:
        s = l.strip()
        if not s: res.append(l); continue
        if re.match(r'^\(?图\s*[\d\-.]+\)?', s): continue
        if re.match(r'^!\[.*\]\(.*\)$', s): continue
        res.append(l)
    return '\\n'.join(res)

def extract_intro(lines, start):
    intro = []
    for i in range(start+1, min(start+50, len(lines))):
        l = lines[i].strip()
        if not l or l.startswith('#'):
            if intro and not l: break
            if not intro: continue
            break
        if re.match(r'^\(?图\s*[\d\-.]+\)', l): continue
        intro.append(l)
    return '\\n'.join(intro).strip()

def chunk_section_text(lines):
    chunks = []
    cur = []
    h = '(正文)'
    for l in lines:
        s = l.strip()
        if s.startswith('#'):
            if cur:
                t = '\\n'.join(cur).strip()
                if t: chunks.append({'heading': h, 'text': t})
                cur = []
            h = s.lstrip('#').strip()
        else:
            cur.append(l)
    if cur:
        t = '\\n'.join(cur).strip()
        if t: chunks.append({'heading': h, 'text': t})
    return chunks

def parse_sections(md_text):
    lines = md_text.split('\\n')
    sections = []
    skip = re.compile(r'^(目录|主编寄语|致同学们|后记|编后记|版权|致谢|前言|编者|出版说明|编写人员|鸣谢|内容提要|扉页)$')
    ch, hd, st, lv = '', '', 0, 0
    for i, line in enumerate(lines):
        s = line.strip()
        if not s or s.startswith('<'): continue
        if not s.startswith('#'): continue
        level = len(s) - len(s.lstrip('#'))
        text = s.lstrip('#').strip()
        if not text or skip.match(text): continue
        is_chap = bool(re.match(r'^第[一二三四五六七八九十百千]+章', text))
        is_sec = bool(re.match(r'^第[一二三四五六七八九十百千]+节', text)) or bool(re.match(r'^\d+\.\d+', text))
        if is_chap:
            if hd: sections.append({'heading': hd, 'chapter': ch, 'start': st, 'end': i, 'level': lv})
            ch, hd, st, lv = text, text, i, level
        elif is_sec or (text.startswith('习题') or text.startswith('复习') or level <= 1):
            if hd: sections.append({'heading': hd, 'chapter': ch, 'start': st, 'end': i, 'level': lv})
            hd, st, lv = text, i, level
    if hd:
        sections.append({'heading': hd, 'chapter': ch, 'start': st, 'end': len(lines), 'level': lv})
    return sections

def generate_summary(section_text, intro_text, subject, book, chapter, heading):
    if not DS_API_KEY:
        return None
    prompt = f'''你是高中{subject}教材分析专家。\n生成约150-200字摘要概括本节核心知识点。\n\n科目：{subject}\n教材：{book}\n章节：{chapter}\n标题：{heading}\n\n导语：\n{intro_text[:500] if intro_text else '(无导语)'}\n\n正文节选：\n{section_text[:3000]}'''
    try:
        resp = httpx.post('https://api.deepseek.com/v1/chat/completions',
            headers={'Authorization': f'Bearer {DS_API_KEY}', 'Content-Type': 'application/json'},
            json={'model': 'deepseek-chat',
                  'messages': [
                      {'role': 'system', 'content': '你是一个高中教材分析专家。用中文简洁精炼地概括核心知识点，只输出摘要本身。'},
                      {'role': 'user', 'content': prompt}
                  ],
                  'max_tokens': 1024, 'temperature': 0.3},
            timeout=60)
        if resp.status_code == 200:
            return resp.json()['choices'][0]['message']['content'].strip()
    except:
        pass
    return None

def process_locally(subject, book, zip_path):
    log.info(f'\\n===== {subject} / {book} =====')
    z = zipfile.ZipFile(zip_path)
    md_text = z.read('full.md').decode('utf-8')
    z.close()
    lines = md_text.split('\\n')
    sections = parse_sections(md_text)
    log.info(f'  Found {len(sections)} sections')
    
    bk = safe_key(book)
    sk = safe_key(subject)
    out_base = os.path.join(LOCAL_VEC_DIR, sk, bk)
    os.makedirs(out_base, exist_ok=True)
    
    total = 0
    for si, sec in enumerate(sections):
        try:
            hd = sec['heading']
            ch = sec['chapter'] or hd
            skey = safe_key(hd)
            slines = lines[sec['start']:sec['end']]
            if not slines: continue
            intro = extract_intro(lines, sec['start'])
            sec_text = '\\n'.join(slines)
            if len(strip_images(sec_text).strip()) < 50: continue
            dcs = chunk_section_text(slines)
            log.info(f'  [{si+1}/{len(sections)}] {hd}')
            summary = generate_summary(sec_text, intro, subject, book, ch, hd)
            ck = safe_key(ch)
            if summary:
                stxt = f'{book}\\n{hd} （摘要）\\n\\n{intro}\\n\\n{summary}'
                key = f'{ck}_{skey}_summary'
                item = {'text': stxt, 'metadata': {'subject': subject, 'book': book, 'chapter': ch, 'section': hd, 'type': 'summary'}}
                with open(os.path.join(out_base, f'{key}.json'), 'w') as f:
                    json.dump(item, f, ensure_ascii=False)
                total += 1
            for di, dc in enumerate(dcs):
                dt = dc['text'].strip()
                if len(dt) < 30: continue
                dh = hashlib.md5(dt.encode()).hexdigest()[:8]
                key = f'{ck}_{skey}_{dh}_detail_{di:03d}'
                item = {'text': dt, 'metadata': {'subject': subject, 'book': book, 'chapter': ch, 'section': hd, 'subsection': dc['heading'], 'type': 'detail'}}
                with open(os.path.join(out_base, f'{key}.json'), 'w') as f:
                    json.dump(item, f, ensure_ascii=False)
                total += 1
            log.info(f'    Chunks: {len(dcs)}')
        except Exception as e:
            log.error(f'  [{si+1}] ERROR: {e}')
    log.info(f'  Total: {total} items -> {out_base}')
    return total

def phase3_all(tasks):
    log.info('\\n--- Phase 3: Local chunking + summaries + vectors ---')
    total = 0
    for subject, book, out_dir, tid in tasks:
        zp = os.path.join(out_dir, 'result.zip')
        if os.path.exists(zp):
            n = process_locally(subject, book, zp)
            total += n
        else:
            log.warning(f'  No result.zip for {subject}/{book}')
    log.info(f'\\nPhase 3 complete: {total} total items')
    return total

# === Main ===
def main():
    log.info('=== Selected Textbook MinerU + Local Vectorization ===')
    log.info(f'MINERU_TOKEN: {"SET" if MINERU_TOKEN else "MISSING"}')
    log.info(f'DS_API_KEY: {"SET" if DS_API_KEY else "MISSING"}')
    log.info(f'COS: {COS_BUCKET}')
    
    step = os.environ.get('STEP', '012')
    tasks_path = os.path.join(OUT_DIR, 'xuanbi_tasks.json')
    tasks = []
    
    if '0' in step:
        upload_existing_textbooks()
    if '1' in step:
        tasks = submit_all()
    elif os.path.exists(tasks_path):
        with open(tasks_path) as f:
            tasks = json.load(f)
            log.info(f'Loaded {len(tasks)} tasks from xuanbi_tasks.json')
    if '2' in step:
        poll_all(tasks)
    if '3' in step:
        phase3_all(tasks)
    
    log.info('\\n=== DONE ===')

if __name__ == '__main__':
    main()
