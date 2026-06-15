#!/usr/bin/env python3
"""
迁移必修教材数据：从 idx-public-kb → idx-public-textbook-kb

流程：
  1. 列出 COS 文件桶中 public/kb/textbooks/ 下所有 key
  2. 尝试从 idx-public-kb 读取对应向量（get_vectors）
  3. 写入 idx-public-textbook-kb
"""
import asyncio, json, logging, sys, os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(os.path.join(str(Path(__file__).parent.parent), '.env'))
from qcloud_cos import CosConfig, CosVectorsClient, CosS3Client
from config import settings

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('migrate_textbook')

COS_REGION = settings.TENCENT_COS_REGION
COS_SECRET_ID = settings.TENCENT_COS_SECRET_ID
COS_SECRET_KEY = settings.TENCENT_COS_SECRET_KEY
COS_BUCKET = settings.TENCENT_COS_BUCKET
COS_PREFIX = settings.TENCENT_COS_PREFIX or ''

SOURCE_INDEX = 'idx-public-kb'
TARGET_INDEX = 'idx-public-textbook-kb'
BATCH_SIZE = 50

def _get_file_client():
    config = CosConfig(Region=COS_REGION, SecretId=COS_SECRET_ID, SecretKey=COS_SECRET_KEY)
    return CosS3Client(config)

def _get_vector_client():
    config = CosConfig(Region=COS_REGION, SecretId=COS_SECRET_ID, SecretKey=COS_SECRET_KEY)
    return CosVectorsClient(config)

def ensure_index():
    client = _get_vector_client()
    try:
        client.create_index(
            Bucket=COS_BUCKET,
            Index=TARGET_INDEX,
            DataType='float32',
            Dimension=1024,
            DistanceMetric='cosine',
        )
        logger.info('Created index %s', TARGET_INDEX)
    except Exception as e:
        if 'already exists' in str(e).lower():
            logger.info('Index %s already exists', TARGET_INDEX)
        else:
            logger.warning('create_index %s: %s', TARGET_INDEX, e)

def list_textbook_keys():
    """列出 COS 文件桶中所有教材 key"""
    client = _get_file_client()
    keys = []
    marker = ''
    prefix = COS_PREFIX + 'public/kb/textbooks/'
    while True:
        kwargs = {'Bucket': COS_BUCKET, 'Prefix': prefix, 'MaxKeys': 1000}
        if marker:
            kwargs['Marker'] = marker
        resp = client.list_objects(**kwargs)
        for obj in resp.get('Contents', []):
            key = obj['Key']
            if not key.endswith('.json'):
                continue
            vk = key[len(COS_PREFIX):-5]  # strip prefix and .json
            keys.append(vk)
        if not resp.get('IsTruncated') or resp.get('IsTruncated') == 'false':
            break
        marker = resp.get('NextMarker', '')
        if not marker and resp.get('Contents'):
            marker = resp['Contents'][-1]['Key']
    return keys

def migrate():
    """主迁移流程"""
    ensure_index()
    vclient = _get_vector_client()

    all_keys = list_textbook_keys()
    logger.info('Found %d textbook file keys', len(all_keys))

    total_migrated = 0
    for batch_start in range(0, len(all_keys), BATCH_SIZE):
        batch = all_keys[batch_start:batch_start + BATCH_SIZE]
        batch_num = batch_start // BATCH_SIZE + 1
        total_batches = (len(all_keys) + BATCH_SIZE - 1) // BATCH_SIZE

        logger.info('Batch %d/%d: processing %d keys', batch_num, total_batches, len(batch))

        try:
            _, data = vclient.get_vectors(
                Bucket=COS_BUCKET,
                Index=SOURCE_INDEX,
                Keys=batch,
            )
            vectors = data.get('vectors', [])
        except Exception as e:
            logger.warning('Batch %d get_vectors failed: %s, skipping', batch_num, e)
            continue

        if not vectors:
            logger.info('  No vectors found in source index for this batch')
            continue

        # Write to target index
        try:
            vclient.put_vectors(
                Bucket=COS_BUCKET,
                Index=TARGET_INDEX,
                Vectors=vectors,
            )
            logger.info('  Migrated %d vectors', len(vectors))
            total_migrated += len(vectors)
        except Exception as e:
            logger.error('  put_vectors failed: %s', e)

    logger.info('='*50)
    logger.info('Migration complete: %d vectors migrated to %s', total_migrated, TARGET_INDEX)
    return total_migrated

if __name__ == '__main__':
    dry_run = '--dry-run' in sys.argv
    if dry_run:
        keys = list_textbook_keys()
        logger.info('DRY RUN: Found %d textbook keys, would migrate to %s', len(keys), TARGET_INDEX)
    else:
        migrate()
