#!/usr/bin/env python3
"""
Zentrim API 端到端测试

用法: python3 tests/test_zentrim_api.py [--base-url http://localhost:8080]

测试覆盖:
  - 用户认证 (login / 401)
  - ZentrimEntries CRUD
  - ZentrimBlocks 操作
  - 归档/取消归档
  - 硬删除 + 级联清理
  - 分页查询
  - 边界条件（空参数、不存在的 ID、未认证）
"""

import sys
import json
import time
import argparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError

PASS = "✅"
FAIL = "❌"
SKIP = "⏭️"

parser = argparse.ArgumentParser()
parser.add_argument("--base-url", default="http://localhost:8080")
parser.add_argument("--username", default="test")
parser.add_argument("--password", default="test")
args = parser.parse_args()
BASE = args.base_url.rstrip("/")


# ── HTTP 工具 ──────────────────────────────────────────────────────

def _req(method, path, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body else None
    req = Request(f"{BASE}{path}", data=data, headers=headers, method=method)
    try:
        with urlopen(req) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except HTTPError as e:
        raw = e.read()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw": raw.decode(), "_status": e.code}
    except Exception as e:
        return {"_error": str(e)}


def ok(condition, msg):
    print(f"  {PASS if condition else FAIL} {msg}")
    if not condition:
        errors[0] += 1
    return condition

errors = [0]

# 在 report 辅助变量
_test_items = []

def test(name):
    _test_items.append(name)
    print(f"\n── {name} ──")

def check(label, actual, expected=None, predicate=None):
    if predicate:
        cond = predicate(actual)
    elif expected is not None:
        cond = (actual == expected)
    else:
        cond = bool(actual)
    ok(cond, f"{label}: {_brief(actual)}")
    return cond

def _brief(v, maxlen=120):
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)[:maxlen]
    if isinstance(v, list):
        return f"[{len(v)} items]" if len(v) > 3 else json.dumps(v, ensure_ascii=False)[:maxlen]
    return str(v)[:maxlen]


# ═══════════════════════════════════════════════════════════════════
#  1. 认证
# ═══════════════════════════════════════════════════════════════════

test("1. 用户认证")

def login(u, p):
    return _req("POST", "/api/user/login", {"username": u, "password": p})

res = login(args.username, args.password)
check("登录 (正确凭据)", res,
      predicate=lambda r: r.get("status") == "success" and "token" in r)
TOKEN = res.get("token", "")
USER_ID = res.get("user_id")

res = login("wrong", "wrong")
check("登录 (错误凭据)", res,
      predicate=lambda r: "错误" in json.dumps(r, ensure_ascii=False))

res = login("", "")
check("登录 (空用户名)", res,
      predicate=lambda r: "不能为空" in json.dumps(r, ensure_ascii=False))

# 登录后检查 token 形状：JWT 必须是三段式
if TOKEN:
    parts = TOKEN.split(".")
    check("JWT 三段式", len(parts) == 3)


# ═══════════════════════════════════════════════════════════════════
#  2. 未认证访问
# ═══════════════════════════════════════════════════════════════════

test("2. 未认证访问")

res = _req("GET", "/api/zentrim/entries")
check("无 token 查条目 → 401", res,
      predicate=lambda r: "unauthorized" in json.dumps(r, ensure_ascii=False).lower())

res = _req("POST", "/api/zentrim/entries", {"title": "x"})
check("无 token 创建条目 → 401", res,
      predicate=lambda r: "unauthorized" in json.dumps(r, ensure_ascii=False).lower())

res = _req("DELETE", f"/api/zentrim/entries/nonexistent")
check("无 token 删除 → 401", res,
      predicate=lambda r: "unauthorized" in json.dumps(r, ensure_ascii=False).lower())


# ═══════════════════════════════════════════════════════════════════
#  3. ZentrimEntries CRUD
# ═══════════════════════════════════════════════════════════════════

test("3. 条目操作 (CRUD)")

