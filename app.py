import difflib
import gc
import hashlib
import re
from collections import OrderedDict
from datetime import date, timedelta
from statistics import median

import easyocr
import numpy as np
import pandas as pd
import requests
import streamlit as st
from PIL import Image, ImageOps

# --- MASUKKAN LINK WEB APP GOOGLE SCRIPT ANDA DI SINI ---
WEB_APP_URL = "https://script.google.com/macros/s/AKfycbzpkoQIdq8dPAENFcZ_1kV3iW4_Lvy1f1ww5HKTkpI8J5zfxhHgnvWCBba4x_gYbfrK6Q/exec"

HARI_ID = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Ahad"]
HARI_ALIAS = {
    "senin": 0, "selasa": 1, "rabu": 2, "kamis": 3,
    "jumat": 4, "sabtu": 5, "ahad": 6, "minggu": 6,
}
HARI_KEYS = list(HARI_ALIAS.keys())
BULAN_ID = [
    "januari", "februari", "maret", "april", "mei", "juni",
    "juli", "agustus", "september", "oktober", "november", "desember",
]
BULAN_TITLE = [b.capitalize() for b in BULAN_ID]

TIME_RE = re.compile(
    r"^\s*(\d{1,2})\s*[.:,;]\s*(\d{2})\s*(W[I1lL][BT8]A?)?\s*(.*)$", re.I | re.S
)
RANGE_RE = re.compile(
    r"(\d{1,2})\s*([A-Za-z]{3,10})?\s*[-–—~]?\s*(\d{1,2})\s*([A-Za-z]{3,10})\s*(20\d{2})"
)
MASJID_RE = re.compile(
    r"^\s*(?:masjid|mesjid|musholla|mushola|musala|majelis|gedung|aula|stadion)\b", re.I
)
ALAMAT_RE = re.compile(r"\b(?:Jl\.?|Jln\.?|Jalan|Gg\.?|Gang)\s+[A-Za-z0-9].*", re.I)
PHONE_RE = re.compile(
    r"(?<!\d)(?:\+62|62|0)\s?8\d{1,3}[\s-]?\d{3,4}[\s-]?\d{3,4}(?!\d)"
)
LETTERS_RE = re.compile(r"[^a-z]")
WS_RE = re.compile(r"\s+")
ALNUM_RE = re.compile(r"[A-Za-z0-9]")
STAR_RE = re.compile(r"\*+")


# ============================================================================
#  PEMBERSIH NOISE OCR
# ============================================================================
# Karakter sampah yang sering muncul dari artefak OCR (kurung, ornamen, arab salah baca)
NOISE_EDGE_RE = re.compile(r"^[\{\[\(\|\\/<>~`^_=+*#%&;:\.\-]+|[\{\[\(\|\\/<>~`^_=+*#%&;:\.\-]+$")
# Token sampah pendek di awal yang berakhiran I/1/l/| (mis. "{2u2I", "l2u2l")
NOISE_TOKEN_RE = re.compile(r"^[\{\[\(]?\s*[A-Za-z0-9]{1,5}[I1l|]\s+")
# Hapus karakter kontrol & zero-width
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\u200b-\u200f\u202a-\u202e]")
# Hanya boleh ada 1 spasi
MULTI_WS_RE = re.compile(r"[ \t]+")


def bersihkan_teks(s):
    """Buang karakter sampah hasil OCR di awal/akhir dan spasi berlebih.
    Menangani kasus seperti '{2u2I KITAB AL-USHUL AS-SITTAH' -> 'KITAB AL-USHUL AS-SITTAH'.
    """
    if not s:
        return ""
    s = CTRL_RE.sub("", s)
    s = STAR_RE.sub("", s)
    s = MULTI_WS_RE.sub(" ", s).strip()
    # buang noise di ujung (berulang sampai bersih)
    for _ in range(3):
        new = NOISE_EDGE_RE.sub("", s).strip()
        if new == s:
            break
        s = new
    # buang token sampah di awal, mis. "{2u2I "
    for _ in range(2):
        new = NOISE_TOKEN_RE.sub("", s)
        if new == s:
            break
        s = new
    return s.strip(" ,-|:")


def clean_cell(s):
    return bersihkan_teks(s)


# ============================================================================
#  BAGIAN PARSER (murni Python, tidak butuh Streamlit)
# ============================================================================
def letters_only(s):
    return LETTERS_RE.sub("", s.lower())


def match_hari(text):
    """Return index hari (0=Senin ... 6=Ahad) atau None. Toleran salah baca OCR."""
    t = letters_only(text)
    if len(t) < 3:
        return None
    hit = difflib.get_close_matches(t, HARI_KEYS, n=1, cutoff=0.75)
    return HARI_ALIAS[hit[0]] if hit else None


