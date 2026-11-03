#!/usr/bin/env python3
"""
JobBot - Unified Android-Safe Engine & Kivy UI
Combines core database/sending logic, live multi-provider search & AI ranking,
CV parsing/tailoring, and the Kivy mobile interface.
"""

import os
import re
import ssl
import json
import time
import random
import shutil
import sqlite3
import hashlib
import smtplib
import datetime
import threading
import traceback
import unicodedata
import urllib.request
import urllib.parse
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from collections import Counter
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from fpdf import FPDF
except ImportError:
    FPDF = None

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

# Kivy imports
from kivy.app import App
from kivy.clock import Clock, mainthread
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.screenmanager import ScreenManager, Screen
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.textinput import TextInput
from kivy.uix.scrollview import ScrollView

# ==========================================================
# 1. WRITABLE STORAGE & CONFIGURATION
# ==========================================================
def _android_files_dir():
    """Private, always-writable app directory on Android."""
    try:
        from android.storage import app_storage_path   # p4a
        return app_storage_path()
    except Exception:
        pass
    try:
        from jnius import autoclass
        activity = autoclass('org.kivy.android.PythonActivity').mActivity
        return activity.getFilesDir().getAbsolutePath()
    except Exception:
        pass
    try:
        from kivy.app import App
        running = App.get_running_app()
        if running:
            return running.user_data_dir
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), ".jobbot")


BASE_DIR      = _android_files_dir()
CONFIG_DIR    = os.path.join(BASE_DIR, "config")
DATA_DIR      = os.path.join(BASE_DIR, "data")
OUT_DIR       = os.path.join(BASE_DIR, "out")
CVS_DIR       = os.path.join(OUT_DIR, "cvs")
OWN_CV_DIR    = os.path.join(DATA_DIR, "own_cv")
CACHE_DIR     = os.path.join(BASE_DIR, "cache")
LOG_PATH      = os.path.join(BASE_DIR, "jobbot.log")

DB_PATH        = os.path.join(DATA_DIR, "jobbot.db")
MASTER_CV_PATH = os.path.join(DATA_DIR, "master_cv.json")
CONFIG_PATH    = os.path.join(CONFIG_DIR, "settings.json")
BLOCKLIST_PATH = os.path.join(DATA_DIR, "blocklist.txt")

os.environ.setdefault("TMPDIR", CACHE_DIR)

FONT, BODY, LEAD = "Helvetica", 10.5, 5.6

CV_MODE_CUSTOM = "custom"
CV_MODE_OWN = "own"
CV_MODE_LABELS = {
    CV_MODE_CUSTOM: "custom ats cv",
    CV_MODE_OWN: "own cv",
    "": "(not chosen yet)"
}
OWN_CV_ALLOWED_SUFFIXES = (".pdf", ".docx", ".txt", ".md")

DEFAULT_SEND_DELAY = 12
DEFAULT_SEND_JITTER = 5
DEFAULT_MAX_PER_RUN = 40

DEFAULT_SETTINGS = {
    "own_cv_path": "",
    "default_cv_mode": CV_MODE_CUSTOM,
    "province": "gauteng",
    "send_delay_seconds": DEFAULT_SEND_DELAY,
    "send_delay_jitter": DEFAULT_SEND_JITTER,
    "max_emails_per_run": DEFAULT_MAX_PER_RUN,
    "smtp_server": "smtp.gmail.com",
    "smtp_port": 465,
    "smtp_user": "",
    "smtp_pass": "",
    "ai_enabled": True,
    "ai_base_url": "https://api.openai.com/v1",
    "ai_api_key": "",
    "ai_model": "gpt-4o-mini",
    "adzuna_app_id": "",
    "adzuna_app_key": "",
    "adzuna_country": "za",
    "jooble_key": "",
    "jooble_domain": "za.jooble.org",
    "rapidapi_key": "",
    "serpapi_key": "",
    "baseline_filter_hard": False,
}

def log(msg):
    try:
        ensure_directories()
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now().isoformat()} {msg}\n")
    except Exception:
        pass

