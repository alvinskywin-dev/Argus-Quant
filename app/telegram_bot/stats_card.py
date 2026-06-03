from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

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

def make_stats_card(data: dict) -> str:
    signals = data.get("signals", 12)
    wins = data.get("wins", 8)
    losses = data.get("losses", 4)
    winrate = data.get("winrate", 66.7)
    pnl = data.get("pnl", 18.4)

    main = (46, 255, 95)
    gold = (255, 199, 79)
    white = (245, 245, 245)
    gray = (170, 178, 190)

    img = Image.new("RGB", (1100, 620), (5, 8, 12))
    d0 = ImageDraw.Draw(img)

    for x in range(0, 1100, 60):
        d0.line((x, 0, x, 620), fill=(16, 20, 28), width=1)
    for y in range(0, 620, 60):
        d0.line((0, y, 1100, y), fill=(16, 20, 28), width=1)

    glow = Image.new("RGBA", img.size, (0,0,0,0))
    gd = ImageDraw.Draw(glow)
    gd.rounded_rectangle((35,35,1065,585), radius=36, outline=main + (255,), width=7)
    glow = glow.filter(ImageFilter.GaussianBlur(12))
    img = Image.alpha_composite(img.convert("RGBA"), glow)

    d = ImageDraw.Draw(img)
    d.rounded_rectangle((40,40,1060,580), radius=36, outline=main, width=3, fill=(8,12,18))

    d.text((70,65), "⚡ ARGUS QUANT", font=font(38, True), fill=gold)
    d.text((70,130), "DAILY PERFORMANCE", font=font(56, True), fill=white)

    d.text((70,235), "SIGNALS", font=font(24, True), fill=gray)
    d.text((70,275), str(signals), font=font(62, True), fill=white)

    d.text((330,235), "WINS", font=font(24, True), fill=gray)
    d.text((330,275), str(wins), font=font(62, True), fill=main)

    d.text((560,235), "LOSSES", font=font(24, True), fill=gray)
    d.text((560,275), str(losses), font=font(62, True), fill=(255, 72, 72))

    d.text((70,410), "WINRATE", font=font(26, True), fill=gray)
    d.text((70,450), f"{winrate:.1f}%", font=font(80, True), fill=main)

    d.text((560,410), "TOTAL PNL", font=font(26, True), fill=gray)
    d.text((560,450), f"{pnl:+.2f}%", font=font(80, True), fill=main if pnl >= 0 else (255,72,72))

    d.text((720,545), "Powered by Argus Quant AI", font=font(22, True), fill=gold)

    out = OUT_DIR / "daily_stats.png"
    img.convert("RGB").save(out, "PNG")
    return str(out)