def match_bulan(text):
    """Return nomor bulan 1-12 atau None. Menerima singkatan (Sept, Okt)."""
    t = letters_only(text)
    if len(t) < 3:
        return None
    hit = difflib.get_close_matches(t, BULAN_ID, n=1, cutoff=0.7)
    if hit:
        return BULAN_ID.index(hit[0]) + 1
    for i, b in enumerate(BULAN_ID):
        if b.startswith(t):
            return i + 1
    return None


def parse_time(text):
    """'15.15 WIB' -> ('15.15 WIB', sisa_teks). None kalau bukan jam."""
    m = TIME_RE.match(text)
    if not m:
        return None
    h, mnt, zone, rest = m.group(1), m.group(2), m.group(3), m.group(4).strip()
    if int(h) > 23 or int(mnt) > 59:
        return None
    zona = "WIB"
    if zone:
        z = zone.upper().replace("1", "I").replace("L", "I")
        if z.endswith("TA"):
            zona = "WITA"
        elif z.endswith("T"):
            zona = "WIT"
    return f"{int(h):02d}.{mnt} {zona}", rest


def parse_range(text):
    """Cari '28 SEPTEMBER - 04 OKTOBER 2026' -> (date_awal, date_akhir)."""
    m = RANGE_RE.search(text)
    if not m:
        return None
    d1, m1, d2, m2, y = m.groups()
    bulan2 = match_bulan(m2)
    if not bulan2:
        return None
    bulan1 = match_bulan(m1) if m1 else None
    if not bulan1:
        bulan1 = bulan2 if int(d1) <= int(d2) else (bulan2 - 1 or 12)
    tahun2 = int(y)
    tahun1 = tahun2 - 1 if bulan1 > bulan2 else tahun2
    try:
        return date(tahun1, bulan1, int(d1)), date(tahun2, bulan2, int(d2))
    except ValueError:
        return None


def build_date_map(rng):
    """Peta weekday -> tanggal untuk periode flyer."""
    out = {}
    if not rng:
        return out
    start, end = rng
    for i in range(min((end - start).days + 1, 14)):
        d = start + timedelta(days=i)
        out.setdefault(d.weekday(), d)
    return out


def fmt_date(d):
    return f"{d.day} {BULAN_TITLE[d.month - 1]} {d.year}"


def to_boxes(results):
    """Ubah hasil EasyOCR (bbox, teks, conf) jadi dict yang mudah diolah."""
    boxes = []
    for item in results:
        bbox, text = item[0], item[1]
        text = text.strip()
        if not ALNUM_RE.search(text):  # buang simbol saja, mis. '**'
            continue
        xs = [float(p[0]) for p in bbox]
        ys = [float(p[1]) for p in bbox]
        x1, x2, y1, y2 = min(xs), max(xs), min(ys), max(ys)
        boxes.append({
            "text": text, "x1": x1, "x2": x2, "y1": y1, "y2": y2,
            "yc": (y1 + y2) / 2, "h": max(y2 - y1, 1.0),
        })
    return boxes


def split_merged_time(boxes):
    """Kalau OCR menggabung '15.15 WIB Ustadz ...' jadi satu kotak, pisahkan lagi."""
    out = []
    for b in boxes:
        m = TIME_RE.match(b["text"])
        parsed = parse_time(b["text"]) if m else None
        if parsed and len(parsed[1]) >= 3:
            cut_idx = m.start(4)
            ratio = cut_idx / max(len(b["text"]), 1)
            cut_x = b["x1"] + (b["x2"] - b["x1"]) * ratio
            out.append({**b, "text": b["text"][:cut_idx].strip(), "x2": cut_x})
            out.append({**b, "text": parsed[1], "x1": cut_x})
        else:
            out.append(b)
    return out


def group_lines(boxes):
    """Kelompokkan kotak teks menjadi baris (urut atas-bawah, kiri-kanan)."""
    lines = []
    for b in sorted(boxes, key=lambda b: b["yc"]):
        if lines and abs(b["yc"] - lines[-1]["yc"]) <= 0.6 * b["h"]:
            lines[-1]["boxes"].append(b)
            lines[-1]["yc"] = float(np.mean([x["yc"] for x in lines[-1]["boxes"]]))
        else:
            lines.append({"yc": b["yc"], "boxes": [b]})
    return [sorted(l["boxes"], key=lambda b: b["x1"]) for l in lines]


def split_cells(cells):
    """Pisahkan sel Ustadz dan sel Judul dalam satu baris berdasar celah horizontal terbesar."""
    if not cells:
        return "", ""
    if len(cells) == 1:
        return cells[0]["text"], ""
    gaps = [(cells[i + 1]["x1"] - cells[i]["x2"], i) for i in range(len(cells) - 1)]
    gap, i = max(gaps)
    h = float(median([c["h"] for c in cells]))
    if gap < 0.6 * h:  # tidak ada pemisah kolom yang jelas
        return " ".join(c["text"] for c in cells), ""
    left = " ".join(c["text"] for c in cells[: i + 1])
    right = " ".join(c["text"] for c in cells[i + 1:])
    return left, right


