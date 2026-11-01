# Sarvam AI Hackathon projects

| Project | What it is |
|---|---|
| **[BolKhata](bolkhata/)**: main submission · **[Live demo](https://bolkhata-hackathon.vercel.app)** | A voice-first ledger for small shops. Speak entries in any Indian language and get replies and customer reminders in their language. [Read more →](bolkhata/README.md) |
| **Kagaz Saathi**: earlier prototype | Photograph a document and hear it explained in your language (below). |

Both use Sarvam AI: Saaras (speech-to-text and language detection), Sarvam-105B, Bulbul (text-to-speech) and Sarvam Vision.

---

# Kagaz Saathi · कागज़ साथी

**Photograph any document. Hear it explained in your language. Ask questions by voice.**

Millions of Indians receive bills, bank notices, legal letters and hospital papers they can't fully read, and depend on someone else to tell them what they say. Kagaz Saathi reads the document, explains what it means and what to do next in 11 Indian languages, out loud, and answers follow-up questions spoken in any language (including Hinglish).

## How it uses Sarvam AI

| Step | Sarvam API | What it does |
|---|---|---|
| Read | **Sarvam Vision** (Document AI) | OCR of photos/PDFs in 23 languages, in reading order |
| Explain | **Sarvam-105B** (`sarvam-105b-conversations`) | Structured explanation: summary, key points, action, deadline, warning |
| Speak | **Bulbul v4** | Natural voice in the chosen language, streamed sentence by sentence |
| Listen | **Saaras v4** | Transcribes spoken follow-up questions, code-mixed speech included |

A document in one language can be explained in another (e.g. a Hindi bank notice explained in Tamil), and switching language re-explains instantly without reading the document again.

## Run it

```bash
pip install -r requirements.txt
echo "SARVAM_API_KEY=your_key" > .env
python3 -m uvicorn app:app --port 8000
```

Open http://localhost:8000. The microphone works on `localhost` or HTTPS only; to demo from a phone, use a tunnel such as `ngrok http 8000`.

## 2-minute demo script

1. **Problem (15s):** "Sunita Devi got this letter from her bank. It's in formal Hindi full of terms like NPA and SARFAESI. What does it mean for her house?"
2. **Explain (30s):** Pick *हिन्दी*, tap **🏦 Bank notice (Hindi)**. The explanation appears in ~3s and starts speaking. Point at *What to do*, the deadline and the red warning.
3. **Cross-language (20s):** "Her neighbour Murugan in Madurai speaks Tamil." Tap **🏥 Hospital discharge**, switch to *தமிழ்*: the same paper is now explained and spoken in Tamil, with every medicine and when to take it.
4. **Voice Q&A (30s):** Tap 🎤 and ask in Hinglish: *"Sugar kam ho jaye toh kya karna hai?"* ("What should I do if my sugar drops?") It answers in the chosen language, out loud, from the document only.
5. **Under the hood (15s):** Point at the badges: Sarvam Vision → Sarvam-105B → Bulbul, Saaras for questions, with the time each step took.
6. **Close:** "One photo, any Indian language, no reading required."

## Files

- `app.py`: FastAPI backend (`/api/read`, `/api/explain`, `/api/speak`, `/api/ask`)
- `static/index.html`: single-page mobile-first UI
- `samples/`: demo documents (electricity bill, Hindi bank notice, hospital discharge summary)
- `chat.py`: minimal Sarvam API check

Kagaz Saathi helps people understand documents; it is not legal, medical or financial advice.
