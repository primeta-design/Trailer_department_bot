import os, re, json, base64, asyncio, logging
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, BotCommand, BotCommandScopeChatMember, ForceReply, ReplyKeyboardRemove
from telegram.ext import Application, ChatMemberHandler, CommandHandler, MessageHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)   # token URL loglarga tushmasin
log = logging.getLogger("fleetbot")

# Hamma maxfiy qiymat faqat Railway Variables'dan olinadi
BOT_TOKEN = os.environ["BOT_TOKEN"]
GROUP_ID = int(os.environ["GROUP_ID"])
SHEET_ID = os.environ["SPREADSHEET_ID"]
OWNER_ID = int(os.getenv("OWNER_ID", "0"))   # /lot_trailers va /dropped faqat shu akkaunt uchun
def _parse_creds(value):
    v = value.strip().strip('"').strip("'")
    if v.startswith("{"):
        return json.loads(v)                       # oddiy JSON
    return json.loads(base64.b64decode(v).decode("utf-8"))   # base64

def _load_creds():
    for name in ("GOOGLE_CREDS_JSON", "GOOGLE_SERVICE_ACCOUNT_KEY_BASE64", "GOOGLE_CREDENTIALS_B64"):
        v = os.getenv(name)
        if v and v.strip():
            try:
                return _parse_creds(v)
            except Exception:
                log.error("Variable %s is set but could not be parsed", name)
    raise RuntimeError("No valid Google credentials variable found")

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
    return [r for r in read_block(TAB_LOT) if col(r, "L").lower() == "late"]

def lot_msg(r):
    who = col(r, "F")
    text = (
        f"Reservation ID: {col(r,'C')}\n"
        f"Truck Unit: {col(r,'E')}\n"
        f"Trailer Unit: {col(r,'B')}\n"
        f"Return Date: {col(r,'I')}\n"
        f"Return Location: {col(r,'J')}"
    )
    if who and who.lower() not in ("n/a", "na", "-"):
        if not who.startswith("@"):
            who = "@" + who
        text += f"\n\n{who} Please be informed. The return date is approaching."
    return text

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
    if not update.effective_user or update.effective_user.id != OWNER_ID:
        await update.message.reply_text("This command is only available to the admin.")
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

def assign_line(r):
    who = col(r, "R")
    if not who:
        return ""
    if not who.startswith("@"):
        who = "@" + who
    return f"{who} pls be informed.\n"

DROPPED_FOOTER = ("-------------------------------\n"
                  "Team what’s the plan for the trailer? Are we picking it up soon, "
                  "or should we extend the parking? Please check with Mr @Fleeet_Nate.")

def dropped_text(items):
    parts = ["The Trailers at the parking:\n"]
    for n, (_, icon, r) in enumerate(items, 1):
        parts.append(
            f"{n}. {icon} Trailer unit: {col(r,'B')}\n"
            f"Truck unit: {col(r,'G')}\n"
            f"Dropped date: {col(r,'K')}\n"
            f"Paid until: {col(r,'O')}\n"
            f"Location: {col(r,'S')}\n"
            f"{assign_line(r)}"
        )
    return "\n".join(parts).rstrip("\n") + "\n" + DROPPED_FOOTER

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
    if not update.effective_user or update.effective_user.id != OWNER_ID:
        await update.message.reply_text("This command is only available to the admin.")
        return
    try:
        await send_dropped(context.bot, empty_note="No dropped trailers are due.")
    except Exception:
        log.exception("dropped cmd failed")
        await update.message.reply_text("⚠️ Sheet could not be read.")


# ---------- Truck <-> Trailer qidiruv ----------
def toks(s):
    t = re.findall(r"[A-Za-z0-9]+", (s or "").lower())
    return set(t)

def matches(query, cell):
    q = toks(query.replace(" ", ""))
    return bool(q) and q <= toks(cell)

def lookup_sync(kind, query):
    found = []
    for r in read_block(TAB_LOT):
        trailer, truck = col(r, "B"), col(r, "E")
        if matches(query, truck if kind == "truck" else trailer):
            found.append((trailer, truck, "Repower"))
    for r in read_block(TAB_TRL):
        trailer, truck = col(r, "B"), col(r, "C")
        if matches(query, truck if kind == "truck" else trailer):
            found.append((trailer, truck, "Company rented"))
    return found

