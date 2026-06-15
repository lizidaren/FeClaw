#!/usr/bin/env python3
"""
批量语义切块：用 DeepSeek V4 Flash 对全部教材 full.md 进行精确行号切块
8 并发 + 自动重试 + 结果落盘
"""

import json, sys, os, zipfile, time, asyncio
import httpx
sys.path.insert(0, '/home/lch/Projects/FeClaw')
from config import settings

API_KEY = settings.DEEPSEEK_API_KEY
API = "https://api.deepseek.com/v1/chat/completions"
MODEL = "deepseek-chat"
OUTPUT_DIR = "/home/lch/data/textbooks/chunk_results"
MAX_CONCURRENT = 8
MAX_RETRIES = 3

os.makedirs(OUTPUT_DIR, exist_ok=True)

SYSTEM_PROMPT = """你是一个专业的教材结构分析专家。

任务：对 MinerU 输出的教材 Markdown 文本进行精确的逐行语义切块。

## 输入格式
每行前面有行号标记 `L00001:` 格式。

## 常见问题（请务必修正）
1. 节编号与节名拆成两行，应合并
2. OCR 伪影字符：如 ￥ 等
3. OCR 错字：如 儿->几、狗量->向量
4. 标题级别不准确：请根据语义判断

## 输出格式
```json
{
  "chapters": [
    {
      "title": "第1章 标题",
      "line_start": 1,
      "line_end": 100,
      "sections": [
        {
          "id": "1.1",
          "title": "节标题",
          "line_start": 15,
          "line_end": 50
        }
      ]
    }
  ]
}
```
行号范围必须精确对应原文行号。"""

sem = asyncio.Semaphore(MAX_CONCURRENT)


async def chunk_one_book(session, zip_path, book_key):
    """处理一本教材：读取 full.md → 加行号 → API → 保存"""
    async with sem:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                # Read zip
                with zipfile.ZipFile(zip_path) as z:
                    md = z.read("full.md").decode("utf-8")
                
                lines = md.split("\n")
                numbered = "\n".join(f"L{i+1:05d}:{line}" for i, line in enumerate(lines))
                
                # Check for 1M context limit
                char_count = len(numbered)
                if char_count > 950_000:
                    print(f"  [SKIP] {book_key}: {char_count:,} chars exceeds 950K limit")
                    return {"book": book_key, "status": "skipped", "reason": "context_limit"}
                
                prompt = f"""请分析以下教材的全文内容，根据实际语义结构进行精确切分。
每一行前面都有行号，请输出每一节的精确起始和结束行号。
请跳过封面/前言/目录等非正文内容。

```
{numbered}
```
"""

                payload = {
                    "model": MODEL,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt}
                    ],
                    "max_tokens": 16384,
                    "temperature": 0.2
                }
                headers = {
                    "Authorization": f"Bearer {API_KEY}",
                    "Content-Type": "application/json"
                }

                t0 = time.time()
                resp = await session.post(API, json=payload, headers=headers, timeout=300)
                data = resp.json()
                result_text = data["choices"][0]["message"]["content"]
                elapsed = time.time() - t0

                # Extract JSON from response
                js_start = result_text.find("```json")
                if js_start >= 0:
                    js_start = result_text.find("\n", js_start) + 1
                    js_end = result_text.rfind("```")
                else:
                    js_start = result_text.find("{")
                    js_end = result_text.rfind("}") + 1

                if js_start < 0 or js_end <= js_start:
                    raise ValueError("No JSON found in response")

                parsed = json.loads(result_text[js_start:js_end].strip())
                
                # Add metadata
                parsed["_meta"] = {
                    "book": book_key,
                    "zip_path": zip_path,
                    "chars": char_count,
                    "lines": len(lines),
                    "elapsed_s": round(elapsed, 1)
                }

                # Save to disk
                safe_key = book_key.replace("/", "_")
                out_path = os.path.join(OUTPUT_DIR, f"{safe_key}.json")
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(parsed, f, ensure_ascii=False, indent=2)

                print(f"  [OK] {book_key}: {len(lines)} lines, {char_count:,} chars, {elapsed:.1f}s")
                return {"book": book_key, "status": "ok", "lines": len(lines), "chars": char_count, "elapsed": round(elapsed, 1)}

            except httpx.TimeoutException:
                print(f"  [TIMEOUT] {book_key} (attempt {attempt}/{MAX_RETRIES})")
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(5 * attempt)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    wait = 10 * attempt
                    print(f"  [RATE_LIMIT] {book_key} (attempt {attempt}/{MAX_RETRIES}), waiting {wait}s")
                    await asyncio.sleep(wait)
                else:
                    print(f"  [HTTP {e.response.status_code}] {book_key} (attempt {attempt}/{MAX_RETRIES})")
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(3 * attempt)
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                print(f"  [PARSE] {book_key}: {e} (attempt {attempt}/{MAX_RETRIES})")
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(3 * attempt)
            except Exception as e:
                print(f"  [ERROR] {book_key}: {e} (attempt {attempt}/{MAX_RETRIES})")
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(3 * attempt)

        print(f"  [FAIL] {book_key}: all {MAX_RETRIES} attempts failed")
        return {"book": book_key, "status": "failed"}


async def main():
    print("=" * 60)
    print(f"Batch chunking: all textbooks")
    print(f"Concurrency: {MAX_CONCURRENT}, Max retries: {MAX_RETRIES}")
    print(f"Output dir: {OUTPUT_DIR}")
    print("=" * 60)

    # Find all result.zips
    base = "/home/lch/data/textbooks/mineru_results"
    zips = []
    for root, dirs, files in os.walk(base):
        for f in files:
            if f == "result.zip":
                rel = os.path.relpath(root, base)
                zips.append((os.path.join(root, f), rel))
    
    zips.sort(key=lambda x: x[1])
    print(f"\nFound {len(zips)} result.zip files:\n")
    for zpath, key in zips:
        print(f"  {key}")
    
    print(f"\n" + "=" * 60)
    print(f"Starting batch processing...")
    print()

    import httpx
    async with httpx.AsyncClient(timeout=310) as session:
        tasks = [chunk_one_book(session, zpath, key) for zpath, key in zips]
        results = await asyncio.gather(*tasks)

    # Summary
    ok = sum(1 for r in results if r["status"] == "ok")
    failed = sum(1 for r in results if r["status"] == "failed")
    skipped = sum(1 for r in results if r["status"] == "skipped")
    total_time = sum(r.get("elapsed", 0) for r in results if r["status"] == "ok")
    
    print(f"\n" + "=" * 60)
    print(f"Batch complete!")
    print(f"  OK: {ok}, Failed: {failed}, Skipped: {skipped}")
    print(f"  Total API time: {total_time:.1f}s")
    print(f"  Results saved to: {OUTPUT_DIR}/")
    
    if failed:
        print("\nFailed books:")
        for r in results:
            if r["status"] == "failed":
                print(f"  - {r['book']}")
    
    # Write summary
    summary_path = os.path.join(OUTPUT_DIR, "_summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    asyncio.run(main())
