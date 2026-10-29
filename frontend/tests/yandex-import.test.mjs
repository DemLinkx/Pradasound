import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { stripTypeScriptTypes } from 'node:module';
import { test } from 'node:test';
import { Script } from 'node:vm';

const source = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
function sourceFunction(name) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0);
  const end = source.indexOf('\n}\n', start) + 3;
  return new Script(`${stripTypeScriptTypes(source.slice(start, end))}; ${name}`).runInNewContext({ URL, URLSearchParams });
}
const parse = sourceFunction('yandexPage');
const vkToken = sourceFunction('vkAccessToken');
const spotifyPlayback = sourceFunction('spotifyPlayback');
const listeningPosition = sourceFunction('listeningPosition');
const playbackSnapshot = sourceFunction('playbackSnapshot');

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

test('VK final OAuth URL sends only its token, not the whole URL', () => {
  const token = 'vk1.a.' + 'a'.repeat(40);
  assert.equal(vkToken(`https://oauth.vk.com/blank.html#access_token=${token}&expires_in=86400&user_id=123`), token);
  assert.equal(vkToken(`  ${token}  `), token);
  assert.throws(() => vkToken(`https://evil.example/blank.html#access_token=${token}`));
  assert.throws(() => vkToken(`https://evil.example@oauth.vk.com/blank.html#access_token=${token}`));
  assert.throws(() => vkToken('https://oauth.vk.com/blank.html#error=access_denied'));
});

test('Spotify preview is never treated as a finished full song', () => {
  const result = spotifyPlayback({ duration: 30000, position: 30000, isPaused: true },
    { duration: 30000, position: 29000, started: true }, 176000);
  assert.equal(result.preview, true);
  assert.equal(result.ended, false);
});

test('Spotify pauses and buffering do not advance the shared queue', () => {
  const previous = { duration: 176000, position: 60000, started: true };
  assert.equal(spotifyPlayback({ duration: 176000, position: 60000, isPaused: true }, previous).ended, false);
  assert.equal(spotifyPlayback({ duration: 176000, position: 176000, isPaused: true, isBuffering: true }, previous).ended, false);
});

test('Spotify completion handles an end position and a reset to zero', () => {
  const previous = { duration: 176000, position: 175500, started: true };
  assert.equal(spotifyPlayback({ duration: 176000, position: 176000, isPaused: true }, previous, 176000).ended, true);
  assert.equal(spotifyPlayback({ duration: 176000, position: 0, isPaused: true }, previous, 176000).ended, true);
  assert.equal(spotifyPlayback({ duration: 0, position: 0, isPaused: true }, previous, 176000).ended, true);
  assert.equal(spotifyPlayback({ duration: 176000, position: 0, isPaused: true }, { ...previous, started: false }).ended, false);
});

test('listening position compensates elapsed network time but never advances a pause', () => {
  const snapshot = { position: 30, paused: false, duration: 180, server_time: 100000 };
  assert.equal(listeningPosition(snapshot, 102500), 32.5);
  assert.equal(listeningPosition({ ...snapshot, paused: true }, 102500), 30);
  assert.equal(listeningPosition(snapshot, 99900), 30);
  assert.equal(listeningPosition(snapshot, 500000), 180);
});

test('invalid playback snapshots cannot seek or poison the listening clock', () => {
  const snapshot = { item_id: 'a'.repeat(32), revision: 0, serial: 1, position: 30, duration: 180, paused: false, server_time: 100000 };
  assert.equal(playbackSnapshot(snapshot)?.position, 30);
  assert.equal(playbackSnapshot({ ...snapshot, position: NaN }), null);
  assert.equal(playbackSnapshot({ ...snapshot, position: -1 }), null);
  assert.equal(playbackSnapshot({ ...snapshot, server_time: Infinity }), null);
  assert.equal(playbackSnapshot({ ...snapshot, revision: 1.5 }), null);
});

function listenerHarness() {
  const player = { readyState: 4, paused: true, playbackRate: 1, seeks: 0, plays: 0, pauses: 0, position: 0,
    get currentTime() { return this.position; },
    set currentTime(value) { this.position = value; this.seeks++; },
    play() { this.plays++; this.paused = false; return Promise.resolve(); },
    pause() { this.pauses++; this.paused = true; } };
  const snapshot = { item_id: 'a'.repeat(32), revision: 0, serial: 1, transport_serial: 1,
    position: 46, duration: 180, paused: false, server_time: Date.now() };
  const context = { session: { role: 'Member' }, playbackDevice: true, listenWanted: { current: true },
    listenerError: { current: '' }, remoteRef: { current: snapshot }, currentItem: { id: snapshot.item_id },
    playbackRevision: 0, currentKey: 'track:0', listenerToken: 'private-listener', streamAttempt: 0,
    fallbackId: '', currentTrack: 'track', listenerApplied: { current: '' }, clockOffset: { current: 0 },
    localPause: { current: false }, isSpotify: false, audioPlayers: [{ current: player }],
    activeAudioRef: { current: 0 }, slotKeys: { current: ['track:0', ''] },
    HTMLMediaElement: { HAVE_METADATA: 1 }, listeningPosition,
    localPlayback: () => ({ position: player.currentTime, paused: player.paused }),
    setAudioError: (error) => { throw Error(error); } };
  const start = source.indexOf('function syncListener(');
  const end = source.indexOf('\n  }\n', start) + 5;
  const sync = new Script(`${stripTypeScriptTypes(source.slice(start, end))}; syncListener`).runInNewContext(context);
  return { player, snapshot, context, sync };
}

test('guest starts once and ignores heartbeat drift without changing tempo', () => {
  const { player, snapshot, sync } = listenerHarness();
  sync();
  assert.equal(player.seeks, 1);
  assert.equal(player.plays, 1);
  for (let i = 0; i < 30; i++) {
    snapshot.serial++; snapshot.position = 60 + i * 2;
    sync();
  }
  assert.equal(player.seeks, 1);
  assert.equal(player.plays, 1);
  assert.equal(player.playbackRate, 1);
  // A local buffering pause is left to the media element, not repeatedly restarted.
  player.paused = true; sync();
  assert.equal(player.plays, 1);
});

test('explicit host seek, pause and resume are applied once per command', () => {
  const { player, snapshot, sync } = listenerHarness();
  sync();
  snapshot.transport_serial++; snapshot.position = 100; sync();
  assert.ok(Math.abs(player.currentTime - 100) < .1);
  assert.equal(player.seeks, 2);
  snapshot.transport_serial++; snapshot.paused = true; sync(); sync();
  assert.equal(player.pauses, 1);
  snapshot.transport_serial++; snapshot.paused = false; sync(); sync();
  assert.equal(player.plays, 2);
});

test('unready audio waits to align; explicit guest resume can align again', () => {
  const { player, snapshot, context, sync } = listenerHarness();
  player.readyState = 0; sync();
  assert.equal(context.listenerApplied.current, '');
  player.readyState = 4; snapshot.position = 48; sync();
  assert.equal(player.seeks, 1);
  context.localPause.current = true; sync(true);
  assert.equal(player.pauses, 1);
  context.localPause.current = false; snapshot.position = 75; sync(true);
  assert.equal(player.seeks, 2);
  assert.equal(player.plays, 2);
});
