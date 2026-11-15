import os, re, json, base64, asyncio, logging
from datetime import time as dtime
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)   # token URL loglarga tushmasin
log = logging.getLogger("fleetbot")

# Hamma maxfiy qiymat faqat Railway Variables'dan olinadi
BOT_TOKEN = os.environ["BOT_TOKEN"]
GROUP_ID = int(os.environ["GROUP_ID"])
SHEET_ID = os.environ["SPREADSHEET_ID"]
def _load_creds():
    raw = os.getenv("GOOGLE_CREDS_JSON")
    if raw:
        return json.loads(raw)
    for name in ("GOOGLE_SERVICE_ACCOUNT_KEY_BASE64", "GOOGLE_CREDENTIALS_B64"):
        b64 = os.getenv(name)
        if b64:
            return json.loads(base64.b64decode(b64).decode("utf-8"))
    raise RuntimeError("Google credentials variable is missing")

CREDS = _load_creds()

TZ = ZoneInfo("America/Chicago")
LOT_HOUR, DROPPED_HOUR = 8, 9
TAB_LOT, TAB_DROP, TAB_TRL = "LOT TRAILERS", "DROPPED TRAILERS", "TRL board"


# ---------- Google Sheets (faqat o'qish) ----------
_gc = None
def _sheet():
    global _gc
    if _gc is None:
        creds = Credentials.from_service_account_info(
            CREDS, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
        _gc = gspread.authorize(creds)
    return _gc.open_by_key(SHEET_ID)

def col(row, letter):
    i = ord(letter.upper()) - 65
    return row[i].strip() if i < len(row) else ""

def read_block(tab, key_col="B"):
    """Sarlavhadan keyin birinchi bo'sh (kulrang) qatorgacha. Pastdagi tarix o'qilmaydi."""
    out = []
    for r in _sheet().worksheet(tab).get_all_values()[1:]:
        if not col(r, key_col):
            break
        out.append(r)
    return out


# ---------- LOT TRAILERS: N = Late bo'lsa har kuni 8:00 ----------
def lot_late_rows():
    return [r for r in read_block(TAB_LOT) if col(r, "N").lower() == "late"]

def lot_msg(r):
    return (
        f"Reservation ID: {col(r,'C')}\n\n"
        f"Truck unit: {col(r,'F')}\n"
        f"Trailer unit: {col(r,'B')}\n\n"
        f"Return date: {col(r,'K')}\n"
        f"Return location: {col(r,'L')}"
    )

async def send_lot(bot, empty_note=None):
    rows = await asyncio.to_thread(lot_late_rows)
    if not rows:
        if empty_note:
            await bot.send_message(GROUP_ID, empty_note)
        return
    for r in rows:                      # har bir trailer alohida xabar
        await bot.send_message(GROUP_ID, lot_msg(r))

async def lot_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        await send_lot(context.bot)
    except Exception:
        log.exception("LOT job failed")
        await context.bot.send_message(GROUP_ID, "⚠️ LOT TRAILERS sheet could not be read.")

async def cmd_lot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ID:
        return
    try:
        await send_lot(context.bot, empty_note="No late trailers.")
    except Exception:
        log.exception("lot cmd failed")
        await update.message.reply_text("⚠️ Sheet could not be read.")


# ---------- DROPPED TRAILERS: sheetdagi rangga qaraydi ----------
def classify(bg):
    """Sheetdagi (conditional formatting natijasidagi) rang: 'red' / 'yellow' / None."""
    if not bg:                          # rang berilmagan = oq
        return None
    r, g, b = bg.get("red", 0), bg.get("green", 0), bg.get("blue", 0)
    if r > 0.8 and g < 0.45 and b < 0.45:
        return "red"
    if r > 0.8 and g > 0.55 and b < 0.65:
        return "yellow"
    return None

def dropped_items():
    md = _sheet().fetch_sheet_metadata(params={
        "includeGridData": "true",
        "ranges": f"'{TAB_DROP}'!A1:S40",
        "fields": "sheets(data(rowData(values(formattedValue,effectiveFormat(backgroundColor)))))",
    })
    rows = md["sheets"][0]["data"][0].get("rowData", [])[1:]
    items = []
    for rd in rows:
        vals = rd.get("values", [])
        texts = [v.get("formattedValue", "") for v in vals]
        if not col(texts, "B"):
            break
        bg = vals[1].get("effectiveFormat", {}).get("backgroundColor") if len(vals) > 1 else None
        c = classify(bg)                # B ustun rangi (qatorni sheetdagi qoida bo'yaydi)
        if c:
            items.append((0 if c == "red" else 1, "🔴" if c == "red" else "🟡", texts))
    items.sort(key=lambda x: x[0])
    return items

def dropped_text(items):
    parts = ["The Trailers at the parking:\n"]
    for n, (_, icon, r) in enumerate(items, 1):
        parts.append(
            f"{n}. {icon} Trailer unit: {col(r,'B')}\n"
            f"    Truck unit: {col(r,'G')}\n"
            f"    Dropped date: {col(r,'K')}\n"
            f"    Paid until: {col(r,'O')}\n"
            f"    Location: {col(r,'S')}\n"
        )
    return "\n".join(parts)

async def send_dropped(bot, empty_note=None):
    items = await asyncio.to_thread(dropped_items)
    if not items:
        if empty_note:
            await bot.send_message(GROUP_ID, empty_note)
        return
    text = dropped_text(items)
    while text:
        chunk, text = text[:4000], text[4000:]
        await bot.send_message(GROUP_ID, chunk)

async def dropped_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        await send_dropped(context.bot)
    except Exception:
        log.exception("DROPPED job failed")
        await context.bot.send_message(GROUP_ID, "⚠️ DROPPED TRAILERS sheet could not be read.")

async def cmd_dropped(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ID:
        return
    try:
        await send_dropped(context.bot, empty_note="No dropped trailers are due.")
    except Exception:
        log.exception("dropped cmd failed")
        await update.message.reply_text("⚠️ Sheet could not be read.")


# ---------- Truck <-> Trailer qidiruv ----------
def toks(s):
    t = re.findall(r"[A-Za-z0-9]+", (s or "").lower())
    return {(x.lstrip("0") or "0") if x.isdigit() else x for x in t}

def matches(query, cell):
    q = toks(query.replace(" ", ""))
    return bool(q) and q <= toks(cell)

def lookup_sync(kind, query):
    found = []
    for r in read_block(TAB_LOT):
        trailer, truck = col(r, "B"), col(r, "F")
        if matches(query, truck if kind == "truck" else trailer):
            found.append((trailer, truck, "Repower"))
    for r in read_block(TAB_TRL):
        trailer, truck = col(r, "B"), col(r, "C")
        if matches(query, truck if kind == "truck" else trailer):
            found.append((trailer, truck, "Company rented"))
    return found

LOOKUP_RE = re.compile(r"^/(truck|trailer)[ _]?unit(?:@\w+)?\s*:?\s*(.{1,40})$", re.I)

async def on_lookup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or update.effective_chat.id != GROUP_ID:
        return
    m = LOOKUP_RE.match((msg.text or "").strip())
    if not m:
        return
    kind, query = m.group(1).lower(), m.group(2).strip()
    try:
        found = await asyncio.to_thread(lookup_sync, kind, query)
    except Exception:
        log.exception("lookup failed")
        await msg.reply_text("⚠️ Sheet could not be read.")
        return
    if not found:
        await msg.reply_text(
            "The truck is not using Repower trailer or company rented trailer"
            if kind == "truck" else
            "This trailer is not found in Repower or company rented trailers")
        return
    await msg.reply_text("\n\n".join(
        f"Truck unit: {tk}\nTrailer unit: {tr}\n({src})" for tr, tk, src in found))


async def post_init(app: Application):
    # "/" bosilganda chiqadigan menyu
    await app.bot.set_my_commands([
        BotCommand("lot_trailers", "Late LOT trailers list"),
        BotCommand("dropped", "Dropped trailers due list"),
        BotCommand("truck_unit", "Find trailer by truck unit"),
        BotCommand("trailer_unit", "Find truck by trailer unit"),
    ])

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("lot_trailers", cmd_lot))
    app.add_handler(CommandHandler("dropped", cmd_dropped))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(LOOKUP_RE), on_lookup))
    app.job_queue.run_daily(lot_job, time=dtime(LOT_HOUR, 0, tzinfo=TZ))
    app.job_queue.run_daily(dropped_job, time=dtime(DROPPED_HOUR, 0, tzinfo=TZ))
    app.run_polling()

if __name__ == "__main__":
    main()
