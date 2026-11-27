// Build the design document PDF from docs/design-document.md.
//
//   npm install
//   npm run build:design-pdf
//
// Writes "ARC Rocket Design Optimizer Design Document.pdf" at the repository root,
// which is the file the home page links to. Edit the Markdown, never the PDF.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { marked } from "marked";
import { chromium } from "playwright";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const source = path.join(root, "docs", "design-document.md");
const output = path.join(root, "ARC Rocket Design Optimizer Design Document.pdf");
const title = "ARC Rocket Design Optimizer: Design Document";

const css = `
body { font: 10.5pt/1.55 "DejaVu Sans", "Helvetica Neue", Arial, sans-serif; color: #1b2129; margin: 0; }
h1 { font: 700 24pt/1.15 "DejaVu Serif", Georgia, serif; margin: 0 0 6pt; }
h2 { font: 700 15pt/1.25 "DejaVu Serif", Georgia, serif; margin: 22pt 0 6pt; break-after: avoid; }
h3 { font: 700 11.5pt/1.3 "DejaVu Sans", Arial, sans-serif; margin: 16pt 0 4pt; break-after: avoid; }
h1 + p { color: #5a6470; margin-top: 0; }
p, li { orphans: 3; widows: 3; }
a { color: #1f5fae; text-decoration: none; }
blockquote { margin: 12pt 0; padding: 8pt 12pt; background: #f3f5f8; border-left: 3px solid #9aa8b8; }
blockquote p { margin: 0; }
code { font: 9pt "DejaVu Sans Mono", Menlo, monospace; background: #f1f2f4; padding: 0 2pt; border-radius: 2pt; }
pre { background: #f5f5f3; padding: 9pt 11pt; border-radius: 4pt; white-space: pre; overflow: hidden; font: 7.6pt/1.3 "DejaVu Sans Mono", Menlo, monospace; }
pre code { background: none; padding: 0; font-size: 7.6pt; line-height: 1.3; }
table { border-collapse: collapse; width: 100%; margin: 8pt 0 10pt; font-size: 9.5pt; }
tr { break-inside: avoid; }
th, td { border: 1px solid #d7dbe0; padding: 5pt 7pt; text-align: left; vertical-align: top; }
th { background: #f1f3f5; font-weight: 600; }
li input[type=checkbox] { margin: 0 6pt 0 0; }
`;

const body = marked.parse(readFileSync(source, "utf8"), { gfm: true });
const html = `<!doctype html><html><head><meta charset="utf-8"><title>${title}</title><style>${css}</style></head><body>${body}</body></html>`;

const small = "font:7pt Arial,sans-serif;color:#777;width:100%;padding:0 0.85in;";
// Set CHROMIUM_PATH to use an already-installed Chromium instead of Playwright's own download.
const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage();
await page.setContent(html, { waitUntil: "load" });
await page.pdf({
  path: output,
  format: "Letter",
  printBackground: true,
  displayHeaderFooter: true,
  headerTemplate: `<div style="${small}">${title}</div>`,
  footerTemplate: `<div style="${small}text-align:right;">Page <span class="pageNumber"></span> of <span class="totalPages"></span></div>`,
  margin: { top: "0.8in", bottom: "0.85in", left: "0.85in", right: "0.85in" },
});
await browser.close();
console.log("Wrote " + path.basename(output));
