# LeoAPI

> Status: Project ini masih dalam tahap pengembangan aktif (work in progress). Beberapa fitur, flow auth, dan endpoint dapat berubah sewaktu-waktu.

LeoAPI adalah backend FastAPI untuk image generation Leonardo dengan endpoint OpenAI-compatible, Admin Dashboard, Studio UI, Telegram bot, dan Cookie Pool berbasis full-cookie.

## Fitur Utama

| Area | Detail |
|---|---|
| OpenAI-compatible endpoint | POST /v1/images/generations |
| Cookie Pool | Multi-cookie rotasi + refresh profile/session |
| Full-cookie auth | Input utama wajib full cookie string, bukan JWT-only |
| Auto refresh token | JWT fallback di-refresh otomatis dari full cookie saat runtime |
| Studio Dashboard | Batch submit + polling job progress realtime |
| Studio Prompt Builder | Multiline draft, add/edit/delete, hidden preview item |
| Studio UX | Dark mode, sidebar rapi, compact lightbox preview |
| Telegram Bot | Enable/disable, token, allowlist chat IDs, test connection |
| Auto save image | Simpan hasil generation ke folder lokal (opsional) |
| Persistensi | SQLite di data/app.db |

## Arsitektur Auth (Penting)

1. Request generation tetap menggunakan Bearer JWT.
2. Sumber utama auth adalah full cookie.
3. JWT fallback hanya dipakai jika valid, fresh, dan cocok token Leonardo.
4. Saat backend berhasil resolve JWT baru dari full cookie, fallback token disimpan ulang otomatis ke Cookie Pool.

Tujuan alur ini: menghindari kondisi token cepat expired walau cookie login masih valid.

## Quick Start

1. Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

2. Install dependencies

```bash
uv sync
```

3. Run server

```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

## URL Penting

| Area | Path |
|---|---|
| Health | /health |
| OpenAI images | /v1/images/generations |
| Admin login | /admin/login |
| Studio login | /studio/login |
| Studio dashboard | /studio/dashboard |

## Default Credentials

| Scope | Username | Password |
|---|---|---|
| Admin | admin | admin123 |
| Studio | studio | studio123 |

Ganti credential default sebelum dipakai produksi.

## Aspect Ratio

| Aspect Ratio | Resolution |
|---|---|
| 16:9 | 2752x1536 |
| 9:16 | 1536x2752 |
| 1:1 | 1536x1536 |
| 4:3 | 2048x1536 |

Alias size yang tetap diterima:
1344x768, 768x1344, 1024x1024, 1152x896.

## ExLeo Extension (Folder exleo)

Extension ExLeo dipakai untuk ambil full cookie dari browser dengan format siap pakai di LeoAPI:

```txt
cookie=name=value; ...
token=... (opsional)
```

Catatan:
1. Format export JSON terenkripsi dari cookie manager eksternal bukan format direct-use untuk LeoAPI.
2. LeoAPI menolak JWT-only tanpa full cookie.

## Studio Batch Flow

1. Submit batch ke POST /studio/api/images/batch.
2. Dapatkan job_id.
3. Poll status ke GET /studio/api/images/batch/{job_id}.
4. UI menampilkan request id + status job queue realtime.

## Dokumentasi API

Dokumentasi endpoint terbaru ada di API.md.

## Lisensi

Project ini menggunakan lisensi MIT. Lihat file LICENSE.
