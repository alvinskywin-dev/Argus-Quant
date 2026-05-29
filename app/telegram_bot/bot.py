
"""
Telegram bot.

- Commands: /start /help /scan /toplong /topshort /market /gainers /losers
  /watch /unwatch /watchlist /signalhistory /stats /leaderboard /settings
  /pause /resume /status /health
- Auto publishes new signals to the configured channel/group.
- Edits the signal message in-place when TP/SL events occur.
"""
from __future__ import annotations

import asyncio
import time
import os
from typing import Optional

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, Update,
    constants,
)
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

from app.config import settings
from app.database import repo
from app.market_data import universe
from app.risk import rate_limiter
from app.telegram_bot.formatter import format_event, format_market_overview, format_signal
from app.telegram_bot.event_card import make_event_card
from app.telegram_bot.signal_card import make_signal_card
from app.utils.logger import logger



def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except Exception:
        return default

def _env_str(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()

def _signal_tier(sig: dict) -> str:
    confidence = float(sig.get("confidence", 0) or 0)
    rr = float(sig.get("risk_reward", 0) or 0)

    public_min = _env_float("PUBLIC_MIN_CONFIDENCE", 88)
    public_max = _env_float("PUBLIC_MAX_CONFIDENCE", 91.99)
    vip_min = _env_float("VIP_MIN_CONFIDENCE", 92)
    elite_min = _env_float("ELITE_MIN_CONFIDENCE", 95)
    elite_rr = _env_float("ELITE_MIN_RR", 3.0)
    high_conf = _env_float("HIGH_PRIORITY_CONFIDENCE", 97)
    high_rr = _env_float("HIGH_PRIORITY_RR", 4.0)

    if confidence >= high_conf and rr >= high_rr:
        return "HIGH_PRIORITY"
    if confidence >= elite_min and rr >= elite_rr:
        return "ELITE"
    if confidence >= vip_min:
        return "VIP"
    if public_min <= confidence <= public_max:
        return "PUBLIC"
    return "NONE"


def _route_signal_chats(sig: dict) -> list[str]:
    tier = _signal_tier(sig)

    public_chat = _env_str("PUBLIC_CHAT_ID")
    vip_chat = _env_str("VIP_CHAT_ID")
    elite_chat = _env_str("ELITE_VIP_CHAT_ID")

    if tier == "HIGH_PRIORITY" and elite_chat:
        return [elite_chat]

    if tier == "ELITE" and elite_chat:
        return [elite_chat]

    if tier == "VIP" and vip_chat:
        return [vip_chat]

    if tier == "PUBLIC" and public_chat:
        return [public_chat]

    return []


PAUSE_KEY = "broadcasts_paused"


def _is_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids


def _main_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🔍 Scan Now", callback_data="scan"),
         InlineKeyboardButton("📊 Market", callback_data="market")],
        [InlineKeyboardButton("🚀 Top Longs", callback_data="toplong"),
         InlineKeyboardButton("🔻 Top Shorts", callback_data="topshort")],
        [InlineKeyboardButton("⭐ Watchlist", callback_data="watchlist"),
         InlineKeyboardButton("📈 Stats", callback_data="stats")],
        [InlineKeyboardButton("⏸ Pause", callback_data="pause"),
         InlineKeyboardButton("▶️ Resume", callback_data="resume")],
    ]
    return InlineKeyboardMarkup(rows)


