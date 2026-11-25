from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, StreamingResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pathlib import Path
from urllib.parse import urlparse
import sqlite3, csv, io, os, base64, secrets

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("NOVA_DB_PATH", ROOT.parent / "nova_pulse.db"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

class PostgresConnection:
    def __init__(self):
        if psycopg is None:
            raise RuntimeError("DATABASE_URL задан, но пакет psycopg не установлен")
        self.conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)

    def execute(self, sql, params=()):
        return self.conn.execute(sql.replace("?", "%s"), params)

    def executescript(self, script):
        for statement in script.split(";"):
            if statement.strip():
                self.execute(statement)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.conn.close()



app = FastAPI(title="NOVA PULSE API", version="0.1.0")

@app.middleware("http")
async def require_admin_password(request, call_next):
    password = os.getenv("NOVA_ADMIN_PASSWORD", "")
    if not password:
        return PlainTextResponse(
            "NOVA PULSE is locked. Set NOVA_ADMIN_PASSWORD in the hosting environment.",
            status_code=503,
        )
    expected = "Basic " + base64.b64encode(("admin:" + password).encode("utf-8")).decode("ascii")
    supplied = request.headers.get("authorization", "")
    if not secrets.compare_digest(supplied, expected):
        return PlainTextResponse(
            "Authentication required",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="NOVA PULSE"'},
        )
    return await call_next(request)

STATUSES = {"new", "reviewed", "drafted", "sent", "waiting", "replied", "interested", "client", "rejected", "do_not_contact"}


