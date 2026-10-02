import difflib
import re
from datetime import date, timedelta

import easyocr
import numpy as np
import pandas as pd
import requests
import streamlit as st
from PIL import Image, ImageOps

# --- MASUKKAN LINK WEB APP GOOGLE SCRIPT ANDA DI SINI ---
WEB_APP_URL = "MASUKKAN_URL_WEB_APP_GOOGLE_SCRIPT_ANDA"

HARI_ID = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Ahad"]
HARI_ALIAS = {
    "senin": 0, "selasa": 1, "rabu": 2, "kamis": 3,
    "jumat": 4, "sabtu": 5, "ahad": 6, "minggu": 6,
}
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


# ============================================================================
#  BAGIAN PARSER (murni Python, tidak butuh Streamlit)
# ============================================================================
def letters_only(s):
    return re.sub(r"[^a-z]", "", s.lower())


def match_hari(text):
    """Return index hari (0=Senin ... 6=Ahad) atau None. Toleran salah baca OCR."""
    t = letters_only(text)
    if len(t) < 3:
        return None
    hit = difflib.get_close_matches(t, list(HARI_ALIAS.keys()), n=1, cutoff=0.75)
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


def clean_cell(s):
    s = re.sub(r"\*+", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip(" ,-|:")


def to_boxes(results):
    """Ubah hasil EasyOCR (bbox, teks, conf) jadi dict yang mudah diolah."""
    boxes = []
    for item in results:
        bbox, text = item[0], item[1]
        text = text.strip()
        if not re.search(r"[A-Za-z0-9]", text):  # buang simbol saja, mis. '**'
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
    h = float(np.median([c["h"] for c in cells]))
    if gap < 0.6 * h:  # tidak ada pemisah kolom yang jelas
        return " ".join(c["text"] for c in cells), ""
    left = " ".join(c["text"] for c in cells[: i + 1])
    right = " ".join(c["text"] for c in cells[i + 1:])
    return left, right


def assign_days(row_ys, anchors, clusters):
    """Tentukan index hari untuk tiap baris sesi.

    Cara utama: jumlah blok hari = jumlah nama hari yang terbaca, jadi blok dipisahkan
    di celah vertikal terbesar antar baris. Kalau celahnya tidak cukup jelas, pakai
    cara cadangan: ambil pusat blok label hari terdekat.
    """
    n = len(row_ys)
    if not clusters:
        return [None] * n
    k = len(anchors)
    gaps = [row_ys[i + 1] - row_ys[i] for i in range(n - 1)]
    if k >= 2 and n >= k:
        order = sorted(range(len(gaps)), key=lambda i: gaps[i], reverse=True)
        top, rest = order[: k - 1], order[k - 1:]
        if not rest or min(gaps[i] for i in top) > 1.15 * max(gaps[i] for i in rest):
            cuts, g, result = set(top), 0, []
            for i in range(n):
                result.append(anchors[g][1])
                if i in cuts:
                    g += 1
            return result
    return [min(clusters, key=lambda c: abs(c[0] - y))[1] for y in row_ys]


def find_masjid(boxes, img_h):
    cands = [b for b in boxes if b["yc"] < img_h * 0.3 and MASJID_RE.match(b["text"])]
    if not cands:
        cands = [b for b in boxes if MASJID_RE.match(b["text"])]
    if not cands:
        return ""
    best = max(cands, key=lambda b: b["h"])  # judul besar di header, bukan footer/logo
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

    # --- 1) Cari semua kotak jam: tiap jam = satu sesi kajian ---
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
    med_h = float(np.median([b["h"] for b, _ in time_boxes]))
    y_min = time_boxes[0][0]["yc"] - 3 * med_h
    y_max = time_boxes[-1][0]["yc"] + 3 * med_h

    # --- 2) Kolom kiri: nama hari + bulan + tanggal -> pusat tiap blok hari ---
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

    clusters = []  # (y_tengah_blok, index_hari)
    for i, (y, idx) in enumerate(anchors):
        lo = y - 0.6 * med_h if i else float("-inf")
        hi = anchors[i + 1][0] - 0.6 * med_h if i + 1 < len(anchors) else float("inf")
        members = [b["yc"] for b, _ in labels if lo <= b["yc"] < hi]
        clusters.append((float(np.mean(members)), idx))

    # --- 3) Isi tiap baris sesi: Ustadz (kolom tengah) & Judul (kolom kanan) ---
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

    # --- 4) Alamat jalan (hanya dari teks di luar tabel jadwal) ---
    for b in boxes:
        if id(b) in used:
            continue
        am = ALAMAT_RE.search(b["text"])
        if am:
            out["alamat_jalan"] = clean_cell(am.group(0))
            break

    return out


# ============================================================================
#  BAGIAN TAMPILAN STREAMLIT
# ============================================================================
@st.cache_resource
def load_ocr():
    return easyocr.Reader(["id", "en"], gpu=False)


def siapkan_gambar(image, min_width=1400, max_width=2200):
    """Samakan ukuran gambar supaya OCR stabil (teks kecil diperbesar, foto besar diperkecil)."""
    if image.width < min_width or image.width > max_width:
        target = min_width if image.width < min_width else max_width
        r = target / image.width
        image = image.resize((int(image.width * r), int(image.height * r)), Image.LANCZOS)
    return image


KOLOM = ["hari_waktu", "nama_ustadz", "judul_kajian"]

st.set_page_config(page_title="Ekstraktor Jadwal Kajian", layout="centered")
st.title("🕌 Auto-Input Jadwal Kajian ke Google Sheets")
st.caption("Membaca flyer jadwal (tabel mingguan maupun poster tunggal) dan membuat satu baris per sesi kajian.")

reader = load_ocr()

uploaded_file = st.file_uploader("Upload Flyer Kajian (JPG/PNG)", type=["jpg", "jpeg", "png"])

if uploaded_file is not None:
    image = ImageOps.exif_transpose(Image.open(uploaded_file)).convert("RGB")
    st.image(image, caption="Flyer Kajian", width=350)

    if st.button("🔍 Ekstrak Teks dari Gambar", type="primary"):
        with st.spinner("Membaca teks flyer..."):
            ocr_img = siapkan_gambar(image)
            results = reader.readtext(np.array(ocr_img), detail=1, paragraph=False)
            st.session_state["parsed"] = parse_flyer(results, ocr_img.height)
            st.session_state["run_id"] = st.session_state.get("run_id", 0) + 1

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
        nama_masjid = col1.text_input("Nama Masjid / Tempat", value=p["nama_masjid"], key=f"masjid_{rid}")
        kontak = col2.text_input("Kontak", value=p["kontak"], key=f"kontak_{rid}")
        alamat_jalan = st.text_input(
            "Alamat Jalan", value=p["alamat_jalan"], key=f"alamat_{rid}",
            help="Banyak flyer tidak mencantumkan alamat. Isi manual jika perlu.",
        )

        st.markdown("**Daftar sesi kajian**")
        df = st.data_editor(
            pd.DataFrame(p["rows"], columns=KOLOM),
            num_rows="dynamic",
            hide_index=True,
            key=f"editor_{rid}",
            column_config={
                "hari_waktu": st.column_config.TextColumn("Hari dan Waktu", width="medium"),
                "nama_ustadz": st.column_config.TextColumn("Nama Ustadz", width="large"),
                "judul_kajian": st.column_config.TextColumn("Judul Kajian", width="medium"),
            },
        )
        submit_button = st.form_submit_button("🚀 Simpan Semua ke Google Sheets", type="primary")

    with st.expander("🔧 Hasil OCR mentah (untuk cek jika ada yang salah baca)"):
        st.dataframe(pd.DataFrame(p["raw"]))

    if submit_button:
        rows = df.fillna("").to_dict("records")
        rows = [r for r in rows if str(r["nama_ustadz"]).strip() or str(r["hari_waktu"]).strip()]

        if not rows:
            st.warning("Tidak ada baris untuk disimpan.")
        else:
            sukses, duplikat, gagal = 0, [], []
            bar = st.progress(0.0, text="Mengirim data ke Google Sheets...")
            for i, r in enumerate(rows):
                payload = {
                    "hari_waktu": str(r["hari_waktu"]).strip(),
                    "nama_ustadz": str(r["nama_ustadz"]).strip(),
                    "judul_kajian": str(r["judul_kajian"]).strip(),
                    "nama_masjid": nama_masjid,
                    "alamat_jalan": alamat_jalan,
                    "kontak": kontak,
                    "teks_lengkap": p["teks_lengkap"],
                }
                label = f"{payload['hari_waktu']} — {payload['nama_ustadz']}"
                try:
                    resp = requests.post(WEB_APP_URL, data=payload, timeout=30)
                    hasil = resp.text.strip()
                    if hasil == "SUKSES":
                        sukses += 1
                    elif hasil == "DUPLIKAT":
                        duplikat.append(label)
                    else:
                        gagal.append(f"{label} (respon server: {hasil[:80]})")
                except Exception as e:
                    gagal.append(f"{label} ({e})")
                bar.progress((i + 1) / len(rows), text=f"Mengirim {i + 1}/{len(rows)}...")
            bar.empty()

            if sukses:
                st.success(f"✅ **{sukses} sesi kajian** berhasil masuk ke Google Sheets.")
            if duplikat:
                st.error(f"⚠️ **{len(duplikat)} baris ditolak (duplikat):**\n\n" + "\n".join(f"- {d}" for d in duplikat))
            if gagal:
                st.warning("Beberapa baris gagal dikirim:\n\n" + "\n".join(f"- {g}" for g in gagal))
            if not gagal:
                del st.session_state["parsed"]  # reset form setelah selesai
