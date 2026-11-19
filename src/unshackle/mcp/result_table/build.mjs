// Runtime ships one offline HTML resource. Node is only needed to rebuild/test it.
import { build } from 'esbuild';
import { readFile, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
process.chdir(fileURLToPath(new URL('.', import.meta.url)));
const output = await build({entryPoints: ['app.js'], bundle: true, minify: true,
  format: 'iife', target: ['es2022'], write: false, legalComments: 'inline'});
const script = output.outputFiles[0].text.replaceAll('</script', '<\\/script');
const hash = createHash('sha256').update(script).digest('base64');
const template = await readFile('template.html', 'utf8');
await writeFile('result-table.html', template.replace('SCRIPT_HASH', hash).replace('<!-- APP -->', () => `<script>${script}</script>`));
