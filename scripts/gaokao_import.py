#!/usr/bin/env python3
"""
GAOKAO-Bench 数据集导入脚本

流程：
  1. 读取 GAOKAO-Bench JSON 文件
  2. 拼接标注出处的完整文本 (【YEAR·高考·SUBJECT·CATEGORY】)
  3. Embedding (Qwen text-embedding-v4, 1024d)
  4. 写入 COS 向量桶 idx-public-kb + COS 文件桶 public/kb/gaokao/

用法：
  python scripts/gaokao_import.py [--limit 50] [--data-dir /tmp/gaokao-bench/Data]
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import List, Dict, Any

# 确保能找到 FeClaw 项目根目录
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import settings
from services.vector_search_service import VectorSearchService

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ============================================================
# 科目 / 题型 中文名映射
# ============================================================

SUBJECT_MAP = {
    # 客观题
    "Math_I_MCQs":                ("数学", "理科数学选择题"),
    "Math_II_MCQs":               ("数学", "文科数学选择题"),
    "Physics_MCQs":               ("物理", "选择题"),
    "Chemistry_MCQs":             ("化学", "选择题"),
    "Biology_MCQs":               ("生物", "选择题"),
    "History_MCQs":               ("历史", "选择题"),
    "Geography_MCQs":             ("地理", "选择题"),
    "Political_Science_MCQs":     ("政治", "选择题"),
    "Chinese_Lang_and_Usage_MCQs": ("语文", "语言文字运用"),
    "Chinese_Modern_Lit":         ("语文", "现代文阅读"),
    "English_MCQs":               ("英语", "单项选择"),
    "English_Fill_in_Blanks":     ("英语", "语法填空"),
    "English_Reading_Comp":       ("英语", "阅读理解"),
    "English_Cloze_Test":         ("英语", "完形填空（七选五）"),
    # 主观题
    "Math_I_Open-ended_Questions":     ("数学", "理科数学解答题"),
    "Math_I_Fill-in-the-Blank":        ("数学", "理科数学填空题"),
    "Math_II_Open-ended_Questions":    ("数学", "文科数学解答题"),
    "Math_II_Fill-in-the-Blank":       ("数学", "文科数学填空题"),
    "Physics_Open-ended_Questions":    ("物理", "解答题"),
    "Chemistry_Open-ended_Questions":  ("化学", "解答题"),
    "Biology_Open-ended_Questions":    ("生物", "解答题"),
    "History_Open-ended_Questions":    ("历史", "解答题"),
    "Geography_Open-ended_Questions":  ("地理", "解答题"),
    "Political_Science_Open-ended_Questions": ("政治", "解答题"),
    "Chinese_Language_Literary_Text_Reading":       ("语文", "文学类文本阅读"),
    "Chinese_Language_Practical_Text_Reading":       ("语文", "实用类文本阅读"),
    "Chinese_Language_Classical_Chinese_Reading":    ("语文", "文言文阅读"),
    "Chinese_Language_Ancient_Poetry_Reading":       ("语文", "古代诗歌阅读"),
    "Chinese_Language_Famous_Passages_and_Sentences_Dictation": ("语文", "名篇名句默写"),
    "Chinese_Language_Language_and_Writing_Skills_Open-ended_Questions": ("语文", "语言文字运用（主观）"),
    "English_Language_Error_Correction": ("英语", "短文改错"),
    "English_Language_Cloze_Passage":    ("英语", "完形填空"),
}


# ============================================================
# 类别名规范化
# ============================================================

CATEGORY_NORMALIZE = {
    "（新课标）": "新课标",
    "（全国甲卷）": "全国甲卷",
    "（全国乙卷）": "全国乙卷",
    "全国甲卷理科": "全国甲卷·理科",
    "全国甲卷文科": "全国甲卷·文科",
    "全国乙卷理科": "全国乙卷·理科",
    "全国乙卷文科": "全国乙卷·文科",
    "新课标Ⅰ卷": "新课标Ⅰ卷",
    "新课标Ⅱ卷": "新课标Ⅱ卷",
    "新课标III卷": "新课标Ⅲ卷",
    "全国Ⅰ卷": "全国Ⅰ卷",
    "全国Ⅱ卷": "全国Ⅱ卷",
    "全国III卷": "全国Ⅲ卷",
    "浙江卷": "浙江卷",
    "天津卷": "天津卷",
    "北京卷": "北京卷",
    "上海卷": "上海卷",
}


def normalize_category(cat: str) -> str:
    """规范化卷类别名"""
    cat = cat.strip()
    for k, v in CATEGORY_NORMALIZE.items():
        if k in cat:
            return v
    # fallback: 去掉括号
    return cat.replace("（", "").replace("）", "")


def get_subject_name(filename: str) -> tuple:
    """从文件名推断科目和题型"""
    stem = Path(filename).stem
    # 去掉年份前缀 "2010-2022_" 或 "2010-2013_" 等
    parts = stem.split("_", 1)
    if len(parts) > 1:
        stem = parts[1]
    # 尝试匹配
    for pattern, (subj, qtype) in SUBJECT_MAP.items():
        if stem == pattern or stem.endswith(pattern):
            return subj, qtype
    return "未知", "未知"


# ============================================================
# 数据解析与格式化
# ============================================================

def build_citation(year: str, category: str, subject: str) -> str:
    """构建引用头，如 【2010·高考数学·全国新课标卷】"""
    cat = normalize_category(category)
    return f"【{year}·高考{subject}·{cat}】"


def build_full_text(item: Dict[str, Any], subject: str, qtype: str) -> str:
    """构建用于 embedding 和展示的完整文本"""
    citation = build_citation(item["year"], item["category"], subject)
    question = item["question"].strip()
    answer = item.get("answer", [])
    analysis = item.get("analysis", "").strip()

    parts = [citation]

    # 题目
    parts.append(question)

    # 答案
    if answer:
        if isinstance(answer, list):
            ans_str = " ".join(str(a).strip() for a in answer)
        else:
            ans_str = str(answer).strip()
        parts.append(f"答案：{ans_str}")

    # 解析
    if analysis:
        parts.append(f"解析：{analysis}")

    return "\n".join(parts)


def find_question_start(text: str) -> int:
    """在 question 字段中找到题干开始的起始位置
    
    英语阅读/完形的题目模式: \n28. 或 \n50.
    语文现代文题目模式: \n（1）或 \n(1)
    语文文言文题目模式: \n（1）
    """
    import re
    # 匹配英语题号: newline + digit(s) + . + space
    m = re.search(r'\n\d{1,2}\.\s', text)
    if m:
        return m.start()
    # 匹配中文题号: newline + （数字）
    m = re.search(r'\n[（(]\d[）)]', text)
    if m:
        return m.start()
    return -1


def build_injection_text(item: dict, subject: str, qtype: str,
                          max_head: int = 400, max_tail: int = 300) -> str:
    """智能构建注入文本
    
    策略：
      - 短题（<= 600 字）：全文保留
      - 长题：
        1. citation 始终保留
        2. question 字段：检测题干位置 = 文章开头 + 全部题干 + 选项
        3. answer 始终保留
        4. analysis 截取 tail 部分
    """
    citation = build_citation(item["year"], item["category"], subject)
    question = item["question"].strip()
    answer = item.get("answer", [])
    analysis = item.get("analysis", "").strip()

    ans_str = ""
    if answer:
        if isinstance(answer, list):
            ans_str = " ".join(str(a).strip() for a in answer)
        else:
            ans_str = str(answer).strip()

    # 组装完整文本，判断长度
    full_parts = [citation, question]
    if ans_str:
        full_parts.append(f"答案：{ans_str}")
    if analysis:
        full_parts.append(f"解析：{analysis}")
    full_text = "\n".join(full_parts)

    if len(full_text) <= 600:
        return full_text

    # ---- 长题处理 ----
    # 1. 在 question 字段内找题干起始
    q_start = find_question_start(question)
    if q_start > 0 and q_start < len(question) - 200:
        # 可分离 passage 和题干：passage 部分截取头，题干全保留
        passage = question[:q_start].strip()
        exam_qs = question[q_start:].strip()
        # 截取 passage 开头
        if len(passage) > max_head:
            passage = passage[:max_head] + "\n……（文章中间省略）……"
        # 题干过长也截取最后部分（题干通常在末尾）
        if len(exam_qs) > max_tail * 2:
            # 题干也取头尾
            head_qs = exam_qs[:max_head]
            tail_qs = exam_qs[-max_tail:]
            exam_qs = f"{head_qs}\n……（题目中间省略）……\n{tail_qs}"
        result = f"{citation}\n{passage}\n{exam_qs}"
    else:
        # 无法检测题干位置，退化为通用 head+tail
        head = full_text[:max_head]
        tail = full_text[-max_tail:]
        result = f"{citation}\n{head}\n……（中间省略，可调取知识库工具查看全文）……\n{tail}"

    # 2. 添加答案和解析（解析仅尾）
    if ans_str:
        result += f"\n\n答案：{ans_str}"
    if analysis:
        if len(analysis) > 500:
            result += f"\n解析：{analysis[:500]}"
        else:
            result += f"\n解析：{analysis}"

    return result


def get_category_short(cat: str) -> str:
    """获取简短的卷别代号，用于 key 命名"""
    n = normalize_category(cat)
    # 简化
    n = n.replace("·", "_").replace("Ⅰ", "1").replace("Ⅱ", "2").replace("Ⅲ", "3")
    return n


# ============================================================
# COS 文件桶操作
# ============================================================

def _get_cos_file_client():
    """获取用于普通文件存储的 COS Client"""
    from qcloud_cos import CosConfig, CosS3Client

    config = CosConfig(
        Region=settings.TENCENT_COS_REGION,
        SecretId=settings.TENCENT_COS_SECRET_ID,
        SecretKey=settings.TENCENT_COS_SECRET_KEY,
        Scheme="https",
    )
    return CosS3Client(config)


def upload_to_file_bucket(key: str, data: dict) -> bool:
    """上传完整数据到 COS 文件桶"""
    try:
        client = _get_cos_file_client()
        cos_path = f"public/kb/{key}.json"
        client.put_object(
            Bucket=settings.TENCENT_COS_BUCKET,
            Key=cos_path,
            Body=json.dumps(
                {"text": data["text"], "citation": data["citation"], "key": key},
                ensure_ascii=False,
            ).encode("utf-8"),
            ContentType="application/json; charset=utf-8",
        )
        logger.debug("Uploaded to COS: %s", cos_path)
        return True
    except Exception as e:
        logger.error("Failed to upload %s to COS: %s", key, e)
        return False


# ============================================================
# 主处理逻辑
# ============================================================

async def process_file(
    filepath: str,
    vs: VectorSearchService,
    limit: int = None,
    start: int = 0,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """处理单个数据文件"""
    filename = Path(filepath).name
    subject, qtype = get_subject_name(filename)

    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    examples = data.get("example", [])
    if not examples:
        logger.warning("No examples in %s", filename)
        return {"file": filename, "subject": subject, "count": 0}

    # 切片
    total = len(examples)
    batch = examples[start:]
    if limit:
        batch = batch[:limit]

    logger.info(
        "File: %s | Subject: %s-%s | Total: %d | Processing: %d-%d",
        filename, subject, qtype, total, start, start + len(batch),
    )

    # 准备数据
    items_to_index: List[dict] = []  # for vector search
    file_uploads: List[tuple] = []  # for file bucket

    for item in batch:
        year = item["year"]
        category_short = get_category_short(item["category"])
        idx = item["index"]

        # 生成唯一 key
        key = f"gaokao/{subject}/{year}_{category_short}_{idx}"
        key = key.replace(" ", "_")

        # 构建完整文本（含引用头）
        full_text = build_full_text(item, subject, qtype)
        injection_preview = build_injection_text(item, subject, qtype)
        citation = build_citation(year, item["category"], subject)

        items_to_index.append({
            "key": key,
            "text": full_text,
            "metadata": {
                "text": full_text,
                "citation": citation,
                "subject": subject,
                "qtype": qtype,
                "year": year,
                "category": item["category"],
                "injection_preview": injection_preview,
                "source": "gaokao-bench",
            },
        })

        file_uploads.append((key, {
            "text": full_text,
            "citation": citation,
            "metadata": {
                "subject": subject,
                "qtype": qtype,
                "year": year,
                "category": item["category"],
            },
        }))

    if dry_run:
        logger.info("DRY RUN: would index %d items, upload %d files", len(items_to_index), len(file_uploads))
        # 打印前 3 条预览
        for item in items_to_index[:3]:
            print(f"\n{'='*60}")
            print(f"Key: {item['key']}")
            print(f"Citation: {item['metadata']['citation']}")
            print(f"Text length: {len(item['text'])} chars")
            preview = item['metadata']['injection_preview']
            if len(preview) > 500:
                preview = preview[:250] + "\n..." + preview[-250:]
            print(f"Injection preview:\n{preview}")
        return {"file": filename, "subject": subject, "count": len(items_to_index)}

    # ---- 第 1 步：写入 COS 文件桶 ----
    uploaded = 0
    for key, data in file_uploads:
        if upload_to_file_bucket(key, data):
            uploaded += 1
    logger.info("Uploaded %d/%d files to COS bucket", uploaded, len(file_uploads))

    # ---- 第 2 步：索引到向量桶 idx-public-kb ----
    index_name = "idx-public-kb"
    await vs.index_batch(items_to_index, index_name)
    logger.info("Indexed %d vectors to %s", len(items_to_index), index_name)

    return {"file": filename, "subject": subject, "count": len(items_to_index)}


async def main():
    parser = argparse.ArgumentParser(description="GAOKAO-Bench 数据导入工具")
    parser.add_argument(
        "--data-dir",
        default="/tmp/gaokao-bench/Data",
        help="GAOKAO-Bench 数据目录",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="每个文件最多处理多少条（默认全部）",
    )
    parser.add_argument(
        "--start",
        type=int,
        default=0,
        help="跳过前 N 条（从第 N 条开始）",
    )
    parser.add_argument(
        "--files",
        nargs="+",
        help="指定处理哪些文件（文件名关键词，如 'Math_I_MCQs'），不指定则处理全部",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只预览不写入",
    )
    parser.add_argument(
        "--updates-dir",
        default="/tmp/gaokao-bench-updates/Data",
        help="GAOKAO-Bench-Updates 数据目录（2023-2024）",
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.exists():
        logger.error("数据目录不存在: %s", data_dir)
        sys.exit(1)

    # 收集所有 JSON 文件
    json_files = []
    for root, _, files in os.walk(data_dir):
        for f in files:
            if f.endswith(".json"):
                json_files.append(os.path.join(root, f))

    # 也收集 Updates 目录（2023-2024）
    updates_dir = Path(args.updates_dir)
    if updates_dir.exists():
        for root, _, files in os.walk(updates_dir):
            for f in files:
                if f.endswith(".json"):
                    json_files.append(os.path.join(root, f))

    # 按文件名排序，确保可复现
    json_files.sort()

    # 过滤
    if args.files:
        filtered = []
        for f in json_files:
            fname = Path(f).name
            if any(kw in fname for kw in args.files):
                filtered.append(f)
        json_files = filtered
        logger.info("Filtered to %d files matching keywords: %s", len(json_files), args.files)

    logger.info("Found %d JSON files to process", len(json_files))

    # 初始化向量搜索服务（无 agent_hash = 公共知识库）
    vs = VectorSearchService()

    total_processed = 0
    for fp in json_files:
        try:
            result = await process_file(fp, vs, limit=args.limit, start=args.start, dry_run=args.dry_run)
            total_processed += result["count"]
        except Exception as e:
            logger.error("Failed to process %s: %s", fp, e, exc_info=True)

    logger.info("=" * 50)
    logger.info("Done! Total items processed: %d", total_processed)
    if args.dry_run:
        logger.info("This was a DRY RUN. Run without --dry-run to actually index.")


if __name__ == "__main__":
    asyncio.run(main())
