#!/usr/bin/env python3
"""
5·3 A版 PDF → COS + MinerU
"""
import json, logging, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))
from config import settings
from qcloud_cos import CosConfig, CosS3Client

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('process_53_pdf')

PDF_DIR = '/tmp/2025版.新高考版.高考总复习.数学.5·3A版/图书电子教参'
MINERU_API = 'https://mineru.net/api/v4/extract/task'
MINERU_TOKEN = os.environ.get('MINERU_TOKEN', '')
COS_BUCKET = settings.TENCENT_COS_BUCKET
COS_PREFIX = settings.TENCENT_COS_PREFIX or ''
COS_DOMAIN = 'https://firstentrance-gz01-1257148458.cos.ap-guangzhou.myqcloud.com'

config = CosConfig(Region=settings.TENCENT_COS_REGION,
                    SecretId=settings.TENCENT_COS_SECRET_ID,
                    SecretKey=settings.TENCENT_COS_SECRET_KEY)
client = CosS3Client(config)

import httpx

def collect_pdfs():
    pdfs = []
    for root, _, files in os.walk(PDF_DIR):
        for f in files:
            if f.endswith('.pdf'):
                pdfs.append(os.path.join(root, f))
    return sorted(pdfs)

def upload_to_cos(local_path):
    rel = os.path.relpath(local_path, start=os.path.dirname(PDF_DIR))
    cos_key = f'{COS_PREFIX}public/53-pdf/{rel}'
    with open(local_path, 'rb') as f:
        client.put_object(Bucket=COS_BUCKET, Key=cos_key, Body=f, ACL='public-read')
    url = f'{COS_DOMAIN}/{cos_key}'
    return url, cos_key

def submit_to_mineru(url, name):
    global MINERU_TOKEN
    if not MINERU_TOKEN or len(MINERU_TOKEN) < 10:
        # Reload from .env
        MINERU_TOKEN = os.environ.get('MINERU_TOKEN', '')
    logger.info(f'  Token length: {len(str(MINERU_TOKEN))}')
    resp = httpx.post(MINERU_API,
        headers={'Authorization': f'Bearer {MINERU_TOKEN}', 'Content-Type': 'application/json'},
        json={'url': url},
        timeout=30)
    logger.info(f'  Response: {resp.status_code}')
    data = resp.json()
    if resp.status_code == 200:
        tid = data.get('data', {}).get('task_id')
        logger.info(f'  Submitted: {name} -> {tid}')
        if not tid:
            logger.warning(f'  No task_id! Response: {str(data)[:200]}')
        return tid
    else:
        logger.warning(f'  FAILED {name}: {resp.status_code} {str(data)[:200]}')
        return None

if __name__ == '__main__':
    pdfs = collect_pdfs()
    logger.info(f'Found {len(pdfs)} PDFs')
    
    tasks = []
    for i, pdf in enumerate(pdfs, 1):
        name = os.path.basename(pdf)
        logger.info(f'[{i}/{len(pdfs)}] {name}')
        url, _ = upload_to_cos(pdf)
        tid = submit_to_mineru(url, name)
        if tid:
            tasks.append({'name': name, 'task_id': tid})
        time.sleep(12)
    
    # Save tasks
    with open('/tmp/53_pdf_tasks.json', 'w') as f:
        json.dump(tasks, f, ensure_ascii=False, indent=2)
    logger.info(f'Done! {len(tasks)} tasks submitted')