# 创建
res = _req("POST", "/api/zentrim/entries", {
    "title": "氧化还原反应总结",
    "tags": ["化学", "高考"]
}, token=TOKEN)
ENTRY_ID = res.get("id", "")
check("创建条目", res, predicate=lambda r: "id" in r and r.get("title") == "氧化还原反应总结")
check("  返回 user_id", res.get("user_id") == USER_ID)
check("  默认 status", res.get("status") == "active")
check("  返回 created_at", "created_at" in res)

# 创建一堆条目（用于分页测试）
for i in range(5):
    _req("POST", "/api/zentrim/entries", {"title": f"批量条目 #{i}", "tags": ["bulk"]}, token=TOKEN)

# 查询列表
res = _req("GET", "/api/zentrim/entries?page=1&page_size=3", token=TOKEN)
check("分页查询 (page=1, size=3)", res,
      predicate=lambda r: isinstance(r, list))

res = _req("GET", "/api/zentrim/entries?page=2&page_size=3", token=TOKEN)
check("分页查询 (page=2, size=3)", res,
      predicate=lambda r: isinstance(r, list))

res = _req("GET", "/api/zentrim/entries?page_size=100", token=TOKEN)
check("分页查询 (size=100)", res,
      predicate=lambda r: isinstance(r, list) and len(r) >= 6)
check("  结果按时间倒序", res,
      predicate=lambda r: not r or r[0].get("created_at", "") >= r[-1].get("created_at", ""))

# 获取详情
res = _req("GET", f"/api/zentrim/entries/{ENTRY_ID}", token=TOKEN)
check("获取条目详情", res, predicate=lambda r: r.get("id") == ENTRY_ID)
check("  含标题", res.get("title") == "氧化还原反应总结")
check("  含 tags", "tags" in res)

# 获取不存在的条目
res = _req("GET", "/api/zentrim/entries/does_not_exist_12345678", token=TOKEN)
check("获取不存在的条目 → 404", res,
      predicate=lambda r: "not found" in json.dumps(r, ensure_ascii=False).lower())

# 更新
res = _req("PATCH", f"/api/zentrim/entries/{ENTRY_ID}", {
    "title": "氧化还原反应总结 (修订版)",
    "tags": ["化学", "高考", "修订"]
}, token=TOKEN)
check("更新条目", res, predicate=lambda r: r.get("title", "").find("修订版") >= 0)

res = _req("GET", f"/api/zentrim/entries/{ENTRY_ID}", token=TOKEN)
check("  更新持久化验证", res, predicate=lambda r: "修订版" in r.get("title", ""))


# ═══════════════════════════════════════════════════════════════════
#  4. 归档 / 取消归档
# ═══════════════════════════════════════════════════════════════════

test("4. 归档操作")

res = _req("POST", f"/api/zentrim/entries/{ENTRY_ID}/archive", token=TOKEN)
entry_after_archive = res.get("entry", res)
check("归档条目", entry_after_archive,
      predicate=lambda r: r.get("status") == "archived")
check("  含 archived_at", "archived_at" in entry_after_archive)

res = _req("POST", f"/api/zentrim/entries/{ENTRY_ID}/unarchive", token=TOKEN)
entry_after_ua = res.get("entry", res)
check("取消归档", entry_after_ua,
      predicate=lambda r: r.get("status") == "active")
check("  archived_at 清空", entry_after_ua.get("archived_at") is None)


# ═══════════════════════════════════════════════════════════════════
#  5. Blocks 操作 (核心测试)
# ═══════════════════════════════════════════════════════════════════

test("5. Blocks 操作")

# 创建新条目用于 blocks 测试
res = _req("POST", "/api/zentrim/entries", {"title": "Blocks 测试"}, token=TOKEN)
BLOCK_ENTRY_ID = res.get("id", "")

