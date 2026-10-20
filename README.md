# MT5 Auto Trading Bot Dashboard

Bot layering (grid) + dashboard web lokal untuk **terminal MetaTrader 5 broker apapun** (Demo atau Live).

## Cara Jalanin (Windows)

1. Masuk terminal MetaTrader 5, **login ke akun** (sarankan Demo dulu).
2. Install paket jembatan ( sekali saja ):
   ```
   py -m pip install MetaTrader5
   ```
3. Double-click `start.bat` (atau `python bot.py`).
4. Buka dashboard: **http://localhost:8940**

## Logika Trading

- **Auto-entry:** pas bot di-Start dan tidak ada posisi, langsung buka posisi pertama (arah sesuai config).
- **Layering:** kalau harga jalan melawan di `Jarak Layer (points)`, buka layer baru (lot awal x multiplier per layer), max `Max Layer`.
- **Exit:** seluruh basket di-close otomatis begitu **PnL basket >= Target ($)**, default **$5**. Lalu siklus baru mulai.
- **Safety spike:** kalau floating basket **jatuh ≥ `Spike Drop` (default $50) dalam `Window Spike` (default 10 detik)**, semua posisi di-close dan bot berhenti (tercatat note `spike`). Turun pelan-pelan tidak memicu ini.
- **Safety statis (opsional):** `Batas Loss Basket` (default **0 = mati**) — kalau diisi, basket di-close ketika floating ≤ nilai negatifnya (note `stop-loss`).
- **Trailing stop (opsional):** aktifkan di dashboard. Setelah peak profit basket ≥ `Aktif setelah profit`, basket di-close kalau floating turun sebesar `Trailing (%)` dari peak (note `trailing`).
- **Drawdown:** dashboard menampilkan drawdown equity hari ini dan max drawdown sepanjang masa (disimpan di `dd.json`, riwayat harian 90 hari).
- Semua posisi bot ditandai `magic 972025`, jadi aman campur dengan posisi manual / bot lain.

## File

| File | Isi |
|---|---|
| `bot.py` | jembatan MT5 + engine + web server (satu file) |
| `dashboard.html` | UI (dikirim oleh bot.py di `http://localhost:8940`) |
| `config.json` | config tersimpan, otomatis terbentuk pas Simpan Config |
| `start.bat` | launcher Windows |

## Catatan

- Config berlaku live tanpa restart bot.
- Tombol **Panic Close All** = stop bot + close semua posisi bot.
- Equity ketat? Bot tidak punya stop-loss per posisi target — batasi `Max Layer` dan uji di **Demo** dulu.
