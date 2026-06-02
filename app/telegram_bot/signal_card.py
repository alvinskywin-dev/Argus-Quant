from PIL import Image, ImageDraw, ImageFont, ImageFilter
from pathlib import Path
from app.utils.helpers import fmt_price

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

def make_signal_card(sig: dict) -> str:
    symbol = sig["symbol"]
    side = sig["side"]
    is_long = side == "LONG"

    main = (46, 255, 95) if is_long else (255, 72, 72)
    gold = (255, 199, 79)
    white = (245, 245, 245)
    gray = (170, 178, 190)

    img = Image.new("RGB", (1100, 620), (5, 8, 12))

    grid = ImageDraw.Draw(img)
    for x in range(0, 1100, 60):
        grid.line((x, 0, x, 620), fill=(16, 20, 28), width=1)
    for y in range(0, 620, 60):
        grid.line((0, y, 1100, y), fill=(16, 20, 28), width=1)

    glow = Image.new("RGBA", img.size, (0,0,0,0))
    gd = ImageDraw.Draw(glow)
    gd.rounded_rectangle((35,35,1065,585), radius=36, outline=main + (255,), width=7)
    glow = glow.filter(ImageFilter.GaussianBlur(12))
    img = Image.alpha_composite(img.convert("RGBA"), glow)

    d = ImageDraw.Draw(img)
    d.rounded_rectangle((40,40,1060,580), radius=36, outline=main, width=3, fill=(8,12,18))

    d.text((70,60), "⚡ ARGUS QUANT", font=font(36, True), fill=gold)

    badge = side
    d.rounded_rectangle((70,120,360,210), radius=20, fill=(12,18,25), outline=main, width=2)
    d.text((95,138), badge, font=font(56, True), fill=main)

    d.text((70,250), f"{symbol}", font=font(62, True), fill=white)
    d.text((70,320), f"CONFIDENCE  {sig['confidence']}%", font=font(28, True), fill=gray)

    d.text((70,385), "ENTRY", font=font(22, True), fill=gray)
    d.text((70,420), f"{fmt_price(sig['entry_low'])} → {fmt_price(sig['entry_high'])}", font=font(38, True), fill=white)

    d.text((70,500), f"RR  1 : {sig['risk_reward']}", font=font(32, True), fill=gold)

    # Right panel
    d.text((560,125), "TARGETS", font=font(28, True), fill=main)
    d.text((560,175), f"TP1  {fmt_price(sig['tp1'])}", font=font(34, True), fill=white)
    d.text((560,225), f"TP2  {fmt_price(sig['tp2'])}", font=font(34, True), fill=white)
    d.text((560,275), f"TP3  {fmt_price(sig['tp3'])}", font=font(34, True), fill=white)

    d.text((560,360), "STOP LOSS", font=font(28, True), fill=(255, 90, 90))
    d.text((560,410), fmt_price(sig["stop_loss"]), font=font(44, True), fill=(255, 90, 90))

    # Mini chart
    bars_x = 820
    bars = [30, 48, 72, 100, 130] if is_long else [130, 100, 72, 48, 30]
    for i, h in enumerate(bars):
        d.rounded_rectangle((bars_x+i*42, 520-h, bars_x+28+i*42, 520), radius=8, fill=main)

    if is_long:
        d.line((790,520,1020,320), fill=main, width=6)
    else:
        d.line((790,320,1020,520), fill=main, width=6)

    d.text((735,545), "Powered by Argus Quant AI", font=font(20, True), fill=gold)

    out = OUT_DIR / f"signal_{symbol}_{side}.png"
    img.convert("RGB").save(out, "PNG")
    return str(out)
