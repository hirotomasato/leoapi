import base64
import json
import os
import random
import re
import string
import time
from urllib.parse import unquote
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests


class LeonardoAPIClient:
    def __init__(self):
        self.graphql_url = "https://api.leonardo.ai/v1/graphql"
        self.auth_url = "https://app.leonardo.ai/api/auth"
        self.sentry_rel = "6a0bd1b5b7ef23a4f22608a2ed90c5e753cbc669"
        self.base_headers = {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/json",
            "origin": "https://app.leonardo.ai",
            "referer": "https://app.leonardo.ai/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/145.0.0.0 Safari/537.36",
            "x-leo-schema-version": "latest",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
        }

    def _make_id(self) -> str:
        return "".join(random.choices(string.ascii_lowercase + string.digits, k=8))

    def _sentry_headers(self, token: str) -> Dict[str, str]:
        tid = self._make_id() + self._make_id()
        return {
            "authorization": f"Bearer {token}",
            "sentry-trace": f"{tid}-{self._make_id()[:16]}-0",
            "baggage": (
                "sentry-environment=vercel-production,"
                f"sentry-release={self.sentry_rel},"
                "sentry-public_key=a851bd902378477eae99cf74c62e142a,"
                f"sentry-trace_id={tid},"
                "sentry-org_id=4504767521292288,"
                "sentry-sampled=false"
            ),
        }

    def _fetch_json(self, url: str, method: str, headers: Dict[str, str], body: Optional[str] = None) -> Dict:
        final_headers = {**self.base_headers, **headers}
        response = requests.request(method, url, headers=final_headers, data=body, timeout=60)

        if response.status_code == 204:
            return {}
        if not response.ok:
            raise Exception(f"HTTP {response.status_code}: {response.text[:200]}")
        return response.json()

    def _gql(self, token: str, payload: Dict) -> Dict:
        return self._fetch_json(
            self.graphql_url,
            "POST",
            self._sentry_headers(token),
            json.dumps(payload),
        )

    def _graphql_error_message(self, response_json: Dict[str, Any]) -> str:
        errors = (response_json or {}).get("errors") or []
        if not errors:
            return ""
        messages: List[str] = []
        for err in errors:
            if isinstance(err, dict):
                msg = str(err.get("message", "")).strip()
                if msg:
                    messages.append(msg)
            elif err:
                messages.append(str(err))
        return " | ".join(messages)

    def _extract_cookie_value(self, cookie_str: str, base_name: str) -> Optional[str]:
        cookie_map: Dict[str, str] = {}
        for item in cookie_str.split(";"):
            item = item.strip()
            if not item or "=" not in item:
                continue
            key, value = item.split("=", 1)
            cookie_map[key.strip()] = value.strip()

        if base_name in cookie_map:
            return cookie_map[base_name]

        chunks: List[Tuple[int, str]] = []
        prefix = f"{base_name}."
        for key, value in cookie_map.items():
            if key.startswith(prefix):
                suffix = key[len(prefix):]
                if suffix.isdigit():
                    chunks.append((int(suffix), value))

        if not chunks:
            return None

        chunks.sort(key=lambda x: x[0])
        return "".join(value for _, value in chunks)

    def _looks_like_jwt(self, value: str) -> bool:
        token = (value or "").strip()
        if token.count(".") != 2:
            return False
        return all(re.fullmatch(r"[A-Za-z0-9_-]+", p or "") for p in token.split("."))

    def _normalize_token_candidate(self, value: Any) -> str:
        if not isinstance(value, str):
            return ""
        token = unquote(value.strip())
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        return token

    def _decode_jwt_payload(self, token: str) -> Dict[str, Any]:
        try:
            parts = token.split(".")
            if len(parts) != 3:
                return {}
            payload = parts[1]
            pad = "=" * ((4 - len(payload) % 4) % 4)
            return json.loads(base64.urlsafe_b64decode(payload + pad).decode("utf-8"))
        except Exception:
            return {}

    def _is_likely_leonardo_token(self, token: str) -> bool:
        if not self._looks_like_jwt(token):
            return False
        payload = self._decode_jwt_payload(token)
        if not payload:
            return False

        iss = str(payload.get("iss", "")).lower()
        aud = payload.get("aud")
        token_use = str(payload.get("token_use", "")).lower()

        if "cognito-idp" in iss:
            return True
        if token_use in {"id", "access"}:
            return True
        if "cognito:username" in payload:
            return True
        if isinstance(aud, str) and aud.startswith("https://cognito-idp"):
            return True
        return False

    def _token_exp(self, token: str) -> int:
        payload = self._decode_jwt_payload(token)
        exp = payload.get("exp") if isinstance(payload, dict) else None
        if isinstance(exp, (int, float)):
            return int(exp)
        return 0

    def _is_fresh_token(self, token: str, min_ttl_seconds: int = 120) -> bool:
        if not self._looks_like_jwt(token):
            return False
        exp = self._token_exp(token)
        if not exp:
            return True
        now = int(time.time())
        return exp > now + max(30, int(min_ttl_seconds))

    def _pick_best_token(self, candidates: List[str], min_ttl_seconds: int = 120) -> Optional[str]:
        if not candidates:
            return None

        fresh = [t for t in candidates if self._is_fresh_token(t, min_ttl_seconds=min_ttl_seconds)]
        pool = fresh if fresh else [t for t in candidates if self._looks_like_jwt(t)]
        if not pool:
            return None

        likely = [t for t in pool if self._is_likely_leonardo_token(t)]
        pool = likely if likely else pool

        now = int(time.time())

        def token_rank(token: str) -> Tuple[int, int]:
            payload = self._decode_jwt_payload(token)
            token_use = str(payload.get("token_use", "")).lower() if isinstance(payload, dict) else ""
            use_score = 3 if token_use == "access" else 2 if token_use == "id" else 1
            exp_score = self._token_exp(token) or (now + 120)
            return (use_score, exp_score)

        pool.sort(key=token_rank, reverse=True)
        return pool[0] if pool else None

    def _find_token_in_object(self, data: Any) -> Optional[str]:
        candidates: List[str] = []

        def add_candidate(raw: Any) -> None:
            token = self._normalize_token_candidate(raw)
            if token and self._looks_like_jwt(token):
                candidates.append(token)

        def walk(node: Any) -> None:
            if isinstance(node, str):
                add_candidate(node)
                return

            if isinstance(node, list):
                for item in node:
                    walk(item)
                return

            if isinstance(node, dict):
                known_paths = [
                    ("accessToken",),
                    ("access_token",),
                    ("idToken",),
                    ("id_token",),
                    ("token",),
                    ("user", "accessToken"),
                    ("user", "access_token"),
                    ("user", "idToken"),
                    ("user", "id_token"),
                    ("session", "accessToken"),
                    ("session", "idToken"),
                ]
                for path in known_paths:
                    cur: Any = node
                    ok = True
                    for key in path:
                        if not isinstance(cur, dict) or key not in cur:
                            ok = False
                            break
                        cur = cur[key]
                    if ok:
                        add_candidate(cur)

                for key, value in node.items():
                    key_lower = str(key).lower()
                    if "cf_access_token" in key_lower:
                        continue
                    if (
                        key_lower in {"idtoken", "accesstoken", "id_token", "access_token", "token"}
                        or "token" in key_lower
                        or isinstance(value, (dict, list))
                    ):
                        walk(value)

        walk(data)
        return self._pick_best_token(candidates, min_ttl_seconds=120)

    def get_token_from_cookie(self, cookie_str: str) -> Optional[str]:
        csrf = None
        try:
            csrf_raw = (
                self._extract_cookie_value(cookie_str, "__Host-next-auth.csrf-token")
                or self._extract_cookie_value(cookie_str, "__Secure-next-auth.csrf-token")
                or self._extract_cookie_value(cookie_str, "next-auth.csrf-token")
                or self._extract_cookie_value(cookie_str, "__Host-authjs.csrf-token")
                or self._extract_cookie_value(cookie_str, "__Secure-authjs.csrf-token")
                or self._extract_cookie_value(cookie_str, "authjs.csrf-token")
            )
            if csrf_raw:
                decoded = unquote(csrf_raw)
                csrf = decoded.split("|")[0]
        except Exception:
            pass

        headers = {**self.base_headers, "cookie": cookie_str}

        if csrf:
            try:
                response = self._fetch_json(
                    f"{self.auth_url}/session",
                    "POST",
                    headers,
                    json.dumps({"csrfToken": csrf}),
                )
                token = self._find_token_in_object(response)
                if token and self._is_fresh_token(token, min_ttl_seconds=120):
                    return token
            except Exception:
                pass

        try:
            response = self._fetch_json(f"{self.auth_url}/session", "GET", headers)
            token = self._find_token_in_object(response)
            if token and self._is_fresh_token(token, min_ttl_seconds=120):
                return token
        except Exception:
            pass

        # Last fallback: try known auth session cookies directly.
        for name in [
            "__Secure-next-auth.session-token",
            "next-auth.session-token",
            "__Secure-authjs.session-token",
            "authjs.session-token",
        ]:
            val = self._extract_cookie_value(cookie_str, name)
            token = self._normalize_token_candidate(val)
            if token and self._is_fresh_token(token, min_ttl_seconds=120):
                return token

        return None

    def get_user_info(self, token: str) -> Dict[str, Any]:
        cognito_sub = ""
        try:
            payload = json.loads(base64.b64decode(token.split(".")[1] + "==").decode())
            cognito_sub = payload.get("sub", "")
        except Exception:
            pass

        query = {
            "operationName": "GetUserDetails",
            "variables": {"userSub": cognito_sub},
            "query": """query GetUserDetails($userSub: String) {
  users(where: {user_details: {cognitoId: {_eq: $userSub}}}) {
    id
    user_details {
      subscriptionTokens paidTokens rolloverTokens auth0Email __typename
    }
    __typename
  }
}""",
        }

        last_error = ""

        try:
            rj = self._gql(token, query)
            data = (rj or {}).get("data") or {}
            users = data.get("users") or []
            if users:
                user = users[0]
                details = user.get("user_details", [{}])[0]
                return {
                    "tokens": (
                        details.get("subscriptionTokens", 0)
                        + details.get("paidTokens", 0)
                        + details.get("rolloverTokens", 0)
                    )
                }
            gql_error = self._graphql_error_message(rj)
            if gql_error:
                last_error = gql_error
        except Exception as exc:
            last_error = str(exc)

        fallback_query = {
            "operationName": "GetTokenBalance",
            "variables": {},
            "query": "query GetTokenBalance { user_details { subscriptionTokens paidTokens rolloverTokens __typename } }",
        }
        try:
            rj = self._gql(token, fallback_query)
            data = (rj or {}).get("data") or {}
            user_details = data.get("user_details") or []
            if not user_details:
                gql_error = self._graphql_error_message(rj)
                if gql_error:
                    last_error = gql_error
                raise Exception(last_error or "user_details kosong")

            details = user_details[0]
            return {
                "tokens": (
                    details.get("subscriptionTokens", 0)
                    + details.get("paidTokens", 0)
                    + details.get("rolloverTokens", 0)
                )
            }
        except Exception as exc:
            if not last_error:
                last_error = str(exc)
            raise Exception(f"Gagal ambil balance/token: {last_error}")

    def upload_image_path(self, token: str, file_path: str) -> str:
        ext = Path(file_path).suffix[1:].lower()
        if ext not in ["jpg", "jpeg", "png", "webp"]:
            ext = "jpg"

        gql_ext = "jpg" if ext in ["jpg", "jpeg"] else ext
        content_type = {
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "png": "image/png",
            "webp": "image/webp",
        }.get(ext, "image/jpeg")

        upload_query = {
            "operationName": "UploadImage",
            "variables": {"uploadImageInput": {"uploadType": "INIT", "extension": gql_ext}},
            "query": """mutation UploadImage($uploadImageInput: UploadImageInput!) {
  uploadImage(arg1: $uploadImageInput) {
    uploadId url fields __typename
  }
}""",
        }

        rj = self._gql(token, upload_query)
        ud = rj.get("data", {}).get("uploadImage")
        if not ud:
            raise Exception("UploadImage mutation failed")

        upload_id = ud["uploadId"]
        s3_url = ud["url"]
        fields = json.loads(ud["fields"])

        with open(file_path, "rb") as f:
            file_bytes = f.read()

        boundary = f"----LeoUpload{int(time.time() * 1000)}"
        parts: List[bytes] = []

        for key, value in fields.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
            )

        parts.append(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                f'filename="{Path(file_path).name}"\r\nContent-Type: {content_type}\r\n\r\n'
            ).encode()
        )
        parts.append(file_bytes)
        parts.append(f"\r\n--{boundary}--\r\n".encode())

        body = b"".join(parts)
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}

        s3_response = requests.post(s3_url, headers=headers, data=body, timeout=120)
        if s3_response.status_code not in [200, 204]:
            raise Exception(f"S3 upload failed: {s3_response.status_code}")

        mod_query = {
            "operationName": "GetInitImageModeration",
            "variables": {"akUUID": upload_id},
            "query": """query GetInitImageModeration($akUUID: uuid!) {
  init_image_moderation(where: {akUUID: {_eq: $akUUID}}) {
    akUUID initImageId checkStatus __typename
  }
}""",
        }

        for _ in range(30):
            time.sleep(2)
            r = self._gql(token, mod_query)
            records = r.get("data", {}).get("init_image_moderation", [])
            if records:
                record = records[0]
                check_status = record.get("checkStatus")
                init_image_id = record.get("initImageId")
                if check_status == "Accepted" and init_image_id:
                    return init_image_id
                if check_status == "Rejected":
                    raise Exception("Image rejected by moderation")

        raise Exception("Moderation timeout")

    def upload_image_url(self, token: str, image_url: str) -> str:
        response = requests.get(image_url, timeout=120)
        if not response.ok:
            raise Exception(f"Failed to download image from URL: {image_url}")

        temp_path = f"temp_{int(time.time())}.jpg"
        with open(temp_path, "wb") as f:
            f.write(response.content)

        try:
            return self.upload_image_path(token, temp_path)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def create_generation(
        self,
        token: str,
        prompt: str,
        model_id: str,
        width: int,
        height: int,
        quantity: int,
        init_image_ids: Optional[List[str]] = None,
    ) -> str:
        params = {
            "width": width,
            "height": height,
            "prompt": prompt.strip(),
            "quantity": quantity,
            "style_ids": ["111dc692-d470-4eec-b791-3475abac4c46"],
            "prompt_enhance": "ON",
            "dimensions": f"{width}x{height}",
            "modelId": model_id,
            "negative_prompt": "",
            "guidance_scale": 7.0,
            "num_inference_steps": 30,
        }
        if init_image_ids:
            params["guidances"] = {
                "image_reference": [
                    {"image": {"id": image_id, "type": "UPLOADED"}, "strength": "MID"}
                    for image_id in init_image_ids
                ]
            }

        query = {
            "operationName": "Generate",
            "variables": {
                "request": {
                    "model": "nano-banana-2",
                    "parameters": params,
                    "public": True,
                }
            },
            "query": """mutation Generate($request: CreateGenerationRequest!) {
  generate(request: $request) {
    apiCreditCost generationId __typename
  }
}""",
        }

        rj = self._gql(token, query)
        data = rj.get("data") or {}
        gen_id = data.get("generate", {}).get("generationId")
        if gen_id:
            return gen_id
        errors = [e.get("message", "") for e in rj.get("errors", [])]
        raise Exception(", ".join(errors) or f"Generate failed: {json.dumps(rj)}")

    def poll_status(self, token: str, gen_id: str) -> str:
        query = {
            "operationName": "GetAIGenerationFeedStatuses",
            "variables": {"where": {"id": {"_eq": gen_id}}},
            "query": """query GetAIGenerationFeedStatuses($where: generations_bool_exp = {}) {
  generations(where: $where) {
    id status __typename
  }
}""",
        }
        rj = self._gql(token, query)
        data = rj.get("data") or {}
        generations = data.get("generations", [])
        return generations[0].get("status", "PENDING") if generations else "PENDING"

    def get_image_urls(self, token: str, gen_id: str) -> List[str]:
        query = {
            "operationName": "GetAIGenerationFeed",
            "variables": {"where": {"id": {"_eq": gen_id}}, "limit": 1},
            "query": """query GetAIGenerationFeed($where: generations_bool_exp = {}, $limit: Int) {
  generations(where: $where, limit: $limit) {
    generated_images(order_by: [{url: desc}]) {
      url id __typename
    }
    __typename
  }
}""",
        }
        rj = self._gql(token, query)
        data = rj.get("data") or {}
        generations = data.get("generations", [])
        if not generations:
            return []
        return [img.get("url") for img in generations[0].get("generated_images", []) if img.get("url")]

    def wait_for_completion(self, token: str, gen_id: str, timeout: int = 300, poll_interval: int = 4) -> Dict[str, Any]:
        start = time.time()
        while time.time() - start < timeout:
            status = self.poll_status(token, gen_id)
            if status == "COMPLETED":
                return {"success": True, "images": self.get_image_urls(token, gen_id)}
            if status in ["FAILED", "ERROR"]:
                return {"success": False, "error": "Generation failed"}
            time.sleep(poll_interval)

        try:
            images = self.get_image_urls(token, gen_id)
            if images:
                return {"success": True, "images": images}
        except Exception:
            pass

        return {"success": False, "error": "Generation timeout"}
