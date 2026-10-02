import streamlit as st
import requests
import easyocr
import re
from PIL import Image
import numpy as np

# --- MASUKKAN LINK WEB APP GOOGLE SCRIPT ANDA DI SINI ---
WEB_APP_URL = "https://script.google.com/macros/s/AKfycbzgXQiICBL8NPbpGslk0Vpqi5P5aMixzbDEKS0QCPYCal6vArwSygzRHyYlPA9fW5nf-Q/exec"

@st.cache_resource
def load_ocr():
    return easyocr.Reader(['id', 'en'])

st.set_page_config(page_title="Ekstraktor Jadwal Kajian", layout="centered")
st.title("🕌 Auto-Input Jadwal Kajian ke Google Sheets")
st.caption("Mengekstrak Hari/Waktu, Ustadz, Judul, Tempat, Alamat, dan Kontak dari flyer.")

reader = load_ocr()

uploaded_file = st.file_uploader("Upload Flyer Kajian (JPG/PNG)", type=["jpg", "jpeg", "png"])

if uploaded_file is not None:
    image = Image.open(uploaded_file)
    st.image(image, caption="Flyer Kajian", width=350)
    
    if st.button("🔍 Ekstrak Teks dari Gambar", type="primary"):
        with st.spinner("Membaca teks flyer..."):
            img_array = np.array(image)
            results = reader.readtext(img_array, detail=0)
            extracted_text = " ".join(results)
            
            # --- EKSTRAKSI OTOMATIS BERDASARKAN POLA (REGEX) ---
            
            # 1. Nama Ustadz
            ustadz_match = re.search(r'((?:Ustadz|Ust\.|K\.H\.|Buya|Habib|Prof\.|Dr\.)\s+[A-Za-z\.\s]+)', extracted_text, re.IGNORECASE)
            
            # 2. Hari dan Waktu
            hari_waktu_match = re.search(r'((?:Senin|Selasa|Rabu|Kamis|Jum\'at|Jumat|Sabtu|Ahad|Minggu)[^,\n]*|\d{1,2}\s+(?:Januari|Februari|Maret|April|Mei|Juni|Juli|Agustus|September|Oktober|November|Desember|\d{1,2})\s*\d{0,4}|(?:\d{2}[\.:]\d{2}\s*(?:WIB|WITA|WIT)?))', extracted_text, re.IGNORECASE)
            
            # 3. Nama Masjid / Tempat
            masjid_match = re.search(r'((?:Masjid|Musholla|Musala|Gedung|Majelis|Stadion|Aula)\s+[A-Za-z0-9\s\.-]+)', extracted_text, re.IGNORECASE)
            
            # 4. Alamat Jalan
            alamat_match = re.search(r'((?:Jl\.|Jalan|Gg\.|Gang|Kec\.|Kab\.|Rt|Rw)\s+[A-Za-z0-9\s\.,/-]+)', extracted_text, re.IGNORECASE)
            
            # 5. Nomor Kontak
            kontak_match = re.search(r'(\b(?:08|\+628)[0-9\s-]{8,15}\b)', extracted_text)
            
            # Simpan hasil sementara di session state Streamlit
            st.session_state['parsed_data'] = {
                "hari_waktu": hari_waktu_match.group(1).strip() if hari_waktu_match else "",
                "nama_ustadz": ustadz_match.group(1).strip() if ustadz_match else "",
                "judul_kajian": "",  # Biasanya bervariasi, disiapkan untuk diisi/dikoreksi manual
                "nama_masjid": masjid_match.group(1).strip() if masjid_match else "",
                "alamat_jalan": alamat_match.group(1).strip() if alamat_match else "",
                "kontak": kontak_match.group(1).strip() if kontak_match else "",
                "teks_lengkap": extracted_text
            }

# --- TAMPILAN FORM EDIT & PRATINJAU SEBELUM DIKIRIM ---
if 'parsed_data' in st.session_state:
    st.divider()
    st.subheader("📝 Periksa & Koreksi Data Sebelum Disimpan")
    st.info("Sistem telah mengisi data secara otomatis dari gambar. Silakan perbaiki jika ada yang kurang pas.")

    data = st.session_state['parsed_data']
    
    # Form input untuk 6 kolom sesuai tabel Google Sheets
    with st.form("form_kajian"):
        col1, col2 = st.columns(2)
        
        with col1:
            hari_waktu = st.text_input("Hari dan Waktu", value=data["hari_waktu"])
            nama_ustadz = st.text_input("Nama Ustadz", value=data["nama_ustadz"])
            judul_kajian = st.text_input("Judul Kajian", value=data["judul_kajian"], placeholder="Masukkan tema/judul kajian")
            
        with col2:
            nama_masjid = st.text_input("Nama Masjid / Tempat", value=data["nama_masjid"])
            alamat_jalan = st.text_input("Alamat Jalan", value=data["alamat_jalan"])
            kontak = st.text_input("Kontak", value=data["kontak"])
            
        submit_button = st.form_submit_button("🚀 Simpan ke Google Sheets", type="primary")

    if submit_button:
        with st.spinner("Mengirim data ke Google Sheets..."):
            payload = {
                "hari_waktu": hari_waktu,
                "nama_ustadz": nama_ustadz,
                "judul_kajian": judul_kajian,
                "nama_masjid": nama_masjid,
                "alamat_jalan": alamat_jalan,
                "kontak": kontak,
                "teks_lengkap": data["teks_lengkap"]
            }
            
            try:
                response = requests.post(WEB_APP_URL, data=payload)
                if response.text == "DUPLIKAT":
                    st.error("⚠️ **Data Ditolak:** Kajian dari Ustadz dan Hari/Waktu ini sudah ada di Google Sheets!")
                elif response.text == "SUKSES":
                    st.success("✅ **Berhasil!** Data kajian berhasil masuk ke Google Sheets.")
                    del st.session_state['parsed_data']  # Reset form setelah berhasil
                else:
                    st.warning("Data mungkin terkirim, namun respon server tidak sesuai.")
            except Exception as e:
                st.error(f"Gagal mengirim data: {e}")