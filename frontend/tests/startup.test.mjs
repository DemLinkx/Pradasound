import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const index = readFileSync(new URL('../index.html', import.meta.url), 'utf8');

test('Telegram SDK is served locally and loads before the app', () => {
  const match = index.match(/<script src="(\/assets\/telegram-web-app-([a-f0-9]{16})\.js)"><\/script>/);
  assert.ok(match, 'Telegram SDK should come from the site');
  const sdk = readFileSync(new URL(`../public${match[1]}`, import.meta.url));
  assert.equal(createHash('sha256').update(sdk).digest('hex').slice(0, 16), match[2]);
  assert.ok(index.indexOf(match[0]) < index.indexOf('src="/src/main.tsx"'));
  assert.ok(!index.includes('https://telegram.org/js/telegram-web-app.js'));
});

test('site logo is suitable for high-density icons without the original oversized image', () => {
  const png = readFileSync(new URL('../src/assets/prada-sound-logo.png', import.meta.url));
  assert.equal(png.readUInt32BE(16), 192);
  assert.equal(png.readUInt32BE(20), 192);
  assert.ok(png.length < 100_000);
});
