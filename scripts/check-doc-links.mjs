#!/usr/bin/env node
// Checks the repo's Markdown: every relative link resolves, every #anchor names
// a heading of its target (GitHub's slugs), and every folder README.md has the
// agents' pointer next to it (AGENTS.md, and CLAUDE.md a symlink to it).
// Usage: node scripts/check-doc-links.mjs [ROOT]   (default: the repo this script is in)
// Prints one line per problem and exits 1 when there is any.
import { execFileSync } from 'node:child_process';
import { existsSync, lstatSync, readFileSync, readlinkSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const LINK = /!?\[[^\]]*\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g;
const FENCE = /^(```|~~~)/;

// GitHub's heading anchor: lower case, formatting and punctuation dropped
// (underscores and hyphens kept), spaces to hyphens.
export function slug(heading) {
  return heading
    .trim()
    .toLowerCase()
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/[`*~]/g, '')
    .replace(/[^\p{L}\p{N}_\- ]/gu, '')
    .replace(/ /g, '-');
}

// lines outside fenced code blocks, with their 1-based numbers
function* prose(text) {
  let fenced = false;
  for (const [i, line] of text.split('\n').entries()) {
    if (FENCE.test(line)) {
      fenced = !fenced;
      continue;
    }
    if (!fenced) yield [i + 1, line];
  }
}

export function anchorsOf(text) {
  const seen = new Map();
  const out = new Set();
  for (const [, line] of prose(text)) {
    const m = /^#{1,6}\s+(.*)$/.exec(line);
    if (!m) continue;
    const s = slug(m[1]);
    const n = seen.get(s) ?? 0;
    out.add(n === 0 ? s : `${s}-${n}`);
    seen.set(s, n + 1);
  }
  return out;
}

// problems in FILES (paths relative to ROOT), as "file:line: message" strings
export function check(root, files) {
  const problems = [];
  const anchors = new Map();
  const anchorsAt = (p) => {
    if (!anchors.has(p)) anchors.set(p, anchorsOf(readFileSync(p, 'utf8')));
    return anchors.get(p);
  };
  for (const f of files) {
    const p = join(root, f);
    if (!existsSync(p)) continue;
    for (const [n, line] of prose(readFileSync(p, 'utf8'))) {
      for (const m of line.matchAll(LINK)) {
        const target = m[1];
        if (/^[a-z]+:/.test(target)) continue;
        const hash = target.indexOf('#');
        const path = hash < 0 ? target : target.slice(0, hash);
        const frag = hash < 0 ? '' : target.slice(hash + 1);
        const dest = path ? resolve(dirname(p), path) : p;
        if (!existsSync(dest)) {
          problems.push(`${f}:${n}: missing ${target}`);
        } else if (frag && dest.endsWith('.md') && statSync(dest).isFile() && !anchorsAt(dest).has(frag)) {
          problems.push(`${f}:${n}: no anchor #${frag} in ${relative(root, dest)}`);
        }
      }
    }
  }
  for (const f of files) {
    if (!f.endsWith('/README.md')) continue;
    const dir = join(root, dirname(f));
    if (!existsSync(join(dir, 'AGENTS.md'))) {
      problems.push(`${f}: no AGENTS.md next to it`);
    }
    const claude = join(dir, 'CLAUDE.md');
    if (!existsSync(claude) || !lstatSync(claude).isSymbolicLink() || readlinkSync(claude) !== 'AGENTS.md') {
      problems.push(`${f}: CLAUDE.md next to it is not a symlink to AGENTS.md`);
    }
  }
  return problems;
}

// the tracked and new (not ignored) Markdown files under ROOT, upstream/ excluded
export function markdownFiles(root) {
  return execFileSync('git', ['-C', root, 'ls-files', '--cached', '--others', '--exclude-standard', '-z', '*.md'], {
    encoding: 'utf8',
  })
    .split('\0')
    .filter((f) => f && !f.startsWith('upstream/'));
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const root = resolve(process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..'));
  const problems = check(root, markdownFiles(root));
  for (const p of problems) console.log(p);
  console.log(`${problems.length} problem(s)`);
  process.exitCode = problems.length ? 1 : 0;
}
