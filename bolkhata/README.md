# BolKhata · बोलखाता

**Bolo, khata likh jaayega. Speak, and it's recorded.**

**Live demo: https://bolkhata-hackathon.vercel.app** (works on phones; tap the mic and speak)

A voice-first ledger (bahi-khata) for India's ~63M small shops. The shopkeeper never types: they say what happened in whatever language they think in, and BolKhata records it, reads it back, answers questions about the books and writes payment reminders in each customer's own language.

## The problem
Kirana owners extend credit (udhaar) all day but track it in a paper notebook. Digital khata apps exist, but they need typing, and the shopkeeper is serving customers with both hands busy. Udhaar gets forgotten, reminders are awkward, and supplier bills pile up unrecorded.

## What it does
| Shopkeeper does | BolKhata does | Sarvam |
|---|---|---|
| Says *"Ramesh ko teen packet Maggi aur do kilo atta diya, 340 udhaar"* | Records the entry, replies *"Ramesh's total due is now ₹1,590"* in the same language | Saaras (speech + language detection), Sarvam-105B, Bulbul |
| Speaks Tamil, Bengali or Hinglish, mid-sentence switches included | Auto-detects the language and answers in it; no settings | Saaras / Language ID |
| *"Sabse zyada udhaar kiska hai? Aaj kitni bikri hui?"* | Answers from the ledger, out loud | Sarvam-105B, Bulbul |
| *"Lakshmi ko reminder bhejo"* (in Hindi) | Writes a warm reminder **in Lakshmi's language (Tamil)**, plays it, opens WhatsApp | Sarvam-105B, Bulbul |
| Photographs a handwritten supplier parchi | Reads every line item and records the purchase | Sarvam Vision |
| *"Galat ho gaya, cancel karo"* | Undoes the last entry | Sarvam-105B |

Every entry keeps its evidence: the original voice note or bill photo can be replayed from the khata.

## Run it
```bash
pip install -r ../requirements.txt
python3 -m uvicorn server:app --port 8001   # reads SARVAM_API_KEY from .env here or in the parent folder
```
Open http://localhost:8001 and tap **Load demo shop**. The mic works on `localhost` or HTTPS; for a phone demo use `ngrok http 8001`.

## 2-minute demo script
1. **Hook (15s):** "63 million kirana stores run on a notebook. Udhaar is their biggest risk, and they're too busy to type."
2. **Speak an entry (20s):** Tap 🎙, say in Hinglish: *"Ramesh ko 500 ka saaman udhaar diya."* It answers in Hindi with Ramesh's new total.
3. **Switch language (20s):** Say in Tamil: *"Lakshmi akka 500 rupees thiruppi koduthaanga"* ("Lakshmi akka paid back ₹500"). A Tamil reply comes back, with no setting changed.
4. **Ask (15s):** *"Sabse zyada udhaar kiska hai?"* ("Who owes me the most?") gets a spoken answer.
5. **Remind (20s):** *"Lakshmi ko reminder bhejo"* ("Send Lakshmi a reminder"), asked in Hindi. The message is written in **Tamil** for Lakshmi; play it, then tap WhatsApp.
6. **Parchi (20s):** Parchi tab, then the sample handwritten bill: 5 items and the ₹1,831 total land in the khata in ~2s.
7. **Close (10s):** "Zero typing, any language. And this ledger is the shop's cash-flow history: the first step to credit for millions of unbanked businesses."

## Architecture
- `server.py`: FastAPI + SQLite. `/api/command` does speech or text → language detection → Sarvam-105B intent JSON → ledger action → reply in the detected language. Plus `/api/parchi`, `/api/remind/{id}`, `/api/speak`, `/api/state`, `/api/demo`.
- `static/index.html`: single-page mobile UI (Bolo / Khata / Parchi).
- `data/`: the SQLite ledger and evidence files (git-ignored).

Amounts are always computed by the server, never by the model; the model only extracts entries and phrases replies.
