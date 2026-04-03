import threading
import time
from typing import Any, Dict, List, Optional

import requests

from app.leonardo_service import ASPECT_TO_SIZE, LeonardoPoolService
from app.store import Store


class TelegramBotRunner:
    MODEL_PAGE_SIZE = 8

    def __init__(self, store: Store, service: LeonardoPoolService):
        self.store = store
        self.service = service
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._offset = 0
        self._chat_state: Dict[int, Dict[str, str]] = {}

    def _persist_chat_state(self, chat_id: int, state: Dict[str, str]) -> None:
        try:
            self.store.upsert_telegram_chat_state(
                chat_id=chat_id,
                model_id=state.get("model_id", ""),
                aspect_ratio=state.get("aspect_ratio", "1:1"),
            )
        except Exception:
            return

    def status(self) -> Dict[str, Any]:
        token = (self.store.get_setting("telegram_bot_token", "") or "").strip()
        enabled = self.store.get_setting("telegram_enabled", "0") == "1"
        running = self._thread is not None and self._thread.is_alive()
        return {
            "enabled": enabled,
            "token_set": bool(token),
            "running": running,
        }

    def refresh_from_settings(self) -> None:
        token = (self.store.get_setting("telegram_bot_token", "") or "").strip()
        enabled = self.store.get_setting("telegram_enabled", "0") == "1"
        if enabled and token:
            self.start()
        else:
            self.stop()

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run_loop, name="telegram-bot", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stop_event.set()
            thread = self._thread
            self._thread = None
        if thread and thread.is_alive():
            thread.join(timeout=3)

    def _token(self) -> str:
        return (self.store.get_setting("telegram_bot_token", "") or "").strip()

    def _api_url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self._token()}/{method}"

    def get_me(self) -> Dict[str, Any]:
        response = requests.get(self._api_url("getMe"), timeout=30)
        if not response.ok:
            raise Exception(f"Telegram getMe failed: HTTP {response.status_code}")
        body = response.json()
        if not body.get("ok"):
            raise Exception(f"Telegram getMe error: {body}")
        return body.get("result", {})

    def test_connection(self, test_chat_id: Optional[int] = None) -> Dict[str, Any]:
        token = self._token()
        if not token:
            raise Exception("Bot token masih kosong")

        me = self.get_me()
        tested_chat_id: Optional[int] = None
        if test_chat_id is not None:
            tested_chat_id = int(test_chat_id)
            self._send_message(
                tested_chat_id,
                "LeoAPI Telegram test OK. Bot berhasil terhubung dari admin panel.",
            )

        return {
            "username": me.get("username", ""),
            "name": me.get("first_name", ""),
            "chat_id_tested": tested_chat_id,
        }

    def _allowed_chat_ids(self) -> set[int]:
        raw = (self.store.get_setting("telegram_allowed_chat_ids", "") or "").strip()
        out: set[int] = set()
        for part in raw.replace("\n", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                out.add(int(part))
            except Exception:
                continue
        return out

    def _is_allowed_chat(self, chat_id: int) -> bool:
        allowed = self._allowed_chat_ids()
        if not allowed:
            return True
        return chat_id in allowed

    def _post_json(self, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        response = requests.post(self._api_url(method), json=payload, timeout=60)
        if not response.ok:
            raise Exception(f"Telegram API {method} failed: HTTP {response.status_code}")
        body = response.json()
        if not body.get("ok"):
            raise Exception(f"Telegram API {method} error: {body}")
        return body

    def _post_multipart(self, method: str, data: Dict[str, Any], files: Dict[str, Any]) -> Dict[str, Any]:
        response = requests.post(self._api_url(method), data=data, files=files, timeout=180)
        if not response.ok:
            raise Exception(f"Telegram API {method} failed: HTTP {response.status_code}")
        body = response.json()
        if not body.get("ok"):
            raise Exception(f"Telegram API {method} error: {body}")
        return body

    def _send_message(self, chat_id: int, text: str, reply_markup: Optional[Dict[str, Any]] = None) -> Optional[int]:
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        body = self._post_json("sendMessage", payload)
        message = body.get("result", {})
        return int(message.get("message_id", 0) or 0) or None

    def _edit_message(self, chat_id: int, message_id: int, text: str) -> None:
        try:
            self._post_json(
                "editMessageText",
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": text,
                },
            )
        except Exception:
            return

    def _edit_message_with_markup(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: Dict[str, Any],
    ) -> None:
        try:
            self._post_json(
                "editMessageText",
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": text,
                    "reply_markup": reply_markup,
                },
            )
        except Exception:
            return

    def _send_typing(self, chat_id: int) -> None:
        try:
            self._post_json("sendChatAction", {"chat_id": chat_id, "action": "upload_photo"})
        except Exception:
            return

    def _send_image_from_url(self, chat_id: int, image_url: str, caption: str) -> None:
        response = requests.get(image_url, timeout=180)
        if not response.ok:
            raise Exception(f"Download result failed HTTP {response.status_code}")

        content_type = response.headers.get("content-type", "image/jpeg")
        files = {
            "photo": ("result.jpg", response.content, content_type),
        }
        data = {
            "chat_id": str(chat_id),
            "caption": caption,
        }
        self._post_multipart("sendPhoto", data=data, files=files)

    def _bot_keyboard(self) -> Dict[str, Any]:
        return {
            "keyboard": [
                [{"text": "🎨 Select Model"}, {"text": "📐 Select Ratio"}],
                [{"text": "🧾 My Config"}],
            ],
            "resize_keyboard": True,
        }

    def _ratio_label(self, ratio: str) -> str:
        labels = {
            "16:9": "🖥️ 16:9",
            "9:16": "📱 9:16",
            "1:1": "🟦 1:1",
            "4:3": "🖼️ 4:3",
        }
        return labels.get(ratio, ratio)

    def _model_markup(self, selected_model_id: str = "", page: int = 0) -> Dict[str, Any]:
        models = self.store.list_models()
        total = len(models)
        if total == 0:
            return {"inline_keyboard": []}

        page_size = self.MODEL_PAGE_SIZE
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = max(0, min(page, total_pages - 1))
        start = page * page_size
        end = start + page_size
        page_models = models[start:end]

        rows: List[List[Dict[str, str]]] = []
        row: List[Dict[str, str]] = []
        for item in page_models:
            model_id = item.get("model_id", "")
            name = item.get("name", "Model")[:26]
            is_selected = model_id == selected_model_id
            row.append({
                "text": f"✅ {name}" if is_selected else f"🎯 {name}",
                "callback_data": f"model:{model_id}",
            })
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)

        nav_row: List[Dict[str, str]] = []
        if page > 0:
            nav_row.append({"text": "⬅️ Prev", "callback_data": f"model_page:{page - 1}"})
        nav_row.append({"text": f"📄 {page + 1}/{total_pages}", "callback_data": "model_page:noop"})
        if page < total_pages - 1:
            nav_row.append({"text": "Next ➡️", "callback_data": f"model_page:{page + 1}"})
        rows.append(nav_row)

        return {"inline_keyboard": rows}

    def _ratio_markup(self, selected_ratio: str = "") -> Dict[str, Any]:
        rows = []
        row: List[Dict[str, str]] = []
        for ratio in ASPECT_TO_SIZE.keys():
            label = self._ratio_label(ratio)
            if ratio == selected_ratio:
                label = f"✅ {label}"
            row.append({"text": label, "callback_data": f"ratio:{ratio}"})
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        return {"inline_keyboard": rows}

    def _get_chat_state(self, chat_id: int) -> Dict[str, str]:
        state = self._chat_state.get(chat_id)
        if state is None:
            persisted = self.store.get_telegram_chat_state(chat_id)
            state = {
                "model_id": (persisted or {}).get("model_id", "") or self.store.get_default_model_id() or "",
                "aspect_ratio": (persisted or {}).get("aspect_ratio", "")
                or self.store.get_setting("default_aspect_ratio", "1:1"),
                "reference_image_url": "",
            }
            self._chat_state[chat_id] = state
        return state

    def _handle_start(self, chat_id: int) -> None:
        self._send_message(
            chat_id,
            "LeoAPI Telegram bot aktif. Pilih model/ratio, upload reference image (opsional), lalu kirim prompt. Saat generate, bot akan tampilkan loading lalu kirim hasil otomatis.",
            reply_markup=self._bot_keyboard(),
        )

    def _handle_file_reference(self, chat_id: int, file_id: str) -> None:
        file_meta = self._post_json("getFile", {"file_id": file_id})
        file_path = file_meta.get("result", {}).get("file_path", "")
        if not file_path:
            raise Exception("File path tidak ditemukan dari Telegram")
        image_url = f"https://api.telegram.org/file/bot{self._token()}/{file_path}"
        state = self._get_chat_state(chat_id)
        state["reference_image_url"] = image_url
        self._send_message(chat_id, "Reference image tersimpan. Sekarang kirim prompt untuk generate.")

    def _handle_prompt(self, chat_id: int, prompt: str) -> None:
        state = self._get_chat_state(chat_id)
        model_id = state.get("model_id") or self.store.get_default_model_id() or ""
        aspect_ratio = state.get("aspect_ratio") or self.store.get_setting("default_aspect_ratio", "1:1")
        refs: List[str] = []
        if state.get("reference_image_url"):
            refs.append(state["reference_image_url"])

        if not model_id:
            self._send_message(chat_id, "Model default belum ada. Tambahkan model dulu di admin panel.")
            return

        model_name = model_id
        model = self.store.get_model_by_model_id(model_id)
        if model:
            model_name = model.get("name", model_id)

        loading_message_id = self._send_message(
            chat_id,
            (
                "⏳ Generating image...\n"
                f"Model: {model_name}\n"
                f"Ratio: {self._ratio_label(aspect_ratio)}\n"
                f"Reference: {'yes' if refs else 'no'}"
            ),
        )

        loading_stop = threading.Event()

        def loading_worker() -> None:
            frames = ["⏳", "⌛"]
            idx = 0
            while not loading_stop.is_set():
                self._send_typing(chat_id)
                if loading_message_id:
                    frame = frames[idx % len(frames)]
                    dots = "." * ((idx % 3) + 1)
                    self._edit_message(
                        chat_id,
                        loading_message_id,
                        (
                            f"{frame} Generating image{dots}\n"
                            f"Model: {model_name}\n"
                            f"Ratio: {self._ratio_label(aspect_ratio)}\n"
                            f"Reference: {'yes' if refs else 'no'}"
                        ),
                    )
                idx += 1
                loading_stop.wait(3)

        worker = threading.Thread(target=loading_worker, name=f"tg-loading-{chat_id}", daemon=True)
        worker.start()

        try:
            result = self.service.generate_images(
                prompt=prompt,
                n=1,
                model_id=model_id,
                aspect_ratio=aspect_ratio,
                reference_image_urls=refs,
                save_results=False,
            )
        finally:
            loading_stop.set()
            worker.join(timeout=1)

        images = [item.get("url", "") for item in result.get("data", []) if item.get("url")]
        if not images:
            if loading_message_id:
                self._edit_message(chat_id, loading_message_id, "⚠️ Generate selesai tapi tidak ada URL hasil.")
            self._send_message(chat_id, "Generate selesai tapi tidak ada URL hasil.")
            return

        if loading_message_id:
            self._edit_message(chat_id, loading_message_id, "✅ Generate selesai. Mengirim hasil...")

        for idx, image_url in enumerate(images[:4], start=1):
            caption = f"Result {idx} | {self._ratio_label(aspect_ratio)} | {model_name}"
            self._send_image_from_url(chat_id, image_url, caption)

        self._send_message(chat_id, "🎉 Done. Gambar sudah dikirim otomatis.")

        # Reference is one-time by default to avoid accidental reuse.
        state["reference_image_url"] = ""

    def _handle_callback(self, update: Dict[str, Any]) -> None:
        callback = update.get("callback_query", {})
        data = callback.get("data", "")
        message = callback.get("message", {})
        chat = message.get("chat", {})
        chat_id = int(chat.get("id", 0) or 0)
        if chat_id == 0:
            return
        if not self._is_allowed_chat(chat_id):
            self._send_message(chat_id, "Chat ID ini belum diizinkan oleh admin.")
            return

        state = self._get_chat_state(chat_id)
        message_id = int(message.get("message_id", 0) or 0)
        if data.startswith("model:"):
            model_id = data.split(":", 1)[1].strip()
            model = self.store.get_model_by_model_id(model_id)
            if not model:
                self._send_message(chat_id, "Model tidak ditemukan.")
                return
            state["model_id"] = model_id
            self._persist_chat_state(chat_id, state)
            self._send_message(chat_id, f"✅ Model aktif: {model.get('name', 'Model')}")
        elif data.startswith("model_page:"):
            page_value = data.split(":", 1)[1].strip()
            if page_value == "noop":
                pass
            else:
                try:
                    page = int(page_value)
                except Exception:
                    page = 0
                if message_id:
                    self._edit_message_with_markup(
                        chat_id,
                        message_id,
                        "🎨 Pilih model untuk generate:",
                        self._model_markup(state.get("model_id", ""), page=page),
                    )
        elif data.startswith("ratio:"):
            ratio = data.split(":", 1)[1].strip()
            if ratio in ASPECT_TO_SIZE:
                state["aspect_ratio"] = ratio
                self._persist_chat_state(chat_id, state)
                self._send_message(chat_id, f"✅ Aspect ratio aktif: {self._ratio_label(ratio)}")

        try:
            self._post_json("answerCallbackQuery", {"callback_query_id": callback.get("id", "")})
        except Exception:
            return

    def _handle_message(self, update: Dict[str, Any]) -> None:
        message = update.get("message", {})
        chat = message.get("chat", {})
        chat_id = int(chat.get("id", 0) or 0)
        if chat_id == 0:
            return

        if not self._is_allowed_chat(chat_id):
            self._send_message(chat_id, "Chat ID ini belum diizinkan oleh admin.")
            return

        text = (message.get("text") or "").strip()

        if text.startswith("/start"):
            self._handle_start(chat_id)
            return

        if text in {"Select Model", "🎨 Select Model"}:
            state = self._get_chat_state(chat_id)
            self._send_message(
                chat_id,
                "🎨 Pilih model untuk generate:",
                reply_markup=self._model_markup(state.get("model_id", ""), page=0),
            )
            return

        if text in {"Select Ratio", "📐 Select Ratio"}:
            state = self._get_chat_state(chat_id)
            self._send_message(
                chat_id,
                "📐 Pilih aspect ratio:",
                reply_markup=self._ratio_markup(state.get("aspect_ratio", "")),
            )
            return

        if text in {"My Config", "🧾 My Config"}:
            state = self._get_chat_state(chat_id)
            model_name = "(unset)"
            model_id = state.get("model_id", "")
            if model_id:
                model = self.store.get_model_by_model_id(model_id)
                if model:
                    model_name = model.get("name", model_id)
                else:
                    model_name = model_id
            ratio = self._ratio_label(state.get("aspect_ratio", "1:1"))
            ref = "yes" if state.get("reference_image_url") else "no"
            self._send_message(chat_id, f"Model: {model_name}\nRatio: {ratio}\nReference: {ref}")
            return

        photos = message.get("photo") or []
        if photos:
            file_id = photos[-1].get("file_id", "")
            if file_id:
                self._handle_file_reference(chat_id, file_id)
                return

        document = message.get("document") or {}
        mime_type = (document.get("mime_type") or "").lower()
        if document.get("file_id") and mime_type.startswith("image/"):
            self._handle_file_reference(chat_id, document.get("file_id", ""))
            return

        if text and not text.startswith("/"):
            try:
                self._handle_prompt(chat_id, text)
            except Exception as exc:
                self._send_message(chat_id, f"Generate gagal: {exc}")
            return

        self._send_message(chat_id, "Kirim /start untuk mulai.")

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            enabled = self.store.get_setting("telegram_enabled", "0") == "1"
            if not enabled or not self._token():
                time.sleep(2)
                continue

            try:
                payload = {
                    "timeout": 45,
                    "offset": self._offset,
                    "allowed_updates": ["message", "callback_query"],
                }
                response = requests.post(self._api_url("getUpdates"), json=payload, timeout=60)
                if not response.ok:
                    time.sleep(2)
                    continue
                body = response.json()
                if not body.get("ok"):
                    time.sleep(2)
                    continue
                updates = body.get("result", [])
                for update in updates:
                    update_id = int(update.get("update_id", 0) or 0)
                    if update_id >= self._offset:
                        self._offset = update_id + 1
                    if update.get("callback_query"):
                        self._handle_callback(update)
                    elif update.get("message"):
                        self._handle_message(update)
            except Exception:
                time.sleep(2)
