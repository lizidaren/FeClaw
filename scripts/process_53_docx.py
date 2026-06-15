#!/usr/bin/env python3
"""
5·3 A版 十年高考真题分类汇编 → idx-public-gaokao-kb
"""
import asyncio, logging, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv(os.path.join(str(Path(__file__).parent.parent), '.env'))
from services.vector_search_service import VectorSearchService

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('process_53_docx')

DOCX_BASE = '/tmp/2025版.新高考版.高考总复习.数学.5·3A版/图书拓展资源/10年高考真题专题分类汇编'

def parse_topic_name(dirname: str) -> str:
    """从目录名提取专题名称"""
    # e.g., '专题三函数及其性质' -> '函数及其性质'
    parts = dirname.replace('专题', '').split(' ', 1)
    if len(parts) > 1:
        return parts[1]
    return dirname

def extract_questions(filepath: str, topic: str, subtopic: str) -> list:
    """从 .docx 中提取独立题目"""
    import docx
    d = docx.Document(filepath)
    paragraphs = [p.text.strip() for p in d.paragraphs if p.text.strip()]
    
    questions = []
    current_q = []
    in_answer = False
    
    for text in paragraphs:
        if not text:
            continue
        # 检测新题目（带题号的如 "1." "2."）
        if text[0].isdigit() and ('.' in text[:4] or ')' in text[:4]):
            if current_q:
                questions.append('\n'.join(current_q))
            current_q = [text]
            in_answer = False
            continue
        # 答案行
        if text.startswith('答案') or text.startswith('答案　'):
            in_answer = True
        if text.startswith('解析') or text.startswith('解题指导'):
            in_answer = True
        current_q.append(text)
    
    if current_q:
        questions.append('\n'.join(current_q))
    
    return questions

def collect_all_docx() -> list:
    """收集所有 .docx 文件及其元数据"""
    items = []
    for root, _, files in os.walk(DOCX_BASE):
        for f in files:
            if not f.endswith('.docx'):
                continue
            fp = os.path.join(root, f)
            rel = os.path.relpath(root, DOCX_BASE).split(os.sep)
            topic = parse_topic_name(rel[0]) if len(rel) >= 1 else ''
            # Extract subtopic from filename
            subtopic = f.replace('.docx', '')
            # Clean subtopic: remove prefix like '7_', '1_'
            if '_' in subtopic:
                subtopic = subtopic.split('_', 1)[1]
            
            questions = extract_questions(fp, topic, subtopic)
            for q in questions:
                items.append({
                    'topic': topic,
                    'subtopic': subtopic,
                    'text': q,
                    'source': '5-3-gaokao',
                    'subject': '数学',
                })
            logger.info(f'  {topic}/{subtopic}: {len(questions)} questions')
    return items

async def main():
    print('\n' + '='*60)
    print('  5·3 十年高考真题 → idx-public-gaokao-kb')
    print('='*60 + '\n')
    
    logger.info('Step 1: Collecting .docx files...')
    items = collect_all_docx()
    logger.info(f'Total questions: {len(items)}')
    
    if not items:
        logger.warning('No items found!')
        return
    
    logger.info('Step 2: Embedding and indexing to idx-public-gaokao-kb...')
    vs = VectorSearchService(agent_hash=None)
    
    # Batch index all items
    batch_size = 50
    total = 0
    for i in range(0, len(items), batch_size):
        batch = items[i:i+batch_size]
        # Prepare items for VectorSearchService
        vs_items = []
        for item in batch:
            text = item['text']
            # Add topic/subtopic prefix for context
            full_text = f'【{item["topic"]}·{item["subtopic"]}】\n{text}'
            vs_items.append({
                'key': f'53-gaokao/数学/{item["topic"]}/{item["subtopic"]}_{hash(text) & 0xffff:04x}',
                'text': full_text,
                'metadata': {
                    'text': full_text,
                    'source': '53-gaokao',
                    'subject': '数学',
                    'topic': item['topic'],
                    'subtopic': item['subtopic'],
                }
            })
        
        await vs.index_batch(vs_items, 'idx-public-gaokao-kb')
        total += len(vs_items)
        batch_num = i // batch_size + 1
        total_batches = (len(items) + batch_size - 1) // batch_size
        logger.info(f'  Batch {batch_num}/{total_batches}: indexed {len(vs_items)} vectors')
    
    logger.info(f'Complete! {total} vectors indexed to idx-public-gaokao-kb')

if __name__ == '__main__':
    asyncio.run(main())
