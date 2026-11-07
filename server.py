import os, json, hmac, hashlib, sqlite3, asyncio, random, uuid
from urllib.parse import parse_qsl
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (Message, WebAppInfo, InlineKeyboardMarkup,
                           InlineKeyboardButton, MenuButtonWebApp)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "TeleKiller_RvXTNrDc").lstrip("@").lower()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))      # необязательно; ID надёжнее username
_dom = os.getenv("RAILWAY_PUBLIC_DOMAIN")
WEBAPP_URL = (os.getenv("WEBAPP_URL") or f"https://{_dom}").rstrip("/")
PORT = int(os.getenv("PORT", 8080))
BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.getenv("DATA_DIR", BASE)
UPLOADS = os.path.join(DATA, "uploads")
os.makedirs(UPLOADS, exist_ok=True)

db = sqlite3.connect(os.path.join(DATA, "data.db"), check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute("""CREATE TABLE IF NOT EXISTS images(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected
    uploader_id INTEGER,
    uploader_name TEXT,
    wins INTEGER NOT NULL DEFAULT 0,
    shows INTEGER NOT NULL DEFAULT 0)""")
db.commit()

# ---------- проверка пользователя Telegram ----------
def get_user(request):
    init = request.headers.get("X-Init-Data", "")
    data = dict(parse_qsl(init, keep_blank_values=True))
    their_hash = data.pop("hash", None)
    if not their_hash:
        raise web.HTTPUnauthorized()
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    mine = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mine, their_hash):
        raise web.HTTPUnauthorized()
    return json.loads(data["user"])

def is_admin(u):
    return (u.get("username") or "").lower() == ADMIN_USERNAME or (ADMIN_ID and u["id"] == ADMIN_ID)

def require_admin(request):
    u = get_user(request)
    if not is_admin(u):
        raise web.HTTPForbidden()
    return u

# ---------- API ----------
async def me(request):
    u = get_user(request)
    return web.json_response({"id": u["id"], "admin": bool(is_admin(u))})

async def duel(request):
    get_user(request)
    rows = db.execute("SELECT id FROM images WHERE status='approved'").fetchall()
    if len(rows) < 2:
        return web.json_response({"pair": None})
    a, b = random.sample([r["id"] for r in rows], 2)
    return web.json_response({"pair": [a, b]})

async def vote(request):
    get_user(request)
    d = await request.json()
    w, l = int(d["winner"]), int(d["loser"])
    db.execute("UPDATE images SET wins=wins+1, shows=shows+1 WHERE id=? AND status='approved'", (w,))
    db.execute("UPDATE images SET shows=shows+1 WHERE id=? AND status='approved'", (l,))
    db.commit()
    return web.json_response({"ok": True})

async def upload(request):
    u = get_user(request)
    reader = await request.multipart()
    field = await reader.next()
    if not field or field.name != "file":
        raise web.HTTPBadRequest()
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}.get(
        field.headers.get("Content-Type", ""))
    if not ext:
        raise web.HTTPBadRequest(text="only jpg/png/webp/gif")
    name = f"{uuid.uuid4().hex}.{ext}"
    size = 0
    with open(os.path.join(UPLOADS, name), "wb") as f:
        while chunk := await field.read_chunk():
            size += len(chunk)
            if size > 10 * 1024 * 1024:
                f.close(); os.remove(os.path.join(UPLOADS, name))
                raise web.HTTPRequestEntityTooLarge(10 * 1024 * 1024, size)
            f.write(chunk)
    adm = bool(is_admin(u))
    db.execute("INSERT INTO images(filename,status,uploader_id,uploader_name) VALUES(?,?,?,?)",
               (name, "approved" if adm else "pending", u["id"], u.get("username") or u.get("first_name")))
    db.commit()
    return web.json_response({"ok": True, "approved": adm})

async def pending(request):
    require_admin(request)
    rows = db.execute("SELECT id, uploader_name FROM images WHERE status='pending' ORDER BY id").fetchall()
    return web.json_response([dict(r) for r in rows])

async def moderate(request):
    require_admin(request)
    d = await request.json()
    status = "approved" if d["action"] == "approve" else "rejected"
    db.execute("UPDATE images SET status=? WHERE id=?", (status, int(d["id"])))
    db.commit()
    return web.json_response({"ok": True})

async def champion(request):
    get_user(request)
    r = db.execute("SELECT id, wins, shows FROM images WHERE status='approved' AND wins>0 "
                   "ORDER BY wins DESC, shows ASC LIMIT 1").fetchone()
    return web.json_response(dict(r) if r else None)

async def image(request):
    r = db.execute("SELECT filename FROM images WHERE id=?", (int(request.match_info["id"]),)).fetchone()
    if not r:
        raise web.HTTPNotFound()
    return web.FileResponse(os.path.join(UPLOADS, r["filename"]))

async def index(request):
    return web.FileResponse(os.path.join(BASE, "static", "index.html"))

# ---------- бот ----------
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

@dp.message(CommandStart())
async def start(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Играть", web_app=WebAppInfo(url=WEBAPP_URL))]])
    await m.answer("Выбирай лучшую картинку!", reply_markup=kb)

async def main():
    app = web.Application(client_max_size=12 * 1024 * 1024)
    app.add_routes([
        web.get("/", index), web.get("/api/me", me), web.get("/api/duel", duel),
        web.post("/api/vote", vote), web.post("/api/upload", upload),
        web.get("/api/pending", pending), web.post("/api/moderate", moderate),
        web.get("/api/champion", champion), web.get("/img/{id}", image)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    # кнопка мини-приложения слева от строки ввода
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
        text="Играть", web_app=WebAppInfo(url=WEBAPP_URL)))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