def db():
    if DATABASE_URL:
        return PostgresConnection()
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db():
    with db() as c:
        if DATABASE_URL:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS campaigns (
              id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, niche TEXT DEFAULT '', region TEXT DEFAULT '', source TEXT DEFAULT 'CSV / разрешённый источник', status TEXT DEFAULT 'active', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS leads (
              id BIGSERIAL PRIMARY KEY, campaign_id BIGINT REFERENCES campaigns(id) ON DELETE SET NULL,
              title TEXT NOT NULL DEFAULT '', url TEXT NOT NULL UNIQUE, seller TEXT DEFAULT '', city TEXT DEFAULT '', description TEXT DEFAULT '', price TEXT DEFAULT '', published_at TEXT DEFAULT '', source TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'new', notes TEXT DEFAULT '', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS templates (
              id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, niche TEXT DEFAULT '', body TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS message_log (
              id BIGSERIAL PRIMARY KEY, lead_id BIGINT NOT NULL REFERENCES leads(id) ON DELETE CASCADE, body TEXT NOT NULL, channel TEXT DEFAULT 'manual', status TEXT DEFAULT 'draft', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
        else:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS campaigns (
              id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, niche TEXT DEFAULT '', region TEXT DEFAULT '', source TEXT DEFAULT 'CSV / разрешённый источник', status TEXT DEFAULT 'active', created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS leads (
              id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id INTEGER REFERENCES campaigns(id) ON DELETE SET NULL,
              title TEXT NOT NULL DEFAULT '', url TEXT NOT NULL, seller TEXT DEFAULT '', city TEXT DEFAULT '', description TEXT DEFAULT '', price TEXT DEFAULT '', published_at TEXT DEFAULT '', source TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'new', notes TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
              UNIQUE(url)
            );
            CREATE TABLE IF NOT EXISTS templates (
              id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, niche TEXT DEFAULT '', body TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS message_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE, body TEXT NOT NULL, channel TEXT DEFAULT 'manual', status TEXT DEFAULT 'draft', created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            """)
        old_names = (
            "01 — Аудит сообщества", "02 — Первое впечатление",
            "03 — Упаковка услуг", "04 — Больше доверия",
            "05 — Комплексное решение", "06 — Короткое предложение",
            "Первичное предложение — оформление ВК", "Первичное предложение — сайт"
        )
        c.execute("DELETE FROM templates WHERE name IN (" + ",".join("?" for _ in old_names) + ")", old_names)
        defaults = [
            ("01 — Сократить ручную работу", "Автоматизация бизнеса", "Здравствуйте! Увидел ваше объявление «{title}». Подскажите, какие процессы в работе сейчас занимают больше всего времени у сотрудников? В NOVA DIGITAL разрабатываем решения для автоматизации рутины — от обработки обращений до уведомлений и передачи данных. Могу предложить несколько идей под вашу задачу."),
            ("02 — Обработка заявок", "Автоматизация бизнеса", "Добрый день! Обратил внимание на ваше объявление «{title}». Если заявки поступают из разных каналов, их обработку можно частично автоматизировать, чтобы не терять обращения и быстрее отвечать клиентам. Мы в NOVA DIGITAL настраиваем такие процессы. Актуальна ли для вас подобная задача?"),
            ("03 — Telegram-бот", "Автоматизация бизнеса", "Здравствуйте! Увидел, что вы предлагаете «{title}». Для компаний вашей сферы можно создать Telegram-бота, который отвечает на типовые вопросы, собирает заявки и передаёт информацию ответственному сотруднику. В NOVA DIGITAL разрабатываем ботов под конкретные процессы. Могу рассказать, как это применить в вашем случае."),
            ("04 — Интеграция сервисов", "Автоматизация бизнеса", "Добрый день! Посмотрел ваше объявление «{title}». Часто часть времени уходит на перенос информации между таблицами, мессенджерами и рабочими сервисами. Такие операции можно связать и автоматизировать. Мы помогаем настраивать подобные решения. Есть ли у вас сейчас повторяющиеся задачи такого типа?"),
            ("05 — Индивидуальная автоматизация", "Автоматизация бизнеса", "Здравствуйте! Увидел ваше объявление «{title}». В NOVA DIGITAL помогаем автоматизировать отдельные бизнес-процессы: приём и распределение заявок, уведомления, сбор данных и внутренние операции. Не предлагаю готовый пакет вслепую — сначала разбираемся, где именно автоматизация даст пользу. Могу задать пару вопросов о вашем процессе?"),
            ("06 — Короткое знакомство", "Автоматизация бизнеса", "Добрый день! Я представляю NOVA DIGITAL. Мы помогаем бизнесу сокращать ручную работу с помощью автоматизации, интеграций и Telegram-ботов. Увидел ваше объявление «{title}» и решил уточнить: есть ли у вас процессы, которые хотелось бы упростить или автоматизировать?")
        ]
        for name, niche, body in defaults:
            exists = c.execute("SELECT 1 FROM templates WHERE name=?", (name,)).fetchone()
            if not exists:
                c.execute("INSERT INTO templates(name,niche,body) VALUES(?,?,?)", (name, niche, body))


@app.on_event("startup")
def startup():
    init_db()


class CampaignIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    niche: str = ""
    region: str = ""
    source: str = "CSV / разрешённый источник"


class LeadIn(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    url: str = Field(min_length=8, max_length=2000)
    seller: str = ""
    city: str = ""
    description: str = ""
    price: str = ""
    campaign_id: int | None = None


class LeadUpdate(BaseModel):
    status: str | None = None
    notes: str | None = None
    campaign_id: int | None = None


class TemplateIn(BaseModel):
    name: str
    niche: str = ""
    body: str


class DraftIn(BaseModel):
    template_id: int | None = None


@app.get("/api/health")
def health():
    return {"ok": True, "app": "NOVA PULSE", "version": "0.1.0"}


@app.get("/api/stats")
def stats():
    with db() as c:
        total = c.execute("SELECT COUNT(*) n FROM leads").fetchone()["n"]
        campaigns = c.execute("SELECT COUNT(*) n FROM campaigns").fetchone()["n"]
        counts = {r["status"]: r["n"] for r in c.execute("SELECT status,COUNT(*) n FROM leads GROUP BY status")}
        return {"leads": total, "campaigns": campaigns, "replied": counts.get("replied", 0) + counts.get("interested", 0) + counts.get("client", 0), "interested": counts.get("interested", 0) + counts.get("client", 0), "statuses": counts}


@app.get("/api/campaigns")
def campaigns():
    with db() as c:
        return [dict(r) for r in c.execute("SELECT c.*, COUNT(l.id) lead_count FROM campaigns c LEFT JOIN leads l ON l.campaign_id=c.id GROUP BY c.id ORDER BY c.id DESC")]


@app.post("/api/campaigns")
def create_campaign(data: CampaignIn):
    with db() as c:
        cur = c.execute("INSERT INTO campaigns(name,niche,region,source) VALUES(?,?,?,?) RETURNING id", (data.name.strip(), data.niche, data.region, data.source))
        return dict(c.execute("SELECT * FROM campaigns WHERE id=?", (cur.fetchone()["id"],)).fetchone())


@app.delete("/api/campaigns/{campaign_id}")
def delete_campaign(campaign_id: int):
    with db() as c:
        cur = c.execute("DELETE FROM campaigns WHERE id=?", (campaign_id,))
        if not cur.rowcount:
            raise HTTPException(404, "Кампания не найдена")
        return {"ok": True}


@app.get("/api/leads")
def leads(q: str = "", status: str = "", campaign_id: int | None = None, limit: int = 1000, offset: int = 0):
    sql = "SELECT l.*,c.name campaign_name FROM leads l LEFT JOIN campaigns c ON c.id=l.campaign_id WHERE 1=1"
    args = []
    if q:
        sql += " AND (l.title LIKE ? OR l.seller LIKE ? OR l.city LIKE ? OR l.description LIKE ?)"
        args += [f"%{q}%"] * 4
    if status:
        sql += " AND l.status=?"
        args.append(status)
    if campaign_id:
        sql += " AND l.campaign_id=?"
        args.append(campaign_id)
    sql += " ORDER BY l.id DESC LIMIT ? OFFSET ?"
    args += [min(max(limit, 1), 5000), max(offset, 0)]
    with db() as c:
        return [dict(r) for r in c.execute(sql, args)]


@app.post("/api/import")
async def import_csv(file: UploadFile = File(...), campaign_id: int | None = None):
    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(400, "Загрузите CSV-файл")
    raw = await file.read()
    text = None
    for enc in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            pass
    if text is None:
        raise HTTPException(400, "Не удалось прочитать кодировку CSV")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise HTTPException(400, "В CSV отсутствуют заголовки столбцов")
    aliases = {"title": ["title", "название", "заголовок"], "url": ["url", "link", "ссылка", "ссылка на объявление"], "seller": ["seller", "продавец", "автор"], "city": ["city", "город", "регион"], "description": ["description", "описание"], "price": ["price", "цена"], "published_at": ["published_at", "date", "дата"], "source": ["source", "источник"]}
    norm = {str(k).strip().lower(): k for k in reader.fieldnames}

    def val(row, key):
        for alias in aliases[key]:
            if alias in norm:
                return (row.get(norm[alias]) or "").strip()
        return ""

    inserted = duplicates = invalid = 0
    with db() as c:
        for row in reader:
            url = val(row, "url")
            parsed = urlparse(url)
            if not parsed.scheme or not parsed.netloc:
                invalid += 1
                continue
            title = val(row, "title") or url
            cur = c.execute("INSERT INTO leads(campaign_id,title,url,seller,city,description,price,published_at,source) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(url) DO NOTHING RETURNING id", (campaign_id, title, url, val(row, "seller"), val(row, "city"), val(row, "description"), val(row, "price"), val(row, "published_at"), val(row, "source") or "CSV"))
            if cur.fetchone():
                inserted += 1
            else:
                duplicates += 1
    return {"inserted": inserted, "duplicates": duplicates, "invalid": invalid}


@app.post("/api/leads")
def create_lead(data: LeadIn):
    parsed = urlparse(data.url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(400, "Укажите корректную ссылку на объявление")
    with db() as c:
        if data.campaign_id is not None and not c.execute("SELECT 1 FROM campaigns WHERE id=?", (data.campaign_id,)).fetchone():
            raise HTTPException(404, "Кампания не найдена")
        cur = c.execute(
            "INSERT INTO leads(campaign_id,title,url,seller,city,description,price,source) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(url) DO NOTHING RETURNING id",
            (data.campaign_id, data.title.strip(), data.url.strip(), data.seller.strip(), data.city.strip(), data.description.strip(), data.price.strip(), "Авито — ручной поиск")
        )
        created = cur.fetchone()
        if not created:
            existing = c.execute("SELECT id FROM leads WHERE url=?", (data.url.strip(),)).fetchone()
            return {"ok": True, "duplicate": True, "id": existing["id"] if existing else None}
        return {"ok": True, "duplicate": False, "id": created["id"]}


@app.patch("/api/leads/{lead_id}")
def update_lead(lead_id: int, data: LeadUpdate):
    updates = []
    args = []
    if data.status is not None:
        if data.status not in STATUSES:
            raise HTTPException(400, "Недопустимый статус")
        updates.append("status=?")
        args.append(data.status)
    if data.notes is not None:
        updates.append("notes=?")
        args.append(data.notes)
    if data.campaign_id is not None:
        updates.append("campaign_id=?")
        args.append(data.campaign_id or None)
    if not updates:
        raise HTTPException(400, "Нет изменений")
    updates.append("updated_at=CURRENT_TIMESTAMP")
    args.append(lead_id)
    with db() as c:
        cur = c.execute(f"UPDATE leads SET {','.join(updates)} WHERE id=?", args)
        if not cur.rowcount:
            raise HTTPException(404, "Лид не найден")
        return dict(c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())


class BulkDeleteIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=500)


@app.post("/api/leads/bulk-delete")
def bulk_delete_leads(data: BulkDeleteIn):
    ids = list(dict.fromkeys(data.ids))
    with db() as c:
        cur = c.execute("DELETE FROM leads WHERE id IN (" + ",".join("?" for _ in ids) + ")", ids)
        return {"ok": True, "deleted": cur.rowcount}


@app.delete("/api/leads/{lead_id}")
def delete_lead(lead_id: int):
    with db() as c:
        cur = c.execute("DELETE FROM leads WHERE id=?", (lead_id,))
        if not cur.rowcount:
            raise HTTPException(404, "Лид не найден")
        return {"ok": True}


@app.get("/api/templates")
def templates():
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM templates ORDER BY id DESC")]


@app.post("/api/templates")
def create_template(data: TemplateIn):
    with db() as c:
        cur = c.execute("INSERT INTO templates(name,niche,body) VALUES(?,?,?) RETURNING id", (data.name, data.niche, data.body))
        return dict(c.execute("SELECT * FROM templates WHERE id=?", (cur.fetchone()["id"],)).fetchone())


@app.post("/api/leads/{lead_id}/draft")
def create_draft(lead_id: int, data: DraftIn):
    with db() as c:
        lead = c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if not lead:
            raise HTTPException(404, "Лид не найден")
        if data.template_id is not None:
            tpl = c.execute("SELECT * FROM templates WHERE id=?", (data.template_id,)).fetchone()
        else:
            pool = c.execute("SELECT * FROM templates WHERE niche=? ORDER BY id ASC", ("Автоматизация бизнеса",)).fetchall()
            if not pool:
                raise HTTPException(400, "Сначала добавьте хотя бы один шаблон")
            draft_count = c.execute("SELECT COUNT(*) n FROM message_log").fetchone()["n"]
            tpl = pool[draft_count % len(pool)]
        if not tpl:
            raise HTTPException(404, "Шаблон не найден")
        body = tpl["body"]
        for key in ("title", "seller", "city", "description", "price"):
            body = body.replace("{" + key + "}", lead[key] or "")
        cur = c.execute("INSERT INTO message_log(lead_id,body) VALUES(?,?) RETURNING id", (lead_id, body))
        c.execute("UPDATE leads SET status='drafted',updated_at=CURRENT_TIMESTAMP WHERE id=?", (lead_id,))
        return {"id": cur.fetchone()["id"], "lead_id": lead_id, "body": body, "template_id": tpl["id"], "template_name": tpl["name"], "status": "draft"}


@app.get("/api/export.csv")
def export_csv():
    with db() as c:
        rows = c.execute("SELECT title,url,seller,city,description,price,published_at,source,status,notes FROM leads ORDER BY id DESC").fetchall()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["title", "url", "seller", "city", "description", "price", "published_at", "source", "status", "notes"])
    for row in rows:
        writer.writerow(list(row))
    data = "\ufeff" + out.getvalue()
    return StreamingResponse(iter([data]), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=nova_pulse_leads.csv"})


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