# PUT blocks (全量替换)
blocks_payload = {"blocks": [
    {"type": "text", "data": {"version": 1, "main": [{"text": "这是正文"}]},
     "text": "这是正文", "model_name": "text-embedding-v3"},
    {"type": "text", "data": {"version": 1, "main": [{"text": "第二段"}]},
     "text": "第二段"},
    {"type": "photo", "data": {"key": "cos://test/photo.jpg", "width": 1920, "height": 1080},
     "text": "一张测试照片", "model_name": "multimodal-v1"},
]}
res = _req("PUT", f"/api/zentrim/entries/{BLOCK_ENTRY_ID}/blocks", blocks_payload, token=TOKEN)
check("PUT blocks (全量替换)", res, predicate=lambda r: r is not None)

# 验证
res = _req("GET", f"/api/zentrim/entries/{BLOCK_ENTRY_ID}/blocks", token=TOKEN)
check("GET blocks", res, predicate=lambda r: "blocks" in r)
blocks = res.get("blocks", [])
check(f"  返回 {len(blocks)} 个 block", len(blocks) == 3)
check("  排序正确", blocks, predicate=lambda blk: blk[0]["sort_order"] == 0 and blk[2]["sort_order"] == 2)
check("  block.type 正确", blocks,
      predicate=lambda blk: [b["type"] for b in blk] == ["text", "text", "photo"])
check("  text/search 字段", blocks,
      predicate=lambda blk: blk[0]["text"] == "这是正文")
check("  model_name 正确", blocks,
      predicate=lambda blk: blk[0]["model_name"] == "text-embedding-v3" and blk[2]["model_name"] == "multimodal-v1")
check("  photo data 含 key", blocks,
      predicate=lambda blk: blk[2]["data"].get("key") == "cos://test/photo.jpg")

# 空 blocks 列表
res = _req("PUT", f"/api/zentrim/entries/{BLOCK_ENTRY_ID}/blocks", {"blocks": []}, token=TOKEN)
check("PUT 空 blocks", res, predicate=lambda r: r is not None)
res = _req("GET", f"/api/zentrim/entries/{BLOCK_ENTRY_ID}/blocks", token=TOKEN)
check("  验证空 blocks", res, predicate=lambda r: len(r.get("blocks", [])) == 0)

# 不存在的条目
res = _req("PUT", "/api/zentrim/entries/nonexistent_123/blocks", {"blocks": []}, token=TOKEN)
check("PUT blocks (不存在条目)", res,
      predicate=lambda r: "not found" in json.dumps(r, ensure_ascii=False).lower())


# ═══════════════════════════════════════════════════════════════════
#  6. 边界条件 & 恶意输入
# ═══════════════════════════════════════════════════════════════════

test("6. 边界条件")

# 标题超长
long_title = "A" * 1000
res = _req("POST", "/api/zentrim/entries", {"title": long_title}, token=TOKEN)
check("超长标题 → 412 (合理限制)", res, predicate=lambda r: "string_too_long" in json.dumps(r, ensure_ascii=False))

# 空标题
res = _req("POST", "/api/zentrim/entries", {"title": ""}, token=TOKEN)
check("空标题", res, predicate=lambda r: "id" in r)

# 空 body
res = _req("POST", "/api/zentrim/entries", {}, token=TOKEN)
check("空 body → validation error", res, predicate=lambda r: "Field required" in json.dumps(r, ensure_ascii=False) or "missing" in json.dumps(r, ensure_ascii=False))

# 恶意 tag
res = _req("POST", "/api/zentrim/entries", {
    "title": "XSS 测试",
    "tags": ["<script>alert(1)</script>", "'; DROP TABLE users;--"]
}, token=TOKEN)
check("恶意 tag (XSS/SQLi)", res, predicate=lambda r: "id" in r)
ENTRY_ID2 = res.get("id", "")

res = _req("GET", f"/api/zentrim/entries/{ENTRY_ID2}", token=TOKEN)
check("  恶意 tag 持久化", res,
      predicate=lambda r: "<script>" in json.dumps(r.get("tags", []), ensure_ascii=False))