def find_masjid(boxes, img_h):
    cands = [b for b in boxes if b["yc"] < img_h * 0.3 and MASJID_RE.match(b["text"])]
    if not cands:
        cands = [b for b in boxes if MASJID_RE.match(b["text"])]
    if not cands:
        return ""
    best = max(cands, key=lambda b: b["h"])
    name = clean_cell(best["text"])
    return name.title() if name.isupper() else name


def parse_flyer(results, img_h):
    boxes = split_merged_time(to_boxes(results))
    lines = group_lines(boxes)
    full_text = " ".join(b["text"] for line in lines for b in line)

    rng = parse_range(full_text)
    date_map = build_date_map(rng)
    out = {
        "rows": [],
        "nama_masjid": find_masjid(boxes, img_h),
        "alamat_jalan": "",
        "kontak": "",
        "teks_lengkap": full_text,
        "periode": f"{fmt_date(rng[0])} s/d {fmt_date(rng[1])}" if rng else "",
        "raw": [
            {"teks": b["text"], "x": int(b["x1"]), "y": int(b["yc"]), "tinggi": int(b["h"])}
            for b in sorted(boxes, key=lambda b: (b["yc"], b["x1"]))
        ],
    }

    m = PHONE_RE.search(full_text)
    if m:
        out["kontak"] = m.group(0).strip()

    time_boxes = []
    for b in boxes:
        t = parse_time(b["text"])
        if t and len(t[1]) < 3:
            time_boxes.append((b, t[0]))
    if not time_boxes:
        return out
    time_boxes.sort(key=lambda t: t[0]["yc"])
    time_ids = {id(b) for b, _ in time_boxes}
    time_left = min(b["x1"] for b, _ in time_boxes)
    med_h = float(median([b["h"] for b, _ in time_boxes]))
    y_min = time_boxes[0][0]["yc"] - 3 * med_h
    y_max = time_boxes[-1][0]["yc"] + 3 * med_h

    labels = []
    for b in boxes:
        if b["x2"] > time_left or not (y_min <= b["yc"] <= y_max):
            continue
        idx = match_hari(b["text"])
        is_label = (
            idx is not None
            or match_bulan(b["text"]) is not None
            or re.fullmatch(r"\d{1,2}", b["text"].strip())
        )
        if is_label:
            labels.append((b, idx))
    labels.sort(key=lambda t: t[0]["yc"])
    anchors = [(b["yc"], idx) for b, idx in labels if idx is not None]

    clusters = []
    for i, (y, idx) in enumerate(anchors):
        lo = y - 0.6 * med_h if i else float("-inf")
        hi = anchors[i + 1][0] - 0.6 * med_h if i + 1 < len(anchors) else float("inf")
        members = [b["yc"] for b, _ in labels if lo <= b["yc"] < hi]
        clusters.append((float(np.mean(members)), idx))

    used = set(time_ids)
    for tb, tstr in time_boxes:
        tol = max(0.7 * tb["h"], 6)
        cells = [
            b for b in boxes
            if id(b) not in time_ids
            and b["x1"] >= tb["x2"] - 2
            and abs(b["yc"] - tb["yc"]) <= tol
        ]
        cells.sort(key=lambda b: b["x1"])
        used.update(id(c) for c in cells)
        ustadz, judul = split_cells(cells)

        hari_idx = None
        if clusters:
            hari_idx = min(clusters, key=lambda c: abs(c[0] - tb["yc"]))[1]
        bagian = []
        if hari_idx is not None:
            d = date_map.get(hari_idx)
            bagian.append(f"{HARI_ID[hari_idx]}, {fmt_date(d)}" if d else HARI_ID[hari_idx])
        bagian.append(tstr)

        out["rows"].append({
            "hari_waktu": " - ".join(bagian),
            "nama_ustadz": clean_cell(ustadz),
            "judul_kajian": clean_cell(judul),
        })

    for b in boxes:
        if id(b) in used:
            continue
        am = ALAMAT_RE.search(b["text"])
        if am:
            out["alamat_jalan"] = clean_cell(am.group(0))
            break

    return out