LOOKUP_START = re.compile(r"^/(truck|trailer)[ _]?unit", re.I)
LOOKUP_RE = re.compile(r"^/(truck|trailer)[ _]?unit\s*:?\s*(.*)$", re.I)

async def on_lookup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or update.effective_chat.id != GROUP_ID:
        return
    # @bot_nomi (buyruq oxirida yoki alohida qatorda) olib tashlanadi, qatorlar birlashtiriladi
    text = re.sub(r"@\w+", " ", msg.text or "")
    text = " ".join(text.split())
    m = LOOKUP_RE.match(text)
    if not m:
        return
    kind, query = m.group(1).lower(), m.group(2).strip()
    if not query:
        await msg.reply_text(
            f"Enter the {kind} unit number:",
            reply_markup=ForceReply(selective=True, input_field_placeholder=f"{kind.capitalize()} unit number"))
        return
    if len(query) > 40:
        await msg.reply_text(f"Please enter a unit number, for example:\n/{kind}_unit : 4558")
        return
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


PROMPTS = {"Enter the truck unit number:": "truck", "Enter the trailer unit number:": "trailer"}

async def on_lookup_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or update.effective_chat.id != GROUP_ID:
        return
    prev = msg.reply_to_message
    if not prev or not prev.from_user or prev.from_user.id != context.bot.id:
        return
    kind = PROMPTS.get((prev.text or "").strip())
    if not kind:
        return
    query = " ".join(re.sub(r"@\w+", " ", msg.text or "").split())
    if not query or len(query) > 40:
        await msg.reply_text("Please enter a valid unit number.", reply_markup=ReplyKeyboardRemove(selective=True))
        return
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
            "This trailer is not found in Repower or company rented trailers", reply_markup=ReplyKeyboardRemove(selective=True))
        return
    await msg.reply_text("\n\n".join(
        f"Truck unit: {tk}\nTrailer unit: {tr}\n({src})" for tr, tk, src in found), reply_markup=ReplyKeyboardRemove(selective=True))


# ---------- Drivers guruhlari: report / drop off / update chat ----------
TITLE_RE = re.compile(r"#\s*([A-Za-z0-9-]+)(?:\s+(.+))?")          # "Rawdeal #721 Driver Name"
DRV_START = re.compile(r"^/(trailer|update[ _]?chat)", re.I)
PICK_RE = re.compile(r"^/trailer[ _]?unit\s*:?\s*(?P<unit>[A-Za-z0-9-]+)\s+pick(?:ed)?[ -]?up\s+location\s*:?\s*(?P<loc>.+)$", re.I)
DROP_RE = re.compile(r"^/trailer(?:[ _]?unit\s*:?\s*(?P<unit>[A-Za-z0-9-]+))?\s*drop(?:ped)?[ -]?off\s+location\s*:?\s*(?P<loc>.+)$", re.I)
UPDATE_RE = re.compile(r"^/update[ _]?chat$", re.I)
LINE = "====================="

DRV_USAGE = (
    "Usage:\n"
    "/trailer unit 5624473\n"
    "picked up location : 10560 C F Hawn Fwy, Dallas, TX 75217, USA\n\n"
    "/trailer drop off location : 10560 C F Hawn Fwy, Dallas, TX 75217\n\n"
    "/update chat"
)

async def is_staff(bot, user_id):
    """Owner yoki trailers guruhining a'zosi."""
    if user_id == OWNER_ID:
        return True
    try:
        m = await bot.get_chat_member(GROUP_ID, user_id)
    except Exception:
        return False
    if m.status == "restricted":
        return bool(getattr(m, "is_member", False))
    return m.status in ("creator", "administrator", "member")

async def send_and_pin(bot, chat_id, text):
    m = await bot.send_message(chat_id, text)
    try:
        await bot.pin_chat_message(chat_id, m.message_id, disable_notification=True)
    except Exception:
        log.warning("Could not pin in chat %s", chat_id)
        await bot.send_message(chat_id, "⚠️ I could not pin the message. Please make me an admin with the 'Pin messages' permission.")

def driver_report(unit, truck, driver, kind, loc):
    d = datetime.now(TZ)
    return (
        f"Trailer# {unit}\n{LINE}\n"
        f"Truck unit: {truck}\nDriver name: {driver}\n{LINE}\n"
        f" Status :  ///\n\n"
        f"{kind} location :\n{loc}\n{LINE}\n"
        f"Date:{d.month}/{d.day}/{d.year}"
    )

