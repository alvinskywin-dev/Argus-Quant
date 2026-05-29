from PIL import Image, ImageDraw, ImageFont, ImageFilter
from pathlib import Path

OUT_DIR = Path("/tmp/cards")
OUT_DIR.mkdir(parents=True, exist_ok=True)

def font(size, bold=False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for fp in candidates:
        if Path(fp).exists():
            return ImageFont.truetype(fp, size)
    return ImageFont.load_default()

def make_event_card(payload: dict) -> str:
    event = payload.get("event", "TP1")
    symbol = payload.get("symbol", "BTCUSDT")
    side = payload.get("side", "LONG")
    pnl = float(payload.get("pnl_pct", 0))
    entry = payload.get("entry", "108250")
    exit_price = payload.get("exit", "109000")
    hold = payload.get("hold", "1H 24M")

    is_win = event.startswith("TP")
    main = (46, 255, 95) if is_win else (255, 72, 72)
    gold = (255, 199, 79)
    white = (245, 245, 245)
    gray = (170, 178, 190)
    bg = (5, 8, 12)

    img = Image.new("RGB", (1100, 620), bg)

    # background grid
    grid = ImageDraw.Draw(img)
    for x in range(0, 1100, 60):
        grid.line((x, 0, x, 620), fill=(16, 20, 28), width=1)
    for y in range(0, 620, 60):
        grid.line((0, y, 1100, y), fill=(16, 20, 28), width=1)

    # glow border
    glow = Image.new("RGBA", img.size, (0,0,0,0))
    gd = ImageDraw.Draw(glow)
    gd.rounded_rectangle((35,35,1065,585), radius=36, outline=main + (255,), width=7)
    glow = glow.filter(ImageFilter.GaussianBlur(12))
    img = Image.alpha_composite(img.convert("RGBA"), glow)

    d = ImageDraw.Draw(img)

    d.rounded_rectangle((40,40,1060,580), radius=36, outline=main, width=3, fill=(8,12,18))

    # header
    d.text((70,60), "⚡ ALPHA RADAR", font=font(36, True), fill=gold)

    # badge
    badge = "TP1 HIT" if is_win else "STOP LOSS"
    d.rounded_rectangle((70,120,450,210), radius=20, fill=(12,18,25), outline=main, width=2)
    d.text((95,138), badge, font=font(58, True), fill=main)

    # pair
    d.text((70,250), f"{symbol}  •  {side}", font=font(44, True), fill=white)

    # info blocks
    d.text((70,330), "ENTRY", font=font(22, True), fill=gray)
    d.text((70,365), str(entry), font=font(38, True), fill=white)

    d.text((355,330), "EXIT", font=font(22, True), fill=gray)
    d.text((355,365), str(exit_price), font=font(38, True), fill=white)

    d.text((640,330), "HOLD", font=font(22, True), fill=gray)
    d.text((640,365), str(hold), font=font(38, True), fill=white)

    # giant profit
    label = "PROFIT" if is_win else "LOSS"
    d.text((70,445), label, font=font(26, True), fill=gray)
    d.text((70,485), f"{pnl:+.2f}%", font=font(86, True), fill=main)

    # mini chart
    bars_x = 820
    bars = [25, 45, 65, 95, 120]
    for i, h in enumerate(bars):
        d.rounded_rectangle(
            (bars_x + i*42, 520-h, bars_x+28+i*42, 520),
            radius=8,
            fill=main
        )

    d.line((790,520,1020,320), fill=main, width=6)

    # footer
    d.text((760,545), "Powered by Alpha Radar AI", font=font(20, True), fill=gold)

    out = OUT_DIR / f"event_{symbol}_{event}.png"
    img.convert("RGB").save(out, "PNG")
    return str(out)