# ============================================================================
#  PARSER POSTER TUNGGAL
# ============================================================================
LABEL_MAP = {
    "kitab": "kitab", "buku": "kitab",
    "materi": "materi", "tema": "materi", "judul": "materi",
    "topik": "materi", "bahasan": "materi", "pembahasan": "materi",
    "pemateri": "pemateri", "pembicara": "pemateri", "narasumber": "pemateri",
    "penceramah": "pemateri", "pengisi": "pemateri",
    "tempat": "tempat", "lokasi": "tempat",
    "waktu": "waktu", "pukul": "waktu", "jam": "waktu",
    "hari": "waktu", "tanggal": "waktu",
}
LABEL_KEYS = list(LABEL_MAP.keys())
MAX_BARIS = {"kitab": 3, "materi": 4, "pemateri": 3, "tempat": 2}

STOP_RE = re.compile(
    r"(https?://|www\.|bit\.ly|youtu|\.com\b"
    r"|^\s*(?:click|klik|subscribe|salurkan|donasi|konfirmasi|rek(?:ening)?\b"
    r"|infaq|infak|transfer|hubungi|contact))",
    re.I,
)
DOA_RE = re.compile(r"\b(?:haf|rah)[a-z]*ull?[aeo]h[a-z'’]*", re.I)
NUMDATE_RE = re.compile(
    r"(?<!\d)(\d{1,2})\s*[./,\-]\s*(\d{1,2})\s*[./,\-]\s*(20\d{2})(?!\d)"
)
TEXTDATE_RE = re.compile(r"(?<!\d)(\d{1,2})\s+([A-Za-z]{3,10})\.?,?\s+(20\d{2})(?!\d)")
TIME_ANY_RE = re.compile(
    r"(?<![\d.:/\-])((?:[01]?\d|2[0-3])\s*[.:]\s*[0-5]\d)(?![.:/]\d)(?!\d)"
    r"(\s*W[I1lL][BT8]A?\b)?",
    re.I,
)
TIME_TAIL_RE = re.compile(
    r"\s*(?:[-–—~]|s/?d|sampai|hingga)\s*"
    r"((?:[01]?\d|2[0-3])\s*[.:]\s*[0-5]\d|selesai)(\s*W[I1lL][BT8]A?\b)?",
    re.I,
)
SHOLAT_RE = re.compile(
    r"\b(ba['’`]?\s?da|bakda|ba['’`]?dha|setelah|sesudah|usai|selepas|menjelang|sebelum)"
    r"\s+(?:sholat\s+|shalat\s+|salat\s+)?"
    r"(subuh|shubuh|subh|dzuhur|zuhur|dhuhur|zhuhur|ashar|asar|maghrib|magrib|isya|isyak|isha)\b",
    re.I,
)
SHOLAT_ID = {
    "subuh": "Subuh", "shubuh": "Subuh", "subh": "Subuh",
    "dzuhur": "Dzuhur", "zuhur": "Dzuhur", "dhuhur": "Dzuhur", "zhuhur": "Dzuhur",
    "ashar": "Ashar", "asar": "Ashar",
    "maghrib": "Maghrib", "magrib": "Maghrib",
    "isya": "Isya", "isyak": "Isya", "isha": "Isya",
}
MASJID_ANY_RE = re.compile(
    r"\b(?:masjid|mesjid|musholla|mushola|musala|surau|majelis|aula)\b[^,\n]*", re.I
)
MASJID_DONASI_RE = re.compile(
    r"\b((?:masjid|mesjid)\s+[A-Za-z'’\- ]+?)(?=\s*(?:,|\bke\b|\brek\b|\(|\d|$))", re.I
)
USTADZ_RE = re.compile(
    r"^\s*(?:ustadz|ustaz|ustadzah|ust\.?|kyai|kiai|kh\.?|dr\.?|syaikh|syekh|habib|buya|abuya)\b",
    re.I,
)
QUOTE_RE = re.compile(r"[\"“”„]\s*(.{5,}?)\s*[\"“”„]")
QUOTE_STRIP = "\"“”„'‘’` "
LABEL_SPLIT_RE = re.compile(r"^\s*([A-Za-z' ]{3,16}?)\s*[:：;]\s*(.*)$", re.S)
WORD_RE = re.compile(r"[A-Za-z]+")
WORD4_RE = re.compile(r"[A-Za-z'’`]{4,}")
PUKUL_RE = re.compile(r"\b(?:pukul|pkl|jam|waktu)\b", re.I)
BUKAN_JUDUL_RE = re.compile(
    r"(?i)\b(?:ustadz|ustaz|bersama|masjid|mesjid|jalan|jl\.?|grogol|depok|wib|wita|wit"
    r"|ba['’`]?da|sebelum|sesudah|sholat|shalat|kajian|info)\b"
)


def fix_ocr_digits(s):
    s = re.sub(r"(?<=\d)[OoQ](?=[\d./\-:\s]|$)", "0", s)
    s = re.sub(r"(?<=[\d./])[Il|](?=\d)", "1", s)
    return s