async def trailers_of_truck(truck):
    found = await asyncio.to_thread(lookup_sync, "truck", truck)
    return list(dict.fromkeys(tr for tr, _, _ in found))

async def on_driver_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not msg or not chat or chat.id == GROUP_ID or chat.type not in ("group", "supergroup"):
        return
    if not user or not await is_staff(context.bot, user.id):
        await msg.reply_text("This command is only available to the team.")
        return
    t = TITLE_RE.search(chat.title or "")
    if not t:
        await msg.reply_text("Group name must look like: Rawdeal #721 Driver Name")
        return
    truck = t.group(1)
    driver = (t.group(2) or "").strip() or "N/A"
    text = " ".join(re.sub(r"@\w+", " ", msg.text or "").split())

    try:
        if UPDATE_RE.match(text):
            trailers = await trailers_of_truck(truck)
            if trailers:
                body = f"#Truck {truck}\n" + "\n".join(f"#Trailer {tr}" for tr in trailers)
            else:
                body = f"#Truck {truck}\nDoing PO load"
            await send_and_pin(context.bot, chat.id, body)
            return

        m = PICK_RE.match(text)
        if m:
            unit, loc = m.group("unit"), m.group("loc").strip()[:200]
            await context.bot.send_message(chat.id, driver_report(unit, truck, driver, "Pick up", loc))
            await send_and_pin(context.bot, chat.id, f"#Truck {truck}\n#Trailer {unit}")
            return

        m = DROP_RE.match(text)
        if m:
            unit, loc = m.group("unit"), m.group("loc").strip()[:200]
            if not unit:
                trailers = await trailers_of_truck(truck)
                if not trailers:
                    await msg.reply_text(
                        f"No trailer found for truck {truck} in the sheet. Please write:\n"
                        "/trailer unit 5624473\ndropped off location : <address>")
                    return
                if len(trailers) > 1:
                    await msg.reply_text(
                        f"More than one trailer found for truck {truck}: {', '.join(trailers)}.\n"
                        "Please write: /trailer unit <number>\ndropped off location : <address>")
                    return
                unit = trailers[0]
            await context.bot.send_message(chat.id, driver_report(unit, truck, driver, "Drop off", loc))
            return
    except Exception:
        log.exception("driver command failed")
        await msg.reply_text("⚠️ Something went wrong, please try again.")
        return

    await msg.reply_text(DRV_USAGE)

async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Botni team'ga kirmaydigan odam yangi guruhga qo'shsa, bot o'sha guruhdan chiqib ketadi."""
    cm = update.my_chat_member
    if not cm or cm.chat.id == GROUP_ID or cm.chat.type not in ("group", "supergroup"):
        return
    added = cm.old_chat_member.status in ("left", "kicked") and cm.new_chat_member.status in ("member", "administrator")
    if added and not await is_staff(context.bot, cm.from_user.id):
        log.warning("Added to chat %s by a non-team user, leaving", cm.chat.id)
        await context.bot.leave_chat(cm.chat.id)


async def post_init(app: Application):
    # "/" bosilganda chiqadigan menyu: hamma uchun
    await app.bot.set_my_commands([
        BotCommand("truck_unit", "Find trailer by truck unit"),
        BotCommand("trailer_unit", "Find truck by trailer unit"),
    ])
    # faqat admin uchun to'liq menyu
    if OWNER_ID:
        try:
            await app.bot.set_my_commands([
                BotCommand("lot_trailers", "Late LOT trailers list"),
                BotCommand("dropped", "Dropped trailers due list"),
                BotCommand("truck_unit", "Find trailer by truck unit"),
                BotCommand("trailer_unit", "Find truck by trailer unit"),
            ], scope=BotCommandScopeChatMember(chat_id=GROUP_ID, user_id=OWNER_ID))
        except Exception:
            log.exception("Could not set admin command menu")

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("lot_trailers", cmd_lot))
    app.add_handler(CommandHandler("dropped", cmd_dropped))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(LOOKUP_START), on_lookup))
    app.add_handler(MessageHandler(filters.TEXT & filters.REPLY & ~filters.COMMAND, on_lookup_reply))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(DRV_START), on_driver_cmd), group=1)
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER), group=1)
    app.job_queue.run_daily(lot_job, time=dtime(LOT_HOUR, 0, tzinfo=TZ))
    app.job_queue.run_daily(dropped_job, time=dtime(DROPPED_HOUR, 0, tzinfo=TZ))
    app.run_polling()

if __name__ == "__main__":
    main()
