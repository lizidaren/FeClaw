#!/usr/bin/env python3
"""
曲一线解析结果 → SQLite (kaoxiang_kaodian) + 向量索引 (idx-public-math-trends)
"""
import asyncio, json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from dotenv import load_dotenv
load_dotenv()

from models.kaoxiang_models import init_kaoxiang_db, get_kaoxiang_session, KaoxiangKaodian

PARSED_DIR = '/home/lch/data/kaoxiang/qiyixian_parsed'
SOURCE_NAME = '2025版新高考版《曲一线·考点考法清单》数学考点清单+题型清单'


def import_sqlite():
    init_kaoxiang_db()
    session = get_kaoxiang_session()
    total = 0
    for fname in sorted(os.listdir(PARSED_DIR)):
        if not fname.endswith('.json'):
            continue
        with open(os.path.join(PARSED_DIR, fname)) as f:
            data = json.load(f)
        for kd in data.get('kaodian_list', []):
            name = kd.get('kaodian', '').strip()
            if not name:
                continue
            existing = session.query(KaoxiangKaodian).filter_by(
                subject='math', topic_full=data.get('topic_full', ''),
                kaodian=name, exam_trend=kd.get('exam_trend', '')
            ).first()
            if existing:
                old = json.loads(existing.exam_examples or '[]')
                existing.exam_examples = json.dumps(list(dict.fromkeys(old + kd.get('exam_examples', []))), ensure_ascii=False)
            else:
                session.add(KaoxiangKaodian(
                    subject='math', topic_full=data.get('topic_full', ''),
                    section=data.get('section', ''), kaodian=name,
                    exam_examples=json.dumps(kd.get('exam_examples', []), ensure_ascii=False),
                    exam_trend=kd.get('exam_trend', ''), exam_frequency=kd.get('exam_frequency', ''),
                    core_competency=kd.get('core_competency', ''), source_name=SOURCE_NAME,
                    source_file=data.get('file', ''),
                ))
                total += 1
        session.commit()
    print(f'SQLite: 新增 {total}, 总计 {session.query(KaoxiangKaodian).count()}')
    session.close()


async def vectorize_prop_forms():
    from services.vector_search_service import VectorSearchService
    vs = VectorSearchService()
    items = []
    for fname in sorted(os.listdir(PARSED_DIR)):
        if not fname.endswith('.json'):
            continue
        with open(os.path.join(PARSED_DIR, fname)) as f:
            data = json.load(f)
        pf = data.get('prop_form', '').strip()
        if not pf:
            continue
        items.append({
            'key': f'prop-form-qiyixian-{fname.replace(".json", "")}',
            'text': f'# {data["topic_full"]} 命题形式\n\n{pf}',
            'metadata': {'level': 'topic', 'source': 'qiyixian', 'source_name': SOURCE_NAME,
                         'topic_full': data['topic_full'], 'analysis_type': '命题形式'}
        })
    if items:
        await vs.index_batch(items, 'idx-public-math-trends')
        print(f'向量: 入库 {len(items)} 条命题形式')


if __name__ == '__main__':
    import_sqlite()
    print()
    asyncio.run(vectorize_prop_forms())
    print('完成')
