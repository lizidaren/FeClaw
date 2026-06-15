import sys, os, json, re, time, zipfile, hashlib, asyncio
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '..', '.env'))
import httpx

DS_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
DS_API = 'https://api.deepseek.com/chat/completions'
MAX_W = 16
LARGE_TH = 2000
CHUNK_DIR = '/home/lch/data/textbooks/chunk_results_v2'
PROC_DIR = '/home/lch/data/textbooks/chunk_processed'
os.makedirs(PROC_DIR, exist_ok=True)

DEEP_SUBJECTS = {'chemistry', 'math_rja', 'math_xj', 'physics', 'biology'}

L2_PROMPT = '\n'
L3_PROMPT = '\n'
SUMMARY_PROMPT = '\n'

async def main():
    print('Phase 2-5 placeholder')

if __name__ == '__main__':
    asyncio.run(main())
