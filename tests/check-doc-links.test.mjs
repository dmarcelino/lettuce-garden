// scripts/check-doc-links.mjs against temp Markdown trees.
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { test } from 'node:test';
import { check, slug } from '../scripts/check-doc-links.mjs';

function tree(files) {
  const root = mkdtempSync(join(tmpdir(), 'doclinks-'));
  for (const [path, text] of Object.entries(files)) {
    mkdirSync(dirname(join(root, path)), { recursive: true });
    writeFileSync(join(root, path), text);
  }
  return root;
}

test('slugs follow GitHub: underscores and hyphens kept, code and punctuation dropped', () => {
  assert.equal(slug('What `check_stack` enforces'), 'what-check_stack-enforces');
  assert.equal(slug('Upgrading `LETTUCE_REF`'), 'upgrading-lettuce_ref');
  assert.equal(slug('How `run.sh` works'), 'how-runsh-works');
  assert.equal(slug('dig, gh and secret-tool'), 'dig-gh-and-secret-tool');
});

test('a link to an existing file and heading passes', () => {
  const root = tree({
    'a.md': '[b](b.md#what-check_stack-enforces) [self](#top) [web](https://example.com/x)\n# Top\n',
    'b.md': '# What `check_stack` enforces\n',
  });
  assert.deepEqual(check(root, ['a.md', 'b.md']), []);
});

test('a missing file or anchor is reported with its line', () => {
  const root = tree({
    'a.md': 'intro\n[gone](gone.md)\n[b](b.md#nope)\n',
    'b.md': '# Here\n',
  });
  assert.deepEqual(check(root, ['a.md', 'b.md']), ['a.md:2: missing gone.md', 'a.md:3: no anchor #nope in b.md']);
});

test('links and headings inside fenced code are ignored', () => {
  const root = tree({
    'a.md': '```\n[gone](gone.md)\n# Not a heading\n```\n[x](#not-a-heading)\n',
  });
  assert.deepEqual(check(root, ['a.md']), ['a.md:5: no anchor #not-a-heading in a.md']);
});

test('a repeated heading gets a numbered anchor', () => {
  const root = tree({ 'a.md': '# Rules\n# Rules\n[x](#rules-1) [y](#rules-2)\n' });
  assert.deepEqual(check(root, ['a.md']), ['a.md:3: no anchor #rules-2 in a.md']);
});

test('a folder README needs AGENTS.md and CLAUDE.md linking to it', () => {
  const root = tree({ 'ok/README.md': '# ok\n', 'ok/AGENTS.md': '# a\n', 'bare/README.md': '# bare\n', 'README.md': '# root\n' });
  symlinkSync('AGENTS.md', join(root, 'ok/CLAUDE.md'));
  assert.deepEqual(check(root, ['README.md', 'ok/README.md', 'bare/README.md']), [
    'bare/README.md: no AGENTS.md next to it',
    'bare/README.md: CLAUDE.md next to it is not a symlink to AGENTS.md',
  ]);
});

test('a CLAUDE.md that is a copy, not a symlink, is reported', () => {
  const root = tree({ 'f/README.md': '# f\n', 'f/AGENTS.md': '# a\n', 'f/CLAUDE.md': '# a\n' });
  assert.deepEqual(check(root, ['f/README.md']), ['f/README.md: CLAUDE.md next to it is not a symlink to AGENTS.md']);
});
