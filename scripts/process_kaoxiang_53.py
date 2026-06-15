#!/usr/bin/env python3
"""
5·3 十年高考真题 → DeepSeek V4 Pro 考向分析 Pipeline

从所有子专题 docx 提取全部题目，用 DeepSeek V4 Pro 进行 4 维度考向分析。
16 并发，自动重试，断点续跑。
"""
import os, sys, json, re, time, asyncio, datetime, pathlib
from docx import Document
import httpx

# ── 配置 ──────────────────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

DOCX_DIR = pathlib.Path('/tmp/2025版.新高考版.高考总复习.数学.5·3A版/图书拓展资源/10年高考真题专题分类汇编')
OUT_DIR = pathlib.Path('/tmp/5-3-kaoxiang-results')
OUT_DIR.mkdir(parents=True, exist_ok=True)

MAX_CONCURRENT = 16
MAX_RETRIES = 3
DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
DEEPSEEK_URL = 'https://api.deepseek.com/chat/completions'

# ── 专题名映射 ────────────────────────────────────────────────
TOPIC_NAMES = {
    '专题一': '专题一 集合与常用逻辑用语',
    '专题二': '专题二 不等式',
    '专题三': '专题三 函数',
    '专题四': '专题四 导数',
    '专题五': '专题五 三角函数',
    '专题六': '专题六 平面向量与复数',
    '专题七': '专题七 数列',
    '专题八': '专题八 立体几何',
    '专题九': '专题九 解析几何',
    '专题十': '专题十 计数原理',
    '专题十一': '专题十一 概率统计',
    '专题十二': '专题十二 应用与创新',
}

# ── 题号格式转换 ──────────────────────────────────────────────

def format_exam_name(name: str) -> str:
    """规范化考试名称
    新课标Ⅱ → 新课标Ⅱ卷
    山东理 → 山东卷（理）
    课标Ⅰ理 → 课标Ⅰ卷（理）
    """
    name = name.strip()
    if not name:
        return '未知卷'
    if name.endswith('卷'):
        return name
    if name[-1] in ('理', '文'):
        return name[:-1] + '卷（' + name[-1] + '）'
    return name + '卷'


def convert_question_headers(text: str) -> str:
    """将 docx 中的题号格式转换为统一格式
    输入: "1.(2023新课标Ⅱ,2,5分)设集合..."
    输出: "【2023•高考数学•新课标Ⅱ卷】第2题（5分）设集合..."
    """
    def replacer(m):
        year = m.group(2)
        exam = format_exam_name(m.group(3))
        qnum = m.group(4)
        score = m.group(5)
        return f'【{year}•高考数学•{exam}】第{qnum}题（{score}分）'
    
    return re.sub(r'(\d+)\.\((\d{4})([^,]+?),(\d+),(\d+)分\)', replacer, text)


# ── DeepSeek Prompt ───────────────────────────────────────────

SYSTEM_PROMPT = '你是一位专业的数学教师，擅长分析高考数学考情考向。直接输出分析内容，不要以问候语开头。'

def build_user_prompt(topic_name: str, subtopic: str, questions_text: str) -> str:
    return f'''分析以下高考数学专题「{subtopic}」的考情考向。

在分析中引用题目时统一使用【年份•高考数学•卷别】第X题（分值）格式。

题目列表：
{questions_text}

请从以下四个维度分析：

维度一、细分考点
将本专题拆解为具体的子考点，每道题对应哪个子考点？

维度二、考查方式（严格按以下5类分类）
每道题分别属于以下哪类（每道题只选一个最合适的类别）：
- 单考：只考查本知识点，不涉及其他模块知识
- 结合：在本知识模块内综合多个子考点
- 拼盘：跨知识模块（如集合+复数、集合+不等式）
- 一题多解：可以用多种不同的数学方法解决
- 情境嵌入：设置了实际情境或应用题背景
对每道题明确标注所属类别，并简要说明理由。

维度三、易错点
列出该知识点常见易错点，每点对应到具体题号。

维度四、趋势分析
从频率、分值、题型变化、考查内容重心等方面分析近年趋势。
无可用数据请明确说明，不要强加因果。

输出格式为清晰的 Markdown。'''


