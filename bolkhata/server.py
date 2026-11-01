"""BolKhata: a voice-first ledger for small shops. Speak entries in any Indian language."""

import base64
import difflib
import json
import os
import re
import sqlite3
import time
import urllib.parse
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sarvamai import SarvamAI
from sarvamai.core.api_error import ApiError

ROOT = Path(__file__).parent
DATA = ROOT / "data"
EVIDENCE = DATA / "evidence"
EVIDENCE.mkdir(parents=True, exist_ok=True)

# Load .env (this folder or the repo root) into the environment.
for env_file in (ROOT / ".env", ROOT.parent / ".env"):
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() and not line.lstrip().startswith("#"):
                os.environ.setdefault(key.strip(), value.strip())

API_KEY = os.environ.get("SARVAM_API_KEY")
if not API_KEY:
    raise SystemExit("SARVAM_API_KEY is not set. Add it to .env.")

client = SarvamAI(api_subscription_key=API_KEY, timeout=90)
app = FastAPI(title="BolKhata")
app.mount("/evidence", StaticFiles(directory=EVIDENCE), name="evidence")
app.mount("/samples", StaticFiles(directory=ROOT / "samples"), name="samples")

LLM = "sarvam-105b-conversations"

# Languages Bulbul can speak, with a matching voice where one exists.
LANGUAGES = {
    "hi-IN": ("Hindi", "ritu_hi_customer"),
    "en-IN": ("English", "simran_en_customer"),
    "bn-IN": ("Bengali", "roopa_bn_conversational"),
    "ta-IN": ("Tamil", "gokul_ta_narration"),
    "te-IN": ("Telugu", "kavitha_te_conversation"),
    "mr-IN": ("Marathi", "ishita_mr_conversational"),
    "gu-IN": ("Gujarati", "pooja_gu_conversational"),
    "kn-IN": ("Kannada", "chaitra_kn_conversation"),
    "ml-IN": ("Malayalam", None),
    "pa-IN": ("Punjabi", "anand_pa_conversation"),
    "od-IN": ("Odia", None),
}
DEFAULT_LANGUAGE = "hi-IN"
KINDS = ("credit", "payment", "sale", "purchase", "expense")
# How each kind is described to the model when it phrases a confirmation (in everyday shop words).
KIND_WORDS = {
    "credit": "goods given on credit to",
    "payment": "money received back from",
    "sale": "cash sale",
    "purchase": "stock bought from",
    "expense": "shop expense",
}


def speech_language(code: str | None) -> str:
    """Detected languages Bulbul can't speak (e.g. Urdu) fall back to Hindi."""
    return code if code in LANGUAGES else DEFAULT_LANGUAGE


# ---------------------------------------------------------------- database

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT,
    language TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    customer_id INTEGER REFERENCES customers(id),
    party TEXT,
    amount REAL NOT NULL,
    items TEXT,
    source TEXT NOT NULL,
    transcript TEXT,
    language TEXT,
    evidence TEXT,
    created_at TEXT NOT NULL,
    undone INTEGER NOT NULL DEFAULT 0
);
"""


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DATA / "bolkhata.db")
    conn.row_factory = sqlite3.Row
    return conn


with db() as conn:
    conn.executescript(SCHEMA)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def shop_name(conn) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = 'shop_name'").fetchone()
    return row["value"] if row else "Apni Dukaan"


def balances(conn) -> dict[int, float]:
    rows = conn.execute(
        """SELECT customer_id,
                  SUM(CASE kind WHEN 'credit' THEN amount WHEN 'payment' THEN -amount ELSE 0 END) AS due
           FROM entries WHERE undone = 0 AND customer_id IS NOT NULL GROUP BY customer_id"""
    ).fetchall()
    return {r["customer_id"]: r["due"] for r in rows}


def find_or_create_customer(conn, name: str, language: str) -> sqlite3.Row:
    name = name.strip().title()
    customers = conn.execute("SELECT * FROM customers").fetchall()
    by_key = {c["name"].lower(): c for c in customers}
    match = difflib.get_close_matches(name.lower(), list(by_key), n=1, cutoff=0.85)
    if match:
        return by_key[match[0]]
    cur = conn.execute(
        "INSERT INTO customers (name, language, created_at) VALUES (?, ?, ?)", (name, language, now())
    )
    return conn.execute("SELECT * FROM customers WHERE id = ?", (cur.lastrowid,)).fetchone()


def add_entry(conn, kind, amount, *, customer=None, party=None, items="", source, transcript=None,
              language=None, evidence=None, created_at=None) -> int:
    cur = conn.execute(
        """INSERT INTO entries (kind, customer_id, party, amount, items, source, transcript, language,
                                evidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (kind, customer["id"] if customer else None, party or (customer["name"] if customer else None),
         float(amount), items or "", source, transcript, language, evidence, created_at or now()),
    )
    return cur.lastrowid