class TelegramBot:
    def __init__(self) -> None:
        self.app: Optional[Application] = None
        self._started_at = time.time()
        # Latest top setups, refreshed by signal flow + scanner queries
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
        h(CommandHandler("watch", self.cmd_watch))
        h(CommandHandler("unwatch", self.cmd_unwatch))
        h(CommandHandler("watchlist", self.cmd_watchlist))
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
        await update.message.reply_text(
            "*AI Futures Signal System*\n\n"
            "Live scanner over all USDT-M futures pairs, AI-scored setups,\n"
            "premium signals with TP1/TP2/TP3 + stop loss.\n\n"
            "Tap a button below or use /help for commands.",
            parse_mode=constants.ParseMode.HTML,
            reply_markup=_main_keyboard(),
        )

    async def cmd_help(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        text = (
            "*Available commands*\n\n"
            "/scan — request a fresh market scan\n"
            "/market — overview + winrate\n"
            "/toplong — top long setups\n"
            "/topshort — top short setups\n"
            "/gainers — 24h gainers\n"
            "/losers — 24h losers\n"
            "/watch SYMBOL — add to watchlist\n"
            "/unwatch SYMBOL — remove from watchlist\n"
            "/watchlist — your watchlist\n"
            "/signalhistory — last 10 signals\n"
            "/stats — performance (7d)\n"
            "/leaderboard — best pairs\n"
            "/settings — current config\n"
            "/pause /resume — broadcasts (admin)\n"
            "/status — uptime + health"
        )
        await update.message.reply_text(text, parse_mode=constants.ParseMode.HTML)

    async def cmd_scan(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            f"🔄 Scanning *{len(universe.symbols)}* symbols across "
            f"`{','.join(settings.timeframes)}`...\n"
            f"Signals will be auto-broadcast as they qualify.",
            parse_mode=constants.ParseMode.HTML,
        )

    async def cmd_toplong(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        items = [s for s in self._top_long[:10]]
        if not items:
            await update.message.reply_text("No qualifying long setups right now.")
            return
        lines = ["🚀 *Top Long Setups*\n"]
        for s in items:
            lines.append(f"• `{s['symbol']}`  conf `{s['confidence']}%`  RR `1:{s['risk_reward']}`")
        await update.message.reply_text("\n".join(lines), parse_mode=constants.ParseMode.HTML)

    async def cmd_topshort(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        items = [s for s in self._top_short[:10]]
        if not items:
            await update.message.reply_text("No qualifying short setups right now.")
            return
        lines = ["🔻 *Top Short Setups*\n"]
        for s in items:
            lines.append(f"• `{s['symbol']}`  conf `{s['confidence']}%`  RR `1:{s['risk_reward']}`")
        await update.message.reply_text("\n".join(lines), parse_mode=constants.ParseMode.HTML)

    async def cmd_market(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        wr = await repo.winrate_summary(days=7)
        text = format_market_overview({
            "gainers": universe.gainers(5),
            "losers": universe.losers(5),
            "winrate": wr,
        })
        await update.message.reply_text(text, parse_mode=constants.ParseMode.HTML)

    async def cmd_gainers(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        items = universe.gainers(10)
        lines = ["📈 *Top Gainers (24h)*\n"]
        for g in items:
            lines.append(f"• `{g['symbol']}`  +{g['change_pct']:.2f}%  vol ${g['quote_volume']/1e6:.0f}M")
        await update.message.reply_text("\n".join(lines), parse_mode=constants.ParseMode.HTML)

    async def cmd_losers(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        items = universe.losers(10)
        lines = ["📉 *Top Losers (24h)*\n"]
        for g in items:
            lines.append(f"• `{g['symbol']}`  {g['change_pct']:.2f}%  vol ${g['quote_volume']/1e6:.0f}M")
        await update.message.reply_text("\n".join(lines), parse_mode=constants.ParseMode.HTML)

    async def cmd_watch(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not ctx.args:
            await update.message.reply_text("Usage: /watch BTCUSDT")
            return
        symbol = ctx.args[0].upper()
        added = await repo.add_watch(update.effective_user.id, symbol)
        await update.message.reply_text(
            f"⭐ Added `{symbol}`" if added else f"`{symbol}` already on your watchlist.",
            parse_mode=constants.ParseMode.HTML,
        )

    async def cmd_unwatch(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not ctx.args:
            await update.message.reply_text("Usage: /unwatch BTCUSDT")
            return
        symbol = ctx.args[0].upper()
        removed = await repo.remove_watch(update.effective_user.id, symbol)
        await update.message.reply_text(
            f"Removed `{symbol}`" if removed else f"`{symbol}` was not on your watchlist.",
            parse_mode=constants.ParseMode.HTML,
        )

    async def cmd_watchlist(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        items = await repo.list_watch(update.effective_user.id)
        if not items:
            await update.message.reply_text("Your watchlist is empty. /watch BTCUSDT to add.")
            return
        await update.message.reply_text(
            "⭐ *Your watchlist*\n\n" + "\n".join(f"• `{s}`" for s in items),
            parse_mode=constants.ParseMode.HTML,
        )

    async def cmd_signal_history(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        sigs = await repo.get_recent_signals(10)
        if not sigs:
            await update.message.reply_text("No signals yet.")
            return
        lines = ["📜 *Recent Signals*\n"]
        for s in sigs:
            emo = "🚀" if s.side == "LONG" else "🔻"
            lines.append(
                f"{emo} `{s.symbol}` {s.side} {s.confidence}% → *{s.status}* "
                f"({s.pnl_pct:+.2f}%)"
            )
        await update.message.reply_text("\n".join(lines), parse_mode=constants.ParseMode.HTML)

    async def cmd_stats(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        wr = await repo.winrate_summary(days=7)
        text = (
            f"📈 *Performance (7d)*\n\n"
            f"• Total signals: `{wr['total']}`\n"
            f"• Wins: `{wr['wins']}`\n"
            f"• Losses: `{wr['losses']}`\n"
            f"• Win rate: `{wr['winrate']:.1f}%`\n"
            f"• Avg PnL: `{wr['avg_pnl']:+.2f}%`"
        )
        await update.message.reply_text(text, parse_mode=constants.ParseMode.HTML)

    async def cmd_leaderboard(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        rows = await repo.leaderboard(10)
        if not rows:
            await update.message.reply_text("Not enough data yet.")
            return
        lines = ["🏆 *Top Performing Pairs*\n"]
        for i, r in enumerate(rows, 1):
            lines.append(
                f"{i}. `{r['symbol']}`  avg `{r['avg_pnl']:+.2f}%`  ({r['signals']} signals)"
            )
        await update.message.reply_text("\n".join(lines), parse_mode=constants.ParseMode.HTML)

    async def cmd_settings(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        text = (
            "*Active settings*\n\n"
            f"• Timeframes: `{','.join(settings.timeframes)}`\n"
            f"• Scan interval: `{settings.scan_interval_sec}s`\n"
            f"• Min confidence: `{settings.min_confidence}%`\n"
            f"• Min RR: `1:{settings.min_rr}`\n"
            f"• Cooldown: `{settings.signal_cooldown_sec}s`\n"
            f"• Max signals/hr: `{settings.max_signals_per_hour}`\n"
            f"• Min volume: `${settings.min_quote_volume_usdt/1e6:.0f}M`\n"
            f"• Universe size: `{len(universe.symbols)}`"
        )
        await update.message.reply_text(text, parse_mode=constants.ParseMode.HTML)

    async def cmd_pause(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_admin(update.effective_user.id):
            await update.message.reply_text("Admin only.")
            return
        await repo.set_setting(PAUSE_KEY, "1")
        await update.message.reply_text("⏸ Broadcasts paused.")

    async def cmd_resume(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_admin(update.effective_user.id):
            await update.message.reply_text("Admin only.")
            return
        await repo.set_setting(PAUSE_KEY, "0")
        await update.message.reply_text("▶️ Broadcasts resumed.")

    async def cmd_status(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        import psutil
        proc = psutil.Process()
        mem = proc.memory_info().rss / 1024 / 1024
        cpu = psutil.cpu_percent(interval=0.2)
        up = int(time.time() - self._started_at)
        h, rem = divmod(up, 3600)
        m, s = divmod(rem, 60)
        used = await rate_limiter.used()
        paused = (await repo.get_setting(PAUSE_KEY, "0")) == "1"
        await update.message.reply_text(
            f"*System status*\n\n"
            f"• Uptime: `{h}h {m}m {s}s`\n"
            f"• Memory: `{mem:.1f} MB`\n"
            f"• CPU: `{cpu:.1f}%`\n"
            f"• Universe: `{len(universe.symbols)}`\n"
            f"• Signals last hour: `{used}/{settings.max_signals_per_hour}`\n"
            f"• Broadcasts: {'⏸ paused' if paused else '▶️ active'}",
            parse_mode=constants.ParseMode.HTML,
        )

    async def on_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        await q.answer()
        data = q.data or ""
        # Bridge to slash commands
        fake_update = update
        if data == "scan":
            return await self.cmd_scan(fake_update, ctx)
        if data == "market":
            return await self.cmd_market(fake_update, ctx)
        if data == "toplong":
            return await self.cmd_toplong(fake_update, ctx)
        if data == "topshort":
            return await self.cmd_topshort(fake_update, ctx)
        if data == "watchlist":
            return await self.cmd_watchlist(fake_update, ctx)
        if data == "stats":
            return await self.cmd_stats(fake_update, ctx)
        if data == "pause":
            return await self.cmd_pause(fake_update, ctx)
        if data == "resume":
            return await self.cmd_resume(fake_update, ctx)

    async def fallback(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        # Silent — we don't want noise in groups
        return

    # ---------------- broadcast API ----------------

    async def cmd_getconfig(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        text = (
            "⚙️ <b>Runtime Config</b>\n\n"
            f"MIN_CONFIDENCE • <code>{settings.min_confidence}</code>\n"
            f"COOLDOWN_MIN • <code>{settings.symbol_cooldown_minutes}</code>\n"
            f"MAX_SIGNALS_HOUR • <code>{settings.max_signals_per_hour}</code>"
        )
        await update.message.reply_text(text, parse_mode=constants.ParseMode.HTML)

    async def cmd_setconfidence(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_admin(update.effective_user.id):
            return

        try:
            val = int(ctx.args[0])
            settings.min_confidence = val
            await update.message.reply_text(f"✅ MIN_CONFIDENCE set to {val}")
        except Exception:
            await update.message.reply_text("Usage: /setconfidence 92")

    async def cmd_setcooldown(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_admin(update.effective_user.id):
            return

        try:
            val = int(ctx.args[0])
            settings.symbol_cooldown_minutes = val
            await update.message.reply_text(f"✅ COOLDOWN set to {val} minutes")
        except Exception:
            await update.message.reply_text("Usage: /setcooldown 180")

    async def cmd_setmaxsignals(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_admin(update.effective_user.id):
            return

        try:
            val = int(ctx.args[0])
            rate_limiter.max = val
            settings.max_signals_per_hour = val
            await update.message.reply_text(f"✅ MAX_SIGNALS_PER_HOUR set to {val}")
        except Exception:
            await update.message.reply_text("Usage: /setmaxsignals 1")



    async def alert_admin(self, title: str, message: str) -> None:
        if self.app is None:
            return

        text = (
            f"🚨 <b>ALPHA RADAR ALERT</b>\n\n"
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
        if self.app is None or not settings.telegram_signal_chat_id:
            return []

        sent_messages: list[dict] = []

        try:
            card = make_signal_card(sig)

            tier = _signal_tier(sig)
            tier_title = {
                "HIGH_PRIORITY": "🚨 <b>HIGH PRIORITY ELITE SIGNAL</b>",
                "ELITE": "🔥 <b>ELITE SIGNAL</b>",
                "VIP": "💎 <b>VIP SIGNAL</b>",
                "PUBLIC": "⚡ <b>ALPHA RADAR SIGNALS</b>",
            }.get(tier, "⚡ <b>ALPHA RADAR SIGNALS</b>")

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
                f"🚀 LEVERAGE • <code>{LEVERAGE_MAP.get(sig.get('timeframe','1h'),'3x')}</code>"
            )

            for chat_id in _route_signal_chats(sig):
                chat_id = chat_id.strip()
                if not chat_id:
                    continue

                try:
                    with open(card, "rb") as photo:
                        msg = await self.app.bot.send_photo(
                            chat_id=chat_id,
                            photo=photo,
                            caption=caption,
                            parse_mode=constants.ParseMode.HTML,
                        )

                    sent_messages.append({
                        "chat_id": str(chat_id),
                        "message_id": int(msg.message_id),
                    })

                except Exception as exc:  # noqa: BLE001
                    logger.exception(f"signal broadcast failed for {chat_id}: {exc}")

        except Exception as exc:  # noqa: BLE001
            logger.exception(f"signal card generation failed: {exc}")

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
            f"⚡ ALPHA RADAR SIGNALS"
        )

        # Preferred: per-channel message mapping
        targets = []
        if signal_id:
            try:
                rows = await repo.get_signal_messages(int(signal_id))
                targets = [
                    {
                        "chat_id": str(r.chat_id),
                        "reply_to": int(r.telegram_message_id),
                    }
                    for r in rows
                ]
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"load signal messages failed: {exc}")

        # Fallback: broadcast to configured chats without reply
        if not targets:
            targets = [
                {"chat_id": chat_id.strip(), "reply_to": payload.get("telegram_message_id")}
                for chat_id in str(settings.telegram_signal_chat_id).split(",")
                if chat_id.strip()
            ]

        for item in targets:
            chat_id = item["chat_id"]
            reply_to = item.get("reply_to")

            try:
                await self.app.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=constants.ParseMode.HTML,
                    reply_to_message_id=reply_to,
                )
            except Exception as reply_exc:  # noqa: BLE001
                logger.warning(f"reply event failed for {chat_id}, sending standalone: {reply_exc}")
                try:
                    await self.app.bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode=constants.ParseMode.HTML,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception(f"event broadcast failed for {chat_id}: {exc}")

