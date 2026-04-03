# ExLeo Chrome Extension

Extension ini digunakan untuk mengambil full cookie Leonardo dari browser agar bisa langsung dipakai di LeoAPI.

## Fitur

- Ambil full cookie `app.leonardo.ai` dengan satu klik.
- Full scan cookie dari domain Leonardo (`app.leonardo.ai`, `api.leonardo.ai`, `.leonardo.ai`) lalu dirapikan untuk header cookie yang paling stabil.
- Copy full cookie ke clipboard.
- Simpan ke file `.txt` (`cookie=...`).

## Cara Install (Developer Mode)

1. Buka Chrome dan masuk ke `chrome://extensions`.
2. Aktifkan `Developer mode`.
3. Klik `Load unpacked`.
4. Pilih folder `exleo`.

## Cara Pakai

1. Login dulu ke `https://app.leonardo.ai`.
2. Pastikan tab aktif sekarang adalah `app.leonardo.ai` (bukan tab lain).
3. Klik icon `ExLeo`.
4. Klik `Ambil Full Cookie`.
5. Klik `Copy` lalu paste ke Admin LeoAPI -> Cookie Pool.

Output extension bisa berisi dua baris:

```txt
cookie=...
token=...
```

Jika `token` ada, backend LeoAPI akan memakai token itu sebagai fallback saat endpoint session Leonardo terproteksi checkpoint.

## Catatan Format

- File JSON export dari Cookie Manager (seperti dari hotcleaner) biasanya terenkripsi/compressed dan tidak bisa dipakai langsung sebagai header cookie untuk API.
- ExLeo mengeluarkan output siap pakai untuk LeoAPI:
	- `cookie=name=value; ...`
	- `token=...` (opsional)

## Catatan

- LeoAPI sekarang menolak JWT token-only.
- Input yang benar adalah full cookie string (`name=value; name2=value2; ...`).
