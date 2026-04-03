const fetchBtn = document.getElementById('fetchBtn');
const copyBtn = document.getElementById('copyBtn');
const saveBtn = document.getElementById('saveBtn');
const output = document.getElementById('cookieOutput');
const statusText = document.getElementById('statusText');

let latestCookie = '';
let latestSessionToken = '';


function setStatus(message, kind = '') {
  statusText.textContent = message;
  statusText.classList.remove('ok', 'err');
  if (kind) {
    statusText.classList.add(kind);
  }
}

function normalizeCookie(cookieList) {
  return cookieList
    .filter((c) => c && c.name && typeof c.value === 'string')
    .sort((a, b) => a.name.localeCompare(b.name))
    .map((c) => `${c.name}=${c.value}`)
    .join('; ');
}

function dedupeRawCookies(cookieList) {
  const byKey = new Map();
  for (const item of cookieList) {
    if (!item || !item.name || typeof item.value !== 'string') continue;
    const key = `${item.name}|${item.domain || ''}|${item.path || ''}`;
    const prev = byKey.get(key);
    if (!prev) {
      byKey.set(key, item);
      continue;
    }

    const prevExpiry = typeof prev.expirationDate === 'number' ? prev.expirationDate : 0;
    const nextExpiry = typeof item.expirationDate === 'number' ? item.expirationDate : 0;
    byKey.set(key, nextExpiry >= prevExpiry ? item : prev);
  }
  return Array.from(byKey.values());
}

function hasRequiredMarkers(cookieString) {
  const lower = (cookieString || '').toLowerCase();
  const hasSession =
    lower.includes('next-auth.session-token=') ||
    lower.includes('__secure-next-auth.session-token=') ||
    lower.includes('authjs.session-token=') ||
    lower.includes('__secure-authjs.session-token=') ||
    lower.includes('next-auth.session-token.0=') ||
    lower.includes('__secure-next-auth.session-token.0=') ||
    lower.includes('authjs.session-token.0=') ||
    lower.includes('__secure-authjs.session-token.0=');

  const hasCsrf =
    lower.includes('next-auth.csrf-token=') ||
    lower.includes('__host-next-auth.csrf-token=') ||
    lower.includes('authjs.csrf-token=');

  return { hasSession, hasCsrf };
}

function looksLikeJwt(token) {
  if (typeof token !== 'string') return false;
  const t = token.trim();
  const parts = t.split('.');
  if (parts.length !== 3) return false;
  return parts.every((p) => /^[A-Za-z0-9_-]+$/.test(p));
}

function decodeJwtPayload(token) {
  try {
    if (!looksLikeJwt(token)) return null;
    const payload = token.trim().split('.')[1] || '';
    const base = payload.replace(/-/g, '+').replace(/_/g, '/');
    const padded = base + '='.repeat((4 - (base.length % 4)) % 4);
    const json = atob(padded);
    return JSON.parse(json);
  } catch {
    return null;
  }
}

function jwtExpiry(token) {
  const payload = decodeJwtPayload(token);
  if (!payload || typeof payload.exp !== 'number') return 0;
  return payload.exp;
}

function isLikelyLeonardoJwt(token) {
  const payload = decodeJwtPayload(token);
  if (!payload || typeof payload !== 'object') return false;
  const iss = String(payload.iss || '').toLowerCase();
  const tokenUse = String(payload.token_use || '').toLowerCase();
  const aud = payload.aud;

  if (iss.includes('cognito-idp')) return true;
  if (tokenUse === 'id' || tokenUse === 'access') return true;
  if (typeof aud === 'string' && aud.length >= 8) return true;
  return false;
}

function isUsableJwt(token, minTtlSeconds = 180) {
  if (!looksLikeJwt(token)) return false;
  const exp = jwtExpiry(token);
  if (!exp) return true;
  const now = Math.floor(Date.now() / 1000);
  return exp > now + Math.max(30, minTtlSeconds);
}