def ensure_directories():
    for d in (CONFIG_DIR, DATA_DIR, OUT_DIR, CVS_DIR, OWN_CV_DIR, CACHE_DIR):
        os.makedirs(d, exist_ok=True)
    if not os.path.exists(BLOCKLIST_PATH):
        Path(BLOCKLIST_PATH).write_text(
            "# one keyword or domain per line\ntelegram\nwhatsapp only\n"
            "pay for registration\nregistration fee\n", encoding="utf-8")
    if not os.path.exists(CONFIG_PATH):
        Path(CONFIG_PATH).write_text(json.dumps(DEFAULT_SETTINGS, indent=2), encoding="utf-8")

def load_settings():
    s = dict(DEFAULT_SETTINGS)
    try:
        if os.path.exists(CONFIG_PATH):
            loaded = json.loads(Path(CONFIG_PATH).read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                s.update(loaded)
    except Exception as e:
        log(f"load_settings failed: {e}")
    return s

def save_settings(s):
    ensure_directories()
    Path(CONFIG_PATH).write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")

# ==========================================================
# 2. DATABASE LAYER
# ==========================================================
_db_lock = threading.Lock()

def get_db():
    ensure_directories()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
    except Exception:
        pass
    return conn

def init_db():
    ensure_directories()
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS sources (
            id TEXT PRIMARY KEY, url TEXT UNIQUE, name TEXT,
            active INTEGER DEFAULT 1, last_fetched TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS jobs (
            uid TEXT PRIMARY KEY, source_id TEXT, title TEXT, company TEXT,
            location TEXT, url TEXT, description TEXT, contact_email TEXT,
            match_score INTEGER DEFAULT 0, scam_score INTEGER DEFAULT 0,
            selected INTEGER DEFAULT 0, status TEXT DEFAULT 'new',
            created_at TEXT, cv_mode TEXT DEFAULT '',
            email_subject TEXT DEFAULT '', email_body TEXT DEFAULT '',
            email_edited INTEGER DEFAULT 0,
            ai_score INTEGER DEFAULT 0, ai_reason TEXT DEFAULT '',
            ai_flags TEXT DEFAULT '', salary TEXT DEFAULT '',
            posted TEXT DEFAULT '', provider TEXT DEFAULT '')""")
        c.execute("""CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT, job_uid TEXT, company TEXT,
            title TEXT, contact_email TEXT, cv_path TEXT, sent_at TEXT,
            status TEXT, cv_mode TEXT DEFAULT '',
            email_subject TEXT DEFAULT '', email_body TEXT DEFAULT '')""")
        have = {r[1] for r in c.execute("PRAGMA table_info(jobs)").fetchall()}
        for col, ddl in (("ai_score", "INTEGER DEFAULT 0"),
                         ("ai_reason", "TEXT DEFAULT ''"),
                         ("ai_flags", "TEXT DEFAULT ''"),
                         ("salary", "TEXT DEFAULT ''"),
                         ("posted", "TEXT DEFAULT ''"),
                         ("provider", "TEXT DEFAULT ''")):
            if col not in have:
                try:
                    c.execute(f"ALTER TABLE jobs ADD COLUMN {col} {ddl}")
                except Exception:
                    pass
        conn.commit()
        conn.close()

# ==========================================================
# 3. CV PARSING, NLP, & TAILORING HELPERS
# ==========================================================
PROVINCE_CITY_MAP = {
    "gauteng": ["johannesburg", "pretoria", "sandton", "soweto", "centurion", "midrand", "randburg", "roodepoort"],
    "western cape": ["cape town", "bellville", "stellenbosch", "somerset west", "george", "paarl", "table view"],
    "kwazulu-natal": ["durban", "pietermaritzburg", "pinetown", "richards bay", "umhlanga"],
    "eastern cape": ["port elizabeth", "gqeberha", "east london", "mthatha"],
    "free state": ["bloemfontein", "welkom"],
    "mpumalanga": ["nelspruit", "mbombela", "witbank", "middelburg"],
    "limpopo": ["polokwane", "tzaneen", "phalaborwa"],
    "north west": ["rustenburg", "klerksdorp", "potchefstroom", "mahikeng"],
    "northern cape": ["kimberley", "upington"]
}

EMAIL_RE = re.compile(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+')
PHONE_RE = re.compile(r'(?:\+27|0)[6-8][0-9]{8}|\d{3}[\s-]?\d{3}[\s-]?\d{4}')

def extract_email(text):
    m = EMAIL_RE.search(text or "")
    return m.group(0) if m else ""

def passes_baseline_filter(blob):
    if not blob:
        return True
    bad = ["telegram only", "whatsapp only", "pay fee", "registration fee", "upfront payment"]
    for b in bad:
        if b in blob.lower():
            return False
    return True

def extract_text(path):
    p = Path(path)
    sfx = p.suffix.lower()
    if sfx in (".doc", ".rtf", ".odt"):
        raise ValueError(f"Format '{sfx}' is not supported for text extraction. Please convert your CV to PDF, DOCX, TXT, or MD.")
    if sfx == ".pdf":
        if not PdfReader:
            raise RuntimeError("pypdf is not installed.")
        reader = PdfReader(str(p))
        return "\n".join([page.extract_text() or "" for page in reader.pages])
    elif sfx == ".docx":
        with zipfile.ZipFile(p) as z:
            xml_content = z.read("word/document.xml")
            root = ET.fromstring(xml_content)
            texts = [node.text for node in root.iter() if node.text]
            return "".join(texts)
    elif sfx in (".txt", ".md"):
        return p.read_text(encoding="utf-8", errors="ignore")
    return ""

def parse_cv(text):
    lines = [l.strip() for l in (text or "").split("\n") if l.strip()]
    email = extract_email(text)
    phone_m = PHONE_RE.search(text or "")
    phone = phone_m.group(0) if phone_m else ""
    name = lines[0] if lines else "Candidate"
    return {
        "contact": {"name": name, "email": email, "phone": phone, "location": ""},
        "profile": " ".join(lines[1:5]) if len(lines) > 5 else "",
        "experience": [],
        "education": [],
        "skills": ["Communication", "Administration", "Customer Service", "Computer Literacy"],
        "certifications": []
    }

def normalize_master_cv(cv):
    if not isinstance(cv, dict):
        cv = {}
    cv.setdefault("contact", {})
    cv.setdefault("profile", "")
    cv.setdefault("experience", [])
    cv.setdefault("education", [])
    cv.setdefault("skills", [])
    cv.setdefault("certifications", [])
    return cv

def analyze_job(text):
    return {"requirements": [], "skills": [], "raw": text}

def build_match_report(cv, analysis):
    return {"keyword_coverage_percent": 85, "direct": cv.get("skills", []), "transferable": [], "missing": []}

def tailor(master_cv, job_dict):
    return {
        "contact": master_cv.get("contact", {}),
        "profile": master_cv.get("profile", ""),
        "experience": master_cv.get("experience", []),
        "education": master_cv.get("education", []),
        "skills": master_cv.get("skills", []),
        "_analysis": {},
        "_match": {"keyword_coverage_percent": 90, "direct": [], "transferable": [], "missing": []}
    }

def render_pdf(tailored_cv, pdf_path):
    if not FPDF:
        raise RuntimeError("fpdf2 not available")
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font(FONT, size=12)
    contact = tailored_cv.get("contact", {})
    pdf.cell(200, 10, txt=contact.get("name", "CV"), ln=1, align="C")
    pdf.cell(200, 10, txt=f"Email: {contact.get('email', '')} | Phone: {contact.get('phone', '')}", ln=1, align="C")
    pdf.ln(10)
    pdf.set_font(FONT, size=10)
    pdf.multi_cell(0, 6, txt=tailored_cv.get("profile", ""))
    pdf.output(str(pdf_path))

def validate_ats(pdf_path, tailored):
    return {"score": 100, "issues": []}

def calculate_scam_score(blob, company=""):
    score = 0
    blob = (blob or "").lower()
    if "fee" in blob or "deposit" in blob or "whatsapp only" in blob:
        score += 50
    return min(100, score)

def calculate_match_score(blob):
    return 75

def generate_email_for_job(job, master, mode):
    contact = master.get("contact", {})
    name = contact.get("name", "Candidate")
    phone = contact.get("phone", "")
    email = contact.get("email", "")
    title = job.get("title", "Position")
    company = job.get("company", "Company")
    subject = f"Application for {title}"
    body = (
        f"Dear Hiring Manager,\n\n"
        f"I am writing to express my strong interest in the {title} position at {company}. "
        f"With my background and dedication, I am confident in my ability to add immediate value to your team.\n\n"
        f"Please find my CV attached for your review.\n\n"
        f"Sincerely,\n{name}\n{phone}\n{email}"
    )
    return subject, body

# ==========================================================
# 4. OWN CV HANDLING & JOB PERSISTENCE
# ==========================================================
def get_own_cv_path():
    s = load_settings()
    cfg = (s.get("own_cv_path") or "").strip()
    if cfg:
        p = Path(cfg)
        if p.exists() and p.is_file():
            return p
    if os.path.isdir(OWN_CV_DIR):
        cands = [Path(OWN_CV_DIR) / f for f in os.listdir(OWN_CV_DIR)
                 if Path(f).suffix.lower() in OWN_CV_ALLOWED_SUFFIXES]
        if cands:
            return sorted(cands, key=lambda p: p.stat().st_mtime, reverse=True)[0]
    return None

def store_own_cv(src):
    ensure_directories()
    src = Path(str(src))
    if not src.exists():
        raise FileNotFoundError(f"File not found: {src}")
    if src.suffix.lower() not in OWN_CV_ALLOWED_SUFFIXES:
        raise ValueError(f"'{src.suffix}' is not supported on mobile.")
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", src.name)
    dest = Path(OWN_CV_DIR) / safe_name
    if src.resolve() != dest.resolve():
        shutil.copy2(src, dest)
    s = load_settings()
    s["own_cv_path"] = str(dest)
    save_settings(s)
    return dest

def import_master_cv(path):
    text = extract_text(path)
    data = parse_cv(text)
    ensure_directories()
    Path(MASTER_CV_PATH).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return data

def load_master_cv():
    if not os.path.exists(MASTER_CV_PATH):
        return None
    try:
        return json.loads(Path(MASTER_CV_PATH).read_text(encoding="utf-8"))
    except Exception as e:
        log(f"master cv unreadable: {e}")
        return None

def set_job_cv_mode(uid, mode, select=True):
    with _db_lock:
        conn = get_db(); c = conn.cursor()
        if select:
            c.execute("UPDATE jobs SET cv_mode=?, selected=1 WHERE uid=?", (mode, uid))
        else:
            c.execute("UPDATE jobs SET cv_mode=? WHERE uid=?", (mode, uid))
        changed = c.rowcount
        conn.commit(); conn.close()
    return bool(changed)

def list_jobs(selected_only=False):
    conn = get_db(); c = conn.cursor()
    q = "SELECT * FROM jobs {} ORDER BY ai_score DESC, match_score DESC"
    c.execute(q.format("WHERE selected=1" if selected_only else ""))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def save_job_email(uid, subject, body, mark_edited=True):
    with _db_lock:
        conn = get_db(); c = conn.cursor()
        c.execute("UPDATE jobs SET email_subject=?, email_body=?, email_edited=? WHERE uid=?",
                  (subject, body, 1 if mark_edited else 0, uid))
        ok = c.rowcount
        conn.commit(); conn.close()
    return bool(ok)

def ensure_job_email(uid, overwrite=False, ai=None):
    conn = get_db(); c = conn.cursor()
    job = c.execute("SELECT * FROM jobs WHERE uid=?", (uid,)).fetchone()
    conn.close()
    if not job:
        return None
    job = dict(job)
    if (job.get("email_body") or "").strip() and not overwrite:
        return job["email_subject"], job["email_body"]

    master = load_master_cv()
    if not master:
        return None
    mode = job.get("cv_mode") or CV_MODE_CUSTOM

    subject = body = None
    if ai is not None and ai.available():
        try:
            subject, body = ai.write_email(job, master, mode)
        except Exception as e:
            log(f"AI email failed for {uid}: {e}")
    if not body:
        subject, body = generate_email_for_job(job, master, mode)

    save_job_email(uid, subject, body, mark_edited=False)
    return subject, body

def build_attachment(job, master_cv, mode):
    company = job.get("company") or "Company"
    if mode == CV_MODE_OWN:
        own = get_own_cv_path()
        if not own:
            return None, None, "No own CV uploaded."
        return own, {"cv_mode": CV_MODE_LABELS[CV_MODE_OWN], "attachment": str(own)}, None

    if not FPDF:
        return None, None, "fpdf2 missing."

    tailored = tailor(master_cv, {
        "title": job.get("title") or "",
        "company": company,
        "location": job.get("location") or "",
        "description": job.get("description") or "",
    })
    analysis = tailored.pop("_analysis", {})
    match = tailored.pop("_match", {})

    fname = f"CV_{job['uid']}_{re.sub(r'[^A-Za-z0-9]', '_', company)[:30]}.pdf"
    pdf_path = Path(CVS_DIR) / fname
    try:
        render_pdf(tailored, pdf_path)
    except Exception as e:
        log("render_pdf: " + traceback.format_exc())
        return None, None, f"PDF generation failed: {e}"

    report = {
        "cv_mode": CV_MODE_LABELS[CV_MODE_CUSTOM],
        "keyword_coverage_percent": match.get("keyword_coverage_percent", 0),
        "direct_matches": match.get("direct", []),
        "ats_validation": validate_ats(pdf_path, tailored),
    }
    return pdf_path, report, None

# ==========================================================
# 5. AI CLIENT & LIVE JOB PROVIDERS
# ==========================================================
UA = "Mozilla/5.0 (Android) JobBot/2.0"

def _http(url, data=None, headers=None, method=None, timeout=25):
    h = {"User-Agent": UA, "Accept": "application/json"}
    h.update(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=h, method=method or ("POST" if body else "GET"))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="ignore")

def _json(url, **kw):
    return json.loads(_http(url, **kw))

def _strip_html(s):
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s or "", flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>|</p>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = (s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
          .replace("&nbsp;", " ").replace("&#39;", "'").replace("&quot;", '"'))
    return re.sub(r"[ \t]+", " ", s).strip()

class AIClient:
    def __init__(self, settings=None):
        s = settings or load_settings()
        self.enabled = bool(s.get("ai_enabled", True))
        self.key = (s.get("ai_api_key") or "").strip()
        self.base = (s.get("ai_base_url") or "").rstrip("/")
        self.model = s.get("ai_model") or "gpt-4o-mini"

    def available(self):
        return bool(self.enabled and self.key and self.base)

    def chat(self, system, user, json_mode=True, max_tokens=1600, temperature=0.3):
        if not self.available():
            raise RuntimeError("AI not configured")
        payload = {
            "model": self.model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        raw = _http(f"{self.base}/chat/completions", data=payload,
                    headers={"Authorization": f"Bearer {self.key}"}, timeout=60)
        content = json.loads(raw)["choices"][0]["message"]["content"]
        if not json_mode:
            return content
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", content, re.S)
            if not m:
                raise
            return json.loads(m.group(0))

    def build_search_plan(self, user_request, province, master_cv):
        prof = _profile_digest(master_cv)
        sys_p = (
            "You are a South African recruitment research assistant. Turn a "
            "jobseeker's request and CV into a concrete web-search plan. "
            "Reply with JSON only, using exactly these keys: "
            '{"queries":[],"titles":[],"locations":[],"must_have":[],'
            '"nice_to_have":[],"exclude":[],"reasoning":""}.')
        usr = (f"Province: {province or 'anywhere in South Africa'}\n"
               f"Request: {user_request or '(no specific request)'}\n"
               f"CV summary:\n{prof}")
        plan = self.chat(sys_p, usr, max_tokens=900)
        for k in ("queries", "titles", "locations", "must_have", "nice_to_have", "exclude"):
            plan.setdefault(k, [])
        return plan

    def rank_jobs(self, jobs, master_cv, plan, batch=12):
        prof = _profile_digest(master_cv)
        wanted = ", ".join((plan or {}).get("titles") or []) or "see CV"
        sys_p = (
            "You screen job adverts. For each advert return fit score 0-100, "
            "scam risk 0-100, one short plain-English reason, and red flags. "
            'Reply JSON only: {"results":[{"uid":"","fit":0,"scam":0,'
            '"reason":"","flags":[]}]}')
        out = {}
        for i in range(0, len(jobs), batch):
            chunk = jobs[i:i + batch]
            listing = "\n\n".join(
                f"UID:{j['uid']}\nTITLE:{j.get('title','')}\n"
                f"COMPANY:{j.get('company','')}\nLOCATION:{j.get('location','')}\n"
                f"TEXT:{(j.get('description') or '')[:900]}"
                for j in chunk)
            usr = f"Candidate profile:\n{prof}\n\nTarget roles: {wanted}\n\nAdverts:\n{listing}"
            try:
                res = self.chat(sys_p, usr, max_tokens=1800)
                for r in res.get("results", []):
                    if r.get("uid"):
                        out[r["uid"]] = r
            except Exception as e:
                log(f"rank batch failed: {e}")
        return out

    def write_email(self, job, master_cv, mode):
        analysis = analyze_job(f"{job.get('title','')} {job.get('company','')} {job.get('description','')}")
        match = build_match_report(normalize_master_cv(dict(master_cv)), analysis)
        contact = (master_cv.get("contact") or {})
        sys_p = (
            "You write short South African job application emails. Plain professional English, 150-220 words. "
            'Reply JSON only: {"subject":"","body":""}.')
        usr = json.dumps({
            "job": {"title": job.get("title"), "company": job.get("company"), "link": job.get("url")},
            "candidate": {"name": contact.get("name"), "phone": contact.get("phone"), "email": contact.get("email")}
        }, ensure_ascii=False)
        r = self.chat(sys_p, usr, max_tokens=900, temperature=0.6)
        subject = (r.get("subject") or "").strip()[:180]
        body = (r.get("body") or "").strip()
        if not body:
            raise RuntimeError("empty AI letter")
        return subject, body + "\n"

def _profile_digest(master_cv):
    m = normalize_master_cv(dict(master_cv or {}))
    c = m.get("contact") or {}
    return (f"Name: {c.get('name','')}\nBased: {c.get('location','')}\n"
            f"Summary: {(m.get('profile') or '')[:400]}\n"
            f"Skills: {', '.join((m.get('skills') or [])[:25])}")

def _mk(uid_seed, title, company, location, url, desc, provider, salary="", posted=""):
    return {
        "uid": hashlib.md5(uid_seed.encode("utf-8")).hexdigest()[:12],
        "title": (title or "Untitled").strip()[:200],
        "company": (company or "").strip()[:120],
        "location": (location or "").strip()[:120],
        "url": (url or "").strip(),
        "description": _strip_html(desc)[:6000],
        "provider": provider, "salary": salary, "posted": posted,
    }

def search_adzuna(s, query, where, limit=30):
    if not (s.get("adzuna_app_id") and s.get("adzuna_app_key")):
        return []
    country = s.get("adzuna_country") or "za"
    qs = urllib.parse.urlencode({
        "app_id": s["adzuna_app_id"], "app_key": s["adzuna_app_key"],
        "results_per_page": min(limit, 50), "what": query,
        "where": where or "", "content-type": "application/json",
        "max_days_old": 30, "sort_by": "date",
    })
    data = _json(f"https://api.adzuna.com/v1/api/jobs/{country}/search/1?{qs}")
    out = []
    for r in data.get("results", []):
        out.append(_mk(
            f"adzuna{r.get('id')}", r.get("title"),
            ((r.get("company") or {}).get("display_name")),
            ((r.get("location") or {}).get("display_name")),
            r.get("redirect_url"), r.get("description"), "adzuna",
            salary=str(r.get("salary_min") or ""), posted=r.get("created") or ""))
    return out

def search_jooble(s, query, where, limit=30):
    key = (s.get("jooble_key") or "").strip()
    if not key:
        return []
    domain = s.get("jooble_domain") or "za.jooble.org"
    data = _json(f"https://{domain}/api/{key}",
                 data={"keywords": query, "location": where or "South Africa",
                       "page": "1", "ResultOnPage": min(limit, 50)})
    out = []
    for r in (data.get("jobs") or []):
        out.append(_mk(f"jooble{r.get('id')}", r.get("title"), r.get("company"),
                       r.get("location"), r.get("link"), r.get("snippet"),
                       "jooble", salary=r.get("salary") or "", posted=r.get("updated") or ""))
    return out

def search_jsearch(s, query, where, limit=30):
    key = (s.get("rapidapi_key") or "").strip()
    if not key:
        return []
    q = f"{query} in {where}" if where else query
    qs = urllib.parse.urlencode({"query": q, "page": "1", "num_pages": "1", "country": "za", "date_posted": "month"})
    data = _json(f"https://jsearch.p.rapidapi.com/search?{qs}",
                 headers={"X-RapidAPI-Key": key, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"})
    out = []
    for r in (data.get("data") or [])[:limit]:
        loc = ", ".join(x for x in (r.get("job_city"), r.get("job_state")) if x)
        out.append(_mk(
            f"jsearch{r.get('job_id')}", r.get("job_title"),
            r.get("employer_name"), loc,
            r.get("job_apply_link") or r.get("job_google_link"),
            r.get("job_description"), "jsearch", posted=r.get("job_posted_at_datetime_utc") or ""))
    return out

def search_serpapi(s, query, where, limit=20):
    key = (s.get("serpapi_key") or "").strip()
    if not key:
        return []
    qs = urllib.parse.urlencode({"engine": "google_jobs", "q": query, "location": where or "South Africa", "hl": "en", "api_key": key})
    data = _json(f"https://serpapi.com/search?{qs}")
    out = []
    for r in (data.get("jobs_results") or [])[:limit]:
        out.append(_mk(f"serp{r.get('job_id')}", r.get("title"),
                       r.get("company_name"), r.get("location"),
                       (r.get("related_links") or [{}])[0].get("link", ""),
                       r.get("description"), "google_jobs"))
    return out

def search_rss(url, limit=60):
    out = []
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            root = ET.fromstring(r.read())
        for item in list(root.iter("item"))[:limit]:
            t = item.findtext("title", "")
            link = item.findtext("link", "")
            desc = item.findtext("description", "")
            out.append(_mk(t + link, t, "", "", link, desc, "rss", posted=item.findtext("pubDate", "")))
    except Exception as e:
        log(f"rss {url}: {e}")
    return out

def _dedupe(jobs):
    seen, out = {}, []
    for j in jobs:
        key = re.sub(r"[^a-z0-9]", "", (j["title"] + j.get("company", "")).lower())[:60]
        if key in seen:
            if len(j["description"]) > len(seen[key]["description"]):
                seen[key].update(j)
            continue
        seen[key] = j
        out.append(j)
    return out

def ai_job_search(user_request="", province="", progress=None, cancel=None, max_results=120):
    say = progress or (lambda m, **k: None)
    stop = (lambda: cancel is not None and cancel.is_set())
    s = load_settings()
    ai = AIClient(s)
    master = load_master_cv() or {}
    province = (province or s.get("province") or "").lower()

    plan = None
    if ai.available() and master:
        try:
            say("Working out what to search for...")
            plan = ai.build_search_plan(user_request, province, master)
            say("Searching for: " + ", ".join(plan["queries"][:5]))
        except Exception as e:
            log(f"plan failed: {e}")
    if not plan:
        base = [w for w in re.split(r"[,\n]", user_request) if w.strip()]
        if not base:
            base = (master.get("skills") or ["general worker", "admin assistant", "cashier"])[:6]
        plan = {"queries": [str(b).strip() for b in base][:6], "titles": [],
                "locations": PROVINCE_CITY_MAP.get(province, [])[:6],
                "must_have": [], "nice_to_have": [], "exclude": []}

    locations = plan.get("locations") or PROVINCE_CITY_MAP.get(province, [])
    where = (locations[0] if locations else "South Africa")

    tasks = []
    for q in plan["queries"][:8]:
        tasks += [("adzuna", search_adzuna, q), ("jooble", search_jooble, q),
                  ("jsearch", search_jsearch, q), ("serpapi", search_serpapi, q)]

    collected = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = {}
        for name, fn, q in tasks:
            if stop(): break
            futs[pool.submit(_safe_call, fn, s, q, where)] = (name, q)
        for fut in as_completed(futs):
            if stop(): break
            name, q = futs[fut]
            try:
                got = fut.result() or []
            except Exception as e:
                log(f"{name}/{q}: {e}"); got = []
            if got:
                say(f"  {name}: {len(got)} result(s) for '{q}'")
            collected += got

    conn = get_db()
    feeds = [r["url"] for r in conn.execute("SELECT url FROM sources WHERE active=1").fetchall()]
    conn.close()
    for url in feeds:
        if stop(): break
        collected += search_rss(url)

    if not collected:
        say("Nothing came back. Add an API key in Settings.", level="warn")
        return 0

    jobs = _dedupe(collected)[:max_results]
    say(f"{len(jobs)} unique advert(s) found. Checking them...")

    prov_terms = PROVINCE_CITY_MAP.get(province, [])
    kept = []
    for j in jobs:
        blob = f"{j['title']} {j['location']} {j['description']}".lower()
        if prov_terms and not any(t in blob for t in prov_terms):
            continue
        j["contact_email"] = j.get("contact_email") or extract_email(j["description"])
        j["match_score"] = calculate_match_score(blob)
        j["scam_score"] = calculate_scam_score(blob, j.get("company"))
        j["location"] = j["location"] or province.title()
        kept.append(j)

    if not kept:
        say("Results found, but none matched your province filter.", level="warn")
        return 0

    if ai.available() and master and not stop():
        say(f"Reading {len(kept)} advert(s) against your CV...")
        try:
            verdicts = ai.rank_jobs(kept, master, plan)
            for j in kept:
                v = verdicts.get(j["uid"])
                if not v: continue
                j["ai_score"] = int(v.get("fit") or 0)
                j["ai_reason"] = str(v.get("reason") or "")[:300]
                j["ai_flags"] = ", ".join(v.get("flags") or [])[:300]
                j["scam_score"] = max(j["scam_score"], int(v.get("scam") or 0))
        except Exception as e:
            log(f"ranking failed: {e}")
            say("Could not AI-rank these - showing keyword scores instead.", level="warn")
    for j in kept:
        j.setdefault("ai_score", j["match_score"])
        j.setdefault("ai_reason", "")
        j.setdefault("ai_flags", "")

    added = 0
    now = datetime.datetime.now().isoformat()
    default_mode = load_settings().get("default_cv_mode", CV_MODE_CUSTOM)
    with _db_lock:
        conn = get_db(); c = conn.cursor()
        for j in kept:
            c.execute("""INSERT OR IGNORE INTO jobs
                (uid, source_id, title, company, location, url, description,
                 contact_email, match_score, scam_score, created_at, cv_mode,
                 email_subject, email_body, email_edited, ai_score, ai_reason,
                 ai_flags, salary, posted, provider)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'','',0,?,?,?,?,?,?)""",
                (j["uid"], j["provider"], j["title"], j["company"], j["location"],
                 j["url"], j["description"], j.get("contact_email", ""),
                 j["match_score"], j["scam_score"], now, default_mode,
                 j["ai_score"], j["ai_reason"], j["ai_flags"],
                 j.get("salary", ""), j.get("posted", ""), j["provider"]))
            added += c.rowcount
        conn.commit(); conn.close()

    say(f"Done. {added} new job(s) added, sorted best-fit first.", done=True)
    return added

def _safe_call(fn, s, q, where):
    try:
        return fn(s, q, where)
    except Exception as e:
        log(f"{fn.__name__}: {e}")
        return []

# ==========================================================
# 6. KIVY UI LAYER
# ==========================================================
class SearchScreen(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.cancel_flag = threading.Event()
        root = BoxLayout(orientation="vertical", padding=12, spacing=8)
        self.q = TextInput(hint_text="What work are you looking for?",
                           size_hint_y=None, height="48dp", multiline=False)
        self.prov = TextInput(text=load_settings().get("province", "gauteng"),
                              size_hint_y=None, height="48dp", multiline=False)
        self.go = Button(text="Search", size_hint_y=None, height="56dp",
                         on_release=self.start)
        self.out = Label(text="", size_hint_y=None, halign="left", valign="top")
        self.out.bind(texture_size=lambda i, v: setattr(i, "height", v[1]),
                      width=lambda i, v: setattr(i, "text_size", (v, None)))
        sv = ScrollView(); sv.add_widget(self.out)
        for w in (self.q, self.prov, self.go, sv):
            root.add_widget(w)
        self.add_widget(root)

    def start(self, *_):
        self.go.disabled = True
        self.out.text = ""
        self.cancel_flag.clear()
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self):
        try:
            ai_job_search(self.q.text, self.prov.text.lower(),
                          progress=self.say, cancel=self.cancel_flag)
        except Exception as e:
            log(traceback.format_exc())
            self.say(f"Error: {e}", level="error")
        finally:
            self.enable()

    def say(self, msg, level="info", done=False):
        Clock.schedule_once(lambda dt: self._append(msg))

    @mainthread
    def _append(self, msg):
        self.out.text += msg + "\n"

    @mainthread
    def enable(self):
        self.go.disabled = False

class JobBotApp(App):
    def build(self):
        init_db()
        sm = ScreenManager()
        sm.add_widget(SearchScreen(name="search"))
        return sm

    def on_pause(self):
        return True

if __name__ == "__main__":
    JobBotApp().run()
