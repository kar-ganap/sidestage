// Parse every mermaid block in a markdown file with the real mermaid parser.
// A diagram that does not parse renders as a red error box on GitHub, which is
// worse than no diagram at all. mermaid sanitises labels through DOMPurify, so
// it needs a DOM even just to parse.
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><body></body>", { pretendToBeVisual: true });
global.window = dom.window;
global.document = dom.window.document;
const mermaid = (await import("mermaid")).default;
mermaid.initialize({ startOnLoad: false, securityLevel: "loose" });

const file = process.argv[2];
const md = readFileSync(file, "utf8");
const blocks = [...md.matchAll(/```mermaid\n([\s\S]*?)```/g)].map(m => m[1]);
console.log(`${file}: ${blocks.length} mermaid blocks`);
let bad = 0;
for (const [i, src] of blocks.entries()) {
  const kind = src.trim().split("\n")[0].slice(0, 30);
  try {
    await mermaid.parse(src);
    console.log(`   OK    #${i + 1}  ${kind}`);
  } catch (e) {
    bad++;
    console.log(`   FAIL  #${i + 1}  ${kind}\n         ${String(e.message).split("\n").slice(0,3).join(" / ")}`);
  }
}
process.exit(bad ? 1 : 0);
