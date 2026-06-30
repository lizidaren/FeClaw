# Integrate web_search_image into web_search tool

## Context
FeClaw is a Python FastAPI backend with Agent tools in `services/tools/web_tools.py`.
We just discovered Alibaba Cloud's Responses API with `web_search_image` built-in tool.
The existing `web_search` tool is in `WebToolsMixin` class.

## Task
Add image search capability to the existing `web_search` tool.

### Requirements

#### 1. New parameter
Add `allow_images: bool = False` to `web_search` signature.

When `allow_images=True` and using Qwen-based search (balanced/auto levels), also call the Responses API with `web_search_image` tool to search for images related to the query.

#### 2. Download images to VFS
When images are found:
1. For each image URL, download the image content using httpx
2. Save to VFS path: `images/fetched/{query_slug}_{idx}.{ext}`
3. Track which files were saved

#### 3. Result format
Return a combined result with both text search results and image search results:

```
🔍 搜索「光合作用」(balanced) ...

[文字搜索结果]
...

[图片搜索结果] 共 5 张
  [1] 光合作用示意图 → vfs: /images/fetched/xxxx.jpg
  [2] 叶绿体结构图 → vfs: /images/fetched/xxxx.jpg
  ...
```

#### 4. Responses API call
Use the OpenAI-compatible Responses API:
- Endpoint: `https://dashscope.aliyuncs.com/compatible-mode/v1/responses`
- Tool: `{"type": "web_search_image"}`
- Model: `qwen3.6-flash` (same as current search model)
- Parse output items of type `web_search_image_call` to get image results

#### 5. Image download
- Use httpx to download each image
- Timeout: 10s per image
- Max 5 images per search to limit costs/download time
- Save to VFS via `self._vfs.async_write(path, content)` for binary content? Actually VFS write is for text. Need to use `self.storage.put_object()` directly for binary files.
- Supported formats: jpg, jpeg, png, gif, webp
- Generate unique filenames with `{sanitized_query}_{idx}`

#### 6. Error handling
- If Responses API fails, fall back to text-only search (no images)
- If individual image download fails, skip that image
- If allow_images=True but search already has good text results, still search images
- Timeout: 30s total for the image search portion

### Key code patterns

For downloading binary content to VFS:
```python
async with httpx.AsyncClient(timeout=10.0) as client:
    resp = await client.get(url, headers={"User-Agent": "..."})
    resp.raise_for_status()
    # Save to storage
    self.storage.put_object(cos_key, resp.content)
```

For the Responses API call:
```python
body = {
    "model": "qwen3.6-flash",
    "input": query,
    "tools": [{"type": "web_search_image"}],
    "stream": False,
}
async with httpx.AsyncClient(timeout=30.0) as client:
    resp = await client.post(RESPONSES_URL, headers=headers, json=body)
    data = resp.json()
    # Parse output for web_search_image_call items
    for item in data.get('output', []):
        if item.get('type') == 'web_search_image_call':
            images = json.loads(item.get('output', '[]'))
```

### Files to modify
- `services/tools/web_tools.py` - Add image search to web_search, add helper methods
- Maybe `services/tools/file_ops.py` or VFS if needed for binary image storage

### Do NOT modify
- Don't change any existing tool behaviors when allow_images=False
- Don't modify the VFS base paths
- Don't add new dependencies
