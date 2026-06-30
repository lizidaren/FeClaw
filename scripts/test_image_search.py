"""
测试：通过 Playwright 浏览器搜索图片
"""
import asyncio, json
from playwright.async_api import async_playwright


async def search_bing_images(query="植物细胞结构示意图"):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        page.set_default_timeout(15000)

        search_url = f"https://cn.bing.com/images/search?q={query}&count=15"
        print(f"搜索: {search_url}")

        await page.goto(search_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        images = await page.evaluate("""
        () => {
            const results = [];
            const items = document.querySelectorAll('.imgpt, .mimg, .iusc');
            items.forEach(item => {
                const m = item.getAttribute('m') || '';
                const src = item.getAttribute('src') || (item.querySelector('img') ? item.querySelector('img').getAttribute('src') : '');
                try {
                    const data = JSON.parse(m);
                    results.push({
                        url: data.murl || data.imgurl || src,
                        title: data.t || '',
                        pageUrl: data.purl || '',
                    });
                } catch(e) {
                    if (src && src.startsWith('http')) {
                        results.push({url: src, title: '', pageUrl: ''});
                    }
                }
            });
            return results.slice(0, 10);
        }
        """)

        print(f"\n找到 {len(images)} 张图片\n")
        for i, img in enumerate(images, 1):
            print(f"  [{i}] {img.get('title', '')[:50]}")
            print(f"      url: {img.get('url', '')[:120]}")
            if img.get('pageUrl'):
                print(f"      page: {img.get('pageUrl', '')[:100]}")
            print()

        await browser.close()


async def search_baidu_images(query="植物细胞结构示意图"):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        page.set_default_timeout(15000)

        search_url = f"https://image.baidu.com/search?word={query}&tn=baiduimage"
        print(f"\n百度搜图: {search_url}")

        await page.goto(search_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        images = await page.evaluate("""
        () => {
            const results = [];
            const items = document.querySelectorAll('img.main_img, img[src*="http"]');
            const seen = new Set();
            items.forEach(item => {
                const src = item.getAttribute('src') || item.getAttribute('data-imgurl') || '';
                if (src && src.startsWith('http') && !seen.has(src)) {
                    seen.add(src);
                    results.push({url: src, title: item.getAttribute('alt') || ''});
                }
            });
            return results.slice(0, 15);
        }
        """)

        print(f"找到 {len(images)} 张图片\n")
        for i, img in enumerate(images[:5], 1):
            print(f"  [{i}] {img.get('title', '')[:50]}")
            print(f"      url: {img.get('url', '')[:120]}")
            print()

        await browser.close()


async def search_sogou_images(query="植物细胞结构示意图"):
    """搜狗搜图：不用浏览器，直接爬"""
    import httpx, re
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml"
    }
    async with httpx.AsyncClient(headers=headers, timeout=10) as client:
        r = await client.get(f"https://pic.sogou.com/pics?query={query}")
        content = r.text
        urls = re.findall(r'"thumbUrl":"([^"]+)"', content)
        titles = re.findall(r'"title":"([^"]+)"', content)

        print(f"\n搜狗搜图: 找到 {len(urls)} 张图片\n")
        for i in range(min(5, len(urls))):
            title = titles[i] if i < len(titles) else ""
            print(f"  [{i+1}] {title[:50]}")
            print(f"      url: {urls[i][:120]}")
            print()

if __name__ == "__main__":
    asyncio.run(search_bing_images())
    asyncio.run(search_baidu_images())
    asyncio.run(search_sogou_images())