# ── 收集 docx 文件 ───────────────────────────────────────────

def collect_docx():
    """遍历所有专题目录，收集 docx 文件"""
    files = []
    for topic_dir in sorted(DOCX_DIR.iterdir()):
        if not topic_dir.is_dir() or not topic_dir.name.startswith('专题'):
            continue
        topic = topic_dir.name
        # docx 在 1_10年高考真题分类题组/ 子目录下
        group_dir = topic_dir / '1_10年高考真题分类题组'
        if not group_dir.is_dir():
            continue
        for docx_path in sorted(group_dir.glob('*.docx')):
            # 跳过临时文件
            if docx_path.name.startswith('~$'):
                continue
            # 解析子专题名
            # 文件名格式: {序号}_{子专题名}（十年高考）.docx
            stem = docx_path.stem  # 不带扩展名
            # 去掉开头的序号_
            if '_' in stem:
                subtopic = stem.split('_', 1)[1]
            else:
                subtopic = stem
            # 去掉（十年高考）后缀
            subtopic = subtopic.replace('（十年高考）', '').replace('(十年高考）', '').strip()
            files.append({
                'topic': topic,
                'topic_full': TOPIC_NAMES.get(topic, topic),
                'subtopic': subtopic,
                'path': str(docx_path),
                'stem': stem,
            })
    return files


# ── 读取 docx 并格式化题目 ────────────────────────────────────

def extract_questions(docx_path: str) -> str:
    """读取 docx，提取全部题目文本并格式化"""
    try:
        doc = Document(docx_path)
        lines = [p.text for p in doc.paragraphs if p.text.strip()]
        text = '\n'.join(lines)
    except Exception as e:
        print(f'  ⚠️ 读取出错: {e}')
        return ''
    # 跳过前几行的专题名/子专题名（它们是元数据，非题目）
    # 找到第一个 "1.(数字" 或类似题号开头
    match = re.search(r'\d+\.\(\d{4}', text)
    if match:
        text = text[match.start():]
    # 转换题号格式
    text = convert_question_headers(text)
    return text


# ── 单个子专题 DeepSeek 调用 ──────────────────────────────────

async def analyze_subtopic(client: httpx.AsyncClient, sem: asyncio.Semaphore,
                            info: dict) -> dict:
    topic_full = info['topic_full']
    subtopic = info['subtopic']
    
    async with sem:
        # 提取题目
        questions_text = extract_questions(info['path'])
        if not questions_text:
            print(f'  ⏭️ {topic_full}/{subtopic}: 题目提取为空，跳过')
            return None
        
        # 统计题目数量
        num_q = len(re.findall(r'【\d{4}•高考数学', questions_text))
        
        # 构建 prompt
        user_prompt = build_user_prompt(topic_full, subtopic, questions_text)
        
        # 重试循环
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                start = time.time()
                r = await client.post(DEEPSEEK_URL,
                    headers={'Authorization': f'Bearer {DEEPSEEK_API_KEY}', 'Content-Type': 'application/json'},
                    json={
                        'model': 'deepseek-v4-pro',
                        'messages': [
                            {'role': 'system', 'content': SYSTEM_PROMPT},
                            {'role': 'user', 'content': user_prompt}
                        ],
                        'thinking': {'type': 'enabled'}
                    })
                elapsed = time.time() - start
                
                if r.status_code == 429:
                    wait = 10 * attempt
                    print(f'  ⏳ {topic_full}/{subtopic}: 429 rate limit, wait {wait}s (attempt {attempt})')
                    await asyncio.sleep(wait)
                    continue
                
                if r.status_code != 200:
                    print(f'  ⚠️ {topic_full}/{subtopic}: HTTP {r.status_code} (attempt {attempt})')
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(5 * attempt)
                    continue
                
                d = r.json()
                msg = d.get('choices', [{}])[0].get('message', {})
                content = msg.get('content', '')
                usage = d.get('usage', {})
                
                if not content:
                    print(f'  ⚠️ {topic_full}/{subtopic}: 空响应 (attempt {attempt})')
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(5 * attempt)
                    continue
                
                # 加标题
                title = f'# {topic_full} — {subtopic} 考向考情分析'
                full_content = title + '\n\n' + content
                
                result = {
                    'topic': info['topic'],
                    'topic_full': topic_full,
                    'subtopic': subtopic,
                    'source_docx': info['stem'] + '.docx',
                    'num_questions': num_q,
                    'model': 'deepseek-v4-pro',
                    'prompt_version': 'v3',
                    'analysis': full_content,
                    'metadata': {
                        'elapsed': round(elapsed, 1),
                        'tokens': usage.get('total_tokens', 0),
                        'timestamp': datetime.datetime.now().isoformat(),
                    }
                }
                print(f'  ✅ {topic_full}/{subtopic}: {num_q}题, {round(elapsed,1)}s, {usage.get("total_tokens", 0)}tokens')
                return result
                
            except httpx.TimeoutException:
                print(f'  ⏳ {topic_full}/{subtopic}: Timeout (attempt {attempt})')
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(10 * attempt)
            except Exception as e:
                print(f'  ❌ {topic_full}/{subtopic}: {e} (attempt {attempt})')
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(5 * attempt)
        
        print(f'  ❌ {topic_full}/{subtopic}: 全部重试失败')
        return None


