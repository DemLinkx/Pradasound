import { useEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties, FormEvent } from 'react';

type Session = { lobby_id: string; role: 'Main' | 'Member'; host_token?: string };
type YandexTrack = { id: string; title: string; artist: string };
type YandexLogin = { login_id: string; verification_url: string; user_code: string; interval: number; expires_in: number };
type QueueItem = { id: string; track_id: string; owner_id: string; access_revision?: number };
type Profile = { name: string; username: string | null; photo_url: string | null };
type TelegramWebApp = { initData: string; ready: () => void; expand: () => void;
  openTelegramLink?: (url: string) => void; openLink?: (url: string) => void;
  themeParams?: { bg_color?: string } };
const telegram = (window as Window & { Telegram?: { WebApp?: TelegramWebApp } }).Telegram?.WebApp;
const telegramInitData = telegram?.initData || '';
function Avatar({ profile, size = 38 }: { profile?: Profile | null; size?: number }) {
  const initials = (profile?.name || '?').trim().slice(0, 1).toLocaleUpperCase('ru');
  return <span className="avatar" style={{ width: size, height: size, fontSize: size * .42 }} aria-hidden="true">
    {initials}{profile?.photo_url && <img src={profile.photo_url} alt="" referrerPolicy="no-referrer"
      onError={(event) => { event.currentTarget.style.display = 'none'; }} />}
  </span>;
}
function profileLabel(profile?: Profile | null) { return profile?.username ? `@${profile.username}` : profile?.name || 'Гость'; }
function clockTime(seconds: number) {
  if (!Number.isFinite(seconds) || seconds < 0) return '0:00';
  const total = Math.floor(seconds);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
}
type YtPlayer = { destroy: () => void; playVideo: () => void; pauseVideo: () => void;
  seekTo: (seconds: number, allowSeekAhead: boolean) => void; setVolume: (volume: number) => void;
  getCurrentTime: () => number; getDuration: () => number };
type YtApi = { Player: new (element: HTMLElement, options: {
  videoId: string; width: number; height: number; playerVars: Record<string, string | number>;
  events: { onReady: (event: { target: YtPlayer }) => void;
    onStateChange: (event: { data: number }) => void; onError: (event: { data: number }) => void };
}) => YtPlayer };
const STORAGE_KEY = 'prada-sound-lobby';
const YANDEX_KEY = 'prada-sound-yandex';
const VIDEO_ID = /^[A-Za-z0-9_-]{11}$/;
const QUEUE_ID = /^(?:yandex_[0-9]{1,20}|[A-Za-z0-9_-]{11})$/;
const guestTokens = new Map<string, string>();
function memberToken(lobbyId: string) {
  const key = `prada-sound-member-${lobbyId}`;
  let saved = guestTokens.get(lobbyId);
  try { saved ||= sessionStorage.getItem(key) || ''; } catch {}
  if (saved && /^[a-f0-9]{64}$/.test(saved)) return saved;
  const token = Array.from(crypto.getRandomValues(new Uint8Array(32)), (byte) => byte.toString(16).padStart(2, '0')).join('');
  guestTokens.set(lobbyId, token);
  try { sessionStorage.setItem(key, token); } catch {}
  return token;
}
let ytApiPromise: Promise<YtApi> | null = null;
function youtubeApi(): Promise<YtApi> {
  const browser = window as Window & { YT?: YtApi; onYouTubeIframeAPIReady?: () => void };
  if (browser.YT?.Player) return Promise.resolve(browser.YT);
  return ytApiPromise ||= new Promise<YtApi>((resolve, reject) => {
    const timeout = window.setTimeout(() => reject(new Error('Плеер YouTube не загрузился.')), 10000);
    browser.onYouTubeIframeAPIReady = () => { clearTimeout(timeout); resolve(browser.YT!); };
    const script = document.createElement('script');
    script.src = 'https://www.youtube.com/iframe_api';
    script.onerror = () => { clearTimeout(timeout); reject(new Error('Плеер YouTube недоступен.')); };
    document.head.append(script);
  }).catch((error) => { ytApiPromise = null; throw error; });
}