function pickBestJwt(tokens) {
  const now = Math.floor(Date.now() / 1000);
  const usable = (tokens || []).filter((t) => isUsableJwt(t, 180));
  if (!usable.length) return '';

  const likely = usable.filter((t) => isLikelyLeonardoJwt(t));
  const pool = likely.length ? likely : usable;

  const rank = (token) => {
    const payload = decodeJwtPayload(token) || {};
    const tokenUse = String(payload.token_use || '').toLowerCase();
    const useScore = tokenUse === 'access' ? 3 : tokenUse === 'id' ? 2 : 1;
    const expScore = jwtExpiry(token) || (now + 120);
    return [useScore, expScore];
  };

  pool.sort((a, b) => {
    const [aUse, aExp] = rank(a);
    const [bUse, bExp] = rank(b);
    if (bUse !== aUse) return bUse - aUse;
    return bExp - aExp;
  });
  return pool[0] || '';
}

function findTokenInObject(data) {
  const found = new Set();

  const walk = (node) => {
    if (!node) return;
    if (typeof node === 'string') {
      if (isUsableJwt(node, 120)) found.add(node.trim());
      return;
    }
    if (Array.isArray(node)) {
      node.forEach(walk);
      return;
    }
    if (typeof node === 'object') {
      const paths = [
        ['accessToken'],
        ['access_token'],
        ['idToken'],
        ['id_token'],
        ['token'],
        ['user', 'accessToken'],
        ['user', 'idToken'],
        ['session', 'accessToken'],
        ['session', 'idToken'],
      ];
      for (const path of paths) {
        let cur = node;
        let ok = true;
        for (const key of path) {
          if (!cur || typeof cur !== 'object' || !(key in cur)) {
            ok = false;
            break;
          }
          cur = cur[key];
        }
        if (ok && typeof cur === 'string' && isUsableJwt(cur, 120)) {
          found.add(cur.trim());
        }
      }
      Object.entries(node).forEach(([key, value]) => {
        const lower = String(key || '').toLowerCase();
        if (lower.includes('cf_access_token')) return;
        if (lower.includes('token') || Array.isArray(value) || (value && typeof value === 'object')) {
          walk(value);
        }
      });
    }
  };

  walk(data);
  return pickBestJwt(Array.from(found));
}

function extractCsrf(cookieString) {
  const parts = cookieString.split(';').map((x) => x.trim());
  const names = [
    '__Host-next-auth.csrf-token',
    '__Secure-next-auth.csrf-token',
    'next-auth.csrf-token',
    '__Host-authjs.csrf-token',
    '__Secure-authjs.csrf-token',
    'authjs.csrf-token',
  ];
  for (const part of parts) {
    const idx = part.indexOf('=');
    if (idx <= 0) continue;
    const key = part.slice(0, idx).trim();
    const rawVal = part.slice(idx + 1).trim();
    if (!names.includes(key)) continue;
    const decoded = decodeURIComponent(rawVal);
    return decoded.split('|')[0] || '';
  }
  return '';
}

async function fetchSessionTokenFromBrowser(cookieString) {
  // Preferred path: read token from page storage through content script.
  try {
    const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
    const active = tabs && tabs[0];
    const isLeoTab = active && typeof active.url === 'string' && active.url.startsWith('https://app.leonardo.ai/');
    if (active && active.id && isLeoTab) {
      const res = await chrome.tabs.sendMessage(active.id, { type: 'GET_LEO_TOKEN' });
      if (res && res.ok && Array.isArray(res.tokens) && res.tokens.length) {
        const token = pickBestJwt(res.tokens);
        if (token) return token;
      }
    }
  } catch {
    // Content script might not be ready; continue with session endpoint fallback.
  }

  const csrfToken = extractCsrf(cookieString);
  const url = 'https://app.leonardo.ai/api/auth/session';

  if (csrfToken) {
    try {
      const postRes = await fetch(url, {
        method: 'POST',
        credentials: 'include',
        headers: {
          'content-type': 'application/json',
        },
        body: JSON.stringify({ csrfToken }),
      });
      if (postRes.ok) {
        const postJson = await postRes.json();
        const token = findTokenInObject(postJson);
        if (isUsableJwt(token, 120)) return token;
      }
    } catch {
      // Ignore and fallback to GET.
    }
  }

  try {
    const getRes = await fetch(url, { method: 'GET', credentials: 'include' });
    if (!getRes.ok) return '';
    const getJson = await getRes.json();
    const token = findTokenInObject(getJson);
    return isUsableJwt(token, 120) ? token : '';
  } catch {
    return '';
  }
}

