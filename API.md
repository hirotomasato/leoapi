# API Reference

Dokumen ini menjelaskan endpoint publik LeoAPI, endpoint Studio, dan perilaku auth terbaru.

## Base URL

- Local: http://127.0.0.1:8000

## Endpoint Summary

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | /health | None | Health check |
| POST | /v1/images/generations | None | OpenAI-compatible image generation |
| POST | /studio/api/images/generations | Studio session | Studio single generation |
| POST | /studio/api/images/batch | Studio session | Submit batch dan dapatkan job_id |
| GET | /studio/api/images/batch/{job_id} | Studio session | Poll progress batch realtime |

## Auth Behavior (Cookie Pool)

1. Sumber auth utama adalah full cookie string.
2. Backend resolve Bearer JWT dari cookie saat runtime.
3. JWT fallback dipakai hanya jika token valid, fresh, dan terdeteksi sebagai token Leonardo.
4. Saat token baru berhasil diambil dari cookie, backend update fallback token otomatis ke DB Cookie Pool.

## 1) Health

Request:

```http
GET /health
```

Response:

```json
{
  "status": "ok"
}
```

## 2) OpenAI-Compatible Image Generation

Request:

```http
POST /v1/images/generations
Content-Type: application/json
```

Body:

| Field | Type | Required | Notes |
|---|---|---|---|
| prompt | string | Yes | Prompt utama |
| model | string | No | Model ID, fallback default model |
| n | integer | No | 1..4, default 1 |
| aspect_ratio | string | No | 16:9, 9:16, 1:1, 4:3 |
| size | string | No | Alias aspect ratio |
| image_url | string | No | Single reference image |
| image_urls | array[string] | No | Multiple reference image |

Aspect ratio map:

| aspect_ratio | resolution |
|---|---|
| 16:9 | 2752x1536 |
| 9:16 | 1536x2752 |
| 1:1 | 1536x1536 |
| 4:3 | 2048x1536 |

Supported size aliases:

- 2752x1536, 1344x768 => 16:9
- 1536x2752, 768x1344 => 9:16
- 1536x1536, 1024x1024 => 1:1
- 2048x1536, 1152x896 => 4:3

Reference image rules:

- image_url dan image_urls digabung.
- Maksimum 3 URL pertama dipakai.
- URL harus public dan bisa diakses server.

Example request:

```bash
curl -sS -X POST "http://127.0.0.1:8000/v1/images/generations" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "desa di kaki gunung saat golden hour",
    "n": 1,
    "aspect_ratio": "9:16",
    "image_url": "https://example.com/reference.jpg"
  }'
```

Example response:

```json
{
  "created": 1775035361,
  "data": [
    {
      "url": "https://cdn.leonardo.ai/users/.../image-0.jpg"
    }
  ],
  "provider": {
    "generation_id": "1f12daba-0b65-6f00-bc85-3fcf99be9e4a",
    "used_cookie_id": 16,
    "aspect_ratio": "9:16",
    "model_id": "7418e71f-4133-4e1b-9895-bee19f48f2ce",
    "saved_files": [],
    "auto_save_enabled": false
  }
}
```

## 3) Studio Single Generate

Request:

```http
POST /studio/api/images/generations
Content-Type: application/json
```

Auth:

- Butuh Studio session login.
- 401 jika belum login.
- 403 jika Studio disabled.

Body sama dengan /v1/images/generations.

Jika studio_proxy_base_url aktif, request akan di-forward ke proxy.

## 4) Studio Batch Submit

Request:

```http
POST /studio/api/images/batch
Content-Type: application/json
```

Body:

| Field | Type | Required | Notes |
|---|---|---|---|
| prompts | array[string] | Yes | Daftar prompt batch |
| model | string | No | Model ID |
| n | integer | No | 1..4 |
| size | string | No | Alias aspect ratio |
| aspect_ratio | string | No | 16:9, 9:16, 1:1, 4:3 |
| image_url | string | No | Single reference image |
| image_urls | array[string] | No | Multi reference image |
| concurrency | integer | No | 1..8, default 3 |

Response:

```json
{
  "job_id": "5ecf0a786d4c42ea9eec8b6f07e6f8d8",
  "status": "queued",
  "created": 1775036000,
  "count": 2,
  "poll_url": "/studio/api/images/batch/5ecf0a786d4c42ea9eec8b6f07e6f8d8"
}
```

## 5) Studio Batch Progress

Request:

```http
GET /studio/api/images/batch/{job_id}
```

Response fields penting:

- job_id
- status: queued, running, completed
- progress: 0..100
- completed, success, failed
- results[] dengan item status: queued, running, done, failed

## Auto Save

Admin settings:

- auto_save_images
- save_images_dir

Saat aktif, backend download hasil image URL ke folder lokal dan isi provider.saved_files.

## Common Errors

Invalid aspect ratio:

```json
{
  "error": {
    "message": "aspect_ratio must be one of 16:9, 9:16, 1:1, 4:3"
  }
}
```

No prompts provided:

```json
{
  "error": {
    "message": "No prompts provided"
  }
}
```

Studio login required:

```json
{
  "error": {
    "message": "Studio login required"
  }
}
```

Job not found:

```json
{
  "error": {
    "message": "Job not found"
  }
}
```
