// Run by `npm run build`: fails when the production build contains the dev mock bridge, its
// `?mock=` switches or its synthetic fixtures. Usage: node scripts/check-dist.mjs [folder]
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

// The dev mock's data ('synthetic-job', the '?mock=' switches) and the engine recordings under
// src/mock/fixtures/engine ('project-one', 'job-one'): none of it belongs in the shipped app.
const FORBIDDEN = ['synthetic-job', 'mock=', 'project-one', 'job-one'];
const dist = process.argv[2]
  ? resolve(process.argv[2])
  : fileURLToPath(new URL('../dist', import.meta.url));

function* filesIn(folder) {
  for (const entry of readdirSync(folder, { withFileTypes: true })) {
    const path = join(folder, entry.name);
    if (entry.isDirectory()) yield* filesIn(path);
    else yield path;
  }
}

// An empty or missing folder would pass without checking anything.
if (!existsSync(join(dist, 'index.html'))) {
  console.error(`dist check: no index.html in ${dist}; build first.`);
  process.exit(1);
}
const found = [];
let checked = 0;
for (const file of filesIn(dist)) {
  checked += 1;
  // latin1 reads every byte, so images and fonts are searched without failing to decode.
  const text = readFileSync(file, 'latin1');
  for (const needle of FORBIDDEN)
    if (text.includes(needle)) found.push(`${relative(dist, file)} contains "${needle}"`);
}
if (found.length) {
  console.error(`dist check failed: the build contains the dev mock.\n  ${found.join('\n  ')}`);
  process.exit(1);
}
console.log(`dist check: ${checked} files, none contains ${FORBIDDEN.join(' or ')}.`);
