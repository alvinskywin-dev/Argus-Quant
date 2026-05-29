# Alpha Radar Signals — Phase 0 (Minimal Patches)

5 unified-diff patches áp dụng bằng `patch -p1`. Tổng cộng ~240 dòng diff,
**100% additive** vào source bạn đang chạy — không ghi đè file nào.

## Tất cả feature hiện tại được bảo toàn:
- VIP/Public routing trong `bot.py` (`_route_signal_chats`)
- `SignalMessage` multi-channel mapping
- `market_bias` + realtime price cache (`ws_engine.py`)
- Dashboard auth/tabs (login/logout/cookie)
- Uptime monitor, daily/weekly stats jobs, performance reports
- Admin commands (`/setconfidence`, `/setcooldown`, `/setmaxsignals`, `/getconfig`)
- Card modules (`event_card.py`, `signal_card.py`, `stats_card.py`)
- Adaptive scoring penalties trong `scorer.py` (V2 logic của bạn)
- BTC bias + RSI penalties + momentum bonuses trong `filters.py`

## Danh sách patch

| # | File | Diff | Mục đích |
|---|---|---|---|
| 01 | `app/risk/levels.py` | +43 lines | Hard SL cap (`MAX_SL_PCT=5%`), trả `sl_pct` + `sl_clamped` |
| 02 | `app/risk/filters.py` | +24 lines | Thêm param `levels=None` (optional), SL distance sanity check |
| 03 | `app/scanner/scanner.py` | +6 / -4 lines | Build levels TRƯỚC khi gọi filters, pass `levels` vào |
| 04 | `app/telegram_bot/bot.py` | +27 / -7 lines | TP/SL event fallback: log INFO khi message gone, ERROR + admin alert chỉ khi standalone cũng fail |
| 05 | `app/dashboard/server.py` | +1 line | Thêm `import os` ở module level (fix `NameError` trong `get_stats()`) |

## Verify nội dung từng patch trước khi áp

```bash
# Đọc nội dung patch
less 01-risk-levels-sl-cap.patch
less 02-risk-filters-sl-sanity.patch
less 03-scanner-reorder.patch
less 04-bot-event-fallback.patch
less 05-dashboard-import-os.patch
```

Dòng có `+` ở đầu = thêm vào. Dòng có `-` = xóa đi. Patch 01,02,05 chỉ có `+`,
patch 03,04 có swap thứ tự / refactor try-except (xem chi tiết bên dưới).

---

## Cách deploy

### Bước 1 — Upload patches lên VPS

Từ máy local:

```bash
scp 01-risk-levels-sl-cap.patch \
    02-risk-filters-sl-sanity.patch \
    03-scanner-reorder.patch \
    04-bot-event-fallback.patch \
    05-dashboard-import-os.patch \
    botuser@VPS_IP:~/futures-signal-bot/
```

### Bước 2 — Backup + dry-run

```bash
cd ~/futures-signal-bot

# Backup các file sẽ chỉnh
mkdir -p .phase0-backup
cp app/risk/levels.py        .phase0-backup/
cp app/risk/filters.py       .phase0-backup/
cp app/scanner/scanner.py    .phase0-backup/
cp app/telegram_bot/bot.py   .phase0-backup/
cp app/dashboard/server.py   .phase0-backup/

# Dry-run để chắc patches apply được
for p in 0?-*.patch; do
  echo "=== $p ==="
  patch -p1 --dry-run < "$p"
done
```

Mỗi patch phải in `checking file ...` không có error/reject. Nếu báo
`HUNK FAILED` hoặc `Reversed (or previously applied)`, đừng tiếp tục — paste log
lên cho tôi xem.

### Bước 3 — Áp patches

```bash
cd ~/futures-signal-bot
for p in 0?-*.patch; do
  echo "=== applying $p ==="
  patch -p1 < "$p"
done
```

Mỗi patch in `patching file ...`. Không có dòng `FAILED` hay `rejected`.

### Bước 4 — Verify syntax

