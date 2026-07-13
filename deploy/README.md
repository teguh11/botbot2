# Deploy Freqtrade (NFI) ke VPS

Deploy 24/7 via **GitHub Actions** (SSH ke VPS). Idempotent — aman di-run berkali.

## 1. Set GitHub Secrets (sekali)

Repo → Settings → Secrets and variables → Actions → New repository secret:

| Secret | Isi |
|---|---|
| `SERVER_HOST` | IP / hostname VPS |
| `SERVER_USER` | user SSH (mis. `ubuntu`) |
| `SSH_PRIVATE_KEY` | private key yang authorized di VPS |
| `FREQUI_PASSWORD` | password login dashboard FreqUI |

> `SERVER_HOST`, `SERVER_USER`, `SSH_PRIVATE_KEY` mungkin sudah ada dari setup lama.
> Yang baru cukup tambah **`FREQUI_PASSWORD`**.

## 2. Jalankan deploy

Actions tab → **Deploy Freqtrade to VPS** → Run workflow.

Workflow otomatis (di Ubuntu 24.04):
1. Install Python 3.12, build tools, nginx
2. Install TA-Lib (via `.deb` resmi)
3. Bikin swap 2 GB (buffer memori NFI di VPS 2 GB)
4. Sync repo ke `origin/master`
5. Bikin `.venv` + install `requirements.txt` + FreqUI
6. Bikin `.env` (exchange key kosong = dry-run; secret FreqUI di-generate & password dari `FREQUI_PASSWORD`)
7. Install & start systemd service `freqtrade` (auto-restart, MemoryMax 1700M)
8. Set nginx reverse-proxy `/` → FreqUI `127.0.0.1:8080`

## 3. Akses dashboard

```
http://<SERVER_HOST>/
user: botbot2   password: <FREQUI_PASSWORD>
```

> Pastikan port 80 terbuka di Security Group VPS.
> Bot mulai dry-run; NFI download warmup candle dulu (beberapa menit) sebelum aktif.

## 4. Operasi di server

```bash
sudo systemctl status freqtrade      # status
sudo journalctl -u freqtrade -f      # log live
sudo systemctl restart freqtrade     # restart
```

## Catatan

- **Dry-run** (paper trading). Untuk live butuh **akun SPOT Binance** — isi
  `FREQTRADE__EXCHANGE__KEY/SECRET` di `.env` server & set `dry_run: false`.
- Whitelist 20 pair (aman untuk RAM 2 GB). Naikkan bertahap kalau RAM lega;
  pantau `free -h` / `systemctl status freqtrade`.
- Kalau OOM-restart berulang: kurangi pair di `user_data/config_nfi.json`.