async function getLeonardoCookies() {
  const queries = [
    { url: 'https://app.leonardo.ai/api/auth/session' },
    { url: 'https://app.leonardo.ai/' },
    { url: 'https://leonardo.ai/' },
    { url: 'https://api.leonardo.ai/' },
    { domain: '.leonardo.ai' },
    { domain: 'app.leonardo.ai' },
    { domain: 'leonardo.ai' },
  ];

  const results = await Promise.allSettled(
    queries.map((query) => chrome.cookies.getAll(query))
  );

  const merged = [];
  for (const result of results) {
    if (result.status !== 'fulfilled' || !Array.isArray(result.value)) continue;
    for (const item of result.value) {
      if (!item || !item.name || typeof item.value !== 'string') continue;
      merged.push(item);
    }
  }

  const uniqueRaw = dedupeRawCookies(merged);

  return {
    rawCookies: uniqueRaw,
    rawCookieTotal: uniqueRaw.length,
  };
}

fetchBtn.addEventListener('click', async () => {
  setStatus('Mengambil cookie dari browser...');
  fetchBtn.disabled = true;

  try {
    const cookieData = await getLeonardoCookies();
    const rawCookies = cookieData.rawCookies || [];
    const rawCookieTotal = Number(cookieData.rawCookieTotal || rawCookies.length);
    if (!rawCookies.length) {
      latestCookie = '';
      latestSessionToken = '';
      output.value = '';
      copyBtn.disabled = true;
      saveBtn.disabled = true;
      setStatus('Cookie tidak ditemukan. Login dulu ke app.leonardo.ai.', 'err');
      return;
    }

    latestCookie = normalizeCookie(rawCookies);
    latestSessionToken = await fetchSessionTokenFromBrowser(latestCookie);
    const markers = hasRequiredMarkers(latestCookie);
    if (!markers.hasSession || !markers.hasCsrf) {
      copyBtn.disabled = true;
      saveBtn.disabled = true;
      const missing = [
        markers.hasSession ? null : 'session-token',
        markers.hasCsrf ? null : 'csrf-token',
      ].filter(Boolean).join(' + ');
      setStatus(`Cookie auth belum lengkap (${missing}). Pastikan login penuh di app.leonardo.ai`, 'err');
      output.value = latestCookie;
      return;
    }

    output.value = latestSessionToken
      ? `cookie=${latestCookie}\ntoken=${latestSessionToken}`
      : `cookie=${latestCookie}`;
    copyBtn.disabled = false;
    saveBtn.disabled = false;
    if (latestSessionToken) {
      setStatus(`Berhasil: full cookie ${rawCookieTotal} item + token session terdeteksi.`, 'ok');
    } else {
      setStatus(`Full cookie ${rawCookieTotal} item, tapi token session belum kebaca. Buka tab app.leonardo.ai lalu klik ulang.`, 'err');
    }
  } catch (err) {
    latestCookie = '';
    latestSessionToken = '';
    output.value = '';
    copyBtn.disabled = true;
    saveBtn.disabled = true;
    setStatus(`Gagal ambil cookie: ${String(err)}`, 'err');
  } finally {
    fetchBtn.disabled = false;
  }
});

copyBtn.addEventListener('click', async () => {
  const textToCopy = output.value.trim();
  if (!textToCopy) {
    setStatus('Belum ada cookie untuk dicopy.', 'err');
    return;
  }

  try {
    await navigator.clipboard.writeText(textToCopy);
    setStatus('Cookie berhasil dicopy ke clipboard.', 'ok');
  } catch {
    output.select();
    document.execCommand('copy');
    setStatus('Cookie berhasil dicopy (fallback).', 'ok');
  }
});

saveBtn.addEventListener('click', async () => {
  const textToSave = output.value.trim();
  if (!textToSave) {
    setStatus('Belum ada cookie untuk disimpan.', 'err');
    return;
  }

  const content = textToSave.endsWith('\n') ? textToSave : `${textToSave}\n`;
  const blob = new Blob([content], { type: 'text/plain' });
  const url = URL.createObjectURL(blob);

  try {
    await chrome.downloads.download({
      url,
      filename: 'leonardo-full-cookie.txt',
      saveAs: true,
      conflictAction: 'uniquify',
    });
    setStatus('File cookie berhasil disimpan.', 'ok');
  } catch (err) {
    setStatus(`Gagal simpan file: ${String(err)}`, 'err');
  } finally {
    URL.revokeObjectURL(url);
  }
});