# ── 主流程 ─────────────────────────────────────────────────────

def get_output_path(info: dict) -> pathlib.Path:
    """生成输出文件路径"""
    safe_topic = info['topic'].replace(' ', '_')
    safe_subtopic = info['subtopic'].replace(' ', '_').replace('/', '_').replace('\\', '_')
    return OUT_DIR / f'{safe_topic}_{safe_subtopic}.json'


async def main():
    files = collect_docx()
    print(f'📊 共发现 {len(files)} 个子专题')
    
    # 断点续跑：检查已完成
    pending = []
    done_count = 0
    for info in files:
        out_path = get_output_path(info)
        if out_path.exists():
            done_count += 1
        else:
            pending.append(info)
    print(f'✅ 已完成: {done_count}, 📝 待处理: {len(pending)}')
    
    if not pending:
        print('🎉 全部完成！')
        return
    
    print(f'🚀 启动 {MAX_CONCURRENT} 并发 DeepSeek V4 Pro 分析...')
    
    sem = asyncio.Semaphore(MAX_CONCURRENT)
    async with httpx.AsyncClient(timeout=300) as client:
        # 创建所有任务，每个任务携带 info 用于保存
        async def task(info):
            result = await analyze_subtopic(client, sem, info)
            return info, result
        
        coros = [task(info) for info in pending]
        completed = 0
        for coro in asyncio.as_completed(coros):
            info, result = await coro
            if result:
                out_path = get_output_path(info)
                with open(out_path, 'w', encoding='utf-8') as f:
                    json.dump(result, f, ensure_ascii=False, indent=2)
                completed += 1
            
            done_now = done_count + completed
            print(f'📊 进度: {done_now}/{len(files)} ({done_now/len(files)*100:.0f}%)')
            
            # 每完成一个子专题就写进度摘要
            if completed % 5 == 0 or completed == len(pending):
                write_summary(files)
    
    # 汇总
    done_now = done_count + completed
    print(f'\n🎉 完成！{done_now}/{len(files)} 个子专题处理成功')
    
    # 最终摘要
    write_summary(files)
    print(f'\n🎉 完成！{done_count + completed}/{len(files)} 个子专题处理成功')


def write_summary(files):
    """写进度摘要到 _summary.json"""
    summary = []
    for info in files:
        out_path = get_output_path(info)
        if out_path.exists():
            with open(out_path) as f:
                data = json.load(f)
            summary.append({
                'topic': data['topic'],
                'subtopic': data['subtopic'],
                'num_questions': data['num_questions'],
                'elapsed': data['metadata']['elapsed'],
                'tokens': data['metadata']['tokens'],
            })
    with open(OUT_DIR / '_summary.json', 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)


if __name__ == '__main__':
    asyncio.run(main())
