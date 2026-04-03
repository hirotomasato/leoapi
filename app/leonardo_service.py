import time
import re
import json
import base64
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from fastapi import HTTPException

from app.leonardo_client import LeonardoAPIClient
from app.store import Store


ASPECT_TO_SIZE: Dict[str, Tuple[int, int]] = {
    "16:9": (2752, 1536),
    "9:16": (1536, 2752),
    "1:1": (1536, 1536),
    "4:3": (2048, 1536),
}


class LeonardoPoolService:
    def __init__(self, store: Store):
        self.store = store
        self.api = LeonardoAPIClient()

    def _resolve_size(self, aspect_ratio: str) -> Tuple[int, int]:
        if aspect_ratio not in ASPECT_TO_SIZE:
            return ASPECT_TO_SIZE["1:1"]
        return ASPECT_TO_SIZE[aspect_ratio]

    def _is_jwt(self, value: str) -> bool:
        jwt_parts = (value or "").split(".")
        return len(jwt_parts) == 3 and all(re.fullmatch(r"[A-Za-z0-9_-]+", p or "") for p in jwt_parts)

    def _jwt_exp(self, value: str) -> int:
        token = (value or "").strip()
        if not self._is_jwt(token):
            return 0
        try:
            payload = token.split(".")[1]
            pad = "=" * ((4 - len(payload) % 4) % 4)
            data = json.loads(base64.urlsafe_b64decode(payload + pad).decode("utf-8"))
            exp = data.get("exp")
            if isinstance(exp, (int, float)):
                return int(exp)
        except Exception:
            return 0
        return 0

    def _is_jwt_fresh(self, value: str, min_ttl_seconds: int = 300) -> bool:
        token = (value or "").strip()
        if not self._is_jwt(token):
            return False
        exp = self._jwt_exp(token)
        if not exp:
            return True
        now = int(time.time())
        return exp > now + max(30, int(min_ttl_seconds))

    def _extract_auth_parts(self, raw_auth_value: str) -> Tuple[Optional[str], Optional[str]]:
        raw = (raw_auth_value or "").strip()
        if not raw:
            return None, None

        token: Optional[str] = None
        cookie: Optional[str] = None

        lines = [line.strip() for line in raw.splitlines() if line.strip() and not line.strip().startswith("#")]
        if not lines:
            lines = [raw]

        for line in lines:
            if line.lower().startswith("cookie:"):
                line = line.split(":", 1)[1].strip()

            if "=" in line:
                key, value = line.split("=", 1)
                key = key.strip().lower()
                value = value.strip()

                if key == "token" and value:
                    token = value
                    continue

                if key == "cookie" and value:
                    cookie = value
                    continue

                if ";" in line:
                    cookie = line
                    continue

                if "next-auth" in key or key.startswith("__host-next-auth") or key.startswith("__secure-next-auth"):
                    cookie = line
                    continue
            else:
                if line.count(".") == 2 and " " not in line and len(line) > 40:
                    token = line
                    continue

                if ";" in line and "=" in line:
                    cookie = line

        return token, cookie

    def _compose_store_auth_value(self, cookie_payload: str, token: Optional[str]) -> str:
        normalized_cookie = (cookie_payload or "").strip()
        if normalized_cookie.lower().startswith("cookie:"):
            normalized_cookie = normalized_cookie.split(":", 1)[1].strip()

        normalized_token = (token or "").strip()
        if (
            normalized_token
            and self._is_jwt_fresh(normalized_token, min_ttl_seconds=300)
            and self.api._is_likely_leonardo_token(normalized_token)
        ):
            return f"cookie={normalized_cookie}\ntoken={normalized_token}"
        return normalized_cookie

    def _refresh_cookie_fallback_token(self, cookie_id: int, raw_auth_value: str, resolved_token: str) -> None:
        parsed_token, parsed_cookie = self._extract_auth_parts(raw_auth_value)
        cookie_payload = (parsed_cookie or "").strip()
        if not cookie_payload:
            raw = (raw_auth_value or "").strip()
            if ";" in raw and "=" in raw:
                cookie_payload = raw

        if not cookie_payload:
            return

        current_value = (raw_auth_value or "").strip()
        next_value = self._compose_store_auth_value(cookie_payload, resolved_token or parsed_token or "")
        if next_value and next_value != current_value:
            self.store.update_cookie_value(cookie_id, next_value)

    def _is_auth_error(self, message: str) -> bool:
        text = (message or "").strip().lower()
        if not text:
            return False
        markers = [
            "jwt expired",
            "token expired",
            "invalid token",
            "invalid bearer",
            "unauthorized",
            "forbidden",
            "access denied",
            "401",
            "403",
            "session refresh gagal",
            "failed token",
            "failed to fetch token",
            "auth tidak valid",
            "authentication",
        ]
        return any(m in text for m in markers)

    def _resolve_token(self, raw_auth_value: str) -> str:
        value = (raw_auth_value or "").strip()
        token, cookie = self._extract_auth_parts(value)

        # Prefer resolving from full cookie first so we don't get stuck using stale token= fallback.
        cookie_payload = (cookie or value).strip()
        if cookie_payload.lower().startswith("cookie:"):
            cookie_payload = cookie_payload.split(":", 1)[1].strip()

        cookie_token = self.api.get_token_from_cookie(cookie_payload) or ""
        if cookie_token and self._is_jwt(cookie_token):
            return cookie_token

        if token and self._is_jwt_fresh(token, min_ttl_seconds=120) and self.api._is_likely_leonardo_token(token):
            return token

        if self._is_jwt_fresh(value, min_ttl_seconds=120) and self.api._is_likely_leonardo_token(value):
            return value

        return ""

    def _save_generated_images(self, generation_id: str, image_urls: List[str]) -> List[str]:
        save_dir = self.store.get_setting("save_images_dir", "data/generated").strip() or "data/generated"
        root = Path(save_dir)
        root.mkdir(parents=True, exist_ok=True)

        saved_files: List[str] = []
        ts = int(time.time())
        for idx, url in enumerate(image_urls, start=1):
            response = requests.get(url, timeout=180)
            if not response.ok:
                raise Exception(f"Download image gagal: HTTP {response.status_code}")

            suffix = Path(url.split("?", 1)[0]).suffix or ".jpg"
            filename = f"{generation_id}_{idx}_{ts}{suffix}"
            out_path = root / filename
            out_path.write_bytes(response.content)
            saved_files.append(str(out_path))

        return saved_files

    def add_cookie_validated(self, raw_auth_value: str) -> Dict[str, int]:
        value = (raw_auth_value or "").strip()
        if not value:
            raise ValueError("Cookie/token tidak boleh kosong")

        parsed_token, parsed_cookie = self._extract_auth_parts(value)
        cookie_payload = (parsed_cookie or "").strip()
        if cookie_payload.lower().startswith("cookie:"):
            cookie_payload = cookie_payload.split(":", 1)[1].strip()

        # Cookie pool must store full cookie strings so token can be refreshed reliably.
        if not cookie_payload or ";" not in cookie_payload or "=" not in cookie_payload:
            if parsed_token or self._is_jwt(value):
                raise ValueError("Input JWT ditolak. Wajib paste full cookie string dari browser.")
            raise ValueError("Format cookie tidak valid. Wajib full cookie string (name=value; ...)")

        lower_cookie = cookie_payload.lower()
        has_session_marker = (
            "next-auth.session-token" in lower_cookie
            or "authjs.session-token" in lower_cookie
            or "__secure-next-auth.session-token" in lower_cookie
            or "__secure-authjs.session-token" in lower_cookie
            or "__host-next-auth.csrf-token" in lower_cookie
            or "next-auth.csrf-token" in lower_cookie
        )
        if not has_session_marker:
            raise ValueError("Cookie bukan session Leonardo yang valid. Ambil ulang dari extension saat sudah login app.leonardo.ai")

        token = self.api.get_token_from_cookie(cookie_payload) or ""
        if not token and parsed_token:
            candidate = parsed_token.strip()
            if self._is_jwt_fresh(candidate, min_ttl_seconds=300) and self.api._is_likely_leonardo_token(candidate):
                token = candidate
        if not token:
            raise ValueError("Auth tidak valid: gagal mendapatkan token")
        if token.count(".") != 2:
            raise ValueError("Token session tidak valid untuk API (bukan JWT bearer)")

        info = self.api.get_user_info(token)
        balance = int(info.get("tokens", 0) or 0)

        # Keep token fallback together with cookie so session refresh can still work
        # when provider blocks direct /api/auth/session calls from server side.
        store_value = self._compose_store_auth_value(cookie_payload, token or parsed_token or "")

        self.store.add_cookie(store_value)
        saved = self.store.get_cookie_by_value(store_value)
        if saved:
            self.store.update_cookie_profile(saved["id"], "", balance)
            if balance > 0:
                self.store.mark_cookie_used(saved["id"])

        return {"balance": balance}

    def _pick_cookie_token(self) -> Tuple[Dict, str]:
        cookies = self.store.list_active_cookies()
        if not cookies:
            raise HTTPException(status_code=400, detail="No active cookie configured in admin panel")

        last_error = "No valid cookie/token found"
        for cookie in cookies:
            cookie_id = cookie["id"]
            cookie_value = cookie["value"]

            try:
                token = self.api.get_token_from_cookie(cookie_value)
                if not token:
                    self.store.mark_cookie_error(cookie_id, "Failed to fetch token from cookie")
                    last_error = "Failed to fetch token from cookie"
                    continue

                info = self.api.get_user_info(token)
                tokens = int(info.get("tokens", 0) or 0)
                if tokens <= 0:
                    self.store.mark_cookie_error(cookie_id, "Token balance is empty")
                    last_error = "All active cookies have empty token balance"
                    continue

                self.store.mark_cookie_used(cookie_id)
                return cookie, token
            except Exception as exc:
                self.store.mark_cookie_error(cookie_id, str(exc))
                last_error = str(exc)

        raise HTTPException(status_code=503, detail=last_error)

    def generate_images(
        self,
        prompt: str,
        n: int,
        model_id: str,
        aspect_ratio: str,
        reference_image_urls: List[str],
        save_results: Optional[bool] = None,
    ) -> Dict:
        width, height = self._resolve_size(aspect_ratio)

        errors: List[str] = []
        cookies = self.store.list_active_cookies()
        if not cookies:
            raise HTTPException(status_code=400, detail="No active cookie configured in admin panel")

        for cookie in cookies:
            cookie_id = cookie["id"]
            cookie_value = cookie["value"]

            for attempt in range(2):
                try:
                    token = self._resolve_token(cookie_value)
                    if not token:
                        self.store.mark_cookie_error(cookie_id, "Failed to fetch token from cookie")
                        errors.append(f"cookie#{cookie_id}: failed token")
                        break

                    info = self.api.get_user_info(token)
                    self._refresh_cookie_fallback_token(cookie_id, cookie_value, token)
                    self.store.update_cookie_profile(
                        cookie_id,
                        "",
                        int(info.get("tokens", 0) or 0),
                    )
                    tokens = int(info.get("tokens", 0) or 0)
                    if tokens <= 0:
                        self.store.mark_cookie_error(cookie_id, "Token balance is empty")
                        errors.append(f"cookie#{cookie_id}: empty balance")
                        break

                    init_image_ids: List[str] = []
                    for image_url in reference_image_urls[:3]:
                        init_image_ids.append(self.api.upload_image_url(token, image_url))

                    gen_id = self.api.create_generation(
                        token=token,
                        prompt=prompt,
                        model_id=model_id,
                        width=width,
                        height=height,
                        quantity=max(1, min(4, n)),
                        init_image_ids=init_image_ids or None,
                    )

                    result = self.api.wait_for_completion(token=token, gen_id=gen_id, timeout=300, poll_interval=4)
                    if not result.get("success"):
                        error_msg = str(result.get("error", "generation failed"))
                        if self._is_auth_error(error_msg) and attempt == 0:
                            continue
                        if self._is_auth_error(error_msg):
                            self.store.mark_cookie_error(cookie_id, error_msg)
                        errors.append(f"cookie#{cookie_id}: {error_msg}")
                        break

                    self.store.mark_cookie_used(cookie_id)
                    urls = result.get("images", [])
                    auto_save_enabled = self.store.get_setting("auto_save_images", "0") == "1"
                    if save_results is not None:
                        auto_save_enabled = bool(save_results)
                    saved_files: List[str] = []
                    if auto_save_enabled and urls:
                        try:
                            saved_files = self._save_generated_images(gen_id, urls)
                        except Exception:
                            # Auto-save failures should not mark cookie auth as broken.
                            pass

                    self.store.add_generation_log(
                        provider_generation_id=gen_id,
                        used_cookie_id=cookie_id,
                        model_id=model_id,
                        aspect_ratio=aspect_ratio,
                        prompt=prompt,
                        image_urls_json=json.dumps(urls),
                        saved_files_json=json.dumps(saved_files),
                        save_enabled=auto_save_enabled,
                        status="success",
                        error_message="",
                    )

                    return {
                        "created": int(time.time()),
                        "data": [{"url": url} for url in urls],
                        "provider": {
                            "generation_id": gen_id,
                            "used_cookie_id": cookie_id,
                            "aspect_ratio": aspect_ratio,
                            "model_id": model_id,
                            "saved_files": saved_files,
                            "auto_save_enabled": auto_save_enabled,
                        },
                    }
                except Exception as exc:
                    msg = str(exc)
                    if self._is_auth_error(msg) and attempt == 0:
                        continue
                    if self._is_auth_error(msg):
                        self.store.mark_cookie_error(cookie_id, msg)
                    errors.append(f"cookie#{cookie_id}: {msg}")
                    break

        detail = "All cookies failed. " + " | ".join(errors[:6])
        raise HTTPException(status_code=503, detail=detail)

    def refresh_cookie_profiles(self) -> Dict[str, int]:
        cookies = self.store.list_cookies()
        checked = 0
        ok = 0

        for cookie in cookies:
            checked += 1
            cookie_id = cookie["id"]
            cookie_value = cookie["value"]
            try:
                token = self._resolve_token(cookie_value)
                if not token:
                    self.store.mark_cookie_error(cookie_id, "Failed to fetch token from cookie")
                    self.store.update_cookie_profile(cookie_id, "", 0)
                    continue

                info = self.api.get_user_info(token)
                self._refresh_cookie_fallback_token(cookie_id, cookie_value, token)
                self.store.update_cookie_profile(
                    cookie_id,
                    "",
                    int(info.get("tokens", 0) or 0),
                )
                self.store.mark_cookie_used(cookie_id)
                ok += 1
            except Exception as exc:
                self.store.mark_cookie_error(cookie_id, str(exc))

        return {"checked": checked, "ok": ok}

    def refresh_cookie_sessions(self) -> Dict[str, int]:
        cookies = self.store.list_cookies()
        checked = 0
        ok = 0

        for cookie in cookies:
            checked += 1
            cookie_id = cookie["id"]
            cookie_value = cookie["value"]
            try:
                token = self._resolve_token(cookie_value)
                if not token:
                    self.store.mark_cookie_error(cookie_id, "Session refresh gagal: token tidak ditemukan")
                    continue
                if token.count(".") != 2:
                    self.store.mark_cookie_error(cookie_id, "Session refresh gagal: token bearer tidak valid")
                    continue

                info = self.api.get_user_info(token)
                self._refresh_cookie_fallback_token(cookie_id, cookie_value, token)
                self.store.update_cookie_profile(
                    cookie_id,
                    "",
                    int(info.get("tokens", 0) or 0),
                )
                self.store.mark_cookie_used(cookie_id)
                ok += 1
            except Exception as exc:
                self.store.mark_cookie_error(cookie_id, f"Session refresh gagal: {exc}")

        return {"checked": checked, "ok": ok}
