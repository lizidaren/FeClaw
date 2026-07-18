#!/usr/bin/env python3
"""
高考数据迁移脚本：从 idx-public-kb 中分离 gaokao-bench 数据到 idx-public-gaokao-kb

流程：
  1. 列出 COS 文件桶中 public/kb/gaokao/ 下的所有 key
  2. 从 idx-public-kb 查询对应向量
  3. 写入 idx-public-gaokao-kb（复用已有向量和 metadata）
  4. 验证迁移结果

用法：
  python scripts/migrate_gaokao_index.py [--dry-run] [--batch-size 50]
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from qcloud_cos import CosConfig, CosVectorsClient, CosS3Client
from config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

VECTOR_BUCKET = "firstentrance-gzvec-1257148458"
VECTOR_ENDPOINT = "vectors.ap-guangzhou.coslake.com"
SOURCE_INDEX = "idx-public-kb"
TARGET_INDEX = "idx-public-gaokao-kb"
S3_PREFIX = "public/kb/gaokao/"

# Gaokao metadata filter — must have source=gaokao-bench or source starting with 'gaokao'
GAOKAO_SOURCES = ("gaokao-bench",)


def _get_cos_file_client():
    """COS S3 文件桶客户端"""
    config = CosConfig(
        Region=settings.TENCENT_COS_REGION,
        SecretId=settings.TENCENT_COS_SECRET_ID,
        SecretKey=settings.TENCENT_COS_SECRET_KEY,
        Scheme="https",
    )
    return CosS3Client(config)


def _get_cos_vector_client():
    """COS 向量桶客户端"""
    config = CosConfig(
        Region="ap-guangzhou",
        SecretId=settings.TENCENT_COS_SECRET_ID,
        SecretKey=settings.TENCENT_COS_SECRET_KEY,
        Endpoint=VECTOR_ENDPOINT,
        Scheme="https",
    )
    return CosVectorsClient(config)


def list_gaokao_s3_keys() -> list:
    """列出 COS 文件桶中所有 gaokao 相关 key"""
    client = _get_cos_file_client()
    all_keys = []
    marker = ""

    while True:
        resp = client.list_objects(
            Bucket=settings.TENCENT_COS_BUCKET,
            Prefix=S3_PREFIX,
            Marker=marker,
            MaxKeys=1000,
        )
        contents = resp.get("Contents", [])
        for obj in contents:
            key = obj["Key"]
            if key.endswith(".json"):
                # 提取向量 key: public/kb/gaokao/数学/2024_新课标_1.json
                # -> gaokao/数学/2024_新课标_1
                vector_key = key[len("public/kb/"):-len(".json")]
                all_keys.append(vector_key)

        if not resp.get("IsTruncated") or resp.get("IsTruncated") == "false":
            break
        marker = resp.get("NextMarker", "")
        if not marker and contents:
            marker = contents[-1]["Key"]

    logger.info("Found %d gaokao keys in S3 bucket", len(all_keys))
    return all_keys


def get_vectors_batch(client: CosVectorsClient, keys: list) -> list:
    """从 idx-public-kb 批量获取向量"""
    try:
        resp_headers, resp_data = client.get_vectors(
            Bucket=VECTOR_BUCKET,
            Index=SOURCE_INDEX,
            Keys=keys,
            ReturnData=True,
            ReturnMetaData=True,
        )
        vectors = resp_data.get("vectors", [])
        return vectors
    except AttributeError:
        # get_vectors 可能不存在于旧版 SDK，回退逐个 query
        logger.warning("get_vectors not available, falling back to query_vectors")
        return []
    except Exception as e:
        logger.error("get_vectors failed: %s", e)
        return []


def ensure_index(client: CosVectorsClient, index: str):
    """确保目标 index 存在"""
    try:
        _, data = client.get_index(Bucket=VECTOR_BUCKET, Index=index)
        if data and isinstance(data, dict) and "indexName" in data:
            logger.info("Index %s already exists", index)
            return
    except Exception:
        pass

    try:
        client.create_index(
            Bucket=VECTOR_BUCKET,
            Index=index,
            DataType="float32",
            Dimension=1024,
            DistanceMetric="cosine",
        )
        logger.info("Created index %s", index)
    except Exception as e:
        logger.warning("create_index %s failed (may already exist): %s", index, e)


def put_vectors_batch(client: CosVectorsClient, vectors: list) -> int:
    """批量写入向量到 idx-public-gaokao-kb"""
    try:
        client.put_vectors(
            Bucket=VECTOR_BUCKET,
            Index=TARGET_INDEX,
            Vectors=vectors,
        )
        return len(vectors)
    except Exception as e:
        logger.error("put_vectors failed: %s", e)
        return 0


async def migrate(batch_size: int = 50, dry_run: bool = False):
    """执行迁移"""
    # 1. 列出所有 gaokao S3 key
    vector_keys = list_gaokao_s3_keys()
    if not vector_keys:
        logger.warning("No gaokao keys found in S3")
        return

    vec_client = _get_cos_vector_client()

    # 2. 确保目标 index 存在
    if not dry_run:
        ensure_index(vec_client, TARGET_INDEX)

    # 3. 分批获取向量并写入
    total_migrated = 0
    total_batches = (len(vector_keys) + batch_size - 1) // batch_size

    for i in range(0, len(vector_keys), batch_size):
        batch_keys = vector_keys[i:i + batch_size]
        batch_num = i // batch_size + 1
        logger.info("Batch %d/%d: processing %d keys", batch_num, total_batches, len(batch_keys))

        if dry_run:
            logger.info("DRY RUN: would fetch and migrate %d keys", len(batch_keys))
            for k in batch_keys[:5]:
                logger.info("  Example key: %s", k)
            if len(batch_keys) > 5:
                logger.info("  ... and %d more", len(batch_keys) - 5)
            continue

        # 获取向量
        vectors = get_vectors_batch(vec_client, batch_keys)
        if not vectors:
            logger.warning("Batch %d: no vectors returned, trying alternative method", batch_num)
            # Fallback: re-embed text from S3 and insert
            migrated = await _fallback_migrate_batch(batch_keys, TARGET_INDEX, "gaokao-bench")
            total_migrated += migrated
            continue

        # 过滤：只迁移 metadata.source 匹配 gaokao 的向量
        gaokao_vectors = []
        for v in vectors:
            meta = v.get("metadata", {})
            source = meta.get("source", "")
            if source and (source in GAOKAO_SOURCES or source.startswith("gaokao")):
                gaokao_vectors.append(v)

        skipped = len(vectors) - len(gaokao_vectors)
        if skipped:
            logger.info("Batch %d: filtered %d non-gaokao vectors", batch_num, skipped)

        if gaokao_vectors:
            count = put_vectors_batch(vec_client, gaokao_vectors)
            total_migrated += count
            logger.info("Batch %d: migrated %d vectors", batch_num, count)

    logger.info("=" * 50)
    logger.info("Migration complete: %d vectors migrated to %s", total_migrated, TARGET_INDEX)

    if not dry_run:
        # 4. 验证
        await validate(TARGET_INDEX, total_migrated)


async def _fallback_migrate_batch(keys: list, target_index: str, source_value: str, cos_prefix: str = '') -> int:
    """回退方案：从 COS 文件桶读取文本，重新 embedding 并写入目标 index"""
    from services.vector_search_service import VectorSearchService

    s3_client = _get_cos_file_client()
    vs = VectorSearchService()

    items = []
    for key in keys:
        cos_path = f"{cos_prefix}public/kb/{key}.json"
        try:
            resp = s3_client.get_object(
                Bucket=settings.TENCENT_COS_BUCKET,
                Key=cos_path,
            )
            body = resp["Body"].get_raw_stream().read()
            data = json.loads(body)
            text = data.get("text", "")
            if text:
                items.append({
                    "key": key,
                    "text": text,
                    "metadata": {
                        "text": text,
                        "citation": data.get("citation", ""),
                        "source": source_value,
                    },
                })
        except Exception as e:
            logger.warning("Failed to read S3 %s: %s", cos_path, e)

    if items:
        await vs.index_batch(items, target_index)
        logger.info("Fallback: indexed %d vectors to %s", len(items), target_index)

    return len(items)


async def validate(index: str, expected: int):
    """验证迁移结果 —— 采样查询确认数据存在"""
    from services.vector_search_service import VectorSearchService

    vs = VectorSearchService()
    test_queries = [
        "高考数学二次函数",
        "高考物理牛顿定律",
        "高考语文阅读理解",
    ]

    logger.info("Validating migration with %d test queries...", len(test_queries))
    found_total = 0
    for q in test_queries:
        results = await vs.search(q, index=index, top_k=20)
        gaokao_count = sum(
            1 for r in results
            if r.get("metadata", {}).get("source", "").startswith("gaokao")
        )
        found_total += gaokao_count
        logger.info("  Query '%s': %d gaokao results in top-20", q[:30], gaokao_count)

    avg = found_total / len(test_queries) if test_queries else 0
    logger.info("Validation: avg %.1f gaokao results per query (target index has %d vectors)", avg, expected)
    if avg < 1:
        logger.warning("Validation FAILED: no gaokao results found in %s!", index)
    else:
        logger.info("Validation PASSED: gaokao data confirmed in %s", index)


TEXTBOOK_S3_PREFIX = "public/kb/textbooks/"
TEXTBOOK_TARGET_INDEX = "idx-public-textbook-kb"


def list_textbook_s3_keys() -> list:
    """列出 COS 文件桶中所有 textbook 相关 key"""
    client = _get_cos_file_client()
    all_keys = []
    marker = ""
    cos_prefix = (settings.STORAGE_PREFIX or "")
    full_prefix = cos_prefix + TEXTBOOK_S3_PREFIX
    strip_prefix = cos_prefix + "public/kb/"

    while True:
        resp = client.list_objects(
            Bucket=settings.TENCENT_COS_BUCKET,
            Prefix=full_prefix,
            Marker=marker,
            MaxKeys=1000,
        )
        contents = resp.get("Contents", [])
        for obj in contents:
            key = obj["Key"]
            if key.endswith(".json"):
                vector_key = key[len(strip_prefix):-len(".json")]
                all_keys.append(vector_key)

        if not resp.get("IsTruncated") or resp.get("IsTruncated") == "false":
            break
        marker = resp.get("NextMarker", "")
        if not marker and contents:
            marker = contents[-1]["Key"]

    logger.info("Found %d textbook keys in S3 bucket", len(all_keys))
    return all_keys


async def migrate_textbooks(batch_size: int = 50, dry_run: bool = False):
    """执行教材迁移：从 idx-public-kb 分离 textbooks/ 数据到 idx-public-textbook-kb"""
    vector_keys = list_textbook_s3_keys()
    if not vector_keys:
        logger.warning("No textbook keys found in S3")
        return

    vec_client = _get_cos_vector_client()

    if not dry_run:
        ensure_index(vec_client, TEXTBOOK_TARGET_INDEX)

    total_migrated = 0
    total_batches = (len(vector_keys) + batch_size - 1) // batch_size

    for i in range(0, len(vector_keys), batch_size):
        batch_keys = vector_keys[i:i + batch_size]
        batch_num = i // batch_size + 1
        logger.info("Batch %d/%d: processing %d keys", batch_num, total_batches, len(batch_keys))

        if dry_run:
            logger.info("DRY RUN: would fetch and migrate %d keys", len(batch_keys))
            for k in batch_keys[:5]:
                logger.info("  Example key: %s", k)
            if len(batch_keys) > 5:
                logger.info("  ... and %d more", len(batch_keys) - 5)
            continue

        vectors = get_vectors_batch(vec_client, batch_keys)
        if not vectors:
            logger.warning("Batch %d: no vectors returned, trying fallback", batch_num)
            cos_prefix = settings.STORAGE_PREFIX or ""
            migrated = await _fallback_migrate_batch(batch_keys, TEXTBOOK_TARGET_INDEX, "textbook", cos_prefix=cos_prefix)
            total_migrated += migrated
            continue

        count = put_vectors_batch(vec_client, vectors)
        total_migrated += count
        logger.info("Batch %d: migrated %d vectors", batch_num, count)

    logger.info("=" * 50)
    logger.info("Textbook migration complete: %d vectors migrated to %s", total_migrated, TEXTBOOK_TARGET_INDEX)

    if not dry_run:
        await validate(TEXTBOOK_TARGET_INDEX, total_migrated)


async def main():
    parser = argparse.ArgumentParser(description="迁移 gaokao / textbook 数据到独立 index")
    parser.add_argument("--dry-run", action="store_true", help="只显示将要迁移的 key，不实际写入")
    parser.add_argument("--batch-size", type=int, default=50, help="每批处理的 key 数量")
    parser.add_argument("--textbook", action="store_true", help="迁移教材数据（默认迁移高考数据）")
    args = parser.parse_args()

    if args.textbook:
        await migrate_textbooks(batch_size=args.batch_size, dry_run=args.dry_run)
    else:
        await migrate(batch_size=args.batch_size, dry_run=args.dry_run)


if __name__ == "__main__":
    asyncio.run(main())