def entry_dict(row) -> dict:
    return {k: row[k] for k in row.keys()}


def snapshot(conn) -> dict:
    """Everything the UI shows: totals, customers with balances, recent entries."""
    due = balances(conn)
    today = datetime.now().date().isoformat()
    totals = {k: 0.0 for k in KINDS}
    for r in conn.execute(
        "SELECT kind, SUM(amount) AS s FROM entries WHERE undone = 0 AND created_at >= ? GROUP BY kind", (today,)
    ):
        totals[r["kind"]] = r["s"]
    customers = []
    for c in conn.execute(
        """SELECT c.*, MAX(e.created_at) AS last_at FROM customers c
           LEFT JOIN entries e ON e.customer_id = c.id AND e.undone = 0
           GROUP BY c.id"""
    ):
        customers.append({**entry_dict(c), "balance": round(due.get(c["id"], 0), 2)})
    customers.sort(key=lambda c: (-c["balance"], c["name"]))
    entries = [entry_dict(r) for r in conn.execute(
        "SELECT * FROM entries ORDER BY created_at DESC, id DESC LIMIT 200"
    )]
    # Last 7 days of totals per kind, for the trend sparklines.
    first_day = datetime.now().date() - timedelta(days=6)
    daily = {(first_day + timedelta(days=i)).isoformat(): {k: 0.0 for k in KINDS} for i in range(7)}
    for r in conn.execute(
        """SELECT substr(created_at, 1, 10) AS day, kind, SUM(amount) AS s FROM entries
           WHERE undone = 0 AND created_at >= ? GROUP BY day, kind""", (first_day.isoformat(),)
    ):
        if r["day"] in daily:
            daily[r["day"]][r["kind"]] = r["s"]
    return {
        "shop": shop_name(conn),
        "today": totals,
        "outstanding": round(sum(v for v in due.values() if v > 0), 2),
        "customers": customers,
        "entries": entries,
        "daily": [{"date": day, **totals_by_kind} for day, totals_by_kind in daily.items()],
    }


# ---------------------------------------------------------------- Sarvam helpers

def with_retry(fn, *args, **kwargs):
    """Retry Sarvam calls on rate limits, server errors and timeouts."""
    for attempt in range(3):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            transient = status in (429, 500, 502, 503, 504) or isinstance(exc, httpx.TimeoutException)
            if not transient or attempt == 2:
                raise
            time.sleep(1 + attempt * 2)


