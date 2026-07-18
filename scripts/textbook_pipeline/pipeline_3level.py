# Helper functions for 3-level textbook pipeline
import asyncio, json, os, re, sys, time, zipfile, hashlib, uuid, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))
import httpx

DS_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
DS_API = 'https://api.deepseek.com/chat/completions'
MAX_W = 16
LARGE_TH = 2000
MINERU_DIR = '/home/lch/data/textbooks/mineru_results'
CHUNK_DIR = '/home/lch/data/textbooks/chunk_results_v2'
PROC_DIR = '/home/lch/data/textbooks/chunk_processed'
os.makedirs(CHUNK_DIR, exist_ok=True)
os.makedirs(PROC_DIR, exist_ok=True)

SUBJECT_MAP = {
    '化学': 'chemistry', '数学-人教A版': 'math_rja', '数学-湘教版': 'math_xj',
    '物理': 'physics', '生物': 'biology', '英语': 'english',
    '语文': 'chinese', '地理': 'geography', '政治': 'politics',
}
DEEP_SUBJECTS = {'chemistry', 'math_rja', 'math_xj', 'physics', 'biology'}

COS_REGION = None
def get_cos_client():
    global COS_REGION
    try:
        from config import settings
        from qcloud_cos import CosConfig, CosS3Client
        config = CosConfig(Region=settings.TENCENT_COS_REGION, SecretId=settings.TENCENT_COS_SECRET_ID, SecretKey=settings.TENCENT_COS_SECRET_KEY)
        return CosS3Client(config), settings.TENCENT_COS_BUCKET, (settings.STORAGE_PREFIX or '') + 'textbook_originals/'
    except Exception as e:
        print(f'WARN: Cannot init COS: {e}')
        return None, None, None

def extract_book_short(d):
    m = re.search(r'(必修[上下一二三四五六七八九十里外]+)', d)
    return m.group(1) if m else d[:10]

def make_ref_id(subj, book_short, ch, sec=0, kp=0):
    parts = [subj, book_short, str(ch)]
    if sec: parts.append(str(sec))
    if kp: parts.append(str(kp))
    return '::'.join(parts)

def make_key(ref_id):
    return hashlib.md5(ref_id.encode()).hexdigest()

def read_md(zp):
    with zipfile.ZipFile(zp) as z:
        return z.read('full.md').decode('utf-8')

def add_numbers(text):
    lines = text.split('
')
    return '
'.join(f'L{i+1:05d}:{line}' for i, line in enumerate(lines)), lines

def extract_json(text):
    js_s = text.find('```json')
    if js_s >= 0:
        js_s = text.find('
', js_s) + 1
        js_e = text.rfind('```')
    else:
        js_s = text.find('{')
        js_e = text.rfind('}') + 1
    return json.loads(text[js_s:js_e].strip())

def discover_textbooks():
    books = []
    for subj_dir in sorted(os.listdir(MINERU_DIR)):
        spath = os.path.join(MINERU_DIR, subj_dir)
        if not os.path.isdir(spath): continue
        subject = SUBJECT_MAP.get(subj_dir)
        if not subject: continue
        for book_dir in sorted(os.listdir(spath)):
            zp = os.path.join(spath, book_dir, 'result.zip')
            if os.path.exists(zp):
                books.append({'zip_path': zp, 'book_key': f'{subj_dir}/{book_dir}',
                    'subject': subject, 'subject_cn': subj_dir, 'book_dir': book_dir,
                    'book_short': extract_book_short(book_dir)})
    return books

def is_real_chapter(ch):
    if ch.get('skip') or ch.get('type') == 'appendix': return False
    title = ch.get('title', '')
    skip_kw = ['序言','绪言','前言','封面','目录','致同学']
    if any(k in title for k in skip_kw): return False
    if ch.get('line_end', 0) - ch.get('line_start', 0) < 50: return False
    return True

# Phase 2-5 appended

# ===== Prompts =====
L1_PROMPT = """你是一个专业的教材结构分析专家。任务：对 MinerU 教材Markdown全文精确切分章级别结构。每行有行号 L00001: 格式。

修正OCR问题：标题拆分合并、￥伪影、儿->几。
习题（练习与应用）从行号范围排除。各章整理与提升属复习总结，保留。
附录标记 type="appendix"。

注意科目差异：语文的"整本书阅读"或"项目探究"类单元不要切分，保留完整。

输出JSON：
{"chapters": [
  {"title": "第1章 标题", "line_start": 1, "line_end": 100, "skip": false,
   "sections": [{"id": "1.1", "title": "节标题", "line_start": 15, "line_end": 50}]}
]}
行号精确对应原文。"""
L2_PROMPT = """你是一个教材章节结构分析专家。任务：将教材一章的内容精确切分为节(section)级别。每行有 L00001: 行号标记。

注意科目差异：
- 语文：节 = 课（每一课包含1篇或多篇课文），学习提示附在课内，单元导语和单元学习任务各自独立
- 化学/生物/数学/物理：节 = 1.1、1.2 等自然节
- 语文的整本书阅读/项目探究单元：不切，type=whole_unit

要求：
- 节末练习题从范围排除
- 节内蓝色粗体子标题保留但标记 type="subsection"

输出JSON：
{"sections": [
  {"id": "1.1", "title": "节标题", "line_start": 15, "line_end": 50, "type": "normal"}
]}"""