def cap_words(s):
    return " ".join(w[:1].upper() + w[1:] for w in s.split())


def poster_lines(boxes):
    out = []
    for cells in group_lines(boxes):
        txt = bersihkan_teks(" ".join(c["text"] for c in cells))
        if txt:
            out.append({
                "text": txt,
                "y": float(np.mean([c["yc"] for c in cells])),
                "h": float(max(c["h"] for c in cells)),
            })
    return out


def poster_label(text):
    m = LABEL_SPLIT_RE.match(text)
    if m:
        words = WORD_RE.findall(m.group(1))
        if words:
            hit = difflib.get_close_matches(words[0].lower(), LABEL_KEYS, n=1, cutoff=0.8)
            if hit:
                return LABEL_MAP[hit[0]], m.group(2).strip(), True
        return "?", m.group(2).strip(), False
    t = letters_only(text)
    if 3 <= len(t) <= 12:
        hit = difflib.get_close_matches(t, LABEL_KEYS, n=1, cutoff=0.85)
        if hit:
            return LABEL_MAP[hit[0]], "", False
    return None, text, False


def find_date(text):
    t = fix_ocr_digits(text)
    for m in NUMDATE_RE.finditer(t):
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            continue
    for m in TEXTDATE_RE.finditer(t):
        bln = match_bulan(m.group(2))
        if bln:
            try:
                return date(int(m.group(3)), bln, int(m.group(1)))
            except ValueError:
                continue
    return None


def find_hari_in(text):
    for w in WORD4_RE.findall(text):
        idx = match_hari(w)
        if idx is not None:
            return idx
    return None


def find_time(text):
    t = fix_ocr_digits(text)
    m = TIME_ANY_RE.search(t)
    if not m:
        return None
    p1 = parse_time(m.group(0))
    if not p1:
        return None
    jam1, zona = p1[0].split(" ")
    r = TIME_TAIL_RE.match(t[m.end():])
    if r:
        akhir = r.group(1)
        if akhir.lower() == "selesai":
            return f"{jam1} - Selesai {zona}"
        p2 = parse_time(akhir + (r.group(2) or ""))
        if p2:
            jam2, zona2 = p2[0].split(" ")
            return f"{jam1} - {jam2} {zona2 if r.group(2) else zona}"
    return f"{jam1} {zona}"


def find_sholat(text):
    m = SHOLAT_RE.search(text)
    if not m:
        return None
    kata = "Sebelum" if m.group(1).lower() in ("sebelum", "menjelang") else "Ba'da"
    return f"{kata} {SHOLAT_ID.get(m.group(2).lower(), m.group(2).title())}"