function youtubeId(input: string) {
  const value = input.trim();
  if (VIDEO_ID.test(value)) return value;
  try {
    const url = new URL(value);
    const host = url.hostname.toLowerCase().replace(/^www\./, '').replace(/^m\./, '');
    if (url.protocol !== 'https:' && url.protocol !== 'http:') return '';
    const candidate = host === 'youtu.be' ? url.pathname.split('/')[1] :
      host === 'youtube.com' || host === 'youtube-nocookie.com' ?
        url.searchParams.get('v') || url.pathname.match(/^\/(?:shorts|embed|live)\/([^/?#]+)/)?.[1] : '';
    return candidate && VIDEO_ID.test(candidate) ? candidate : '';
  } catch { return ''; }
}

function qrPath(value: string) {
  const bytes = new TextEncoder().encode(value);
  if (bytes.length > 154) return '';
  const bits: number[] = [0, 1, 0, 0];
  for (let i = 7; i >= 0; i--) bits.push((bytes.length >>> i) & 1);
  for (const byte of bytes) for (let i = 7; i >= 0; i--) bits.push((byte >>> i) & 1);
  for (let i = 0; i < 4 && bits.length < 156 * 8; i++) bits.push(0);
  while (bits.length % 8) bits.push(0);
  const data = Array.from({ length: bits.length / 8 }, (_, i) =>
    bits.slice(i * 8, i * 8 + 8).reduce((n, bit) => (n << 1) | bit, 0));
  let pad = 0xec;
  while (data.length < 156) { data.push(pad); pad ^= 0xfd; }
  const multiply = (a: number, b: number) => {
    let result = 0;
    while (b) { if (b & 1) result ^= a; a = (a << 1) ^ ((a & 0x80) ? 0x11d : 0); b >>>= 1; }
    return result;
  };
  let generator = [1];
  let power = 1;
  for (let i = 0; i < 20; i++) {
    const next = Array(generator.length + 1).fill(0);
    generator.forEach((coefficient, index) => {
      next[index] ^= coefficient;
      next[index + 1] ^= multiply(coefficient, power);
    });
    generator = next;
    power = multiply(power, 2);
  }
  const blocks = [data.slice(0, 78), data.slice(78)];
  const parity = blocks.map((block) => {
    const result = Array(20).fill(0);
    for (const byte of block) {
      const factor = byte ^ result.shift()!;
      result.push(0);
      for (let i = 0; i < 20; i++) result[i] ^= multiply(generator[i + 1], factor);
    }
    return result;
  });
  const codewords: number[] = [];
  for (let i = 0; i < 78; i++) codewords.push(blocks[0][i], blocks[1][i]);
  for (let i = 0; i < 20; i++) codewords.push(parity[0][i], parity[1][i]);
  const size = 45;
  const cells = Array.from({ length: size }, () => Array(size).fill(false) as boolean[]);
  const fixed = Array.from({ length: size }, () => Array(size).fill(false) as boolean[]);
  const draw = (x: number, y: number, dark: boolean) => {
    if (x >= 0 && y >= 0 && x < size && y < size) { cells[y][x] = dark; fixed[y][x] = true; }
  };
  for (const [left, top] of [[0, 0], [size - 7, 0], [0, size - 7]]) {
    for (let dy = -1; dy <= 7; dy++) for (let dx = -1; dx <= 7; dx++)
      draw(left + dx, top + dy, dx >= 0 && dx <= 6 && dy >= 0 && dy <= 6 &&
        (dx === 0 || dx === 6 || dy === 0 || dy === 6 || (dx >= 2 && dx <= 4 && dy >= 2 && dy <= 4)));
  }
  for (let i = 8; i < size - 8; i++) { draw(i, 6, i % 2 === 0); draw(6, i, i % 2 === 0); }
  for (const x of [6, 22, 38]) for (const y of [6, 22, 38]) {
    if ((x === 6 && y === 6) || (x === 6 && y === 38) || (x === 38 && y === 6)) continue;
    for (let dy = -2; dy <= 2; dy++) for (let dx = -2; dx <= 2; dx++)
      draw(x + dx, y + dy, Math.max(Math.abs(dx), Math.abs(dy)) !== 1);
  }
  let versionRemainder = 7;
  for (let i = 0; i < 12; i++) versionRemainder = (versionRemainder << 1) ^ ((versionRemainder >>> 11) * 0x1f25);
  const versionBits = (7 << 12) | versionRemainder;
  for (let i = 0; i < 18; i++) {
    const dark = ((versionBits >>> i) & 1) !== 0;
    draw(size - 11 + i % 3, Math.floor(i / 3), dark);
    draw(Math.floor(i / 3), size - 11 + i % 3, dark);
  }
  let formatRemainder = 8;
  for (let i = 0; i < 10; i++) formatRemainder = (formatRemainder << 1) ^ ((formatRemainder >>> 9) * 0x537);
  const format = ((8 << 10) | formatRemainder) ^ 0x5412;
  const formatBit = (i: number) => ((format >>> i) & 1) !== 0;
  for (let i = 0; i <= 5; i++) draw(8, i, formatBit(i));
  draw(8, 7, formatBit(6)); draw(8, 8, formatBit(7)); draw(7, 8, formatBit(8));
  for (let i = 9; i < 15; i++) draw(14 - i, 8, formatBit(i));
  for (let i = 0; i < 8; i++) draw(size - 1 - i, 8, formatBit(i));
  for (let i = 8; i < 15; i++) draw(8, size - 15 + i, formatBit(i));
  draw(8, size - 8, true);
  let bitIndex = 0;
  for (let right = size - 1; right >= 1; right -= 2) {
    if (right === 6) right = 5;
    for (let vertical = 0; vertical < size; vertical++) {
      const y = ((right + 1) & 2) === 0 ? size - 1 - vertical : vertical;
      for (let offset = 0; offset < 2; offset++) {
        const x = right - offset;
        if (!fixed[y][x]) {
          const bit = bitIndex < codewords.length * 8 && ((codewords[bitIndex >>> 3] >>> (7 - (bitIndex & 7))) & 1) !== 0;
          cells[y][x] = bit !== ((x + y) % 2 === 0);
          bitIndex++;
        }
      }
    }
  }
  return cells.flatMap((row, y) => row.flatMap((dark, x) => dark ? [`M${x + 4} ${y + 4}h1v1h-1z`] : [])).join('');
}

const STYLES = `
  :root{font-family:Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#f8f7f4;background:#0d0e0f;font-synthesis:none}
  *{box-sizing:border-box}body{margin:0}button,input{font:inherit}button{cursor:pointer}button:disabled{opacity:.42;cursor:not-allowed}
  .app{min-height:100vh;position:relative;isolation:isolate;overflow:hidden;background:radial-gradient(circle at 77% -12%,#74351144,transparent 34%),radial-gradient(circle at 4% 58%,#292a2c,transparent 38%),#0d0e0f}
  .app:before{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;opacity:.12;background-image:url("data:image/svg+xml,%3Csvg viewBox='0 0 180 180' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.8' numOctaves='3' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='.5'/%3E%3C/svg%3E")}
  .shell{width:min(1160px,calc(100% - 40px));margin:0 auto;padding:28px 0 80px}.topbar{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:44px}.brand{display:flex;align-items:center;gap:12px;font-weight:800;letter-spacing:-.045em;font-size:23px}.brandmark{display:grid;place-items:center;width:38px;height:38px;border-radius:13px;background:#ff792b;color:#121212;font-size:23px;box-shadow:0 8px 30px #fb7b2c55}.eyebrow{color:#ff8b3d;text-transform:uppercase;letter-spacing:.18em;font-size:11px;font-weight:800}.muted{color:#a8a8a8}.small{font-size:13px}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
  .card{background:linear-gradient(155deg,#242527ec,#191a1ceb);border:1px solid #ffffff18;border-radius:24px;box-shadow:0 20px 70px #0005;backdrop-filter:blur(18px)}.cardpad{padding:28px}.soft{background:#ffffff08;border:1px solid #ffffff14;border-radius:16px}.button{border:1px solid #ffffff20;background:#ffffff0b;color:#fff;border-radius:13px;padding:12px 18px;font-weight:700;transition:transform .18s,background .18s,border-color .18s}.button:hover:not(:disabled){transform:translateY(-2px);background:#ffffff19}.button:focus-visible,input:focus-visible,a:focus-visible{outline:2px solid #ff8b3d;outline-offset:3px}.button.primary{background:#ff792b;border-color:#ff792b;color:#18100a;box-shadow:0 10px 28px #e9632050}.button.primary:hover:not(:disabled){background:#ff9148}.button.ghost{background:transparent}.button.tiny{padding:8px 12px;font-size:12px;border-radius:10px}.button.linklike{text-decoration:none;display:inline-flex;align-items:center;justify-content:center}
  input{min-width:0;width:100%;padding:13px 15px;background:#0e0f10;border:1px solid #ffffff22;border-radius:12px;color:#fff;outline:none}input::placeholder{color:#777}.field{display:grid;gap:9px;font-size:13px;color:#d4d4d4}.row{display:flex;align-items:center;gap:12px}.stack{display:grid;gap:16px}.sectionhead{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;margin:0 0 18px}.sectionhead h2,.sectionhead h3{margin:4px 0 0;font-size:24px;letter-spacing:-.04em}.sectionhead p{margin:0}.pill{display:inline-flex;align-items:center;gap:8px;padding:8px 12px;border:1px solid #ffffff1a;background:#ffffff0b;border-radius:99px;font-size:12px;color:#d5d5d5}.dot{width:7px;height:7px;border-radius:50%;background:#ff8b3d;box-shadow:0 0 12px #ff8b3d}.connected .dot{background:#64d6a3;box-shadow:0 0 12px #64d6a3}
  .landing{display:grid;grid-template-columns:1.1fr .9fr;gap:34px;align-items:center;min-height:65vh}.landing h1{font-size:clamp(50px,7.2vw,92px);line-height:.96;letter-spacing:-.085em;margin:18px 0 22px}.landing h1 em{font-style:normal;color:#ff792b}.landing p{max-width:470px;line-height:1.7}.entry{padding:30px}.entry h2{font-size:26px;margin:0 0 8px;letter-spacing:-.05em}.entry .divider{display:flex;align-items:center;gap:12px;color:#797979;font-size:12px;margin:22px 0}.entry .divider:before,.entry .divider:after{content:"";flex:1;height:1px;background:#ffffff1c}.entry form{display:grid;gap:12px}
  .roomhead{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;margin-bottom:24px}.roomhead h1{font-size:clamp(34px,5vw,54px);letter-spacing:-.07em;margin:8px 0 6px}.roomhead .actions{display:flex;gap:10px;flex-wrap:wrap}.dashboard{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(300px,.8fr);gap:20px}.maincol,.sidecol{display:grid;align-content:start;gap:20px}.hero{position:relative;overflow:hidden;min-height:290px;background:radial-gradient(circle at 18% 35%,#a64c2180,transparent 37%),linear-gradient(130deg,#282327,#18191b 60%);display:grid;grid-template-columns:1fr auto;gap:24px;align-items:center}.hero:after{content:"";position:absolute;inset:0;pointer-events:none;background:linear-gradient(90deg,transparent,#ffffff05,transparent);transform:skewX(-20deg)}.heroText{position:relative;z-index:1;min-width:0}.heroText h2{font-size:clamp(25px,3.2vw,42px);line-height:1.08;letter-spacing:-.055em;margin:10px 0;overflow-wrap:anywhere}.heroText .muted{margin:0 0 20px}.art{position:relative;z-index:1;width:190px;aspect-ratio:1;border-radius:24px;background:linear-gradient(145deg,#fc8b39,#683018 55%,#181819);box-shadow:0 22px 50px #0009;display:grid;place-items:center;overflow:hidden}.art img{width:100%;height:100%;object-fit:cover}.art .record{width:70%;aspect-ratio:1;border-radius:50%;background:repeating-radial-gradient(circle,#111 0 3px,#242424 4px 6px);border:10px solid #111;box-shadow:0 0 0 1px #ffffff35}.art .record:after{content:"";display:block;width:22%;height:22%;border-radius:50%;background:#ff8b3d;margin:39% auto}.bars{display:flex;align-items:flex-end;height:30px;gap:4px;margin:16px 0}.bars i{display:block;width:4px;height:9px;border-radius:4px;background:#ff8b3d;animation:beat 1s ease-in-out infinite alternate}.bars i:nth-child(2n){animation-delay:.25s;height:22px}.bars i:nth-child(3n){animation-delay:.48s;height:15px}@keyframes beat{to{height:29px;opacity:.45}}.playerline{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.playerline audio{max-width:100%;height:42px;filter:invert(.9) sepia(.15)}
  .miniPlayer{position:relative;z-index:1;width:220px;height:200px;flex:none;overflow:hidden;border-radius:16px;border:1px solid #ffffff26;background:#08090b;box-shadow:0 20px 45px #0008}.fallbackVideo,.fallbackVideo iframe{width:100%;height:100%;border:0}.miniPlayer .spectrum{width:100%;height:100%;display:flex;justify-content:center;align-items:center;gap:5px;background:radial-gradient(circle,#f6783030,transparent 66%),#111315}.spectrum i{width:7px;height:28px;border-radius:8px;background:linear-gradient(#ffad70,#f66a25);animation:beat .7s ease-in-out infinite alternate;animation-delay:calc(var(--n)*-90ms)}.spectrum i:nth-child(3n){height:65px}.spectrum i:nth-child(4n){height:42px}.spectrum.idle i{animation-play-state:paused;opacity:.45}.queue{list-style:none;padding:0;margin:0;display:grid;gap:9px}.queue li,.likes li{display:flex;align-items:center;gap:13px;padding:13px 14px;background:#ffffff09;border:1px solid #ffffff12;border-radius:14px;min-width:0}.queue li.current{background:#ff792b17;border-color:#ff792b66}.queue li[draggable=true]{cursor:grab}.queue li[draggable=true]:active{cursor:grabbing}.queueControls{display:flex;gap:4px;flex:none}.queueControls .button{min-width:30px;padding:6px 8px}.queue .index{color:#797979;font-size:13px;width:20px}.queue .current .index{color:#ff8b3d}.trackmeta{min-width:0;flex:1;overflow-wrap:anywhere}.trackmeta strong{display:block;font-size:14px}.trackmeta small{display:block;color:#999;margin-top:4px}.addform{display:flex;gap:10px}.addform input{flex:1}.share{display:flex;gap:18px;align-items:center;flex-wrap:wrap}.share .sharetext{min-width:0;flex:1 1 175px}.share .sharelink{display:block;width:100%;max-width:100%;text-align:left;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#f7f7f7}.qr{width:150px;height:150px;flex:none;margin:0 auto;padding:8px;border-radius:14px;background:white;shape-rendering:crispEdges}.code{display:inline-block;font-size:clamp(22px,4vw,34px);letter-spacing:.18em;font-weight:800;color:#fff;padding:12px 16px;background:#0f1011;border:1px solid #ff792b55;border-radius:14px}.authbox{padding:18px;border:1px solid #ff792b44;background:linear-gradient(120deg,#ff792b1c,#ffffff06);border-radius:18px}.authbox p{line-height:1.5}.likes{list-style:none;padding:0;margin:12px 0;display:grid;gap:8px;max-height:460px;overflow:auto}.likes li{font-size:13px}.likes li .trackmeta strong{font-size:13px}.notice{padding:13px 15px;border:1px solid #ff792b55;background:#ff792b1a;border-radius:12px;color:#ffbc8d}.roomid{color:#777;font-size:11px;word-break:break-all}
  .avatar{position:relative;display:inline-grid;place-items:center;flex:none;overflow:hidden;border-radius:50%;background:linear-gradient(145deg,#ffab67,#aa4d28);color:#1c1110;font-weight:900;box-shadow:inset 0 0 0 1px #ffffff40}.avatar img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}.profile-chip{display:inline-flex;align-items:center;gap:9px;max-width:50%;padding:4px 12px 4px 4px;border:1px solid #ffffff24;border-radius:99px;background:#ffffff0b;color:#fff;font-size:13px;font-weight:700}.profile-chip span:last-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.roomstatus{display:flex;gap:8px;flex-wrap:wrap}.hostcard{display:flex;align-items:center;gap:14px;padding:17px 20px;border:1px solid #ff923960;border-radius:20px;background:linear-gradient(110deg,#5a2a1d,#282124 56%,#1a1b1d);box-shadow:0 14px 38px #0004}.hostcard>div{min-width:0;display:grid;gap:3px;flex:1}.hostcard strong{font-size:20px;letter-spacing:-.03em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.hostcard small{color:#bdb5b1}.hostbadge{border:1px solid #ff9c4e77;border-radius:8px;color:#ffad6d;font-size:10px;font-weight:900;letter-spacing:.16em;padding:7px 8px}.added-by{display:inline-flex;align-items:center;gap:6px;margin-top:8px;color:#e0c5b5;font-size:12px;font-weight:650}.invite-button{margin-top:10px;width:100%}.telegram-app .shell{padding-top:max(18px,env(safe-area-inset-top));padding-bottom:max(36px,env(safe-area-inset-bottom))}.telegram-app .button{touch-action:manipulation}
  .deck{padding:22px 24px 20px;background:radial-gradient(circle at 96% -20%,#f3782b21,transparent 45%),linear-gradient(145deg,#262528,#191a1c 68%);border-color:#ffffff25;box-shadow:0 22px 65px #0007,inset 0 1px #ffffff0a}.deck-head{display:grid;grid-template-columns:66px minmax(0,1fr) auto;align-items:center;gap:16px}.deck-art{width:66px;height:66px;border-radius:14px;overflow:hidden;display:grid;place-items:center;background:radial-gradient(circle at 34% 25%,#ffbd7f,#bd5422 56%,#462518);color:#26120c;font-size:32px;box-shadow:0 8px 24px #0008,inset 0 1px #ffffff50}.deck-art img{width:100%;height:100%;object-fit:cover}.deck-track{min-width:0}.deck-track h3{font-size:18px;line-height:1.25;letter-spacing:-.035em;margin:5px 0 3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.deck-track p{margin:0;color:#a6a2a0;font-size:12px}.deck-onair{display:inline-flex;align-items:center;gap:7px;color:#a9a3a0;font-size:10px;font-weight:850;letter-spacing:.13em;white-space:nowrap}.deck-onair i{width:7px;height:7px;border-radius:50%;background:#74706e}.deck-onair.playing{color:#ffb27d}.deck-onair.playing i{background:#ff853b;box-shadow:0 0 13px #ff853b}.deck-timeline{display:grid;grid-template-columns:38px minmax(0,1fr) 38px;align-items:center;gap:12px;margin:22px 0 19px;color:#aaa4a0;font-size:11px;font-variant-numeric:tabular-nums}.deck-timeline span:last-child{text-align:right}.deck-range{appearance:none;-webkit-appearance:none;display:block;width:100%;height:5px;padding:0;border:0;border-radius:999px;outline:0;background:linear-gradient(90deg,#ff903f var(--fill),#ffffff30 var(--fill));cursor:pointer}.deck-range::-webkit-slider-thumb{appearance:none;-webkit-appearance:none;width:15px;height:15px;border:3px solid #ffd0a5;border-radius:50%;background:#ff8132;box-shadow:0 0 0 4px #ff813229,0 2px 8px #0008}.deck-range::-moz-range-thumb{width:10px;height:10px;border:3px solid #ffd0a5;border-radius:50%;background:#ff8132;box-shadow:0 0 0 4px #ff813229}.deck-range:disabled{opacity:.43;cursor:default}.deck-range:focus-visible{outline:2px solid #ffae78;outline-offset:7px}.deck-controls{display:flex;align-items:center;gap:12px}.deck-play,.deck-next,.deck-volume-icon{display:grid;place-items:center;flex:none;padding:0;color:#fff;cursor:pointer}.deck-play{width:54px;height:54px;border:1px solid #ffac68;border-radius:50%;background:linear-gradient(145deg,#ffab64,#f2762e);color:#23130d;box-shadow:0 8px 22px #ec6d2f52,inset 0 1px #ffffff75}.deck-play svg{width:23px;height:23px}.deck-next{width:43px;height:43px;border:1px solid #ffffff24;border-radius:50%;background:#ffffff0a}.deck-next svg{width:20px;height:20px}.deck-play:hover:not(:disabled),.deck-next:hover:not(:disabled){transform:translateY(-2px);filter:brightness(1.12)}.deck-play:disabled,.deck-next:disabled{opacity:.38;cursor:not-allowed}.deck-play:focus-visible,.deck-next:focus-visible,.deck-volume-icon:focus-visible{outline:2px solid #ffae78;outline-offset:3px}.deck-next-label{font-size:12px;font-weight:700;color:#c8c2be}.deck-volume{display:flex;align-items:center;gap:10px;margin-left:auto;color:#aaa4a0;font-size:11px;font-variant-numeric:tabular-nums}.deck-volume-icon{width:30px;height:30px;border:0;background:transparent;color:#c6c0bc}.deck-volume-icon svg{width:22px;height:22px}.deck-volume-range{width:95px}.deck-volume>span{min-width:32px;text-align:right}.deck-message{margin-top:17px}.deck-message:empty{display:none}.deck-message p{margin:0}.deck-message .notice{margin-top:10px}.deck-message .button{margin-top:10px}
  .maincol,.sidecol,.card{min-width:0}@media(min-width:851px){.dashboard{grid-template-columns:minmax(0,1fr) minmax(0,340px)}}
  @media(max-width:850px){.dashboard,.landing{grid-template-columns:minmax(0,1fr)}.maincol,.sidecol,.card{min-width:0}.landing{min-height:0}.roomhead{display:block}.roomhead .actions{margin-top:16px}.hero{grid-template-columns:minmax(0,1fr) auto}.art{width:145px}}
  @media(max-width:550px){.shell{width:min(100% - 24px,1160px);padding-top:18px}.cardpad,.entry{padding:20px}.topbar{margin-bottom:26px}.hero{grid-template-columns:1fr}.miniPlayer{width:100%;max-width:320px}.art{width:130px;position:absolute;right:-20px;top:-30px;opacity:.35}.heroText{position:relative}.share{align-items:flex-start}.qr{width:124px;height:124px}.addform{display:grid}.button{padding:11px 14px}.deck{padding:18px}.deck-head{grid-template-columns:54px minmax(0,1fr);gap:12px}.deck-art{width:54px;height:54px;border-radius:12px}.deck-onair{grid-column:2;justify-self:start;margin-top:-5px}.deck-track h3{font-size:16px}.deck-timeline{margin:18px 0}.deck-next-label{display:none}.deck-controls{gap:10px}.deck-volume{gap:6px}.deck-volume-range{width:64px}}
  /* Dark film grain and a single playback stage. */
  .app{background:#191a1c}
  .app:before{z-index:0;inset:0;opacity:1;background:radial-gradient(ellipse at 80% 8%,#38291f55,transparent 55%),radial-gradient(ellipse at 10% 85%,#1f2a3155,transparent 55%);animation:none}
  .app:after{content:"";position:fixed;inset:-30px;z-index:0;pointer-events:none;opacity:.85;mix-blend-mode:multiply;background-size:234px 234px;background-image:url("data:image/svg+xml,%3Csvg viewBox='0 0 180 180' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.9' numOctaves='3' stitchTiles='stitch'/%3E%3CfeColorMatrix type='saturate' values='0'/%3E%3CfeComponentTransfer%3E%3CfeFuncR type='linear' slope='2.2' intercept='-.6'/%3E%3CfeFuncG type='linear' slope='2.2' intercept='-.6'/%3E%3CfeFuncB type='linear' slope='2.2' intercept='-.6'/%3E%3C/feComponentTransfer%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");animation:filmGrain .35s steps(1,end) infinite;will-change:transform}
  @keyframes filmGrain{0%{transform:translate(0,0)}10%{transform:translate(-13px,9px)}20%{transform:translate(8px,-16px)}30%{transform:translate(-18px,-8px)}40%{transform:translate(15px,12px)}50%{transform:translate(-7px,-19px)}60%{transform:translate(19px,-5px)}70%{transform:translate(-16px,17px)}80%{transform:translate(6px,-11px)}90%{transform:translate(13px,19px)}}
  .shell{position:relative;z-index:1;width:min(1440px,calc(100% - 64px));padding-top:22px}.topbar{margin-bottom:24px}
  .landing{min-height:0;padding-top:clamp(24px,4vh,48px)}
  .landing-copy{animation:headlineEnter .65s ease-out both}.headline-word{display:inline-block;transition:transform .3s ease,color .3s ease}.headline-word+.headline-word{margin-left:.08em}.headline-word:hover,.headline-word:active{transform:translateY(-5px);color:#fff}.headline-accent .headline-word:hover,.headline-accent .headline-word:active{color:#ffca98}.headline-accent{display:inline-block;animation:accentPulse 7s ease-in-out infinite}
  @keyframes headlineEnter{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:translateY(0)}}@keyframes accentPulse{0%,100%{color:#f9a15b}50%{color:#ffc18a}}
  .copy-toast{position:fixed;z-index:10;top:max(18px,env(safe-area-inset-top));right:24px;max-width:min(380px,calc(100vw - 32px));margin:0;padding:12px 18px;border:1px solid #db996977;border-radius:14px;background:#24201ff2;color:#ffd0ac;box-shadow:0 12px 35px #0008;pointer-events:none;animation:toastFade 3.6s ease both}
  @keyframes toastFade{0%{opacity:0;transform:translateY(-8px)}8%,84%{opacity:1;transform:translateY(0)}100%{opacity:0;transform:translateY(-8px)}}
  .brandmark{background:radial-gradient(circle at 30% 16%,#4269b4,#132547 63%,#0a1225);color:#dce8ff;border:1px solid #87a8db5c;box-shadow:inset 0 1px #dce8ff54,0 8px 26px #0006}
  .brandmark svg{width:26px;height:26px}
  .eyebrow{color:#ffac6b}.dot{background:#ffac6b;box-shadow:none}.connected .dot{box-shadow:none}
  .landing h1 em{color:#f9a15b}.muted,.trackmeta small,.deck-stage-copy p,.deck-track p,.deck-onair,.deck-timeline,.deck-volume,.deck-next-label,.roomid,.queue .index{color:#c9c8c6}.pill,.field{color:#e0dfdd}input::placeholder{color:#a8a8a8}.entry .divider{color:#aaa}
  .card{background:linear-gradient(155deg,#242628e8,#171a1de8);border-color:#ffffff1b}
  .button:not(:disabled){color:#fff;border-color:#ffffff45;background:#ffffff16}.button.ghost:not(:disabled){background:#17191dcc}.button:not(:disabled):hover{background:#ffffff28}
  .button.primary,.likes .button:not(:disabled),.stage-add:not(:disabled){background:#f58a38;border-color:#f58a38;color:#17100b;box-shadow:0 5px 14px #0003}.button.primary:hover:not(:disabled),.likes .button:not(:disabled):hover,.stage-add:not(:disabled):hover{background:#ffa456;border-color:#ffa456}
  .button:focus-visible,input:focus-visible,a:focus-visible,.deck-play:focus-visible,.deck-next:focus-visible,.deck-volume-icon:focus-visible{outline-color:#dfad7b}
  .dashboard{grid-template-columns:minmax(0,1.45fr) minmax(360px,.9fr);gap:22px}.maincol,.sidecol{gap:18px}
  .hostcard{padding:12px 16px;border-color:#a36c4059;background:linear-gradient(100deg,#54382c99,#242629c9 55%,#1b1e21c9)}.hostbadge{color:#deb28a;border-color:#bd855a80}
  .deck{padding:28px 30px 24px;background:radial-gradient(circle at 84% 8%,#8b563836,transparent 48%),linear-gradient(145deg,#242629ed,#171a1ded 72%);border-color:#d59b7040}
  .deck-stage{display:grid;grid-template-columns:minmax(0,1fr) minmax(210px,36%);align-items:stretch;gap:24px;min-height:228px;padding-bottom:24px;border-bottom:1px solid #ffffff1b}
  .deck-stage-copy{display:flex;flex-direction:column;align-items:flex-start;min-width:0;padding:8px 0}
  .deck-status{display:flex;gap:12px;justify-content:space-between;align-items:center;width:100%;margin-bottom:auto}
  .deck-stage-copy h2{font-size:clamp(28px,3.4vw,48px);line-height:1.04;letter-spacing:-.06em;margin:17px 0 10px;overflow-wrap:anywhere}
  .deck-stage-copy p{margin:0;color:#c9c8c6;font-size:14px}
  .deck-stage-copy .bars{margin:auto 0 0;padding-top:20px}
  .deck-stage-copy .stage-add{margin-top:auto}
  .deck-onair.playing{color:#e7ad7e}.deck-onair.playing i{background:#d99661;box-shadow:0 0 9px #d9966190}
  .miniPlayer{width:100%;height:auto;min-height:228px;aspect-ratio:16/10;border-radius:20px;border-color:#ffffff28;background:#101417;box-shadow:inset 0 1px #ffffff12,0 18px 38px #0005}
  .miniPlayer .spectrum{background:radial-gradient(ellipse at 50% 50%,#bb784327,transparent 66%),linear-gradient(150deg,#17212c,#101417 66%)}
  .spectrum i{background:linear-gradient(#e0af87,#a9643e)}.bars i{background:#c98a5b}
  .deck-timeline{margin:24px 0 20px}.deck-range{background:linear-gradient(90deg,#c78658 var(--fill),#ffffff2a var(--fill))}.deck-range::-webkit-slider-thumb{background:#c78658;border-color:#f2c6a5;box-shadow:0 0 0 4px #c7865825}.deck-range::-moz-range-thumb{background:#c78658;border-color:#f2c6a5;box-shadow:0 0 0 4px #c7865825}
  .deck-play{border-color:#f58a38;background:#f58a38;box-shadow:0 5px 14px #0003}.deck-next:not(:disabled){border-color:#ffffff55;background:#ffffff20}
  .deck-message:empty{display:none}.deck-message{margin-top:12px}
  .share-card{padding:22px 24px}.share-card .sectionhead{margin-bottom:10px}.share-card .share{display:grid;grid-template-columns:minmax(0,1fr) 108px;gap:14px}.share-card .sharetext{min-width:0}.share-card .sharetext p{margin:4px 0 10px}.share-card .sharelink{font-size:12px;padding:10px}.share-card .roomid{margin:10px 0 0}.share-card .qr{width:108px;height:108px}
  .yandex-card{scroll-margin-top:24px}.likes{max-height:min(48vh,520px);overscroll-behavior:contain}.likes li{padding:10px 12px}.likes li:hover,.queue li:hover{border-color:#d39a6b4b}
  .sidecol.music-ready .yandex-card{order:-1}
  .authbox{border-color:#ad744c77;background:linear-gradient(120deg,#9d633326,#ffffff05)}.code{border-color:#b6805970}
  .queue li.current{background:#c786581c;border-color:#bd825562}.queue .current .index{color:#e0a573}
  @media(max-width:1000px){.dashboard{grid-template-columns:minmax(0,1fr) minmax(300px,.8fr)}.deck-stage{grid-template-columns:minmax(0,1fr) 210px}.miniPlayer{min-height:205px}}
  @media(max-width:850px){.dashboard{grid-template-columns:1fr}.shell{width:min(100% - 32px,680px)}.deck-stage{grid-template-columns:minmax(0,1fr) minmax(170px,36%)}.sidecol.music-ready .yandex-card{order:0}}
  @media(max-width:550px){.shell{width:min(100% - 24px,680px);padding-top:16px}.topbar{margin-bottom:18px}.landing{padding-top:12px}.copy-toast{right:16px}.deck{padding:20px}.deck-stage{grid-template-columns:1fr;min-height:0;gap:16px;padding-bottom:20px}.deck-stage-copy{min-height:145px}.deck-stage-copy h2{font-size:clamp(27px,8vw,38px)}.miniPlayer{min-height:0;aspect-ratio:16/9}.share-card{padding:20px}.share-card .share{grid-template-columns:minmax(0,1fr) 96px}.share-card .qr{width:96px;height:96px}.likes{max-height:400px}.brand{font-size:20px}}
  @media(prefers-reduced-motion:reduce){.app:after,.bars i,.spectrum i,.landing-copy,.headline-accent,.copy-toast{animation:none}.button,.headline-word{transition:none}.headline-word:hover,.headline-word:active{transform:none}}
`;

export default function App() {
  const [session, setSession] = useState<Session | null>(() => {
    const params = new URLSearchParams(location.search);
    const start = params.get('tgWebAppStartParam') || '';
    const invited = /^l_[a-f0-9]{32}$/.test(start) ? start.slice(2) : params.get('lobby');
    try {
      const saved: unknown = JSON.parse(sessionStorage.getItem(STORAGE_KEY) || 'null');
      if (saved && typeof saved === 'object' && 'lobby_id' in saved &&
          typeof saved.lobby_id === 'string' && /^[a-f0-9]{32}$/.test(saved.lobby_id) &&
          'role' in saved && (saved.role === 'Member' ||
          (saved.role === 'Main' && 'host_token' in saved && typeof saved.host_token === 'string'))) {
        if (invited && /^[a-f0-9]{32}$/.test(invited) && invited !== saved.lobby_id)
          return { lobby_id: invited, role: 'Member' };
        return saved as Session;
      }
    } catch {}
    if (invited && /^[a-f0-9]{32}$/.test(invited)) return { lobby_id: invited, role: 'Member' };
    return null;
  });
  const [roomId, setRoomId] = useState('');
  const [trackId, setTrackId] = useState('');
  const [queueItems, setQueueItems] = useState<QueueItem[]>([]);
  const tracksQueue = useMemo(() => queueItems.map((item) => item.track_id), [queueItems]);
  const [ownerId, setOwnerId] = useState('');
  const [profiles, setProfiles] = useState<Record<string, Profile>>({});
  const [mainProfile, setMainProfile] = useState<Profile | null>(null);
  const [me, setMe] = useState<Profile | null>(null);
  const [telegramReady, setTelegramReady] = useState(!telegramInitData);
  const [memberCount, setMemberCount] = useState(0);
  const [queueRevision, setQueueRevision] = useState(0);
  const [queueActionPending, setQueueActionPending] = useState(false);
  const [trackTitles, setTrackTitles] = useState<Record<string, string>>({});
  const [playbackRevision, setPlaybackRevision] = useState(0);
  const [status, setStatus] = useState<'connecting' | 'authenticating' | 'connected' | 'closed'>('closed');
  const [error, setError] = useState('');
  const [storageWarning, setStorageWarning] = useState('');
  const [creating, setCreating] = useState(false);
  const [loadingMetadata, setLoadingMetadata] = useState(false);
  const [copyNotice, setCopyNotice] = useState<{ message: string; id: number } | null>(null);
  const copyNoticeId = useRef(0);
  const [attempt, setAttempt] = useState(0);
  const [yandexLogin, setYandexLogin] = useState<YandexLogin | null>(null);
  const [yandexTracks, setYandexTracks] = useState<YandexTrack[]>([]);
  const [yandexConnected, setYandexConnected] = useState(false);
  const [yandexSession, setYandexSession] = useState(() => { try { return sessionStorage.getItem(YANDEX_KEY) || ''; } catch { return ''; } });
  const [hasMoreLikes, setHasMoreLikes] = useState(false);
  const [nextLikesOffset, setNextLikesOffset] = useState(0);
  const [audioUrl, setAudioUrl] = useState('');
  const [audioError, setAudioError] = useState('');
  const [fallbackId, setFallbackId] = useState('');
  const [yandexSearch, setYandexSearch] = useState('');
  const [streamAttempt, setStreamAttempt] = useState(0);
  const [activeAudio, setActiveAudio] = useState(0);
  const activeAudioRef = useRef(0);
  const slotKeys = useRef(['', '']);
  const [playingKey, setPlayingKey] = useState('');
  const [playbackTime, setPlaybackTime] = useState(0);
  const [playbackDuration, setPlaybackDuration] = useState(0);
  const [volume, setVolume] = useState(80);
  const volumeRef = useRef(0.8);
  const previousVolume = useRef(80);
  const [yandexError, setYandexError] = useState('');
  const [loadingLikes, setLoadingLikes] = useState(false);
  const likesRequest = useRef<AbortController | null>(null);
  const loginTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const socket = useRef<WebSocket | null>(null);
  const audioPlayers = [useRef<HTMLAudioElement>(null), useRef<HTMLAudioElement>(null)];
  const preloaded = useRef<{ id: string; url: string; slot: number; key: string } | null>(null);
  const promoting = useRef<{ id: string; url: string; slot: number; revision: number } | null>(null);
  const fadeFrame = useRef<number | null>(null);
  const fading = useRef(false);
  const skipSent = useRef('');
  const ytMount = useRef<HTMLDivElement>(null);
  const ytPlayer = useRef<YtPlayer | null>(null);
  const preparedAudio = useRef(new Map<string, Promise<string>>());
  const retryCount = useRef(0);
  const lastRoom = useRef('');
  const draggingItem = useRef('');

  useEffect(() => {
    if (!copyNotice) return;
    const timer = window.setTimeout(() => setCopyNotice(null), 3600);
    return () => window.clearTimeout(timer);
  }, [copyNotice]);

  useEffect(() => {
    volumeRef.current = volume / 100;
    if (!fading.current) {
      const current = audioPlayers[activeAudioRef.current].current;
      if (current) current.volume = volumeRef.current;
    }
    ytPlayer.current?.setVolume(volume);
  }, [volume]);

  function syncPlaybackTime(player: HTMLAudioElement) {
    setPlaybackTime(Number.isFinite(player.currentTime) ? player.currentTime : 0);
    setPlaybackDuration(Number.isFinite(player.duration) ? player.duration : 0);
  }

  function setPlayerVolume(value: number) {
    const next = Math.max(0, Math.min(100, value));
    if (next > 0) previousVolume.current = next;
    volumeRef.current = next / 100;
    setVolume(next);
  }

  useEffect(() => {
    if (!telegramInitData) return;
    telegram?.ready();
    telegram?.expand();
    const controller = new AbortController();
    void fetch('/api/lobby/telegram/me', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ init_data: telegramInitData }), signal: controller.signal,
    }).then(async (response) => {
      if (!response.ok) throw new Error('Не удалось подтвердить профиль Telegram. Откройте миниапп снова.');
      return response.json();
    }).then((data: { profile: Profile }) => { setMe(data.profile); setTelegramReady(true); })
      .catch((cause) => { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : 'Ошибка входа через Telegram.'); });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    try {
      if (session) sessionStorage.setItem(STORAGE_KEY, JSON.stringify(session));
      else sessionStorage.removeItem(STORAGE_KEY);
    } catch { setStorageWarning('Браузер не разрешил сохранить вход. После перезагрузки войдите снова.'); }
  }, [session]);

  useEffect(() => {
    try { if (yandexSession) sessionStorage.setItem(YANDEX_KEY, yandexSession); else sessionStorage.removeItem(YANDEX_KEY); } catch {}
  }, [yandexSession]);

  useEffect(() => {
    const ws = socket.current;
    if (!yandexSession || status !== 'connected' || ws?.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ action: 'set_yandex_session', yandex_session: yandexSession }));
  }, [yandexSession, status, session?.lobby_id]);

  useEffect(() => {
    if (!yandexSession) return;
    const controller = new AbortController();
    setLoadingLikes(true);
    void fetch('/api/yandex/tracks?offset=0', { headers: { 'X-Yandex-Session': yandexSession }, signal: controller.signal })
      .then(async (response) => { if (!response.ok) throw new Error(String(response.status)); return response.json(); })
      .then((data: { tracks: YandexTrack[]; has_more: boolean; next_offset: number }) => {
        if (controller.signal.aborted) return;
        setYandexTracks(data.tracks); setHasMoreLikes(data.has_more); setNextLikesOffset(data.next_offset); setYandexConnected(true);
      }).catch((cause) => {
        if (controller.signal.aborted) return;
        if (cause instanceof Error && cause.message === '401') { setYandexSession(''); setYandexConnected(false); setYandexError('Сессия Яндекса истекла. Подключите аккаунт заново.'); }
      }).finally(() => { if (!controller.signal.aborted) setLoadingLikes(false); });
    return () => controller.abort();
  }, [yandexSession]);

  useEffect(() => {
    if (!session || !telegramReady) return;
    let active = true;
    setStatus('connecting');
    if (lastRoom.current !== session.lobby_id) {
      lastRoom.current = session.lobby_id;
      setQueueItems([]); setTrackTitles({}); setPlaybackRevision(0); setQueueRevision(0); setOwnerId('');
      setProfiles({}); setMainProfile(null); setMemberCount(0);
    }
    setError('');
    const url = new URL(`/api/lobby/${encodeURIComponent(session.lobby_id)}/ws`, location.href);
    url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(url);
    socket.current = ws;
    ws.onopen = () => {
      if (!active) return;
      setStatus('authenticating');
      try { ws.send(JSON.stringify({ ...(session.role === 'Main' ? { host_token: session.host_token } :
        { member_token: memberToken(session.lobby_id) }),
        ...(telegramInitData ? { telegram_init_data: telegramInitData } : {}) })); }
      catch { setError('Не удалось сохранить идентификатор гостя в браузере.'); ws.close(); }
    };
    ws.onmessage = (event) => {
      if (!active) return;
      try {
        const message: unknown = JSON.parse(event.data);
        if (!message || typeof message !== 'object') throw new Error();
        if ('type' in message && message.type === 'identity' &&
            'owner_id' in message && typeof message.owner_id === 'string' &&
            /^[a-f0-9]{32}$/.test(message.owner_id)) {
          setOwnerId(message.owner_id);
        } else if ('type' in message && message.type === 'lobby_state' &&
            'lobby_id' in message && message.lobby_id === session.lobby_id &&
            'queue_items' in message && Array.isArray(message.queue_items) &&
            message.queue_items.every((item: unknown) => item && typeof item === 'object' &&
              'id' in item && typeof item.id === 'string' && /^[a-f0-9]{32}$/.test(item.id) &&
              'track_id' in item && typeof item.track_id === 'string' && QUEUE_ID.test(item.track_id) &&
              'owner_id' in item && typeof item.owner_id === 'string' && /^[a-f0-9]{32}$/.test(item.owner_id) &&
              (!('access_revision' in item) || typeof item.access_revision === 'number' &&
                Number.isSafeInteger(item.access_revision) && item.access_revision >= 0)) &&
            'playback_revision' in message && typeof message.playback_revision === 'number' &&
            Number.isSafeInteger(message.playback_revision) && message.playback_revision >= 0 &&
            'queue_revision' in message && typeof message.queue_revision === 'number' &&
            Number.isSafeInteger(message.queue_revision) && message.queue_revision >= 0) {
          setQueueItems(message.queue_items as QueueItem[]);
          if ('profiles' in message && message.profiles && typeof message.profiles === 'object')
            setProfiles(message.profiles as Record<string, Profile>);
          if ('main_profile' in message) setMainProfile(message.main_profile as Profile | null);
          if ('members' in message && Array.isArray(message.members)) setMemberCount(message.members.length);
          setQueueRevision(message.queue_revision);
          setQueueActionPending(false);
          if ('track_titles' in message && message.track_titles && typeof message.track_titles === 'object') {
            setTrackTitles(message.track_titles as Record<string, string>);
          }
          setPlaybackRevision(message.playback_revision);
          setStatus('connected');
          retryCount.current = 0;
        } else if ('type' in message && message.type === 'error' &&
                   'error' in message && typeof message.error === 'string') {
          setError(`Сервер: ${message.error}`);
          setQueueActionPending(false);
        } else throw new Error();
      } catch { setError('Не удалось прочитать сообщение сервера.'); }
    };
    ws.onerror = () => { if (active) setError('Ошибка WebSocket. Проверьте ID комнаты и доступность сервера.'); };
    ws.onclose = (event) => {
      if (!active) return;
      setStatus('closed');
      setQueueActionPending(false);
      setError(`Соединение закрыто (${event.code})${event.reason ? `: ${event.reason}` : ''}.`);
      if (event.code !== 1008 && retryCount.current < 8) {
        const delay = Math.min(1000 * 2 ** retryCount.current++, 10000);
        retryTimer = setTimeout(() => setAttempt((value) => value + 1), delay);
      }
    };
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    const resume = () => { if (document.visibilityState === 'visible' && ws.readyState === WebSocket.CLOSED) {
      if (retryTimer) clearTimeout(retryTimer);
      setAttempt((value) => value + 1);
    } };
    document.addEventListener('visibilitychange', resume);
    window.addEventListener('pageshow', resume);
    return () => {
      active = false;
      if (retryTimer) clearTimeout(retryTimer);
      document.removeEventListener('visibilitychange', resume);
      window.removeEventListener('pageshow', resume);
      socket.current = null;
      ws.close();
    };
  }, [session, attempt, telegramReady]);

  useEffect(() => () => { likesRequest.current?.abort(); if (loginTimer.current) clearTimeout(loginTimer.current); }, []);

  const currentItem = queueItems[0];
  const currentTrack = currentItem?.track_id;
  const currentKey = `${playbackRevision}:${currentItem?.id}`;
  const nextItem = queueItems[1];
  const nextTrack = nextItem?.track_id;
  const playbackCredential = currentTrack?.startsWith('yandex_')
    ? `${currentItem.id}:${currentItem.access_revision || 0}` : session?.host_token;
  const nextCredential = nextTrack?.startsWith('yandex_')
    ? `${nextItem.id}:${nextItem.access_revision || 0}` : session?.host_token;
  const audioCacheKey = (item: QueueItem) => `${item.id}:${item.access_revision || 0}`;
  async function resolveAudio(item: QueueItem, signal: AbortSignal): Promise<string> {
    const yandex = item.track_id.startsWith('yandex_');
    if (!session?.host_token || !session.lobby_id) throw new Error('Нет доступа хоста к воспроизведению.');
    const response = await fetch(yandex ?
      `/api/yandex/queue/${session.lobby_id}/${item.id}/stream` : `/api/youtube/audio/${item.track_id}`, {
      method: yandex ? 'GET' : 'POST', signal,
      headers: yandex ? { 'X-Host-Token': session.host_token } :
        { 'X-Lobby-Id': session.lobby_id, 'X-Host-Token': session.host_token },
    });
    const data: { url?: string; detail?: string } = await response.json();
    if (!response.ok || !data.url) throw new Error(data.detail || `Аудиопоток недоступен (HTTP ${response.status}).`);
    if (yandex ? new URL(data.url).protocol !== 'https:' :
        !/^\/api\/youtube\/audio\/file\/[A-Za-z0-9_-]+$/.test(data.url)) throw new Error('Некорректная аудиоссылка.');
    return data.url;
  }
  function prepareAudio(item: QueueItem): Promise<string> {
    const key = audioCacheKey(item);
    const cached = preparedAudio.current.get(key);
    if (cached) return cached;
    if (preparedAudio.current.size >= 8) preparedAudio.current.delete(preparedAudio.current.keys().next().value!);
    const promise = resolveAudio(item, new AbortController().signal).catch((error) => {
      if (preparedAudio.current.get(key) === promise) preparedAudio.current.delete(key);
      throw error;
    });
    preparedAudio.current.set(key, promise);
    return promise;
  }
  useEffect(() => { preparedAudio.current.clear(); }, [session?.lobby_id, session?.host_token]);
  useEffect(() => {
    if (session?.role !== 'Main') return;
    for (const item of queueItems.slice(1, 4)) void prepareAudio(item).catch(() => {});
  }, [queueItems, session?.role, session?.lobby_id, session?.host_token]);
  useEffect(() => {
    const promotion = promoting.current;
    if (promotion && currentItem?.id === promotion.id && playbackRevision === promotion.revision) {
      promoting.current = null;
      activeAudioRef.current = promotion.slot;
      slotKeys.current[promotion.slot] = currentKey;
      setActiveAudio(promotion.slot);
      setAudioUrl(promotion.url);
      setAudioError('');
      setPlayingKey(currentKey);
      const old = audioPlayers[1 - promotion.slot].current;
      const next = audioPlayers[promotion.slot].current;
      if (next) syncPlaybackTime(next);
      if (old) { old.pause(); old.removeAttribute('src'); old.load(); old.volume = volumeRef.current; }
      slotKeys.current[1 - promotion.slot] = '';
      return;
    }
    promoting.current = null;
    if (fadeFrame.current !== null) cancelAnimationFrame(fadeFrame.current);
    fading.current = false;
    preloaded.current = null;
    slotKeys.current = ['', ''];
    setPlayingKey('');
    setPlaybackTime(0);
    setPlaybackDuration(0);
    for (const ref of audioPlayers) {
      const player = ref.current;
      if (player) { player.pause(); player.removeAttribute('src'); player.load(); player.volume = volumeRef.current; }
    }
    setAudioUrl('');
    setAudioError('');
    setFallbackId('');
    if (session?.role !== 'Main' || !currentItem) return;
    const controller = new AbortController();
    void (async () => {
      try {
        const url = await prepareAudio(currentItem);
        if (!controller.signal.aborted) {
          const slot = activeAudioRef.current;
          const player = audioPlayers[slot].current;
          slotKeys.current[slot] = currentKey;
          if (player) { player.src = url; player.volume = volumeRef.current; player.load(); void player.play().catch(() => setAudioError('Нажмите ▶, чтобы начать воспроизведение.')); }
          setAudioUrl(url);
        }
      } catch (cause) {
        if (!controller.signal.aborted) {
          if (currentItem.track_id.startsWith('yandex_')) {
            setAudioError(cause instanceof Error ? cause.message : 'Не удалось открыть аудио.');
            setPlayingKey('');
          } else setFallbackId(currentTrack);
        }
      }
    })();
    return () => controller.abort();
  }, [currentItem?.id, playbackRevision, session?.role, session?.lobby_id, playbackCredential, streamAttempt]);

  useEffect(() => {
    if (session?.role !== 'Main') return;
    if (promoting.current && playbackRevision < promoting.current.revision) return;
    if (fading.current && preloaded.current?.id !== nextItem?.id) {
      fading.current = false;
      if (fadeFrame.current !== null) cancelAnimationFrame(fadeFrame.current);
      const current = audioPlayers[activeAudioRef.current].current;
      if (current) current.volume = volumeRef.current;
    }
    const controller = new AbortController();
    const slot = 1 - activeAudioRef.current;
    const player = audioPlayers[slot].current;
    if (player) { player.pause(); player.removeAttribute('src'); player.load(); player.volume = 0; }
    slotKeys.current[slot] = '';
    preloaded.current = null;
    if (!nextItem) return;
    void (async () => {
      try {
        const url = await prepareAudio(nextItem);
        if (!controller.signal.aborted && player) {
          player.src = url;
          player.load();
          player.currentTime = 0;
          slotKeys.current[slot] = `${playbackRevision + 1}:${nextItem.id}`;
          preloaded.current = { id: nextItem.id, url, slot, key: currentKey };
        }
      } catch {}
    })();
    return () => controller.abort();
  }, [currentKey, nextItem?.id, activeAudio, session?.role, session?.lobby_id, nextCredential]);

  useEffect(() => {
    if (!fallbackId || fallbackId !== currentTrack || session?.role !== 'Main' || !ytMount.current) return;
    let active = true;
    const startupTimer = window.setTimeout(() => {
      if (active) { setAudioError('YouTube не начал воспроизведение. Переходим к следующему треку.'); skipTrack(); }
    }, 15000);
    void youtubeApi().then((api) => {
      if (!active || !ytMount.current) return;
      const mount = document.createElement('div');
      ytMount.current.append(mount);
      ytPlayer.current = new api.Player(mount, {
        videoId: fallbackId, width: 220, height: 200,
        playerVars: { autoplay: 1, playsinline: 1, controls: 0, origin: location.origin },
        events: {
          onReady: (event) => { if (active) { ytPlayer.current = event.target; event.target.setVolume(volumeRef.current * 100); event.target.playVideo(); } },
          onStateChange: (event) => {
            if (!active) return;
            if (event.data === 1) { clearTimeout(startupTimer); setPlayingKey(currentKey); }
            if (event.data === 2) setPlayingKey('');
            if (event.data === 0) skipTrack();
          },
          onError: (event) => {
            if (!active) return;
            setAudioError(`YouTube не разрешил воспроизведение (${event.data}). Переходим к следующему треку.`);
            skipTrack();
          },
        },
      });
    }).catch((cause) => {
      if (active) { setAudioError(cause instanceof Error ? cause.message : 'Плеер YouTube недоступен.'); skipTrack(); }
    });
    return () => {
      active = false; clearTimeout(startupTimer);
      if (typeof ytPlayer.current?.destroy === 'function') ytPlayer.current.destroy();
      ytPlayer.current = null;
      ytMount.current?.replaceChildren();
    };
  }, [fallbackId, currentKey, session?.role]);

  useEffect(() => {
    if (!currentTrack || fallbackId !== currentTrack || session?.role !== 'Main') return;
    const timer = window.setInterval(() => {
      const player = ytPlayer.current;
      if (!player) return;
      const duration = player.getDuration();
      const time = player.getCurrentTime();
      setPlaybackDuration(Number.isFinite(duration) ? duration : 0);
      setPlaybackTime(Number.isFinite(time) ? time : 0);
    }, 400);
    return () => clearInterval(timer);
  }, [fallbackId, currentTrack, session?.role]);

  async function createParty() {
    if (!telegramReady) return;
    setCreating(true);
    setError('');
    try {
      const response = await fetch('/api/lobby/create', { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ init_data: telegramInitData }) });
      if (!response.ok) throw new Error(`Не удалось создать комнату (HTTP ${response.status}).`);
      const data: unknown = await response.json();
      if (!data || typeof data !== 'object' || !('lobby_id' in data) ||
          typeof data.lobby_id !== 'string' || !/^[a-f0-9]{32}$/.test(data.lobby_id) ||
          !('host_token' in data) || typeof data.host_token !== 'string' || !data.host_token) {
        throw new Error('Сервер вернул некорректные данные комнаты.');
      }
      setSession({ lobby_id: data.lobby_id, host_token: data.host_token, role: 'Main' });
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Не удалось создать комнату.'); }
    finally { setCreating(false); }
  }

  function joinParty(event: FormEvent) {
    event.preventDefault();
    const id = roomId.trim();
    if (!/^[a-f0-9]{32}$/.test(id)) { setError('Введите ID комнаты: 32 символа из 0–9 и a–f.'); return; }
    setSession({ lobby_id: id, role: 'Member' });
  }

  async function addTrack(event: FormEvent) {
    event.preventDefault();
    if (loadingMetadata) return;
    const id = youtubeId(trackId);
    if (!id) { setError('Вставьте корректную ссылку YouTube или Video ID из 11 символов.'); return; }
    setLoadingMetadata(true);
    try {
      const response = await fetch(`/api/youtube/metadata/${id}`);
      if (!response.ok) throw new Error('Не удалось получить название YouTube. Повторите добавление.');
      const data: { title: string; artist: string } = await response.json();
      if (!data.title || !data.artist) throw new Error('YouTube не вернул название и исполнителя.');
      if (enqueue(id, `${data.title} — ${data.artist}`)) setTrackId('');
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Не удалось получить название YouTube.'); }
    finally { setLoadingMetadata(false); }
  }

  async function copy(value: string, label: string) {
    const copied = `${label} скопирован${label === 'Ссылка' ? 'а' : ''}.`;
    const notify = (message: string) => setCopyNotice({ message, id: ++copyNoticeId.current });
    const fallback = () => {
      const input = document.createElement('textarea');
      input.value = value; input.style.position = 'fixed'; input.style.opacity = '0';
      document.body.append(input); input.focus(); input.select();
      let ok = false;
      try { ok = document.execCommand('copy'); } catch {}
      input.remove(); return ok;
    };
    if (!navigator.clipboard?.writeText) { notify(fallback() ? copied : 'Не удалось скопировать. Выделите и скопируйте вручную.'); return; }
    try { await navigator.clipboard.writeText(value); notify(copied); }
    catch { notify(fallback() ? copied : 'Не удалось скопировать. Выделите и скопируйте вручную.'); }
  }

  function enqueue(id: string, title?: string) {
    const ws = socket.current;
    if (status !== 'connected' || ws?.readyState !== WebSocket.OPEN) return false;
    if (id.startsWith('yandex_') && !yandexSession) {
      setError('Подключите Яндекс Музыку перед добавлением трека.');
      return false;
    }
    setError('');
    try { ws.send(JSON.stringify({ action: 'add_track', track_id: id,
      ...(title ? { title: title.slice(0, 200) } : {}),
      ...(id.startsWith('yandex_') ? { yandex_session: yandexSession } : {}) })); return true; }
    catch { setError('Не удалось отправить трек. Подключитесь снова.'); return false; }
  }

  async function startYandexLogin() {
    const controller = new AbortController();
    likesRequest.current?.abort();
    if (loginTimer.current) clearTimeout(loginTimer.current);
    likesRequest.current = controller;
    setLoadingLikes(true);
    setYandexLogin(null);
    setYandexTracks([]);
    setYandexError('');
    setYandexConnected(false);
    setYandexSession('');
    try {
      const response = await fetch('/api/yandex/login', { method: 'POST', signal: controller.signal });
      const data: unknown = await response.json();
      if (!response.ok) {
        const detail = data && typeof data === 'object' && 'detail' in data && typeof data.detail === 'string' ? data.detail : `HTTP ${response.status}`;
        throw new Error(detail);
      }
      if (!data || typeof data !== 'object' || !('login_id' in data) || typeof data.login_id !== 'string' ||
          !('verification_url' in data) || typeof data.verification_url !== 'string' ||
          !('user_code' in data) || typeof data.user_code !== 'string' ||
          !('interval' in data) || typeof data.interval !== 'number' ||
          !('expires_in' in data) || typeof data.expires_in !== 'number') throw new Error('Яндекс вернул некорректный код входа.');
      if (!controller.signal.aborted) {
        const login = data as YandexLogin;
        setYandexLogin(login);
        loginTimer.current = setTimeout(() => void checkYandexLogin(login.login_id, controller), Math.max(1, login.interval) * 1000);
      }
    } catch (cause) {
      if (!controller.signal.aborted) {
        setYandexError(cause instanceof Error ? cause.message : 'Не удалось начать вход в Яндекс Музыку.');
        setLoadingLikes(false);
      }
    }
  }

  async function checkYandexLogin(id: string, controller: AbortController) {
    if (controller.signal.aborted) return;
    try {
      const response = await fetch(`/api/yandex/login/${encodeURIComponent(id)}`, { signal: controller.signal });
      const data: unknown = await response.json();
      if (!response.ok) {
        const detail = data && typeof data === 'object' && 'detail' in data && typeof data.detail === 'string' ? data.detail : `HTTP ${response.status}`;
        throw new Error(detail);
      }
      if (!data || typeof data !== 'object' || !('status' in data)) throw new Error('Яндекс вернул некорректный ответ.');
      if (data.status === 'pending' && 'retry_after' in data && typeof data.retry_after === 'number') {
        loginTimer.current = setTimeout(() => void checkYandexLogin(id, controller), Math.max(1, data.retry_after) * 1000);
        return;
      }
      if (data.status !== 'authorized' || !('authenticated' in data) || data.authenticated !== true ||
          !('tracks' in data) || !Array.isArray(data.tracks) || !data.tracks.every((track) => track &&
          typeof track.id === 'string' && /^[0-9]{1,20}$/.test(track.id) &&
          typeof track.title === 'string' && typeof track.artist === 'string')) throw new Error('Яндекс вернул некорректный список треков.');
      if (!controller.signal.aborted && 'session' in data && typeof data.session === 'string') {
        setYandexTracks(data.tracks);
        setYandexSession(data.session);
        setHasMoreLikes('has_more' in data && data.has_more === true);
        setNextLikesOffset('next_offset' in data && typeof data.next_offset === 'number' ? data.next_offset : data.tracks.length);
        setYandexConnected(true);
        setYandexLogin(null);
        setLoadingLikes(false);
      }
    } catch (cause) {
      if (!controller.signal.aborted) {
        setYandexError(cause instanceof Error ? cause.message : 'Не удалось завершить вход в Яндекс Музыку.');
        setYandexLogin(null);
        setLoadingLikes(false);
      }
    }
  }

  async function showMoreLikes() {
    if (!yandexSession || !hasMoreLikes || loadingLikes) return;
    setLoadingLikes(true);
    setYandexError('');
    try {
      const response = await fetch(`/api/yandex/tracks?offset=${nextLikesOffset}`, {
        headers: { 'X-Yandex-Session': yandexSession },
      });
      const data: unknown = await response.json();
      if (!response.ok) throw new Error(`Не удалось загрузить следующую порцию (HTTP ${response.status}).`);
      if (!data || typeof data !== 'object' || !('tracks' in data) || !Array.isArray(data.tracks) ||
          !('next_offset' in data) || typeof data.next_offset !== 'number' ||
          !('has_more' in data) || typeof data.has_more !== 'boolean') throw new Error('Некорректный ответ Яндекса.');
      setYandexTracks((previous) => [...previous, ...data.tracks as YandexTrack[]]);
      setNextLikesOffset(data.next_offset);
      setHasMoreLikes(data.has_more);
    } catch (cause) { setYandexError(cause instanceof Error ? cause.message : 'Не удалось загрузить треки.'); }
    finally { setLoadingLikes(false); }
  }

  function skipTrack() {
    const ws = socket.current;
    if (session?.role !== 'Main' || !tracksQueue.length ||
        status !== 'connected' || ws?.readyState !== WebSocket.OPEN || skipSent.current === currentKey) return false;
    setError('');
    try { ws.send(JSON.stringify({ action: 'skip_track', expected_revision: playbackRevision })); skipSent.current = currentKey; return true; }
    catch { setError('Не удалось переключить трек. Подключитесь снова.'); return false; }
  }

  function togglePlayback() {
    if (!currentTrack || session?.role !== 'Main') return;
    setAudioError('');
    if (fallbackId === currentTrack) {
      if (playingKey === currentKey) ytPlayer.current?.pauseVideo();
      else ytPlayer.current?.playVideo();
      return;
    }
    const current = audioPlayers[activeAudioRef.current].current;
    if (!current || !audioUrl) return;
    if (!current.paused) {
      if (fading.current) {
        fading.current = false;
        if (fadeFrame.current !== null) cancelAnimationFrame(fadeFrame.current);
        audioPlayers[1 - activeAudioRef.current].current?.pause();
        current.volume = volumeRef.current;
      }
      current.pause();
    } else void current.play().catch(() => setAudioError('Не удалось начать воспроизведение.'));
  }

  function seekPlayback(seconds: number) {
    if (!currentTrack || !playbackDuration) return;
    const target = Math.max(0, Math.min(seconds, playbackDuration));
    setPlaybackTime(target);
    if (fallbackId === currentTrack) ytPlayer.current?.seekTo(target, true);
    else {
      const current = audioPlayers[activeAudioRef.current].current;
      if (current && Number.isFinite(current.duration)) current.currentTime = target;
    }
  }

  function changeQueue(action: 'remove_track' | 'move_track', itemId: string, toIndex?: number) {
    const ws = socket.current;
    if (status !== 'connected' || ws?.readyState !== WebSocket.OPEN || queueActionPending) return;
    try {
      ws.send(JSON.stringify({ action, item_id: itemId, expected_queue_revision: queueRevision,
        ...(toIndex === undefined ? {} : { to_index: toIndex }) }));
      setQueueActionPending(true);
    } catch { setError('Не удалось изменить очередь. Подключитесь снова.'); }
  }

  function finishFade() {
    if (!fading.current) return;
    fading.current = false;
    if (fadeFrame.current !== null) cancelAnimationFrame(fadeFrame.current);
    fadeFrame.current = null;
    const ready = preloaded.current;
    if (!ready || ready.key !== currentKey) return;
    const old = audioPlayers[activeAudio].current;
    const next = audioPlayers[ready.slot].current;
    if (old) { old.pause(); old.volume = volumeRef.current; }
    if (next) next.volume = volumeRef.current;
    promoting.current = { id: ready.id, url: ready.url, slot: ready.slot, revision: playbackRevision + 1 };
    if (!skipTrack()) { promoting.current = null; next?.pause(); }
  }

  function maybeCrossfade(slot: number) {
    if (slot !== activeAudioRef.current || slotKeys.current[slot] !== currentKey ||
        fading.current || skipSent.current === currentKey) return;
    const old = audioPlayers[slot].current;
    const ready = preloaded.current;
    const next = ready && audioPlayers[ready.slot].current;
    if (!old || old.paused || !ready || ready.key !== currentKey || ready.id !== nextItem?.id || !next ||
        next.readyState < HTMLMediaElement.HAVE_CURRENT_DATA || !Number.isFinite(old.duration) ||
        old.duration - old.currentTime > 3) return;
    fading.current = true;
    old.volume = volumeRef.current;
    next.pause();
    next.currentTime = 0;
    next.volume = 0;
    void next.play().then(() => {
      if (!fading.current) return;
      const started = performance.now();
      const tick = (now: number) => {
        if (!fading.current) return;
        const elapsed = (now - started) / 1000;
        old.volume = volumeRef.current * Math.max(0, 1 - elapsed / 3);
        next.volume = volumeRef.current * Math.min(1, elapsed / 2);
        if (elapsed >= 3) finishFade();
        else fadeFrame.current = requestAnimationFrame(tick);
      };
      fadeFrame.current = requestAnimationFrame(tick);
    }).catch(() => { fading.current = false; old.volume = volumeRef.current; next.pause(); });
  }

  const statusLabel = { connecting: 'Подключение…', authenticating: 'Вход…', connected: 'Подключено', closed: 'Отключено' }[status];
  const inviteUrl = session ? `${location.origin}${location.pathname}?lobby=${session.lobby_id}` : '';
  const qr = useMemo(() => inviteUrl ? qrPath(inviteUrl) : '', [inviteUrl]);
  const filteredYandexTracks = useMemo(() => {
    const query = yandexSearch.trim().toLocaleLowerCase('ru');
    return query ? yandexTracks.filter((track) => `${track.title} ${track.artist}`.toLocaleLowerCase('ru').includes(query)) : yandexTracks;
  }, [yandexTracks, yandexSearch]);
  const isYandex = currentTrack?.startsWith('yandex_');
  const nowTitle = currentTrack ? trackTitles[currentTrack] || (isYandex ? 'Трек Яндекс Музыки' : `YouTube · ${currentTrack}`) : 'Музыка начинается здесь';
  return <main className={`app ${telegramInitData ? 'telegram-app' : ''}`}><style>{STYLES}</style><div className="shell">
    <header className="topbar"><div className="brand"><span className="brandmark"><svg viewBox="0 0 32 32" fill="none" aria-hidden="true"><path d="M6 23.5c4.7-7.3 9-11.2 20-15M6 16c6.8-3.9 13-5.4 20-4.5M8 27c3.9-4.9 8.3-7.1 16-8" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"/><circle cx="8" cy="27" r="2" fill="currentColor"/></svg></span> PRADA SOUND</div>
      {me ? <span className="profile-chip"><Avatar profile={me} size={32} /><span>{profileLabel(me)}</span></span> :
        <span className="pill"><span className="dot" /> PRIVATE MUSIC ROOM</span>}</header>
    {storageWarning && <p className="notice" role="alert">{storageWarning}</p>}
    {error && <p className="notice" role="alert">{error}</p>}
    {copyNotice && <p key={copyNotice.id} className="copy-toast" role="status">{copyNotice.message}</p>}
    {!session ? <div className="landing">
      <div className="landing-copy"><span className="eyebrow">YOUR NIGHT · YOUR SOUND</span><h1><span className="headline-word">Одна</span>{' '}<span className="headline-word">комната.</span><br /><em className="headline-accent"><span className="headline-word">Один</span>{' '}<span className="headline-word">вайб.</span></em></h1>
        <p className="muted">Создайте вечеринку, позовите друзей и собирайте общий плейлист вместе. Музыка звучит на устройстве хоста.</p></div>
      <section className="card entry"><span className="eyebrow">НАЧАТЬ ВЕЧЕРИНКУ</span><h2>Музыка ждёт вас</h2>
        <p className="muted small">Создайте свою комнату или войдите по приглашению.</p>
        {telegramInitData && !telegramReady && !error && <p className="muted small">Подтверждаем ваш профиль Telegram…</p>}
        <button className="button primary" style={{ width: '100%' }} disabled={creating || !telegramReady} onClick={createParty}>{creating ? 'Создаём комнату…' : 'Создать вечеринку ↗'}</button>
        <div className="divider">ИЛИ</div><form onSubmit={joinParty}>
          <label className="field">ID комнаты<input value={roomId} onChange={(event) => setRoomId(event.target.value)} required maxLength={32} disabled={creating} placeholder="Вставьте ID комнаты" /></label>
          <button className="button" disabled={creating || !telegramReady || !roomId.trim()}>Войти в комнату</button>
        </form></section>
    </div> : <>
      <div className="roomhead"><div><span className="eyebrow">{session.role === 'Main' ? 'ВАША ВЕЧЕРИНКА' : 'ВЫ В ГОСТЯХ'}</span>
        <h1>{session.role === 'Main' ? 'Пульт вечеринки' : 'Общий плейлист'}</h1>
        <div className="roomstatus"><span className={`pill ${status === 'connected' ? 'connected' : ''}`} role="status"><span className="dot" />{statusLabel}</span>
          <span className="pill">{memberCount} в комнате</span></div>
      </div><div className="actions">
        <button className="button ghost" onClick={() => document.getElementById('share-room')?.scrollIntoView({ behavior: 'smooth', block: 'center' })}>Пригласить</button>
        {status === 'closed' && <button className="button" onClick={() => setAttempt((value) => value + 1)}>Подключиться снова</button>}
        <button className="button ghost" onClick={() => { setSession(null); setQueueItems([]); setTrackId(''); setError(''); }}>Выйти</button>
      </div></div>
      <div className="dashboard"><div className="maincol">
        <section className="hostcard" aria-label="Организатор вечеринки"><Avatar profile={mainProfile} size={54} />
          <div><span className="eyebrow">{session.role === 'Main' ? 'ВЫ ПРИНИМАЕТЕ ГОСТЕЙ' : 'ВЫ В ГОСТЯХ У'}</span>
            <strong>{mainProfile ? profileLabel(mainProfile) : 'Хост подключается…'}</strong>
            {mainProfile?.username && <small>{mainProfile.name}</small>}</div>
          <span className="hostbadge">MAIN</span>
        </section>
        {session.role === 'Member' && <section className="card cardpad hero">
          <div className="heroText"><span className="eyebrow">{currentTrack ? 'СЕЙЧАС ИГРАЕТ' : 'ГОТОВО К СТАРТУ'}</span>
            <h2>{nowTitle}</h2><p className="muted small">{currentTrack ? (isYandex ? 'Яндекс Музыка' : 'YouTube') : 'Добавьте первый трек в очередь'}</p>
            {currentTrack && <div className="bars" aria-hidden="true">{Array.from({ length: 20 }, (_, i) => <i key={i} />)}</div>}
          </div>
          <div className="art" aria-hidden="true">{currentTrack && !isYandex ? <img src={`https://i.ytimg.com/vi/${currentTrack}/hqdefault.jpg`} alt="" /> : <span className="record" />}</div>
        </section>}
        {session.role === 'Main' && <section className="card deck" aria-label="Плеер хоста">
          <div className="deck-stage">
            <div className="deck-stage-copy"><div className="deck-status"><span className="eyebrow">{currentTrack ? 'СЕЙЧАС ИГРАЕТ' : 'ПЛЕЕР ХОСТА'}</span>
              <span className={`deck-onair ${playingKey === currentKey && currentTrack ? 'playing' : ''}`}><i />{playingKey === currentKey && currentTrack ? 'В ЭФИРЕ' : 'ОЖИДАНИЕ'}</span></div>
              <h2>{currentTrack ? nowTitle : 'Очередь пока пуста'}</h2>
              <p>{currentTrack ? (isYandex ? 'Яндекс Музыка' : 'YouTube') : 'Добавьте первый трек, чтобы начать.'}</p>
              {currentTrack ? <div className="bars" aria-hidden="true">{Array.from({ length: 20 }, (_, i) => <i key={i} />)}</div> :
                <button className="button tiny stage-add" onClick={() => document.getElementById('add-track')?.scrollIntoView({ behavior: 'smooth', block: 'center' })}>Добавить трек ↓</button>}
            </div>
            <div className="miniPlayer">
              {fallbackId === currentTrack && currentTrack ? <div key={currentKey} ref={ytMount} className="fallbackVideo" /> :
              <div className={`spectrum ${currentTrack && playingKey === currentKey ? '' : 'idle'}`} aria-label="Аудиовизуализатор">
                {Array.from({ length: 17 }, (_, i) => <i key={i} style={{ '--n': i } as CSSProperties} />)}
              </div>}
            </div>
          </div>
          <div className="deck-timeline">
            <span>{clockTime(playbackTime)}</span>
            <input className="deck-range" type="range" min="0" max={Math.max(1, playbackDuration)} step="0.1"
              value={Math.min(playbackTime, Math.max(1, playbackDuration))} disabled={!currentTrack || playbackDuration <= 0}
              style={{ '--fill': `${playbackDuration ? playbackTime / playbackDuration * 100 : 0}%` } as CSSProperties}
              aria-label="Позиция трека" onChange={(event) => seekPlayback(Number(event.target.value))} />
            <span>{clockTime(playbackDuration)}</span>
          </div>
          <div className="deck-controls">
            <button className="deck-play" aria-label={playingKey === currentKey ? 'Пауза' : 'Воспроизвести'}
              disabled={!currentTrack || (!audioUrl && fallbackId !== currentTrack)} onClick={togglePlayback}>
              {playingKey === currentKey && currentTrack ? <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="6" y="4" width="4" height="16" rx="1" /><rect x="14" y="4" width="4" height="16" rx="1" /></svg> :
                <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 4.7a1 1 0 0 1 1.5-.86l11 7.3a1 1 0 0 1 0 1.72l-11 7.3A1 1 0 0 1 8 19.3z" /></svg>}
            </button>
            <button className="deck-next" disabled={status !== 'connected' || !currentTrack} onClick={() => {
              if (fading.current) {
                fading.current = false;
                if (fadeFrame.current !== null) cancelAnimationFrame(fadeFrame.current);
                audioPlayers[1 - activeAudioRef.current].current?.pause();
              }
              skipTrack();
            }} aria-label="Следующий трек" title="Следующий трек">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="m5 5 10 7L5 19V5Z" /><path d="M19 5v14" /></svg>
            </button>
            <span className="deck-next-label">Следующий трек</span>
            <div className="deck-volume">
              <button className="deck-volume-icon" aria-label={volume === 0 ? 'Включить звук' : 'Выключить звук'}
                onClick={() => setPlayerVolume(volume === 0 ? previousVolume.current : 0)}>
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M4 9v6h4l5 4V5L8 9H4Z" />{volume > 0 && <path d="M16 9a4 4 0 0 1 0 6" />}{volume > 50 && <path d="M18.5 5.5a9 9 0 0 1 0 13" />}</svg>
              </button>
              <input className="deck-range deck-volume-range" type="range" min="0" max="100" value={volume}
                style={{ '--fill': `${volume}%` } as CSSProperties} aria-label="Громкость"
                onChange={(event) => setPlayerVolume(Number(event.target.value))} />
              <span>{volume}%</span>
            </div>
          </div>
          {[0, 1].map((slot) => <audio key={slot} ref={audioPlayers[slot]} preload="auto"
            style={{ display: 'none' }}
            onPlay={() => { if (slot === activeAudioRef.current && slotKeys.current[slot] === currentKey) setPlayingKey(currentKey);
              else if (!fading.current) audioPlayers[slot].current?.pause(); }}
            onPause={() => { if (slot === activeAudioRef.current && slotKeys.current[slot] === currentKey && !fading.current && !promoting.current) setPlayingKey(''); }}
            onLoadedMetadata={(event) => { if (slot === activeAudioRef.current && slotKeys.current[slot] === currentKey) syncPlaybackTime(event.currentTarget); }}
            onDurationChange={(event) => { if (slot === activeAudioRef.current && slotKeys.current[slot] === currentKey) syncPlaybackTime(event.currentTarget); }}
            onTimeUpdate={(event) => { if (slot === activeAudioRef.current && slotKeys.current[slot] === currentKey) syncPlaybackTime(event.currentTarget); maybeCrossfade(slot); }}
            onEnded={() => { if (slot === activeAudioRef.current && slotKeys.current[slot] === currentKey) {
              const next = preloaded.current && audioPlayers[preloaded.current.slot].current;
              if (fading.current && next && !next.paused) finishFade();
              else { fading.current = false; skipTrack(); }
            } }}
            onError={() => { if (slot === activeAudioRef.current && slotKeys.current[slot] === currentKey) {
              if (currentTrack && !currentTrack.startsWith('yandex_')) setFallbackId(currentTrack);
              else {
                if (currentItem) preparedAudio.current.delete(audioCacheKey(currentItem));
                setAudioError('Не удалось воспроизвести трек Яндекс Музыки. Повторите загрузку или пропустите его вручную.');
                setAudioUrl('');
                setPlayingKey('');
              }
            } }} />)}
          <div className="deck-message">{currentTrack ? <>
            {fallbackId === currentTrack && <p className="muted small">Аудиопоток YouTube недоступен — пробуем встроенный плеер.</p>}
            {fallbackId === currentTrack && <button className="button tiny" onClick={() => { if (typeof ytPlayer.current?.playVideo === 'function') ytPlayer.current.playVideo(); }}>Запустить YouTube</button>}
            {audioError && <p className="notice" role="alert">{audioError}</p>}
            {isYandex && audioError && currentItem?.owner_id !== ownerId && <p className="muted small">
              Доступ к этому треку предоставляет {profileLabel(profiles[currentItem.owner_id])}. Если доступ истёк, попросите автора подключить Яндекс Музыку заново.</p>}
            {isYandex && audioError && currentItem?.owner_id === ownerId && <button className="button tiny" onClick={() =>
              document.getElementById('yandex-auth')?.scrollIntoView({ behavior: 'smooth', block: 'center' })}>
              Подключить свой Яндекс ↓</button>}
            {audioError && status === 'connected' && <button className="button tiny" onClick={() => {
              if (currentItem) preparedAudio.current.delete(audioCacheKey(currentItem));
              setStreamAttempt((value) => value + 1);
            }}>Повторить загрузку аудио</button>}
            {!audioUrl && !audioError && fallbackId !== currentTrack && <p className="muted small">Получаем аудиопоток…</p>}
          </> : null}</div>
        </section>}
        <section className="card cardpad"><div className="sectionhead"><div><span className="eyebrow">ОБЩИЙ РИТМ</span><h3>Очередь треков</h3></div><span className="pill">{queueItems.length} треков</span></div>
          <div aria-live="polite">{queueItems.length ? <ol className="queue">{queueItems.map((item, index) => <li className={index === 0 ? 'current' : ''} key={item.id}
            draggable={session.role === 'Main' && index > 0 && status === 'connected' && !queueActionPending}
            onDragStart={(event) => { draggingItem.current = item.id; event.dataTransfer.effectAllowed = 'move'; }}
            onDragOver={(event) => { if (session.role === 'Main' && index > 0) event.preventDefault(); }}
            onDrop={(event) => { event.preventDefault(); if (session.role === 'Main' && index > 0 && draggingItem.current && draggingItem.current !== item.id)
              changeQueue('move_track', draggingItem.current, index); draggingItem.current = ''; }}
            onDragEnd={() => { draggingItem.current = ''; }}>
            <span className="index">{index === 0 ? '♫' : String(index + 1).padStart(2, '0')}</span>
            <div className="trackmeta"><strong>{trackTitles[item.track_id] || item.track_id}</strong>
              <small>{item.track_id.startsWith('yandex_') ? 'Яндекс Музыка' : 'YouTube'}{index === 0 ? ' · Сейчас играет' : ''}</small>
              <span className="added-by"><Avatar profile={profiles[item.owner_id]} size={23} />
                Добавил{item.owner_id === ownerId ? '(а) вы' : `(а) ${profileLabel(profiles[item.owner_id])}`}</span></div>
            {session.role === 'Main' && index > 0 && <span className="queueControls">
              <button className="button tiny" aria-label="Поднять трек" title="Поднять" disabled={index <= 1 || queueActionPending || status !== 'connected'} onClick={() => changeQueue('move_track', item.id, index - 1)}>↑</button>
              <button className="button tiny" aria-label="Опустить трек" title="Опустить" disabled={index >= queueItems.length - 1 || queueActionPending || status !== 'connected'} onClick={() => changeQueue('move_track', item.id, index + 1)}>↓</button>
            </span>}
            {item.owner_id === ownerId && <button className="button tiny" aria-label="Удалить свой трек" disabled={queueActionPending || status !== 'connected'} onClick={() => changeQueue('remove_track', item.id)}>Удалить</button>}
          </li>)}</ol> : <p className="muted small">Здесь скоро появится музыка. Добавьте первый трек.</p>}</div>
        </section>
        <section className="card cardpad" id="add-track"><div className="sectionhead"><div><span className="eyebrow">ВАШ ВКЛАД</span><h3>Добавить трек</h3></div></div>
          <form className="addform" onSubmit={addTrack}><input aria-label="YouTube ссылка или Video ID" value={trackId} onChange={(event) => setTrackId(event.target.value)} required
            placeholder="Ссылка YouTube или Video ID" /><button className="button primary" disabled={status !== 'connected' || !trackId.trim() || loadingMetadata}>{loadingMetadata ? 'Ищем название…' : '+ В очередь'}</button></form>
          <p className="muted small">Подойдут youtube.com/watch?v=…, youtu.be/… или 11-значный ID.</p>
        </section>
      </div><div className={`sidecol ${yandexTracks.length ? 'music-ready' : ''}`}>
        <section className="card cardpad share-card" id="share-room"><div className="sectionhead"><div><span className="eyebrow">ПРИГЛАСИТЬ ДРУЗЕЙ</span><h3>Поделиться комнатой</h3></div></div>
          <div className="share"><div className="sharetext"><p className="muted small">Отправьте ссылку или покажите QR-код.</p>
            <button className="button sharelink" title={inviteUrl} onClick={() => void copy(inviteUrl, 'Ссылка')}>{inviteUrl}</button>
            {telegramInitData && <button className="button tiny invite-button" onClick={() =>
              telegram?.openTelegramLink?.(`https://t.me/share/url?url=${encodeURIComponent(inviteUrl)}&text=${encodeURIComponent(`Заходи на вечеринку ${profileLabel(mainProfile)} в Prada Sound`)}`)}>
              Поделиться в Telegram ↗</button>}
            <p className="roomid mono">ID: {session.lobby_id}</p></div>
            {qr ? <svg className="qr" viewBox="0 0 53 53" role="img" aria-label="QR-код приглашения"><path fill="#111" d={qr} /></svg> : <span className="muted small">Ссылка слишком длинная для QR</span>}
          </div>{['localhost', '127.0.0.1'].includes(location.hostname) && <p className="muted small">Для входа с телефона откройте сайт по адресу компьютера в вашей сети или по домену — localhost на телефоне не откроется.</p>}</section>
        <section className="card cardpad yandex-card" id="yandex-auth"><div className="sectionhead"><div><span className="eyebrow">ВАША МУЗЫКА</span><h3>Яндекс Музыка</h3></div></div>
          <p className="muted small">Подключите аккаунт, чтобы добавить лайкнутые треки. Добавленные вами треки звучат на устройстве хоста с вашего доступа к Яндекс Музыке.</p>
          <button className="button primary" onClick={() => void startYandexLogin()} disabled={loadingLikes}>{yandexConnected ? 'Подключить заново' : 'Подключить Яндекс Музыку →'}</button>
          {yandexLogin && <div className="authbox" style={{ marginTop: 16 }}>
            <span className="eyebrow">ПОДТВЕРДИТЕ ВХОД</span><p className="muted small">Откройте Яндекс, введите код и разрешите доступ.</p>
            <div className="row" style={{ flexWrap: 'wrap' }}><code className="code mono">{yandexLogin.user_code}</code><button className="button tiny" onClick={() => void copy(yandexLogin.user_code, 'Код')}>Копировать код</button></div>
            <a className="button primary linklike" style={{ marginTop: 14 }} href={yandexLogin.verification_url} target="_blank" rel="noopener noreferrer"
              onClick={(event) => { if (telegramInitData && telegram?.openLink) { event.preventDefault(); telegram.openLink(yandexLogin.verification_url); } }}>Открыть Яндекс ↗</a>
          </div>}
          {loadingLikes && <p className="muted small" role="status">{yandexLogin ? 'Ждём подтверждения Яндекса…' : 'Получаем код входа…'}</p>}
          {yandexError && <p className="notice" role="alert">{yandexError}</p>}
          {yandexConnected && <p className="muted small" role="status">Загружено треков: {yandexTracks.length}</p>}
          {yandexTracks.length > 0 && <label className="field">Поиск среди загруженных треков
            <input value={yandexSearch} onChange={(event) => setYandexSearch(event.target.value)} placeholder="Название или исполнитель" aria-label="Поиск треков Яндекс Музыки" />
          </label>}
          {yandexTracks.length > 0 && status !== 'connected' && <p className="notice" role="status">Очередь комнаты {status === 'closed' ? 'отключена' : 'подключается'}.
            {status === 'closed' && <> <button className="button tiny" onClick={() => setAttempt((value) => value + 1)}>Подключиться снова</button></>}
          </p>}
          {yandexTracks.length > 0 && filteredYandexTracks.length === 0 && <p className="muted small">Среди загруженных треков ничего не найдено.</p>}
          {filteredYandexTracks.length > 0 && <ul className="likes">{filteredYandexTracks.map((track) => <li key={track.id}>
            <div className="trackmeta"><strong>{track.title}</strong><small>{track.artist}</small></div>
            <button className="button tiny" disabled={status !== 'connected'} onClick={() => enqueue(`yandex_${track.id}`, `${track.title} — ${track.artist}`)}>+ В очередь</button>
          </li>)}</ul>}
          {yandexConnected && hasMoreLikes && <button className="button tiny" onClick={() => void showMoreLikes()} disabled={loadingLikes}>Показать ещё</button>}
        </section>
      </div></div>
    </>}
  </div></main>;
}
