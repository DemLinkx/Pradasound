import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { stripTypeScriptTypes } from 'node:module';
import { test } from 'node:test';
import { Script } from 'node:vm';

const source = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
const start = source.indexOf('function yandexPage(');
assert.ok(start >= 0);
const end = source.indexOf('\n}\n', start) + 3;
const parse = new Script(`${stripTypeScriptTypes(source.slice(start, end))}; yandexPage`).runInNewContext();

test('one unusual or malformed entry does not reject all liked tracks', () => {
  const page = parse({ tracks: [
    { id: '123', title: 'Song', artist: 'Artist' },
    { id: 'ugc-uuid', title: 'Upload', artist: null },
    null, { title: 'Missing ID' },
    { id: '456:789', title: null },
  ], has_more: true, next_offset: 50 });
  assert.equal(page.tracks.length, 3);
  assert.equal(page.tracks[0].playable, true);
  assert.equal(page.tracks[1].playable, false);
  assert.equal(page.tracks[2].id, '456');
  assert.equal(page.tracks[2].title, 'Без названия');
  assert.equal(page.next_offset, 50);
  assert.equal(page.unsupported_count, 3);
});

test('empty library is valid and numeric IDs are normalized', () => {
  assert.equal(parse({ tracks: [] }).tracks.length, 0);
  assert.equal(parse({ tracks: [{ id: 123 }] }).tracks[0].id, '123');
  assert.equal(parse({ tracks: [{ id: Number.MAX_SAFE_INTEGER + 1 }] }).tracks.length, 0);
});

test('unsupported rows stay disabled and cannot disguise themselves as playable', () => {
  const page = parse({ tracks: [{ id: 'custom-id', playable: true }, { id: '123', playable: false }], unsupported_count: 4 });
  assert.ok(page.tracks.every((track) => track.playable === false));
  assert.equal(page.unsupported_count, 4);
});

test('invalid envelope still produces a retryable library error', () => {
  assert.throws(() => parse({ authenticated: true, tracks: null }), /Обновить лайки/);
});
