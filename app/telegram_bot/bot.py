"""
Telegram bot.

- Commands: /start /help /scan /toplong /topshort /market /gainers /losers
  /signalhistory /stats /leaderboard /settings
  /pause /resume /status /health
- Auto publishes new signals to the configured channel/group.
- Edits the signal message in-place when TP/SL events occur.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Optional

import psutil
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    constants,
)
from telegram.error import NetworkError, RetryAfter, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.config import settings
from app.database import repo
from app.market_data import universe
from app.risk import rate_limiter
from app.telegram_bot.formatter import (
    format_market_overview,
    format_signal,
)
from app.telegram_bot.signal_card import make_signal_card
from app.utils.logger import logger

# ---------- constants ----------

LEVERAGE_MAP: dict[str, str] = {
    "5m":  "10x",
    "15m": "7x",
    "1h":  "5x",
    "4h":  "3x",
    "1d":  "2x",
}

PAUSE_KEY = "broadcasts_paused"

# ---------- helpers ----------

def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except Exception:
        return default


def _env_str(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _is_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids


def _signal_tier(sig: dict) -> str:
    """
    Classify signal into a broadcast tier.
    All signals that pass the scanner (>= min_confidence) get at least PUBLIC.
    """
    confidence = float(sig.get("confidence", 0) or 0)

    # Tier thresholds — match MTF pipeline scoring
    # 95-100 = ELITE | 85-94 = VIP | 75-84 = PUBLIC | <75 = reject
    elite_min  = _env_float("ELITE_MIN_CONFIDENCE",  95.0)
    vip_min    = _env_float("VIP_MIN_CONFIDENCE",    85.0)
    public_min = _env_float("PUBLIC_MIN_CONFIDENCE", settings.min_confidence)  # 75.0

    # Use tier embedded in signal if scanner already classified it
    embedded = (sig.get("_tier") or "").upper()
    if embedded in ("ELITE", "VIP", "PUBLIC"):
        return embedded

    if confidence >= elite_min:
        return "ELITE"
    if confidence >= vip_min:
        return "VIP"
    if confidence >= public_min:
        return "PUBLIC"
    return "NONE"


def _route_signal_chats(sig: dict) -> list[str]:
    """
    Return list of chat IDs for this signal's tier.
    Falls back to TELEGRAM_SIGNAL_CHAT_ID so signals always land somewhere
    even when tier-specific chats are not configured.
    """
    tier = _signal_tier(sig)

    elite_chat  = _env_str("ELITE_VIP_CHAT_ID")
    vip_chat    = _env_str("VIP_CHAT_ID")
    public_chat = _env_str("PUBLIC_CHAT_ID")
    fallback    = (settings.telegram_signal_chat_id or "").strip()

    def _pick(*candidates: str) -> str:
        for c in candidates:
            if c:
                return c
        return ""

    if tier == "ELITE":
        chosen = _pick(elite_chat, vip_chat, fallback)
    elif tier == "VIP":
        chosen = _pick(vip_chat, fallback)
    elif tier == "PUBLIC":
        chosen = _pick(public_chat, fallback)
    else:
        # NONE tier — signal passed scanner but is below broadcast tiers;
        # still deliver to the fallback channel if configured.
        chosen = fallback

    if not chosen:
        logger.warning(f"no broadcast target for {sig.get('symbol')} tier={tier} — set TELEGRAM_SIGNAL_CHAT_ID")
        return []

    # Support comma-separated multi-channel routing
    return [c.strip() for c in chosen.split(",") if c.strip()]


async def _tg_send_with_retry(coro_factory, *, max_attempts: int = 4) -> any:
    """
    Retry a Telegram API call with exponential backoff.
    Handles rate-limit (RetryAfter), transient network errors, and timeouts.
    """
    delay = 1.0
    for attempt in range(1, max_attempts + 1):
        try:
            return await coro_factory()
        except RetryAfter as exc:
            wait = max(exc.retry_after, 1)
            logger.warning(f"telegram rate-limit, sleeping {wait}s (attempt {attempt})")
            await asyncio.sleep(wait)
        except (TimedOut, NetworkError) as exc:
            if attempt == max_attempts:
                raise
            logger.warning(f"telegram transient error ({exc}), retry in {delay:.1f}s (attempt {attempt})")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
        except Exception:
            raise
    return None


def _main_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🔍 Scan Now", callback_data="scan"),
         InlineKeyboardButton("📊 Market", callback_data="market")],
        [InlineKeyboardButton("🚀 Top Longs", callback_data="toplong"),
         InlineKeyboardButton("🔻 Top Shorts", callback_data="topshort")],
        [InlineKeyboardButton("📈 Stats", callback_data="stats")],
        [InlineKeyboardButton("⏸ Pause", callback_data="pause"),
         InlineKeyboardButton("▶️ Resume", callback_data="resume")],
    ]
    return InlineKeyboardMarkup(rows)


class TelegramBot:
    def __init__(self) -> None:
        self.app: Optional[Application] = None
        self._started_at = time.time()
        self._top_long: list[dict] = []
        self._top_short: list[dict] = []

    # ---------------- lifecycle ----------------

    async def start(self) -> None:
        if not settings.telegram_bot_token:
            logger.warning("TELEGRAM_BOT_TOKEN not set — telegram bot disabled")
            return
        self.app = (
            Application.builder()
            .token(settings.telegram_bot_token)
            .concurrent_updates(True)
            .build()
        )
        self._register_handlers()
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        logger.info("telegram bot started")

    async def shutdown(self) -> None:
        if self.app is None:
            return
        try:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"telegram shutdown: {exc}")
        logger.info("telegram bot stopped")

    # ---------------- handlers ----------------

    def _register_handlers(self) -> None:
        assert self.app is not None
        h = self.app.add_handler

        h(CommandHandler("start", self.cmd_start))
        h(CommandHandler("help", self.cmd_help))
        h(CommandHandler("scan", self.cmd_scan))
        h(CommandHandler("toplong", self.cmd_toplong))
        h(CommandHandler("topshort", self.cmd_topshort))
        h(CommandHandler("market", self.cmd_market))
        h(CommandHandler("gainers", self.cmd_gainers))
        h(CommandHandler("losers", self.cmd_losers))
        h(CommandHandler("signalhistory", self.cmd_signal_history))
        h(CommandHandler("stats", self.cmd_stats))
        h(CommandHandler("leaderboard", self.cmd_leaderboard))
        h(CommandHandler("settings", self.cmd_settings))
        h(CommandHandler("pause", self.cmd_pause))
        h(CommandHandler("resume", self.cmd_resume))
        h(CommandHandler("status", self.cmd_status))
        h(CommandHandler("health", self.cmd_status))
        h(CommandHandler("setconfidence", self.cmd_setconfidence))
        h(CommandHandler("setcooldown", self.cmd_setcooldown))
        h(CommandHandler("setmaxsignals", self.cmd_setmaxsignals))
        h(CommandHandler("getconfig", self.cmd_getconfig))
        h(CallbackQueryHandler(self.on_callback))
        h(MessageHandler(filters.ALL, self.fallback))

    # ---------------- command implementations ----------------

    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        u = update.effective_user
        await repo.upsert_user(u.id, u.username, is_admin=_is_admin(u.id))
        await update.effective_message.reply_text(
            "<b>AI Futures Signal System</b>\n\n"
            "Live scanner over all USDT-M futures pairs, AI-scored setups,\n"
            "premium signals with TP1/TP2/TP3 + stop loss.\n\n"
            "Tap a button below or use /help for commands.",
            parse_mode=constants.ParseMode.HTML,
            reply_markup=_main_keyboard(),
        )

    async def cmd_help(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        text = (
            "<b>Available commands</b>\n\n"
            "/scan — request a fresh market scan\n"
            "/market — overview + winrate\n"
            "/toplong — top long setups\n"
            "/topshort — top short setups\n"
            "/gainers — 24h gainers\n"
            "/losers — 24h losers\n"
            "/signalhistory — last 10 signals\n"
            "/stats — performance (7d)\n"
            "/leaderboard — best pairs\n"
            "/settings — current config\n"
            "/pause /resume — broadcasts (admin)\n"
            "/status — uptime + health"
        )
        await update.effective_message.reply_text(text, parse_mode=constants.ParseMode.HTML)

    async def cmd_scan(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(
            f"🔄 Scanning <b>{len(universe.symbols)}</b> symbols across "
            f"<code>{','.join(settings.timeframes)}</code>...\n"
            f"Signals will be auto-broadcast as they qualify.",
            parse_mode=constants.ParseMode.HTML,
        )

    async def cmd_toplong(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        items = self._top_long[:10]
        if not items:
            await update.effective_message.reply_text("No qualifying long setups right now.")
            return
        lines = ["🚀 <b>Top Long Setups</b>\n"]
        for s in items:
            lines.append(f"• <code>{s['symbol']}</code>  conf <code>{s['confidence']}%</code>  RR <code>1:{s['risk_reward']}</code>")
        await update.effective_message.reply_text("\n".join(lines), parse_mode=constants.ParseMode.HTML)

    async def cmd_topshort(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        items = self._top_short[:10]
        if not items:
            await update.effective_message.reply_text("No qualifying short setups right now.")
            return
        lines = ["🔻 <b>Top Short Setups</b>\n"]
        for s in items:
            lines.append(f"• <code>{s['symbol']}</code>  conf <code>{s['confidence']}%</code>  RR <code>1:{s['risk_reward']}</code>")
        await update.effective_message.reply_text("\n".join(lines), parse_mode=constants.ParseMode.HTML)

    async def cmd_market(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        wr = await repo.winrate_summary(days=7)
        text = format_market_overview({
            "gainers": universe.gainers(5),
            "losers": universe.losers(5),
            "winrate": wr,
        })
        await update.effective_message.reply_text(text, parse_mode=constants.ParseMode.HTML)

    async def cmd_gainers(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        items = universe.gainers(10)
        lines = ["📈 <b>Top Gainers (24h)</b>\n"]
        for g in items:
            lines.append(f"• <code>{g['symbol']}</code>  +{g['change_pct']:.2f}%  vol ${g['quote_volume']/1e6:.0f}M")
        await update.effective_message.reply_text("\n".join(lines), parse_mode=constants.ParseMode.HTML)

    async def cmd_losers(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        items = universe.losers(10)
        lines = ["📉 <b>Top Losers (24h)</b>\n"]
        for g in items:
            lines.append(f"• <code>{g['symbol']}</code>  {g['change_pct']:.2f}%  vol ${g['quote_volume']/1e6:.0f}M")
        await update.effective_message.reply_text("\n".join(lines), parse_mode=constants.ParseMode.HTML)

    async def cmd_signal_history(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        sigs = await repo.get_recent_signals(10)
        if not sigs:
            await update.effective_message.reply_text("No signals yet.")
            return
        lines = ["📜 <b>Recent Signals</b>\n"]
        for s in sigs:
            emo = "🚀" if s.side == "LONG" else "🔻"
            lines.append(
                f"{emo} <code>{s.symbol}</code> {s.side} {s.confidence}% → <b>{s.status}</b> "
                f"({s.pnl_pct:+.2f}%)"
            )
        await update.effective_message.reply_text("\n".join(lines), parse_mode=constants.ParseMode.HTML)

    async def cmd_stats(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        wr = await repo.winrate_summary(days=7)
        text = (
            "📈 <b>Performance (7d)</b>\n\n"
            f"• Total signals: <code>{wr['total']}</code>\n"
            f"• Wins: <code>{wr['wins']}</code>\n"
            f"• Losses: <code>{wr['losses']}</code>\n"
            f"• Win rate: <code>{wr['winrate']:.1f}%</code>\n"
            f"• Avg PnL: <code>{wr['avg_pnl']:+.2f}%</code>"
        )
        await update.effective_message.reply_text(text, parse_mode=constants.ParseMode.HTML)

    async def cmd_leaderboard(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        rows = await repo.leaderboard(10)
        if not rows:
            await update.effective_message.reply_text("Not enough data yet.")
            return
        lines = ["🏆 <b>Top Performing Pairs</b>\n"]
        for i, r in enumerate(rows, 1):
            lines.append(
                f"{i}. <code>{r['symbol']}</code>  avg <code>{r['avg_pnl']:+.2f}%</code>  ({r['signals']} signals)"
            )
        await update.effective_message.reply_text("\n".join(lines), parse_mode=constants.ParseMode.HTML)

    async def cmd_settings(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        text = (
            "<b>Active settings</b>\n\n"
            f"• Timeframes: <code>{','.join(settings.timeframes)}</code>\n"
            f"• Scan interval: <code>{settings.scan_interval_sec}s</code>\n"
            f"• Min confidence: <code>{settings.min_confidence}%</code>\n"
            f"• Min RR: <code>1:{settings.min_rr}</code>\n"
            f"• Cooldown: <code>{settings.signal_cooldown_sec}s</code>\n"
            f"• Max signals/hr: <code>{settings.max_signals_per_hour}</code>\n"
            f"• Min volume: <code>${settings.min_quote_volume_usdt/1e6:.0f}M</code>\n"
            f"• Universe size: <code>{len(universe.symbols)}</code>"
        )
        await update.effective_message.reply_text(text, parse_mode=constants.ParseMode.HTML)

    async def cmd_pause(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_admin(update.effective_user.id):
            await update.effective_message.reply_text("Admin only.")
            return
        await repo.set_setting(PAUSE_KEY, "1")
        await update.effective_message.reply_text("⏸ Broadcasts paused.")

    async def cmd_resume(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_admin(update.effective_user.id):
            await update.effective_message.reply_text("Admin only.")
            return
        await repo.set_setting(PAUSE_KEY, "0")
        await update.effective_message.reply_text("▶️ Broadcasts resumed.")

    async def cmd_status(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        proc = psutil.Process()
        mem = proc.memory_info().rss / 1024 / 1024
        cpu = psutil.cpu_percent(interval=0.2)
        up = int(time.time() - self._started_at)
        h, rem = divmod(up, 3600)
        m, s = divmod(rem, 60)
        used = await rate_limiter.used()
        paused = (await repo.get_setting(PAUSE_KEY, "0")) == "1"
        await update.effective_message.reply_text(
            "<b>System status</b>\n\n"
            f"• Uptime: <code>{h}h {m}m {s}s</code>\n"
            f"• Memory: <code>{mem:.1f} MB</code>\n"
            f"• CPU: <code>{cpu:.1f}%</code>\n"
            f"• Universe: <code>{len(universe.symbols)}</code>\n"
            f"• Signals last hour: <code>{used}/{settings.max_signals_per_hour}</code>\n"
            f"• Broadcasts: {'⏸ paused' if paused else '▶️ active'}",
            parse_mode=constants.ParseMode.HTML,
        )

    async def cmd_getconfig(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        text = (
            "⚙️ <b>Runtime Config</b>\n\n"
            f"MIN_CONFIDENCE • <code>{settings.min_confidence}</code>\n"
            f"COOLDOWN_MIN • <code>{settings.symbol_cooldown_minutes}</code>\n"
            f"MAX_SIGNALS_HOUR • <code>{settings.max_signals_per_hour}</code>"
        )
        await update.effective_message.reply_text(text, parse_mode=constants.ParseMode.HTML)

    async def cmd_setconfidence(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_admin(update.effective_user.id):
            return
        try:
            val = float(ctx.args[0])
            settings.min_confidence = val
            await update.effective_message.reply_text(f"✅ MIN_CONFIDENCE set to {val}")
        except Exception:
            await update.effective_message.reply_text("Usage: /setconfidence 72")

    async def cmd_setcooldown(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_admin(update.effective_user.id):
            return
        try:
            val = int(ctx.args[0])
            settings.symbol_cooldown_minutes = val
            await update.effective_message.reply_text(f"✅ COOLDOWN set to {val} minutes")
        except Exception:
            await update.effective_message.reply_text("Usage: /setcooldown 180")

    async def cmd_setmaxsignals(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_admin(update.effective_user.id):
            return
        try:
            val = int(ctx.args[0])
            rate_limiter.max = val
            settings.max_signals_per_hour = val
            await update.effective_message.reply_text(f"✅ MAX_SIGNALS_PER_HOUR set to {val}")
        except Exception:
            await update.effective_message.reply_text("Usage: /setmaxsignals 12")

    async def on_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        await q.answer()
        data = q.data or ""
        if data == "scan":
            return await self.cmd_scan(update, ctx)
        if data == "market":
            return await self.cmd_market(update, ctx)
        if data == "toplong":
            return await self.cmd_toplong(update, ctx)
        if data == "topshort":
            return await self.cmd_topshort(update, ctx)
        if data == "stats":
            return await self.cmd_stats(update, ctx)
        if data == "pause":
            return await self.cmd_pause(update, ctx)
        if data == "resume":
            return await self.cmd_resume(update, ctx)

    async def fallback(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        return

    # ---------------- broadcast API ----------------

    async def alert_admin(self, title: str, message: str) -> None:
        if self.app is None:
            return
        text = (
            "🚨 <b>ARGUS QUANT ALERT</b>\n\n"
            f"<b>{title}</b>\n"
            f"<code>{message[:1200]}</code>"
        )
        for admin_id in settings.admin_ids:
            try:
                await self.app.bot.send_message(
                    chat_id=admin_id,
                    text=text,
                    parse_mode=constants.ParseMode.HTML,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"admin alert failed: {exc}")

    async def broadcast_signal(self, sig: dict) -> list[dict]:
        if self.app is None:
            logger.debug("broadcast_signal skipped: telegram app not started")
            return []

        # Check pause flag
        try:
            paused = (await repo.get_setting(PAUSE_KEY, "0")) == "1"
            if paused:
                logger.info(f"broadcast paused — skipping {sig.get('symbol')} {sig.get('side')}")
                return []
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"could not check pause state: {exc}")

        # ── Publisher-level duplicate guard (layer 3 of 3) ──────────────
        # Catches race conditions where two signals for the same symbol
        # were both persisted before either was broadcast.
        # Uses has_active_signal_excluding() so we don't block the signal
        # that was JUST created (its ID is passed via _signal_id).
        if settings.block_same_symbol_while_open:
            symbol     = sig.get("symbol", "")
            signal_id  = sig.get("_signal_id")
            if symbol and signal_id is not None:
                try:
                    from app.database.repo import has_active_signal_excluding
                    if await has_active_signal_excluding(symbol, int(signal_id)):
                        logger.warning(
                            f"SKIP_DUPLICATE_ACTIVE_SIGNAL symbol={symbol} "
                            f"side={sig.get('side')} "
                            f"reason=existing_open_signal (publisher guard)"
                        )
                        return []
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"publisher duplicate check failed: {exc}")

        chats = _route_signal_chats(sig)
        if not chats:
            logger.warning(
                f"no broadcast target for {sig.get('symbol')} {sig.get('side')} "
                f"conf={sig.get('confidence')} tier={_signal_tier(sig)} — "
                "configure TELEGRAM_SIGNAL_CHAT_ID or tier-specific chat env vars"
            )
            return []

        tier = _signal_tier(sig)
        tier_title = {
            "HIGH_PRIORITY": "🚨 <b>HIGH PRIORITY ELITE SIGNAL</b>",
            "ELITE":         "🔥 <b>ELITE SIGNAL</b>",
            "VIP":           "💎 <b>VIP SIGNAL</b>",
            "PUBLIC":        "⚡ <b>ARGUS QUANT</b>",
        }.get(tier, "⚡ <b>ARGUS QUANT</b>")

        caption = (
            f"{tier_title}\n\n"
            f"{'🟢' if sig['side']=='LONG' else '🔴'} "
            f"<code>{sig['symbol']}</code> "
            f"<b>{sig['side']}</b>\n\n"
            f"ENTRY • "
            f"<code>{float(sig['entry_low']):.5f} → {float(sig['entry_high']):.5f}</code>\n"
            f"TARGET • "
            f"<code>{float(sig['tp1']):.5f} • {float(sig['tp2']):.5f} • {float(sig['tp3']):.5f}</code>\n"
            f"STOP • <code>{float(sig['stop_loss']):.5f}</code>\n\n"
            f"⚡ RR • <code>1 : {sig['risk_reward']}</code>\n"
            f"📊 CONFIDENCE • <code>{sig['confidence']}%</code>\n"
            f"🚀 LEVERAGE • <code>{LEVERAGE_MAP.get(sig.get('timeframe', '1h'), '3x')}</code>"
        )

        # Try image card; fall back to text-only on any failure
        card_path: Optional[str] = None
        try:
            card_path = make_signal_card(sig)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"signal card generation failed for {sig.get('symbol')}: {exc} — using text fallback")

        sent_messages: list[dict] = []

        for chat_id in chats:
            try:
                if card_path:
                    async def _send_photo(cid=chat_id):
                        with open(card_path, "rb") as photo:
                            return await self.app.bot.send_photo(
                                chat_id=cid,
                                photo=photo,
                                caption=caption,
                                parse_mode=constants.ParseMode.HTML,
                            )
                    msg = await _tg_send_with_retry(_send_photo)
                else:
                    # text-only fallback (also used when image fails mid-send)
                    text_body = format_signal(sig)
                    async def _send_text(cid=chat_id, tb=text_body):
                        return await self.app.bot.send_message(
                            chat_id=cid,
                            text=tb,
                            parse_mode=constants.ParseMode.HTML,
                        )
                    msg = await _tg_send_with_retry(_send_text)

                if msg:
                    sent_messages.append({
                        "chat_id": str(chat_id),
                        "message_id": int(msg.message_id),
                    })
                    logger.info(
                        f"📤 signal broadcast → {chat_id} "
                        f"{sig.get('symbol')} {sig.get('side')} "
                        f"msg_id={msg.message_id}"
                    )

            except Exception as exc:  # noqa: BLE001
                logger.exception(f"signal broadcast failed for chat {chat_id}: {exc}")
                # Attempt text-only recovery if image send failed
                if card_path:
                    try:
                        text_body = format_signal(sig)
                        async def _fallback(cid=chat_id, tb=text_body):
                            return await self.app.bot.send_message(
                                chat_id=cid,
                                text=tb,
                                parse_mode=constants.ParseMode.HTML,
                            )
                        msg = await _tg_send_with_retry(_fallback, max_attempts=2)
                        if msg:
                            sent_messages.append({
                                "chat_id": str(chat_id),
                                "message_id": int(msg.message_id),
                            })
                            logger.info(f"📤 text fallback sent → {chat_id}")
                    except Exception as exc2:  # noqa: BLE001
                        logger.exception(f"text fallback also failed for {chat_id}: {exc2}")

        return sent_messages

    async def broadcast_event(self, payload: dict) -> None:
        if self.app is None or not settings.telegram_signal_chat_id:
            return

        event = payload.get("event", "UPDATE")
        symbol = payload.get("symbol", "")
        side = payload.get("side", "")
        pnl = float(payload.get("pnl_pct", 0) or 0)
        signal_id = payload.get("signal_id")

        is_sl = event == "SL"
        title = (
            f"🛑 <b>STOP LOSS • {symbol} {side}</b>"
            if is_sl
            else f"🎯 <b>{event} HIT • {symbol} {side}</b>"
        )

        label = "Loss" if is_sl else "Profit"
        text = (
            f"{title}\n\n"
            f"{'🔻' if is_sl else '🔥'} <code>{pnl:+.2f}%</code> {label}\n"
            f"⚡ ARGUS QUANT"
        )

        targets = []
        if signal_id:
            try:
                rows = await repo.get_signal_messages(int(signal_id))
                targets = [
                    {"chat_id": str(r.chat_id), "reply_to": int(r.telegram_message_id)}
                    for r in rows
                ]
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"load signal messages failed: {exc}")

        if not targets:
            targets = [
                {"chat_id": c.strip(), "reply_to": payload.get("telegram_message_id")}
                for c in str(settings.telegram_signal_chat_id).split(",")
                if c.strip()
            ]

        for item in targets:
            chat_id = item["chat_id"]
            reply_to = item.get("reply_to")
            try:
                async def _send(cid=chat_id, rt=reply_to):
                    return await self.app.bot.send_message(
                        chat_id=cid,
                        text=text,
                        parse_mode=constants.ParseMode.HTML,
                        reply_to_message_id=rt,
                    )
                await _tg_send_with_retry(_send)
            except Exception as reply_exc:  # noqa: BLE001
                logger.warning(f"reply event failed for {chat_id}: {reply_exc} — sending standalone")
                try:
                    async def _standalone(cid=chat_id):
                        return await self.app.bot.send_message(
                            chat_id=cid,
                            text=text,
                            parse_mode=constants.ParseMode.HTML,
                        )
                    await _tg_send_with_retry(_standalone, max_attempts=2)
                except Exception as exc:  # noqa: BLE001
                    logger.exception(f"event broadcast failed for {chat_id}: {exc}")
