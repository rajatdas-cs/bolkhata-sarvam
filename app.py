"""Kagaz Saathi: photograph a document, hear it explained in your language."""

import base64
import json
import os
import re
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sarvamai import SarvamAI
from sarvamai.core.api_error import ApiError

ROOT = Path(__file__).parent

# Load .env into the environment (no extra dependency needed).
env_file = ROOT / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() and not line.lstrip().startswith("#"):
            os.environ.setdefault(key.strip(), value.strip())

API_KEY = os.environ.get("SARVAM_API_KEY")
if not API_KEY:
    raise SystemExit("SARVAM_API_KEY is not set. Add it to .env.")

client = SarvamAI(api_subscription_key=API_KEY, timeout=90)
app = FastAPI(title="Kagaz Saathi")
app.mount("/samples", StaticFiles(directory=ROOT / "samples"), name="samples")

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

EXPLAIN_PROMPT = """You help people who cannot easily read official documents (bills, bank letters, legal notices, medical reports, government forms) understand them.

You will get the text of a document read by OCR. Explain it in {language}, using simple everyday words a person with little schooling would understand. Never invent amounts, dates or names that are not in the document. Be precise about which date or amount applies to what (e.g. a due date vs. a later grace period before a penalty).

Reply ONLY with a JSON object, no markdown:
{{
  "doc_type": "what kind of document this is, 2-5 words",
  "summary": "2-3 short sentences: what this document is and what it means for the person",
  "key_points": [{{"label": "short label", "value": "the exact value from the document"}}],
  "action_needed": "what the person should do next, in one or two sentences",
  "deadline": "the most important date, or null",
  "warning": "any risk or penalty they must know about (late fee, disconnection, legal action, emergency symptoms that need a doctor right away), or null only if the document has none",
  "spoken": "a warm 4-6 sentence explanation to be read aloud, as if talking to an elder. Say amounts and dates in words people speak."
}}
All text values must be in {language}. Keep 3-6 key_points, the most important ones. Exception: if the document lists medicines, include every medicine as its own key point (name, dose and when to take it), even if that makes more than 6."""

ASK_PROMPT = """You help a person understand a document they received. Answer their question in {language} in 1-3 short, simple sentences, as if speaking to them. Use only facts from the document and quote amounts exactly as written; never add amounts together or say one amount includes another unless the document says so. If the answer is not in it, say so and suggest who to ask (the office that sent it). Plain text only, no markdown or lists.

Document text:
{doc_text}"""


class ReadRequest(BaseModel):
    file_b64: str
    mime: str
    filename: str = "document"


class ExplainRequest(BaseModel):
    doc_text: str
    language: str = "hi-IN"


class SpeakRequest(BaseModel):
    text: str
    language: str = "hi-IN"


class AskRequest(BaseModel):
    doc_text: str
    language: str = "hi-IN"
    history: list[dict] = []
    question: str | None = None
    audio_b64: str | None = None
    audio_mime: str | None = None


def with_retry(fn, *args, **kwargs):
    """Retry Sarvam calls on rate limits, server errors and timeouts (busy hackathon APIs)."""
    for attempt in range(3):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            transient = status in (429, 500, 502, 503, 504) or isinstance(exc, httpx.TimeoutException)
            if not transient or attempt == 2:
                raise
            time.sleep(1 + attempt * 2)


def check_language(code: str) -> str:
    if code not in LANGUAGES:
        raise HTTPException(400, "Unsupported language")
    return LANGUAGES[code][0]


def read_document(data: bytes, filename: str, mime: str) -> str:
    """Run Sarvam Vision on an image/PDF and return its text in reading order."""
    job = with_retry(client.doc_ai.parse, file=[(filename, data, mime)])
    deadline = time.time() + 120
    while True:
        status = with_retry(client.doc_ai.get_status, job.job_id).status
        if status == "completed":
            break
        if status in ("failed", "cancelled") or time.time() > deadline:
            raise HTTPException(502, f"Document reading {status}")
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
    lines = []
    for page in pages:
        for block in sorted(page["blocks"], key=lambda b: b["reading_order"]):
            lines.append(block["text"])
    return "\n".join(lines).strip()


def parse_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        raise HTTPException(502, "Could not understand the model's reply")
    return json.loads(match.group(0))


def ms_since(start: float) -> int:
    return round((time.time() - start) * 1000)


@app.exception_handler(ApiError)
def sarvam_error(request, exc: ApiError):
    message = "Sarvam is busy right now. Please try again in a moment." if exc.status_code == 429 else "Something went wrong talking to Sarvam. Please try again."
    return JSONResponse({"detail": message}, status_code=502)


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/languages")
def languages():
    return [{"code": code, "name": name} for code, (name, _) in LANGUAGES.items()]


@app.post("/api/read")
def read(req: ReadRequest):
    start = time.time()
    doc_text = read_document(base64.b64decode(req.file_b64), req.filename, req.mime)
    if not doc_text:
        raise HTTPException(422, "No text found. Try a clearer, well-lit photo.")
    return {"doc_text": doc_text, "ms": ms_since(start)}


@app.post("/api/explain")
def explain(req: ExplainRequest):
    language = check_language(req.language)
    start = time.time()
    # sarvam-105b-conversations answers in seconds; sarvam-105b reasoning was too slow for a live demo.
    reply = with_retry(
        client.chat.completions,
        model="sarvam-105b-conversations",
        max_tokens=2000,
        messages=[
            {"role": "system", "content": EXPLAIN_PROMPT.format(language=language)},
            {"role": "user", "content": req.doc_text},
        ],
    )
    return {"explanation": parse_json(reply.choices[0].message.content), "ms": ms_since(start)}


@app.post("/api/speak")
def speak(req: SpeakRequest):
    check_language(req.language)
    start = time.time()
    voice = LANGUAGES[req.language][1]
    kwargs = {"speaker": voice} if voice else {}
    audio = with_retry(
        client.text_to_speech.convert,
        text=req.text[:2000], language_code=req.language, model="bulbul:v4-flash", **kwargs,
    )
    return {"audio": audio.audios, "ms": ms_since(start)}


@app.post("/api/ask")
def ask(req: AskRequest):
    language = check_language(req.language)
    question = (req.question or "").strip()
    timings = {}
    if req.audio_b64:
        start = time.time()
        mime = req.audio_mime or "audio/webm"
        ext = mime.split("/")[-1].split(";")[0]
        stt = with_retry(
            client.speech_to_text.transcribe,
            file=(f"question.{ext}", base64.b64decode(req.audio_b64), mime),
            model="saaras:v4",
            mode="transcribe",
        )
        question = stt.transcript.strip()
        timings["stt"] = ms_since(start)
    if not question:
        raise HTTPException(422, "Didn't catch that. Please try again.")

    start = time.time()
    messages = [{"role": "system", "content": ASK_PROMPT.format(language=language, doc_text=req.doc_text)}]
    messages += [m for m in req.history[-8:] if m.get("role") in ("user", "assistant")]
    messages.append({"role": "user", "content": question})
    reply = with_retry(client.chat.completions, model="sarvam-105b-conversations", messages=messages)
    timings["llm"] = ms_since(start)
    answer = (reply.choices[0].message.content or "").strip()
    return {"question": question, "answer": answer, "ms": timings}