```bash
python3 -c "
import ast
for p in ['app/risk/levels.py','app/risk/filters.py','app/scanner/scanner.py','app/telegram_bot/bot.py','app/dashboard/server.py']:
    ast.parse(open(p).read())
    print(f'{p}: OK')
"
```

Phải in 5 dòng OK.

### Bước 5 — Thêm env (tùy chọn — defaults đã sane)

```bash
cat >> .env <<'EOF'

# === Phase 0 ===
MAX_SL_PCT=5.0
MIN_SL_PCT=0.6
MAX_SL_PCT_FILTER=5.0
EOF
```

### Bước 6 — Restart

```bash
docker compose restart bot
docker compose logs -f bot
```

Đợi `=== all services running ===`.

---

## Verify đã work đúng

### Test dashboard bug fix

```bash
# Login dashboard rồi gọi /api/dashboard — trước đây trả NameError 500,
# giờ phải trả JSON với keys: winrate, signals7d, avgpnl, leaderboard, recent
curl -b cookies.txt http://localhost:8000/api/dashboard
```

### Test SL cap

Vào psql xem signal mới nhất có SL trong tầm 5% không:

```bash
docker compose exec postgres psql -U signals -d signals -c "
SELECT symbol, side,
       entry_low, entry_high, stop_loss,
       ROUND(100 * ABS(stop_loss - (entry_low+entry_high)/2) / ((entry_low+entry_high)/2), 2) AS sl_pct
FROM signals
ORDER BY created_at DESC
LIMIT 10;"
```

`sl_pct` phải ≤ 5.0% cho mọi signal mới.

### Test TP/SL event log clean

Tail log 30 phút:

```bash
docker compose logs bot --since 30m | grep -iE "broadcast|message to be replied"
```

- **Không còn**: `ERROR ... Message to be replied not found`
- **Có thể thấy**: `INFO ... original message gone — sending standalone` (benign)

---

## Rollback (nếu cần)

```bash
cd ~/futures-signal-bot
cp .phase0-backup/levels.py    app/risk/levels.py
cp .phase0-backup/filters.py   app/risk/filters.py
cp .phase0-backup/scanner.py   app/scanner/scanner.py
cp .phase0-backup/bot.py       app/telegram_bot/bot.py
cp .phase0-backup/server.py    app/dashboard/server.py
docker compose restart bot
```

Hoặc dùng `patch -R` (reverse) trên patches:

```bash
cd ~/futures-signal-bot
for p in $(ls -r 0?-*.patch); do
  patch -p1 -R < "$p"
done
docker compose restart bot
```

DB schema không thay đổi → không cần migration.

---

## Tune knobs (qua `.env`, không cần sửa code)

| Knob | Default | Effect |
|---|---|---|
| `MAX_SL_PCT` | 5.0 | Cap cứng SL theo % entry. Giảm xuống 4.0 nếu vẫn thấy SL xa |
| `MIN_SL_PCT` | 0.6 | Min SL distance (tránh spread/noise). Tăng lên 0.8-1.0 nếu thấy SL hit liên tục bởi noise |
| `MAX_SL_PCT_FILTER` | 5.0 | Filter reject setup có sl_pct cao hơn ngưỡng. Bằng MAX_SL_PCT |

Sau khi tune chỉ cần `docker compose restart bot`. Không cần rebuild.

---

## Patch summary

```
01-risk-levels-sl-cap.patch        96 lines diff  |  +43 lines code (additive)
02-risk-filters-sl-sanity.patch    55 lines diff  |  +24 lines code (additive)
03-scanner-reorder.patch           24 lines diff  |  +6 / -4 (swap order)
04-bot-event-fallback.patch        59 lines diff  |  +27 / -7 (refactor try-except)
05-dashboard-import-os.patch        9 lines diff  |  +1 line (bug fix)
─────────────────────────────────────────────────────────────────────────
TOTAL                              243 lines diff
```

Không có file nào bị ghi đè. Không có feature nào bị xóa.