def parse_poster(results, img_h):
    boxes = to_boxes(results)
    lines = poster_lines(boxes)
    full_text = " ".join(l["text"] for l in lines)

    fields = {"kitab": [], "materi": [], "pemateri": [], "tempat": []}
    free, stop_lines = [], []
    cur, prev_y, hits, kuat = None, 0.0, 0, 0

    for ln in lines:
        t = ln["text"]
        if STOP_RE.search(t):
            stop_lines.append(t)
            cur = None
            continue
        key, rest, strong = poster_label(t)
        if key is not None:
            if key != "?":
                hits += 1
                kuat += int(strong)
            cur = key if key in fields else None
            prev_y = ln["y"]
            if rest:
                if cur:
                    fields[cur].append(rest)
                else:
                    free.append({"text": rest, "y": ln["y"], "h": ln["h"]})
            continue
        if cur and len(fields[cur]) < MAX_BARIS[cur] and ln["y"] - prev_y <= 2.8 * ln["h"]:
            fields[cur].append(t)
            prev_y = ln["y"]
        else:
            cur = None
            free.append({"text": t, "y": ln["y"], "h": ln["h"]})

    # --- Pemateri ---
    ustadz = ""
    for s in fields["pemateri"]:
        s = clean_cell(DOA_RE.sub("", s))
        if not s:
            continue
        ustadz = f"{ustadz} & {s}" if ustadz and USTADZ_RE.match(s) else f"{ustadz} {s}".strip()
    if not ustadz:
        for l in free:
            if USTADZ_RE.match(l["text"]):
                ustadz = clean_cell(DOA_RE.sub("", l["text"]))
                break

    # --- Materi ---
    materi = clean_cell(" ".join(fields["materi"]).strip(QUOTE_STRIP))
    if not materi:
        for l in free:
            q = QUOTE_RE.search(l["text"])
            if q:
                materi = clean_cell(q.group(1))
                break

    # --- Fallback judul dari teks terbesar ---
    if not materi and free:
        kandidat = [
            l for l in free
            if len(l["text"]) > 4
            and not BUKAN_JUDUL_RE.search(l["text"])
            and not STOP_RE.search(l["text"])
            and not ALAMAT_RE.search(l["text"])
            and not NUMDATE_RE.search(l["text"])
            and not TEXTDATE_RE.search(l["text"])
        ]
        if kandidat:
            h_max = max(k["h"] for k in kandidat)
            judul_lines = sorted(
                (k for k in kandidat if k["h"] >= 0.45 * h_max),
                key=lambda x: x["y"],
            )
            materi = clean_cell(" ".join(k["text"] for k in judul_lines))

    # --- Kitab ---
    kl = [clean_cell(DOA_RE.sub("", s).replace("_", " ")) for s in fields["kitab"]]
    kl = [s for s in kl if s]
    kitab = ""
    if kl:
        kitab = kl[0] + (f" ({' '.join(kl[1:])})" if len(kl) > 1 else "")

    if kitab and materi:
        judul = f"Kitab {kitab} — {materi}"
    elif kitab:
        judul = f"Kitab {kitab}"
    else:
        judul = materi

    # --- Pembersihan akhir judul (buang noise sisa seperti "{2u2I") ---
    judul = bersihkan_teks(judul)

    # --- Tanggal, hari, jam ---
    cari = [l["text"] for l in free] + fields["tempat"]
    tgl, tgl_line = None, ""
    for txt in cari:
        tgl = find_date(txt)
        if tgl:
            tgl_line = txt
            break
    if not tgl:
        tgl = find_date(" ".join(cari))

    hari_idx = find_hari_in(tgl_line) if tgl_line else None
    if hari_idx is None and tgl:
        hari_idx = tgl.weekday()
    if hari_idx is None:
        for txt in cari:
            hari_idx = find_hari_in(txt)
            if hari_idx is not None:
                break

    jam = None
    prioritas = [t for t in cari if PUKUL_RE.search(t)]
    for txt in prioritas + cari:
        jam = find_time(txt)
        if jam:
            break
    sholat = find_sholat(" ".join(cari))
    waktu = (f"{jam} ({sholat})" if jam else sholat) if sholat else jam

    bagian = []
    if hari_idx is not None:
        bagian.append(f"{HARI_ID[hari_idx]}, {fmt_date(tgl)}" if tgl else HARI_ID[hari_idx])
    elif tgl:
        bagian.append(fmt_date(tgl))
    if waktu:
        bagian.append(waktu)

    # --- Masjid ---
    nama_masjid = ""
    cands = [l for l in free + [{"text": s, "y": 0, "h": 0} for s in fields["tempat"]]
             if MASJID_ANY_RE.search(l["text"])]
    if cands:
        atas = [l for l in cands if l["y"] < img_h * 0.4] or cands
        best = max(atas, key=lambda l: l["h"])
        nama_masjid = clean_cell(MASJID_ANY_RE.search(best["text"]).group(0))
        if nama_masjid.isupper():
            nama_masjid = nama_masjid.title()
    if not nama_masjid:
        for s in stop_lines:
            dm = MASJID_DONASI_RE.search(s)
            if dm:
                nama_masjid = cap_words(clean_cell(dm.group(1)))
                break

    alamat = ""
    for l in free:
        am = ALAMAT_RE.search(l["text"])
        if am:
            alamat = clean_cell(am.group(0))
            break

    kontak = ""
    pm = PHONE_RE.search(full_text)
    if pm:
        kontak = pm.group(0).strip()

    out = {
        "rows": [],
        "nama_masjid": nama_masjid,
        "alamat_jalan": alamat,
        "kontak": kontak,
        "teks_lengkap": full_text,
        "periode": fmt_date(tgl) if tgl else "",
        "label_hits": hits,
        "label_kuat": kuat,
        "raw": [
            {"teks": b["text"], "x": int(b["x1"]), "y": int(b["yc"]), "tinggi": int(b["h"])}
            for b in sorted(boxes, key=lambda b: (b["yc"], b["x1"]))
        ],
    }
    if ustadz or judul:
        out["rows"].append({
            "hari_waktu": " - ".join(bagian),
            "nama_ustadz": ustadz,
            "judul_kajian": judul,
        })
    return out


def _lengkapi(poster, tabel):
    """Isi kolom kosong hasil poster dengan hasil parser tabel."""
    for k in ("nama_masjid", "alamat_jalan", "kontak", "periode"):
        if not poster.get(k) and tabel.get(k):
            poster[k] = tabel[k]
    return poster


