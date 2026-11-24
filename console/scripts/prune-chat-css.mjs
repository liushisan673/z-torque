// 会话页 CSS 清理工具（阶段13 第4批新增，后续页面迁移可复用）。
//
// 背景：会话页从手写结构迁到 @ant-design/x 之后，index.css 里成批的 .chat-* 规则再没有任何
// 源码引用。手工删容易删漏/删错，这里按「源码里是否还有该整词引用」判定失效类名，再删对应规则。
//
// 用法（在任意目录执行都可以，路径按脚本自身位置解析）：
//   node console/scripts/prune-chat-css.mjs --scan   只报告失效类名，不改文件
//   node console/scripts/prune-chat-css.mjs          执行清理
//
// 判定规则：
//   · 一个选择器只有在「它出现的每个类名都已无引用」时才删除（混写选择器保留活的部分）；
//   · 状态修饰类（assistant/user/active/running/…、src-*）不算业务类名，随宿主一起判定；
//   · @media / @supports 递归处理，@keyframes / @font-face 原样保留。
import { readFileSync, readdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

// 脚本位于 console/scripts/，工程根是它的上一级
const CONSOLE_ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
const CSS_FILE = join(CONSOLE_ROOT, "src", "index.css");
const SRC_DIR = join(CONSOLE_ROOT, "src");

const OPAQUE_AT = /^\s*@(keyframes|-webkit-keyframes|font-face|property|counter-style|page)\b/;
const CSS_CLASS = /\.(-?[_a-zA-Z][\w-]*)/g;
// 只处理会话页前缀，避免误删其他页面的类
const PREFIX = "chat-";
const GENERIC_MODIFIERS = new Set(["assistant", "user", "external", "active", "open", "running", "waiting", "done", "error", "fail", "ok", "wait", "current"]);

const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

function collectSources(dir, acc) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) collectSources(full, acc);
    // index.css 自身不算引用来源，否则每个类名都“还在用”
    else if (/\.(tsx?|css)$/.test(entry.name) && !full.endsWith("index.css")) acc.push(full);
  }
  return acc;
}

function findUnusedClasses(cssText) {
  const names = new Set([...cssText.matchAll(CSS_CLASS)].map((m) => m[1]));
  const sources = collectSources(SRC_DIR, []).map((f) => readFileSync(f, "utf8")).join("\n");
  const unused = [];
  for (const name of names) {
    if (!name.startsWith(PREFIX)) continue;
    // 源码里的类名出现在 className 字符串中（没有前导点），用前后边界判断整词引用
    const used = new RegExp("(?<![\\w-])" + escapeRe(name) + "(?![\\w-])").test(sources);
    if (!used) unused.push(name);
  }
  return unused.sort();
}

function classNamesOf(selector) {
  return [...selector.matchAll(CSS_CLASS)].map((m) => m[1]);
}

function selectorIsDead(selector, dead) {
  const names = classNamesOf(selector);
  if (names.length === 0) return false;
  return names.every((n) => dead.has(n) || GENERIC_MODIFIERS.has(n) || n.startsWith("src-"));
}

function splitSelectors(prelude) {
  const out = [];
  let depth = 0;
  let start = 0;
  for (let i = 0; i < prelude.length; i++) {
    const ch = prelude[i];
    if (ch === "(" || ch === "[") depth++;
    else if (ch === ")" || ch === "]") depth--;
    else if (ch === "," && depth === 0) {
      out.push(prelude.slice(start, i));
      start = i + 1;
    }
  }
  out.push(prelude.slice(start));
  return out;
}

function transform(css, dead) {
  let i = 0;
  let out = "";
  const stats = { removedSelectors: 0, removedBlocks: 0 };
  while (i < css.length) {
    const open = css.indexOf("{", i);
    const close = css.indexOf("}", i);
    if (open < 0 || (close >= 0 && close < open)) {
      const stop = close >= 0 ? close + 1 : css.length;
      out += css.slice(i, stop);
      i = stop;
      continue;
    }
    const prelude = css.slice(i, open);
    let depth = 0;
    let j = open;
    for (; j < css.length; j++) {
      if (css[j] === "{") depth++;
      else if (css[j] === "}") {
        depth--;
        if (depth === 0) break;
      }
    }
    const body = css.slice(open + 1, j);
    i = j + 1;
    const trimmed = prelude.trim();
    if (OPAQUE_AT.test(trimmed)) {
      out += prelude + "{" + body + "}";
      continue;
    }
    if (trimmed.startsWith("@")) {
      const inner = transform(body, dead);
      if (inner.text.trim() === "") {
        stats.removedBlocks += 1;
        continue;
      }
      out += prelude + "{" + inner.text + "}";
      stats.removedSelectors += inner.removedSelectors;
      stats.removedBlocks += inner.removedBlocks;
      continue;
    }
    const selectors = splitSelectors(prelude);
    const kept = selectors.filter((s) => !selectorIsDead(s, dead));
    if (kept.length === selectors.length) {
      out += prelude + "{" + body + "}";
      continue;
    }
    stats.removedSelectors += selectors.length - kept.length;
    if (kept.length === 0) {
      stats.removedBlocks += 1;
      continue;
    }
    out += kept.map((s) => s.trim()).join(",\n") + " {" + body + "}";
  }
  return { text: out, ...stats };
}

const cssText = readFileSync(CSS_FILE, "utf8");
const unused = findUnusedClasses(cssText);
if (process.argv.includes("--scan")) {
  console.log(JSON.stringify({ cssFile: CSS_FILE, unusedCount: unused.length, unused }, null, 2));
  process.exit(0);
}

const dead = new Set(unused);
const before = cssText.split("\n").length;
const result = transform(cssText, dead);
const after = result.text.split("\n").length;
// 与活类名混写的复合选择器会留下来（例如 `.chat-msg .chat-md`），单独列出便于人工复核
const leftovers = unused.filter((name) => new RegExp("\\." + escapeRe(name) + "(?![\\w-])").test(result.text));
writeFileSync(CSS_FILE, result.text, "utf8");
console.log(
  JSON.stringify(
    { cssFile: CSS_FILE, linesBefore: before, linesAfter: after, linesRemoved: before - after, removedSelectors: result.removedSelectors, removedBlocks: result.removedBlocks, deadClasses: unused.length, leftovers },
    null,
    2,
  ),
);
