#!/usr/bin/env node
/**
 * 截图模式 — Chromium 渲染 HTML 幻灯片 → PNG 截图
 *
 * 用法: node pptx-rasterizer.js --input deck.html --output /tmp/slides/
 * 输出: /tmp/slides/slide-0.png, slide-1.png, ...
 *
 * 每张 .deck-slide 独立截图，1280x720px
 */

const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

async function main() {
  const args = process.argv.slice(2);
  const inputIdx = args.indexOf("--input");
  const outputIdx = args.indexOf("--output");

  if (inputIdx === -1 || outputIdx === -1) {
    console.error("Usage: pptx-rasterizer.js --input <html> --output <dir>");
    process.exit(1);
  }

  const htmlPath = path.resolve(args[inputIdx + 1]);
  const outputDir = path.resolve(args[outputIdx + 1]);

  if (!fs.existsSync(htmlPath)) {
    console.error(`Error: Input file not found: ${htmlPath}`);
    process.exit(1);
  }

  fs.mkdirSync(outputDir, { recursive: true });

  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.CHROME || undefined,
  });

  const context = await browser.newContext({
    viewport: { width: 1280, height: 720 },
    deviceScaleFactor: 2,
  });
  const page = await context.newPage();

  // Navigate to the HTML file
  await page.goto("file://" + htmlPath, { waitUntil: "networkidle" });

  // Find all slides
  const slides = await page.locator(".deck-slide").all();

  if (slides.length === 0) {
    console.error("Error: No .deck-slide elements found");
    await browser.close();
    process.exit(1);
  }

  for (let i = 0; i < slides.length; i++) {
    const slide = slides[i];
    const outPath = path.join(outputDir, `slide-${i}.png`);

    // Take screenshot of just this element
    await slide.screenshot({ path: outPath, type: "png" });

    const sz = fs.statSync(outPath).size;
    console.log(`  [${i + 1}/${slides.length}] slide-${i}.png (${(sz / 1024).toFixed(0)} KB)`);
  }

  await browser.close();
  console.log(`Done: ${slides.length} slides`);
}

main().catch((err) => {
  console.error(`Error: ${err.message}`);
  process.exit(1);
});
