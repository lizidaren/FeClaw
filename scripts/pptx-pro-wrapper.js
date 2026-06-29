#!/usr/bin/env node
/**
 * html2pptx-pro wrapper — HTML → PPTX 转换
 *
 * 用法: node pptx-pro-wrapper.js --input deck.html --output result.pptx
 *
 * 输入 HTML 要求：
 *   - 每张幻灯片: <section class="deck-slide">...</section>
 *   - 1280×720px，样式写在 <style> 中
 */

const { JSDOM } = require("jsdom");
const fs = require("fs");
const path = require("path");

async function main() {
  const args = process.argv.slice(2);
  const inputIdx = args.indexOf("--input");
  const outputIdx = args.indexOf("--output");

  if (inputIdx === -1 || outputIdx === -1) {
    console.error("Usage: pptx-pro-wrapper.js --input <html> --output <pptx>");
    process.exit(1);
  }

  const htmlPath = args[inputIdx + 1];
  const outputPath = args[outputIdx + 1];

  if (!fs.existsSync(htmlPath)) {
    console.error(`Error: Input file not found: ${htmlPath}`);
    process.exit(1);
  }

  // html2pptx-pro is ESM-only, require dynamic import
  const { default: html2pptx } = await import("html2pptx-pro");

  const html = fs.readFileSync(htmlPath, "utf-8");
  const dom = new JSDOM(html, {
    url: "http://localhost",
    contentType: "text/html",
  });

  // 桥接 JSDOM 的浏览器 API 到 Node.js 全局空间（html2pptx-pro 依赖这些）
  const win = dom.window;
  if (typeof globalThis.Node === "undefined") globalThis.Node = win.Node;
  if (typeof globalThis.Element === "undefined") globalThis.Element = win.Element;
  if (typeof globalThis.Document === "undefined") globalThis.Document = win.Document;
  if (typeof globalThis.HTMLElement === "undefined") globalThis.HTMLElement = win.HTMLElement;
  if (typeof globalThis.HTMLDivElement === "undefined") globalThis.HTMLDivElement = win.HTMLDivElement;
  if (typeof globalThis.DOMRect === "undefined") globalThis.DOMRect = win.DOMRect;
  if (typeof globalThis.CSSStyleDeclaration === "undefined") globalThis.CSSStyleDeclaration = win.CSSStyleDeclaration;
  if (typeof globalThis.document === "undefined") globalThis.document = win.document;
  if (typeof globalThis.getComputedStyle === "undefined") globalThis.getComputedStyle = win.getComputedStyle.bind(win);

  const slides = [...win.document.querySelectorAll(".deck-slide")];

  if (slides.length === 0) {
    console.error("Error: No .deck-slide elements found in HTML");
    process.exit(1);
  }

  // JSDOM 不提供布局引擎，强制设置每页幻灯片尺寸
  for (const s of slides) {
    Object.defineProperty(s, "clientWidth", { value: 1280, configurable: true });
    Object.defineProperty(s, "clientHeight", { value: 720, configurable: true });
    Object.defineProperty(s, "offsetWidth", { value: 1280, configurable: true });
    Object.defineProperty(s, "offsetHeight", { value: 720, configurable: true });
  }

  const outDir = path.dirname(path.resolve(outputPath));
  if (!fs.existsSync(outDir)) {
    fs.mkdirSync(outDir, { recursive: true });
  }

  const pptx = await html2pptx(slides, {
    title: path.basename(outputPath, ".pptx").replace(/[_-]/g, " "),
    author: "FeClaw",
    slideLayout: "LAYOUT_16x9",
  });

  await pptx.writeFile({ fileName: outputPath });

  const sizeKB = (fs.statSync(outputPath).size / 1024).toFixed(0);
  console.log(`Done: ${slides.length} slides -> ${outputPath} (${sizeKB} KB)`);
}

main().catch((err) => {
  console.error(`Error: ${err.message}`);
  process.exit(1);
});