def parse_semua(results, img_h):
    """Pilih parser yang cocok. Tabel mingguan -> parse_flyer;
    poster satu sesi berlabel Kitab/Materi/Pemateri -> parse_poster."""
    poster = parse_poster(results, img_h)
    poster_kuat = poster["rows"] and poster["label_kuat"] >= 2

    if poster_kuat:
        if all(poster.get(k) for k in ("nama_masjid", "alamat_jalan", "kontak", "periode")):
            return poster
        try:
            return _lengkapi(poster, parse_flyer(results, img_h))
        except Exception:
            return poster

    tabel = parse_flyer(results, img_h)
    lengkap = [r for r in tabel["rows"] if r["nama_ustadz"] and r["judul_kajian"]]
    if len(lengkap) >= 2:
        return tabel
    if poster["rows"] and (poster["label_hits"] >= 1 or not lengkap):
        return _lengkapi(poster, tabel)
    return tabel


# ============================================================================
#  BAGIAN TAMPILAN STREAMLIT
# ============================================================================
@st.cache_resource
def load_ocr():
    return easyocr.Reader(["id", "en"], gpu=False)


def siapkan_gambar(image, min_width=1400, max_width=2200):
    """Samakan ukuran gambar supaya OCR stabil."""
    if min_width <= image.width <= max_width:
        return image
    target = min_width if image.width < min_width else max_width
    r = target / image.width
    return image.resize((int(image.width * r), int(image.height * r)), Image.LANCZOS)


def buat_thumbnail(image, sisi=700):
    t = image.copy()
    t.thumbnail((sisi, sisi))
    return t


def _jumlah_huruf(hasil):
    return sum(len(ALNUM_RE.findall(r[1])) for r in hasil)


def baca_ocr(reader, image):
    """OCR dengan cache hasil (gambar sama tidak di-OCR dua kali)."""
    kunci = hashlib.md5(image.tobytes()).hexdigest()
    cache = st.session_state.setdefault("_ocr_cache", OrderedDict())
    if kunci in cache:
        cache.move_to_end(kunci)
        return cache[kunci]

    hasil = reader.readtext(np.array(image), detail=1, paragraph=False, batch_size=4)
    if len(hasil) < 6:
        gray = ImageOps.autocontrast(ImageOps.grayscale(image), cutoff=2)
        hasil2 = reader.readtext(np.array(gray), detail=1, paragraph=False, batch_size=4)
        if _jumlah_huruf(hasil2) > _jumlah_huruf(hasil):
            hasil = hasil2
        del gray
    gc.collect()

    cache[kunci] = hasil
    while len(cache) > 3:
        cache.popitem(last=False)
    return hasil


KOLOM = ["hari_waktu", "nama_ustadz", "judul_kajian", "sumber"]

st.set_page_config(page_title="Ekstraktor Jadwal Kajian", layout="centered")
st.title("🕌 Auto-Input Jadwal Kajian ke Google Sheets")
st.caption("Membaca flyer jadwal (tabel mingguan maupun poster tunggal) dan membuat satu baris per sesi kajian.")

reader = load_ocr()

uploaded_files = st.file_uploader(
    "Upload Flyer Kajian (bisa lebih dari satu)",
    type=["jpg", "jpeg", "png"],
    accept_multiple_files=True,
)

if uploaded_files:
    # simpan byte supaya bisa dibaca berulang
    for f in uploaded_files:
        f.seek(0)

    # tampilkan thumbnail grid
    cols = st.columns(min(len(uploaded_files), 4))
    for i, f in enumerate(uploaded_files):
        f.seek(0)
        img = ImageOps.exif_transpose(Image.open(f)).convert("RGB")
        with cols[i % len(cols)]:
            st.image(buat_thumbnail(img), caption=f.name, use_container_width=True)

    if st.button("🔍 Ekstrak Teks dari Semua Gambar", type="primary"):
        semua_rows = []
        masjid_gabungan = ""
        alamat_gabungan = ""
        kontak_gabungan = ""
        periode_gabungan = ""
        teks_gabungan = []

        progress = st.progress(0.0, text="Memproses...")
        with st.spinner(f"Membaca {len(uploaded_files)} flyer..."):
            for idx, f in enumerate(uploaded_files):
                f.seek(0)
                img = ImageOps.exif_transpose(Image.open(f)).convert("RGB")
                ocr_img = siapkan_gambar(img)
                results = baca_ocr(reader, ocr_img)
                parsed = parse_semua(results, ocr_img.height)

                if not masjid_gabungan and parsed.get("nama_masjid"):
                    masjid_gabungan = parsed["nama_masjid"]
                if not alamat_gabungan and parsed.get("alamat_jalan"):
                    alamat_gabungan = parsed["alamat_jalan"]
                if not kontak_gabungan and parsed.get("kontak"):
                    kontak_gabungan = parsed["kontak"]
                if not periode_gabungan and parsed.get("periode"):
                    periode_gabungan = parsed["periode"]

                for r in parsed.get("rows", []):
                    r["sumber"] = f.name
                    semua_rows.append(r)

                teks_gabungan.append(f"=== {f.name} ===\n{parsed.get('teks_lengkap','')}")
                progress.progress((idx + 1) / len(uploaded_files),
                                  text=f"Selesai {idx + 1}/{len(uploaded_files)}: {f.name}")

        # buang duplikat persis
        seen, rows_unik = set(), []
        for r in semua_rows:
            key = (r.get("hari_waktu", ""), r.get("nama_ustadz", ""), r.get("judul_kajian", ""))
            if key in seen:
                continue
            seen.add(key)
            rows_unik.append(r)

        st.session_state["parsed"] = {
            "rows":         rows_unik,
            "nama_masjid":  masjid_gabungan,
            "alamat_jalan": alamat_gabungan,
            "kontak":       kontak_gabungan,
            "periode":      periode_gabungan,
            "teks_lengkap": "\n\n".join(teks_gabungan),
            "raw":          [],
        }
        st.session_state["run_id"] = st.session_state.get("run_id", 0) + 1
        progress.empty()

