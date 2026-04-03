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

function collectTokensFromObject(obj, out) {
  if (!obj) return;
  if (typeof obj === 'string') {
    if (looksLikeJwt(obj)) out.add(obj.trim());
    return;
  }
  if (Array.isArray(obj)) {
    for (const item of obj) collectTokensFromObject(item, out);
    return;
  }
  if (typeof obj === 'object') {
    for (const [key, value] of Object.entries(obj)) {
      const k = String(key).toLowerCase();
      if (k.includes('token') || k.includes('idtoken') || k.includes('accesstoken')) {
        collectTokensFromObject(value, out);
      }
    }
  }
}

function readStorageTokens() {
  const tokens = new Set();

  const scan = (storage) => {
    try {
      for (let i = 0; i < storage.length; i++) {
        const key = storage.key(i);
        const val = storage.getItem(key);
        if (!key || !val) continue;

        const lowerKey = key.toLowerCase();
        if (lowerKey.includes('cf_access_token')) continue;

        if (isUsableJwt(val, 180)) {
          tokens.add(val.trim());
          continue;
        }

        // Some SDKs store JSON blobs containing idToken/accessToken.
        if (val.startsWith('{') || val.startsWith('[')) {
          try {
            const parsed = JSON.parse(val);
            collectTokensFromObject(parsed, tokens);
          } catch {
            // ignore malformed JSON
          }
        }
      }
    } catch {
      // ignore storage read failures
    }
  };

  scan(localStorage);
  scan(sessionStorage);

  const now = Math.floor(Date.now() / 1000);
  const ordered = Array.from(tokens);
  ordered.sort((a, b) => {
    const aLikely = isLikelyLeonardoJwt(a) ? 1 : 0;
    const bLikely = isLikelyLeonardoJwt(b) ? 1 : 0;
    if (aLikely !== bLikely) return bLikely - aLikely;
    const aExp = jwtExpiry(a) || (now + 120);
    const bExp = jwtExpiry(b) || (now + 120);
    return bExp - aExp;
  });
  return ordered;
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || message.type !== 'GET_LEO_TOKEN') {
    return false;
  }

  const tokens = readStorageTokens();
  sendResponse({ ok: true, tokens });
  return true;
});
