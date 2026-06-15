#!/usr/bin/env python3
"""
5·3 考向分析 DeepSeek 结果 → 向量化入库 idx-public-math-trends

从 /home/lch/data/kaoxiang/ 读取 45 个 JSON 结果文件，
用 Qwen text-embedding-v4 嵌入，8 并发入库。
"""
import asyncio, logging, sys, json, os, re, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv()

from services.vector_search_service import VectorSearchService

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('kaoxiang_vectorize')

DATA_DIR = Path('/home/lch/data/kaoxiang/')
INDEX_NAME = 'idx-public-math-trends'
MAX_CONCURRENT = 8
RETRY_LIMIT = 3


async def vectorize_one(vs: VectorSearchService, sem: asyncio.Semaphore, filepath: Path):
    """处理单个 JSON 结果文件"""
    async with sem:
        try:
            with open(filepath) as f:
                data = json.load(f)
            
            analysis = data.get('analysis', '')
            if not analysis:
                log.warning(f'SKIP {filepath.name}: empty analysis')
                return None
            
            topic = data.get('topic', '')
            subtopic = data.get('subtopic', '')
            
            # 构建唯一 key
            safe_topic = re.sub(r'[\\s/]', '_', topic)
            safe_subtopic = re.sub(r'[\\s/]', '_', subtopic)
            key = f'kaoxiang-53-{safe_topic}-{safe_subtopic}'
            
            metadata = {
                'level': 'subtopic',
                'source': '53',
                'source_name': '2025版.新高考版.高考总复习.数学.5·3A版',
                'topic': data.get('topic', ''),
                'topic_full': data.get('topic_full', ''),
                'subtopic': data.get('subtopic', ''),
                'num_questions': data.get('num_questions', 0),
                'prompt_version': data.get('prompt_version', 'v3'),
            }
            
            # 重试
            for attempt in range(1, RETRY_LIMIT + 1):
                try:
                    await vs.index_text(key, analysis, INDEX_NAME, metadata)
                    log.info(f'OK {filepath.name} ({data.get("num_questions",0)} questions)')
                    return key
                except Exception as e:
                    log.warning(f'RETRY {filepath.name} attempt {attempt}: {e}')
                    if attempt < RETRY_LIMIT:
                        await asyncio.sleep(5 * attempt)
                    else:
                        log.error(f'FAIL {filepath.name} after {RETRY_LIMIT} attempts')
                        return None
        except Exception as e:
            log.error(f'ERROR {filepath.name}: {e}')
            return None


async def main():
    # 收集所有 JSON 结果文件
    files = sorted(DATA_DIR.glob('*.json'))
    files = [f for f in files if f.name != '_summary.json']
    log.info(f'Found {len(files)} result files in {DATA_DIR}')
    
    vs = VectorSearchService()
    sem = asyncio.Semaphore(MAX_CONCURRENT)
    
    start = time.time()
    tasks = [vectorize_one(vs, sem, fp) for fp in files]
    results = await asyncio.gather(*tasks)
    elapsed = time.time() - start
    
    success = sum(1 for r in results if r is not None)
    fail = sum(1 for r in results if r is None)
    log.info(f'Done: {success}/{len(files)} success, {fail} failed in {elapsed:.1f}s')


if __name__ == '__main__':
    asyncio.run(main())