# --- FORM EDIT & PRATINJAU SEBELUM DIKIRIM ---
if "parsed" in st.session_state:
    p = st.session_state["parsed"]
    rid = st.session_state.get("run_id", 0)

    st.divider()
    st.subheader("📝 Periksa & Koreksi Data Sebelum Disimpan")
    info = f"Terdeteksi **{len(p['rows'])} sesi kajian**."
    if p["periode"]:
        info += f" Periode: {p['periode']}."
    st.info(info + " Perbaiki jika ada yang kurang pas; baris bisa diedit, ditambah, atau dihapus.")

    with st.form(f"form_kajian_{rid}"):
        col1, col2 = st.columns(2)
        nama_masjid = col1.text_input(
            "Nama Masjid / Tempat", value=p["nama_masjid"], key=f"masjid_{rid}"
        )
        alamat = col2.text_input(
            "Alamat / Jalan", value=p["alamat_jalan"], key=f"alamat_{rid}"
        )
        kontak = col1.text_input(
            "Kontak / No. HP", value=p["kontak"], key=f"kontak_{rid}"
        )
        periode = col2.text_input(
            "Periode", value=p["periode"], key=f"periode_{rid}"
        )

        st.markdown("**Daftar Sesi Kajian**")
        df_edit = pd.DataFrame(p["rows"], columns=KOLOM)
        edited = st.data_editor(
            df_edit,
            num_rows="dynamic",
            use_container_width=True,
            column_config={
                "hari_waktu":   st.column_config.TextColumn("Hari & Waktu", width="medium"),
                "nama_ustadz":  st.column_config.TextColumn("Nama Ustadz", width="medium"),
                "judul_kajian": st.column_config.TextColumn("Judul Kajian", width="large"),
                "sumber":       st.column_config.TextColumn("Sumber", width="small"),
            },
            key=f"editor_{rid}",
        )

        submitted = st.form_submit_button("💾 Kirim ke Google Sheets", type="primary")

    if submitted:
        rows_out = [
            {k: ("" if pd.isna(v) else str(v).strip()) for k, v in r.items()}
            for r in edited.to_dict(orient="records")
        ]
        rows_out = [r for r in rows_out if any(v for k, v in r.items() if k != "sumber")]

        if not rows_out:
            st.warning("Tidak ada baris untuk dikirim.")
        elif not WEB_APP_URL or "script.google.com" not in WEB_APP_URL:
            st.error("WEB_APP_URL belum diisi dengan benar.")
        else:
            payload = {
                "nama_masjid":  nama_masjid.strip(),
                "alamat_jalan": alamat.strip(),
                "kontak":       kontak.strip(),
                "periode":      periode.strip(),
                "rows":         rows_out,
            }
            with st.spinner("Mengirim ke Google Sheets..."):
                try:
                    r = requests.post(WEB_APP_URL, json=payload, timeout=30)
                    if r.ok:
                        st.success(f"✅ {len(rows_out)} sesi berhasil dikirim!")
                        ctype = r.headers.get("content-type", "")
                        if ctype.startswith("application/json"):
                            st.json(r.json())
                        else:
                            st.code(r.text[:500])
                    else:
                        st.error(f"Gagal ({r.status_code}): {r.text[:300]}")
                except requests.exceptions.Timeout:
                    st.error("Timeout: server Google Sheets tidak merespons dalam 30 detik.")
                except requests.exceptions.RequestException as e:
                    st.error(f"Kesalahan jaringan: {e}")

    with st.expander("🔎 Lihat teks mentah hasil OCR"):
        st.text_area("Teks Lengkap", p.get("teks_lengkap", ""), height=160, label_visibility="collapsed")
        if p.get("raw"):
            st.dataframe(
                pd.DataFrame(p["raw"]),
                use_container_width=True,
                hide_index=True,
            )