def chat(system: str, user: str, max_tokens: int = 1200) -> str:
    reply = with_retry(
        client.chat.completions,
        model=LLM,
        max_tokens=max_tokens,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    return (reply.choices[0].message.content or "").strip()


def parse_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        raise HTTPException(502, "Could not understand the model's reply")
    return json.loads(match.group(0))


def read_document(data: bytes, filename: str, mime: str) -> str:
    """Run Sarvam Vision on an image and return its text in reading order."""
    job = with_retry(client.doc_ai.parse, file=[(filename, data, mime)], content_type="mixed")
    deadline = time.time() + 120
    while True:
        status = with_retry(client.doc_ai.get_status, job.job_id).status
        if status == "completed":
            break
        if status in ("failed", "cancelled") or time.time() > deadline:
            raise HTTPException(502, f"Reading the photo {status}")
        time.sleep(0.5)
    # The SDK cannot parse "digitise" results yet, so fetch them directly.
    resp = with_retry(
        httpx.get,
        f"https://api.sarvam.ai/doc-ai/v1/job/{job.job_id}/results",
        headers={"api-subscription-key": API_KEY},
        timeout=60,
    )
    resp.raise_for_status()
    pages = [p for doc in resp.json()["documents"] for p in doc["pages"]]
    return "\n".join(
        b["text"] for page in pages for b in sorted(page["blocks"], key=lambda b: b["reading_order"])
    ).strip()


def save_evidence(data: bytes, ext: str) -> str:
    name = f"{uuid.uuid4().hex}.{ext}"
    (EVIDENCE / name).write_bytes(data)
    return name


def rupees(x: float) -> str:
    return f"₹{x:,.0f}" if float(x).is_integer() else f"₹{x:,.2f}"


# ---------------------------------------------------------------- prompts

ROUTER_PROMPT = """You are the brain of BolKhata, a ledger (bahi-khata) app for a small Indian shop. The shopkeeper speaks in any Indian language, often mixed with English. Turn what they said into actions.

Existing customers (reuse these exact spellings when the speaker means one of them): {customers}

Reply ONLY with JSON, no markdown:
{{"intent": "add" | "ask" | "remind" | "undo" | "other",
  "entries": [{{"kind": "credit" | "payment" | "sale" | "purchase" | "expense", "customer": "name in English letters, or null", "party": "supplier/payee for purchase or expense, or null", "amount": number, "items": "short item list in English, or empty"}}],
  "remind_customer": "name in English letters, or null",
  "question": "for ask: the question restated in English, else null"}}

Kinds:
- credit: a customer took goods or money on credit (udhaar diya, khate mein likh do, baad mein dega, கடன், ধার, उधार)
- payment: a customer paid back money they owed (paise diye, wapas kiye, jama kiye, chuka diye)
- sale: a cash or UPI sale with no credit
- purchase: the shop bought stock from a supplier or wholesaler
- expense: a shop expense (rent, electricity, transport, salary, chai)
Rules: amounts are plain rupee numbers ("dedh hazaar" = 1500, "dhai sau" = 250, "three forty" = 340). One sentence can contain several entries. Customer names always in English letters with no honorifics (रमेश जी -> Ramesh, லட்சுமி அக்கா -> Lakshmi, Sharma ji -> Sharma). Questions about balances, sales, who owes what -> "ask". Asking to remind someone to pay / send a reminder -> "remind". Cancel or undo the last entry ("galat ho gaya", "cancel karo") -> "undo". If an entry has no amount, use "other"."""

REPLY_PROMPT = """You are BolKhata, a friendly munim (accountant) speaking to a shopkeeper. Say the facts below back to them in {language}, written in its native script, in 1-2 short sentences. Use exactly these names and rupee amounts; never do your own maths. Use the everyday shop word for credit in that language and script (उधार in Hindi, கடன் in Tamil, ধার in Bengali), never the English word. Plain text only, no markdown or emojis."""

ASK_PROMPT = """You are BolKhata, a friendly munim (accountant) for {shop}. Answer the shopkeeper's question in {language}, written in its native script, in 1-3 short sentences, using ONLY the ledger data below. Quote rupee amounts exactly as given; do not add numbers yourself unless the question needs a simple sum. Plain text only, no markdown.

Today is {today}. Ledger data (JSON):
{ledger}"""

REMIND_PROMPT = """Write a short, warm and respectful payment reminder from the shop "{shop}" to its customer {name}, in {language} written in its native script. They owe {amount} in total. 2-3 sentences, like a friendly neighbourhood shopkeeper: no threats, no pressure, thank them for being a regular customer, and say they can pay at the shop or by UPI. Plain text only."""

PARCHI_PROMPT = """You read OCR text of a supplier bill/parchi (often handwritten, Hindi/English mixed) for a small shop. Reply ONLY with JSON, no markdown:
{"supplier": "supplier name in English letters or null", "date": "date as written or null", "items": [{"name": "item name in English, without the quantity", "qty": "quantity with unit as written, e.g. 10kg, 2 dozen, 1 box", "amount": number}], "total": number}
"amount" is the rupee price for that line (usually the right-most number). "qty" is never a price. Use the bill's own total if written; otherwise add the item amounts. Never invent items."""


# ---------------------------------------------------------------- API

class CommandRequest(BaseModel):
    audio_b64: str | None = None
    audio_mime: str | None = None
    text: str | None = None


class SpeakRequest(BaseModel):
    text: str
    language: str = DEFAULT_LANGUAGE


class ParchiRequest(BaseModel):
    image_b64: str
    mime: str
    language: str = DEFAULT_LANGUAGE


class CustomerUpdate(BaseModel):
    name: str | None = None
    phone: str | None = None
    language: str | None = None


@app.exception_handler(ApiError)
def sarvam_error(request, exc: ApiError):
    message = ("Sarvam is busy right now. Please try again in a moment." if exc.status_code == 429
               else "Something went wrong talking to Sarvam. Please try again.")
    return JSONResponse({"detail": message}, status_code=502)


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/state")
def state():
    with db() as conn:
        return snapshot(conn)


@app.get("/api/languages")
def languages():
    return [{"code": code, "name": name} for code, (name, _) in LANGUAGES.items()]


@app.post("/api/command")
def command(req: CommandRequest):
    """One spoken or typed sentence -> detect language -> understand -> act -> reply in that language."""
    timings = {}
    evidence = None
    start = time.time()
    if req.audio_b64:
        audio = base64.b64decode(req.audio_b64)
        mime = req.audio_mime or "audio/webm"
        ext = mime.split("/")[-1].split(";")[0]
        stt = with_retry(
            client.speech_to_text.transcribe,
            file=(f"command.{ext}", audio, mime), model="saaras:v4", mode="transcribe",
        )
        transcript, detected = stt.transcript.strip(), stt.language_code
        evidence = save_evidence(audio, ext)
        timings["stt"] = round((time.time() - start) * 1000)
    else:
        transcript = (req.text or "").strip()
        if not transcript:
            raise HTTPException(422, "Please say or type something.")
        detected = with_retry(client.text.identify_language, input=transcript).language_code
        timings["lid"] = round((time.time() - start) * 1000)
    if not transcript:
        raise HTTPException(422, "Didn't catch that. Please try again.")
    language = speech_language(detected)

    with db() as conn:
        names = [c["name"] for c in conn.execute("SELECT name FROM customers ORDER BY name")]
        start = time.time()
        plan = parse_json(chat(ROUTER_PROMPT.format(customers=", ".join(names) or "none yet"), transcript))
        timings["llm"] = round((time.time() - start) * 1000)
        intent = plan.get("intent")
        # The model sometimes names the entry kind ("payment") as the intent; entries mean "add".
        if intent not in ("ask", "remind", "undo") and any(
                isinstance(e, dict) and e.get("kind") in KINDS for e in plan.get("entries") or []):
            intent = "add"
        result = {"transcript": transcript, "detected": detected, "language": language,
                  "intent": intent, "saved": [], "reminder": None}

        if intent == "add":
            facts = []
            for e in plan.get("entries") or []:
                kind, amount = e.get("kind"), e.get("amount")
                if kind not in KINDS or not isinstance(amount, (int, float)) or amount <= 0:
                    continue
                customer = None
                if kind in ("credit", "payment"):
                    if not e.get("customer"):
                        continue
                    customer = find_or_create_customer(conn, e["customer"], language)
                entry_id = add_entry(
                    conn, kind, amount, customer=customer, party=e.get("party"), items=e.get("items"),
                    source="voice" if req.audio_b64 else "text", transcript=transcript,
                    language=detected, evidence=evidence,
                )
                result["saved"].append(entry_id)
                who = customer["name"] if customer else (e.get("party") or "")
                facts.append(f"Wrote down: {KIND_WORDS[kind]}" + (f" {who}" if who else "") + f", {rupees(amount)}"
                             + (f" ({e['items']})" if e.get("items") else "") + ".")
            due = balances(conn)
            for cid in {r["customer_id"] for r in conn.execute(
                    f"SELECT customer_id FROM entries WHERE id IN ({','.join('?' * len(result['saved']))})",
                    result["saved"]) if r["customer_id"]}:
                name = conn.execute("SELECT name FROM customers WHERE id = ?", (cid,)).fetchone()["name"]
                facts.append(f"{name} now owes {rupees(max(due.get(cid, 0), 0))} in total.")
            if not result["saved"]:
                facts = ["I could not find an amount or a name. Please say it again with the amount."]
            reply = chat(REPLY_PROMPT.format(language=LANGUAGES[language][0]), " ".join(facts), 300)

        elif intent == "ask":
            snap = snapshot(conn)
            ledger = {
                "today_totals": {k: rupees(v) for k, v in snap["today"].items()},
                "total_outstanding_udhaar": rupees(snap["outstanding"]),
                "customers": [{"name": c["name"], "owes": rupees(c["balance"])} for c in snap["customers"]],
                "recent_entries": [
                    {"when": e["created_at"], "kind": e["kind"], "who": e["party"], "amount": rupees(e["amount"]),
                     "items": e["items"]} for e in snap["entries"][:40] if not e["undone"]
                ],
            }
            reply = chat(ASK_PROMPT.format(shop=snap["shop"], language=LANGUAGES[language][0],
                                           today=datetime.now().strftime("%A %d %B %Y"),
                                           ledger=json.dumps(ledger, ensure_ascii=False)),
                         plan.get("question") or transcript, 400)

        elif intent == "remind":
            name = plan.get("remind_customer") or ""
            customer = next((c for c in conn.execute("SELECT * FROM customers")
                             if difflib.SequenceMatcher(None, c["name"].lower(), name.lower()).ratio() > 0.8), None)
            if customer:
                result["reminder"] = make_reminder(conn, customer)
                facts = (f"I have written a polite payment reminder message for {customer['name']} in "
                         f"{LANGUAGES[speech_language(customer['language'])][0]}. {customer['name']} owes "
                         f"{rupees(result['reminder']['balance'])}. Press the WhatsApp button to send the message.")
            else:
                facts = f"I could not find a customer called {name or 'that'}."
            reply = chat(REPLY_PROMPT.format(language=LANGUAGES[language][0]), facts, 300)

        elif intent == "undo":
            last = conn.execute("SELECT * FROM entries WHERE undone = 0 ORDER BY id DESC LIMIT 1").fetchone()
            if last:
                conn.execute("UPDATE entries SET undone = 1 WHERE id = ?", (last["id"],))
                facts = f"Cancelled the last entry: {last['kind']} of {rupees(last['amount'])}" + (
                    f" for {last['party']}." if last["party"] else ".")
            else:
                facts = "There is nothing to cancel."
            reply = chat(REPLY_PROMPT.format(language=LANGUAGES[language][0]), facts, 300)

        else:
            reply = chat(REPLY_PROMPT.format(language=LANGUAGES[language][0]),
                         "I did not understand. Ask them to say an entry like: Ramesh took 500 rupees of goods on credit.", 300)

        timings["total"] = sum(timings.values())
        result.update(reply=reply, ms=timings, state=snapshot(conn))
        return result


def make_reminder(conn, customer) -> dict:
    balance = max(balances(conn).get(customer["id"], 0), 0)
    language = speech_language(customer["language"])
    text = chat(REMIND_PROMPT.format(shop=shop_name(conn), name=customer["name"],
                                     language=LANGUAGES[language][0], amount=rupees(balance)), "Write it.", 400)
    phone = re.sub(r"\D", "", customer["phone"] or "")
    if len(phone) == 10:
        phone = "91" + phone
    link = f"https://wa.me/{phone}?text={urllib.parse.quote(text)}" if phone else \
        f"https://wa.me/?text={urllib.parse.quote(text)}"
    return {"customer_id": customer["id"], "name": customer["name"], "language": language,
            "balance": balance, "text": text, "whatsapp": link}


@app.post("/api/remind/{customer_id}")
def remind(customer_id: int):
    with db() as conn:
        customer = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
        if not customer:
            raise HTTPException(404, "Customer not found")
        return make_reminder(conn, customer)


@app.post("/api/speak")
def speak(req: SpeakRequest):
    start = time.time()
    language = speech_language(req.language)
    voice = LANGUAGES[language][1]
    kwargs = {"speaker": voice} if voice else {}
    audio = with_retry(client.text_to_speech.convert, text=req.text[:2000], language_code=language,
                       model="bulbul:v4-flash", **kwargs)
    return {"audio": audio.audios, "ms": round((time.time() - start) * 1000)}


@app.post("/api/parchi")
def parchi(req: ParchiRequest):
    """Photo of a supplier bill -> Sarvam Vision -> items -> purchase entry."""
    image = base64.b64decode(req.image_b64)
    ext = req.mime.split("/")[-1]
    timings = {}
    start = time.time()
    text = read_document(image, f"parchi.{ext}", req.mime)
    timings["vision"] = round((time.time() - start) * 1000)
    if not text:
        raise HTTPException(422, "Couldn't read the bill. Try a clearer, well-lit photo.")
    start = time.time()
    bill = parse_json(chat(PARCHI_PROMPT, text))
    timings["llm"] = round((time.time() - start) * 1000)
    for item in bill.get("items") or []:
        # If the price landed in "qty", move it to "amount".
        if not isinstance(item.get("amount"), (int, float)) or item["amount"] <= 0:
            qty = str(item.get("qty") or "").replace(",", "")
            if re.fullmatch(r"\d+(\.\d+)?", qty):
                item["amount"], item["qty"] = float(qty), None
    total = bill.get("total")
    if not isinstance(total, (int, float)) or total <= 0:
        total = sum(i.get("amount") or 0 for i in bill.get("items") or [] if isinstance(i.get("amount"), (int, float)))
    if total <= 0:
        raise HTTPException(422, "Couldn't find an amount on the bill.")
    items = ", ".join(f"{i.get('name')} {i.get('qty') or ''}".strip() for i in bill.get("items") or [])
    language = speech_language(req.language)
    with db() as conn:
        entry_id = add_entry(conn, "purchase", total, party=bill.get("supplier"), items=items, source="photo",
                             transcript=text, language=language, evidence=save_evidence(image, ext))
        reply = chat(REPLY_PROMPT.format(language=LANGUAGES[language][0]),
                     f"Recorded a purchase of {rupees(total)}" + (f" from {bill['supplier']}" if bill.get("supplier") else "")
                     + f" with {len(bill.get('items') or [])} items.", 300)
        return {"bill": bill, "entry_id": entry_id, "reply": reply, "language": language,
                "ms": timings, "state": snapshot(conn)}


@app.post("/api/entries/{entry_id}/toggle")
def toggle_entry(entry_id: int):
    with db() as conn:
        conn.execute("UPDATE entries SET undone = 1 - undone WHERE id = ?", (entry_id,))
        return snapshot(conn)


@app.post("/api/customers/{customer_id}")
def update_customer(customer_id: int, req: CustomerUpdate):
    with db() as conn:
        for field in ("name", "phone", "language"):
            value = getattr(req, field)
            if value is not None:
                conn.execute(f"UPDATE customers SET {field} = ? WHERE id = ?", (value.strip(), customer_id))
        return snapshot(conn)


@app.post("/api/demo")
def load_demo():
    """Reset to a believable shop with a week of history across several languages."""
    with db() as conn:
        conn.executescript("DELETE FROM entries; DELETE FROM customers; DELETE FROM settings;")
        conn.execute("INSERT INTO settings VALUES ('shop_name', 'Sharma General Store')")
        people = {
            "Ramesh": ("hi-IN", "9876543210"), "Lakshmi": ("ta-IN", "9845012345"),
            "Sukhwinder": ("pa-IN", "9814098140"), "Anjali": ("bn-IN", "9830098300"),
            "Prakash": ("mr-IN", "9822098220"), "Abdul": ("hi-IN", "9811198111"),
        }
        ids = {}
        for name, (lang, phone) in people.items():
            ids[name] = conn.execute(
                "INSERT INTO customers (name, phone, language, created_at) VALUES (?, ?, ?, ?)",
                (name, phone, lang, now())).lastrowid
        base = datetime.now().replace(hour=9, minute=0, second=0)
        history = [
            (6, "purchase", None, "Gupta Traders", 8400, "atta 50kg, chini 25kg, tel 15L"),
            (6, "credit", "Ramesh", None, 650, "atta, dal, sabun"),
            (5, "credit", "Lakshmi", None, 1200, "rice 10kg, oil"),
            (5, "sale", None, None, 3150, ""),
            (4, "credit", "Sukhwinder", None, 480, "doodh, bread, ande"),
            (4, "credit", "Ramesh", None, 420, "chai patti, biscuit"),
            (3, "payment", "Lakshmi", None, 500, ""),
            (3, "credit", "Anjali", None, 900, "masala, chawal"),
            (3, "expense", None, "Bijli bill", 1800, ""),
            (2, "sale", None, None, 2780, ""),
            (2, "credit", "Prakash", None, 350, "poha, shakkar"),
            (2, "credit", "Abdul", None, 760, "tel, sabun, toothpaste"),
            (1, "payment", "Sukhwinder", None, 480, ""),
            (1, "credit", "Lakshmi", None, 540, "dal, sugar"),
            (1, "sale", None, None, 3420, ""),
            (0, "sale", None, None, 1240, ""),
            (0, "credit", "Ramesh", None, 180, "Maggi, atta"),
        ]
        for i, (days, kind, cust, party, amount, items) in enumerate(history):
            when = (base - timedelta(days=days) + timedelta(minutes=37 * i)).isoformat(timespec="seconds")
            customer = {"id": ids[cust], "name": cust} if cust else None
            add_entry(conn, kind, amount, customer=customer, party=party, items=items, source="demo",
                      created_at=when)
        return snapshot(conn)
