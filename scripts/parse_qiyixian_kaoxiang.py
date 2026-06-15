#!/usr/bin/env python3
"""
曲一线 考情表 + 命题形式 -> DeepSeek V4 Flash 解析
处理所有 46 个文件，兼容有/无 ## 前缀的表格格式。
"""
import asyncio, json, os, re, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from dotenv import load_dotenv
load_dotenv()
import httpx

QIXIYAN_DIR = '/home/lch/data/kaoxiang/qiyixian'
OUT_DIR = '/home/lch/data/kaoxiang/qiyixian_parsed'
MAX_CONCURRENT = 8
MAX_RETRIES = 3
API_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
URL = 'https://api.deepseek.com/chat/completions'
os.makedirs(OUT_DIR, exist_ok=True)

TOPIC_NAMES = {
    1: '专题一 集合与常用逻辑用语', 2: '专题二 不等式', 3: '专题三 函数',
    4: '专题四 导数', 5: '专题五 三角函数', 6: '专题六 平面向量与复数',
    7: '专题七 数列', 8: '专题八 立体几何', 9: '专题九 解析几何',
    10: '专题十 计数原理', 11: '专题十一 概率统计', 12: '专题十二 应用与创新',
}

def get_topic_info(fp):
    name = os.path.basename(fp)
    m = re.search(r'(\d+)\.(\d+)', name)
    section = m.group(0) if m else ''
    if '专题十二' in name:
        topic_num = 12
    elif m:
        topic_num = int(m.group(1))
    else:
        topic_num = 0
    return {'topic_full': TOPIC_NAMES.get(topic_num, f'专题{topic_num}' if topic_num else ''), 'section': section}

def extract_table_and_prop(text):
    """提取考情表HTML和命题形式文本，兼容不同格式"""
    # 找表格：先找 ## 考情 清 单，再试无 ## 前缀的考情 清单
    table_match = re.search(r'(?:##\s*)?考情\s*清?\s*单\s*', text)
    if not table_match:
        return None, None
    table_start = table_match.end()
    # 找 </table> 结束
    table_end = text.find('</table>', table_start)
    if table_end == -1:
        return None, None
    table_html = text[table_start:table_end + 8].strip()
    
    # 提取命题形式：在</table>之后的文本，直到下一个 ## 或 # 开头的行
    after_table = text[table_end + 8:]
    prop_match = re.search(r'##\s*命题\s*形式\s*(.*?)(?=##|\Z)', after_table, re.DOTALL)
    if prop_match:
        prop_form = prop_match.group(1).strip()
    else:
        # 没命题形式标题，取 </table> 后到下一个 ## 或 # 的文本
        pm = re.search(r'\s*(.*?)(?=##|# |\Z)', after_table, re.DOTALL)
        prop_form = pm.group(1).strip() if pm else ''
    return table_html, prop_form

PROMPT = '''你是一个精确的数据提取工具。从以下 HTML 表格中提取高考数学考情数据。

表格带 rowspan（行合并），展开所有合并的单元格为完整行。

输出 JSON 格式：
{
  "kaodian_list": [
    {
      "kaodian": "考点名称",
      "exam_examples": ["真题1", "真题2"],
      "exam_trend": "考向",
      "exam_frequency": "4年考频",
      "core_competency": "核心素养"
    }
  ]
}

只输出 JSON，不要其他文字。
表格内容：\n\n'''

async def parse_one(sem, fp):
    async with sem:
        info = get_topic_info(fp)
        with open(fp, encoding='utf-8') as f:
            table_html, prop_form = extract_table_and_prop(f.read())
        if not table_html:
            return None
        
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=120) as cli:
                    r = await cli.post(URL,
                        headers={'Authorization': f'Bearer {API_KEY}', 'Content-Type': 'application/json'},
                        json={'model': 'deepseek-v4-flash',
                              'messages': [
                                  {'role': 'system', 'content': '你是一个精确的数据提取工具，只输出JSON。'},
                                  {'role': 'user', 'content': PROMPT + table_html[:4000]}
                              ]})
                d = r.json()
                content = d.get('choices', [{}])[0].get('message', {}).get('content', '')
                content = re.sub(r'```json|```', '', content).strip()
                parsed = json.loads(content)
                name = os.path.basename(fp).replace('__', ' | ')
                print(f'  OK {name}: {len(parsed.get("kaodian_list", []))} kaodian')
                return {'file': os.path.basename(fp), 'kaodian_list': parsed.get('kaodian_list', []),
                        'prop_form': prop_form, **info}
            except Exception as e:
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(3 * attempt)
    return None

async def main():
    files = sorted(os.listdir(QIXIYAN_DIR))
    target = []
    for f in files:
        fp = os.path.join(QIXIYAN_DIR, f)
        with open(fp, encoding='utf-8') as fh:
            if '考情' in fh.read():
                target.append(fp)
    print(f'共 {len(target)} 个有考情表的文件')
    
    sem = asyncio.Semaphore(MAX_CONCURRENT)
    done = 0
    coros = [parse_one(sem, fp) for fp in sorted(target)]
    for coro in asyncio.as_completed(coros):
        r = await coro
        if r:
            done += 1
            out_path = os.path.join(OUT_DIR, r['file'].replace('.md', '.json'))
            with open(out_path, 'w', encoding='utf-8') as f:
                json.dump(r, f, ensure_ascii=False, indent=2)
    
    total_kd = 0
    for f in os.listdir(OUT_DIR):
        if f.endswith('.json'):
            with open(os.path.join(OUT_DIR, f)) as _fh:
                total_kd += len(json.load(_fh).get('kaodian_list', []))
    print(f'\n完成: {done} 个文件, {total_kd} 条 kaodian')
    print(f'结果保存在: {OUT_DIR}')

if __name__ == '__main__':
    asyncio.run(main())
