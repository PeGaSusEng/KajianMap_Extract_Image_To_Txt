import streamlit as st
import requests
import json
import google.generativeai as genai
from PIL import Image

# ================================================================
# KONFIGURASI
# ================================================================
# 1. Masukkan API Key Gemini Anda dari https://aistudio.google.com
GEMINI_API_KEY = "AQ.Ab8RN6KNRE_uioa9y_OL7VkIlFnJTAbqwn_8yeJ-mHBgeWDx_Q"

# 2. Masukkan Web App URL dari Google Apps Script Anda
WEB_APP_URL = "https://script.google.com/macros/s/AKfycbzgXQiICBL8NPbpGslk0Vpqi5P5aMixzbDEKS0QCPYCal6vArwSygzRHyYlPA9fW5nf-Q/exec"

# Inisialisasi Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

st.set_page_config(page_title="AI Ekstraktor Jadwal Kajian", layout="centered")
st.title("🕌 Auto-Input Jadwal Kajian (Gemini AI)")
st.caption("Mengekstrak jadwal dari flyer biasa maupun tabel pekanan secara akurat ke Google Sheets.")

uploaded_file = st.file_uploader("Upload Flyer Kajian (JPG/PNG)", type=["jpg", "jpeg", "png"])

if uploaded_file is not None:
    image = Image.open(uploaded_file)
    st.image(image, caption="Flyer Kajian", width=350)
    
    if st.button("🔍 Ekstrak Data dengan AI", type="primary"):
        with st.spinner("Gemini AI sedang membaca dan menganalisis flyer..."):
            try:
                # Prompt khusus agar Gemini mengembalikan format JSON
                prompt = """
                Analisis gambar flyer kajian ini. Ekstrak seluruh jadwal kajian yang ada.
                Jika ada banyak jadwal/penceramah (seperti tabel pekanan), ambil SEMUA jadwalnya satu per satu.
                
                Kembalikan hasilnya HANYA dalam format JSON array of objects tanpa teks penjelasan tambahan:
                [
                  {
                    "hari_waktu": "Hari, Tanggal & Jam (Contoh: Senin, 28 Sept - 15.15 WIB)",
                    "nama_ustadz": "Nama Ustadz beserta gelarnya",
                    "judul_kajian": "Judul/Tema Kajian atau Kitab yang dibahas",
                    "nama_masjid": "Nama Masjid / Tempat penyelenggara",
                    "alamat_jalan": "Alamat lokasi/jalan jika ada",
                    "kontak": "Nomor WhatsApp/HP kontak person jika ada"
                  }
                ]
                """
                
                response = model.generate_content([prompt, image])
                
                # Bersihkan format markdown jika ada
                raw_text = response.text.replace("```json", "").replace("```", "").strip()
                data_list = json.loads(raw_text)
                
                st.session_state['extracted_kajian'] = data_list
                st.success(f"🎉 Berhasil mengekstrak {len(data_list)} jadwal kajian!")
                
            except Exception as e:
                st.error(f"Gagal memproses gambar: {e}")

# ================================================================
# TAMPILAN PERIKSA & SIMPAN KE GOOGLE SHEETS
# ================================================================
if 'extracted_kajian' in st.session_state:
    st.divider()
    st.subheader("📝 Periksa Data Sebelum Disimpan")
    
    data_list = st.session_state['extracted_kajian']
    
    # Pilih kajian mana yang ingin disimpan jika flyer berisi banyak jadwal
    options = [f"{i+1}. {item['hari_waktu']} - {item['nama_ustadz']}" for i, item in enumerate(data_list)]
    selected_index = st.selectbox("Pilih Jadwal Kajian yang Ingin Diinput:", range(len(options)), format_func=lambda x: options[x])
    
    selected_data = data_list[selected_index]
    
    with st.form("form_kirim_sheet"):
        col1, col2 = st.columns(2)
        
        with col1:
            hari_waktu = st.text_input("Hari dan Waktu", value=selected_data.get("hari_waktu", ""))
            nama_ustadz = st.text_input("Nama Ustadz", value=selected_data.get("nama_ustadz", ""))
            judul_kajian = st.text_input("Judul Kajian", value=selected_data.get("judul_kajian", ""))
            
        with col2:
            nama_masjid = st.text_input("Nama Masjid / Tempat", value=selected_data.get("nama_masjid", ""))
            alamat_jalan = st.text_input("Alamat Jalan", value=selected_data.get("alamat_jalan", ""))
            kontak = st.text_input("Kontak", value=selected_data.get("kontak", ""))
            
        submit_button = st.form_submit_button("🚀 Simpan ke Google Sheets", type="primary")

    if submit_button:
        with st.spinner("Mengirim ke Google Sheets..."):
            payload = {
                "hari_waktu": hari_waktu,
                "nama_ustadz": nama_ustadz,
                "judul_kajian": judul_kajian,
                "nama_masjid": nama_masjid,
                "alamat_jalan": alamat_jalan,
                "kontak": kontak
            }
            
            try:
                res = requests.post(WEB_APP_URL, data=payload)
                if res.text == "DUPLIKAT":
                    st.error("⚠️ Data Ditolak: Kajian ustadz ini pada waktu tersebut sudah ada di Google Sheets!")
                elif res.text == "SUKSES":
                    st.success("✅ **Berhasil!** Data berhasil disimpan ke Google Sheets.")
                else:
                    st.warning(f"Respon server: {res.text}")
            except Exception as e:
                st.error(f"Gagal terhubung ke Google Apps Script: {e}")