# ═══════════════════════════════════════════════════════════════════
#  7. 搜索
# ═══════════════════════════════════════════════════════════════════

test("7. 搜索")

# 在 block 里加搜索词
res = _req("POST", "/api/zentrim/entries", {"title": "搜索测试条目"}, token=TOKEN)
SID = res.get("id", "")
_req("PUT", f"/api/zentrim/entries/{SID}/blocks", {"blocks": [
    {"type": "text", "data": {}, "text": "zirconium hafnium test", "model_name": "v3"},
    {"type": "text", "data": {}, "text": "atomic number 72"}
]}, token=TOKEN)

res = _req("GET", f"/api/zentrim/search?q=hafnium", token=TOKEN)
check("搜索关键词 hafnium", res, predicate=lambda r: isinstance(r, dict) and r.get("count", 0) >= 1)

res = _req("GET", f"/api/zentrim/search?q=nonexistent_keyword_xyz_999", token=TOKEN)
check("搜索不存在关键词", res, predicate=lambda r: isinstance(r, dict) and r.get("count", 0) == 0)


# ═══════════════════════════════════════════════════════════════════
#  8. 硬删除 + 级联清理
# ═══════════════════════════════════════════════════════════════════

test("8. 硬删除 & 级联清理")

# 创建一个带 blocks 的条目
res = _req("POST", "/api/zentrim/entries", {"title": "待删除条目"}, token=TOKEN)
DEL_ID = res.get("id", "")
_req("PUT", f"/api/zentrim/entries/{DEL_ID}/blocks", {"blocks": [
    {"type": "text", "data": {}, "text": "将被删除的 text"},
    {"type": "photo", "data": {"key": "cos://test/del.jpg"}, "text": "将被删除的 photo"}
]}, token=TOKEN)

res = _req("DELETE", f"/api/zentrim/entries/{DEL_ID}", token=TOKEN)
check("硬删除条目", res, predicate=lambda r: r is not None)

res = _req("GET", f"/api/zentrim/entries/{DEL_ID}", token=TOKEN)
check("  删除后 404", res,
      predicate=lambda r: "not found" in json.dumps(r, ensure_ascii=False).lower())

res = _req("GET", f"/api/zentrim/entries/{DEL_ID}/blocks", token=TOKEN)
check("  blocks 级联删除", res,
      predicate=lambda r: "not found" in json.dumps(r, ensure_ascii=False).lower())

# 删除不存在的条目
res = _req("DELETE", "/api/zentrim/entries/nonexistent_123456", token=TOKEN)
check("删除不存在条目", res,
      predicate=lambda r: "not found" in json.dumps(r, ensure_ascii=False).lower())


# ═══════════════════════════════════════════════════════════════════
#  9. 并发简单测试
# ═══════════════════════════════════════════════════════════════════

test("9. 并发（简单）")

import threading
_concurrent_errors = []
def _concurrent_create():
    try:
        _req("POST", "/api/zentrim/entries", {"title": "并发测试", "tags": ["concurrent"]}, token=TOKEN)
    except Exception as e:
        _concurrent_errors.append(str(e))

threads = [threading.Thread(target=_concurrent_create) for _ in range(10)]
for t in threads: t.start()
for t in threads: t.join()
check("10 并发创建", len(_concurrent_errors) == 0,
      predicate=lambda ok: ok and _concurrent_errors == [])
if _concurrent_errors:
    print(f"      并发错误: {_concurrent_errors}")


# ═══════════════════════════════════════════════════════════════════
#  ═══ 结果 ═══
# ═══════════════════════════════════════════════════════════════════

total = _test_items
failed = errors[0]
print(f"\n{'═' * 50}")
print(f"测试结果: {len(total) - failed}/{len(total)} 通过"
      f"  {'🎉' if failed == 0 else '😿'}")
if failed:
    print(f"失败: {failed}")
    sys.exit(1)
