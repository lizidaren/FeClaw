"""list_words.py — 词汇表数据脚本（供 code 类型路由调用）

通过环境变量 QUERY_JSON 接收请求参数，输出 JSON 到 stdout。

示例用法：
  QUERY_JSON='{"page": 1, "limit": 20}' python3 list_words.py

如果 bwrap 沙箱不可用，也可通过管道测试：echo '{"page": 1}' | python3 list_words.py
"""

import json, os, sys

# 硬编码常用 SAT/TOEFL 词汇
WORD_LIST = [
    {"word": "aberration", "pos": "n.", "meaning": "偏离常规的行为"},
    {"word": "benevolent", "pos": "adj.", "meaning": "仁慈的，善良的"},
    {"word": "cryptic", "pos": "adj.", "meaning": "神秘的，难以理解的"},
    {"word": "dubious", "pos": "adj.", "meaning": "可疑的，不确定的"},
    {"word": "eloquent", "pos": "adj.", "meaning": "口才好的，有说服力的"},
    {"word": "futile", "pos": "adj.", "meaning": "无效的，无用的"},
    {"word": "gregarious", "pos": "adj.", "meaning": "爱社交的，群居的"},
    {"word": "hoist", "pos": "v.", "meaning": "举起，吊起"},
    {"word": "immutable", "pos": "adj.", "meaning": "不变的，永恒的"},
    {"word": "jovial", "pos": "adj.", "meaning": "快乐的，友好的"},
    {"word": "kinetic", "pos": "adj.", "meaning": "运动的，动力的"},
    {"word": "lethargic", "pos": "adj.", "meaning": "昏昏欲睡的，无精打采的"},
    {"word": "mundane", "pos": "adj.", "meaning": "平凡的，世俗的"},
    {"word": "naive", "pos": "adj.", "meaning": "天真的，幼稚的"},
    {"word": "obsolete", "pos": "adj.", "meaning": "过时的，废弃的"},
    {"word": "pragmatic", "pos": "adj.", "meaning": "务实的，实用的"},
    {"word": "quaint", "pos": "adj.", "meaning": "古雅的，别致的"},
    {"word": "resilient", "pos": "adj.", "meaning": "有弹性的，坚韧的"},
    {"word": "succinct", "pos": "adj.", "meaning": "简洁的，精炼的"},
    {"word": "tenacious", "pos": "adj.", "meaning": "顽强的，坚韧不拔的"},
    {"word": "ubiquitous", "pos": "adj.", "meaning": "无处不在的"},
    {"word": "versatile", "pos": "adj.", "meaning": "多才多艺的，多功能的"},
    {"word": "whimsical", "pos": "adj.", "meaning": "异想天开的，反复无常的"},
    {"word": "yearning", "pos": "n.", "meaning": "渴望，思念"},
    {"word": "zeal", "pos": "n.", "meaning": "热情，热忱"},
    {"word": "affinity", "pos": "n.", "meaning": "喜好，亲和力"},
    {"word": "brevity", "pos": "n.", "meaning": "简洁，短暂"},
    {"word": "candor", "pos": "n.", "meaning": "坦诚，直率"},
    {"word": "diligent", "pos": "adj.", "meaning": "勤奋的，勤勉的"},
    {"word": "empathy", "pos": "n.", "meaning": "共情，同理心"},
]


def handle_request(params: dict) -> dict:
    """处理请求参数，返回分页的词汇列表"""
    page = int(params.get("page", 1))
    limit = int(params.get("limit", 20))
    search = params.get("search", "").strip().lower()

    # 搜索过滤
    filtered = WORD_LIST
    if search:
        filtered = [w for w in WORD_LIST if search in w["word"].lower() or search in w["meaning"].lower()]

    # 分页
    total = len(filtered)
    start = (page - 1) * limit
    end = start + limit
    items = filtered[start:end]

    return {
        "total": total,
        "page": page,
        "limit": limit,
        "total_pages": (total + limit - 1) // limit,
        "items": items,
    }


if __name__ == "__main__":
    # 读取输入
    query_json = os.environ.get("QUERY_JSON", "")
    if not query_json:
        query_json = sys.stdin.read()

    params = {}
    if query_json:
        try:
            params = json.loads(query_json)
        except json.JSONDecodeError:
            pass

    result = handle_request(params)
    print(json.dumps(result, ensure_ascii=False, indent=2))
