# Implement web_fetch tool for FeClaw Agent

## Context
FeClaw is a Python FastAPI backend. The Agent tool system uses a Mixin pattern in `services/tools/`.
Tools are registered via the `@tool` decorator and inherit from `AgentToolsServiceBase`.

## Task
Add a `web_fetch` tool to `services/tools/web_tools.py` in the existing `WebToolsMixin` class.

## Requirements

### Tool signature
```python
async def web_fetch(self, url: str, mode: str = "text", prompt: str = "") -> str:
```

### Modes

**Mode: "text"** (default)
1. Launch Playwright Chromium headless (hidden automation fingerprints)
2. Navigate to URL, wait for `networkidle`
3. Extract rendered text content via `document.body.innerText`
4. Return clean text (max 100K chars)

**Mode: "image"**
1. Launch Playwright Chromium headless
2. Navigate to URL, wait for `networkidle`
3. Take viewport screenshot (1280x720)
4. Save to temp file, return as base64 data URI or file path
5. Clean up temp file

**Mode: "llm-text"**
1. Same as "text" mode but
2. After extracting text, call qwen3.6-flash via aiohttp with the prompt
3. Return the LLM's answer

**Mode: "llm-image"**
1. Same as "image" mode but
2. After taking screenshot, call qwen3.6-flash VLM (multimodal) via aiohttp with the prompt and image
3. Return the LLM's answer

### Playwright stealth requirements
1. Use `--disable-blink-features=AutomationControlled` and `--disable-automation` launch args
2. Before navigation, override `navigator.webdriver` to undefined
3. Set realistic User-Agent: `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36`
4. Set viewport to 1280x720 (randomized slightly)
5. Use `page.add_init_script()` to hide webdriver traces:
   - Override `navigator.webdriver`
   - Override `navigator.plugins` 
   - Override `navigator.languages`
6. Do NOT use `--headless=new` (use `headless=True`)
7. Keep timeout reasonable: 30s for page load

### Curl fallback
When Playwright fails (e.g., not installed, timeout, error):
1. Use httpx to GET the URL (follow redirects, 15s timeout)
2. Strip HTML tags (simple regex: remove script/style tags then all tags)
3. Return cleaned text
4. For "image" / "llm-image" modes: fallback to text extraction (screenshot not possible without browser)

### Reference code

Look at `services/tools/web_tools.py` for the existing Mixin pattern.

Look at `services/tools/universal_parser.py` lines 1579-1595 for the existing `_handle_url` method (httpx + strip tags).

Look at `services/tools/universal_parser.py` lines 1126-1150 for the existing `_vlm_chat` function (VLM image analysis via aiohttp).

Look at `services/tools/universal_parser.py` lines 52-110 for the existing `_qwen_chat` function (text LLM via aiohttp). Reuse or reference similar pattern.

Look at `scripts/pptx-rasterizer.js` for Playwright usage pattern.

### Important details
- Use `asyncio.to_thread` or `subprocess` to run Playwright (it's a sync Node.js script through subprocess? No, use Python's playwright library directly: `from playwright.async_api import async_playwright`)
- Import `playwright` lazily (only when needed) to avoid import overhead
- Handle all exceptions gracefully - return error messages, don't crash
- For VLM calls, the existing `_vlm_chat` function accepts (image_paths, prompt, api_key) format
- For text LLM calls, use the existing aiohttp chat pattern with qwen3.6-flash
- Add proper cleanup for temp files

### Output format
- "text" mode: plain text with note about source URL at top
- "image" mode: file path to the saved screenshot + brief description
- "llm-text" mode: LLM's answer
- "llm-image" mode: LLM's answer (VLM)

## Files to modify
- `services/tools/web_tools.py` - Add `web_fetch` method to `WebToolsMixin` class

Do NOT modify any other files.

## Validation
After implementing, run `python3 -m py_compile services/tools/web_tools.py` to check syntax.
