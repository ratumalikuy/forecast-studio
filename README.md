# Dead Stock Forecast Studio — Multi-Device Firebase Forecasting Platform

Aplikasi forecasting permintaan material intermittent/lumpy (**ADI/CV²**, **Croston**, **SBA**, **TSB**, grid search presisi **0,0001**, evaluasi tanpa *data leakage*) berbasis akun **Firebase Authentication**, dengan penyimpanan permanen dan riwayat per UID di **Cloud Firestore & Firebase Storage**, sinkronisasi lintas perangkat secara *real-time* (`onSnapshot`), backend **Python FastAPI**, dan frontend statis siap deploy ke **GitHub Pages**.

---

## 1. Struktur Project

```text
forecast-studio/
├── frontend/                         # Frontend statis (deploy ke GitHub Pages)
│   ├── index.html                    # Aplikasi dashboard dinamis (Overview, Material Analysis, Forecast, Upload, Datasets & History, Audit)
│   ├── config.js                     # Konfigurasi environment development vs production (tanpa secret!)
│   ├── config.example.js             # Template konfigurasi publik
│   └── .nojekyll                     # Memastikan aset statis disajikan utuh di GitHub Pages
├── backend/                          # Backend Python FastAPI (deploy ke Render)
│   ├── app/
│   │   ├── main.py                   # Endpoint FastAPI + CORS + autentikasi Firebase ID Token
│   │   ├── config.py                 # Environment variables
│   │   ├── auth.py                   # Middleware verifikasi Firebase ID Token
│   │   ├── forecasting.py            # Engine ADI/CV², Croston, SBA, TSB (rumus terverifikasi Excel, vectorized NumPy grid search 0,0001)
│   │   ├── parser.py                 # Parser & validator dinamis .xlsx, .xlsm, .csv (8, 20, 50, 100+ material)
│   │   ├── firebase_store.py         # Penyimpanan chunked Firestore + transaksi activeDatasetId + Firebase Storage
│   │   ├── jobs.py                   # Background job asinkron + progress aktual per tahap & per material
│   │   └── baseline.py               # Referensi 8 material uji Excel untuk pengujian paritas regresi
│   ├── tests/                        # Automated test suite (pytest)
│   │   ├── test_baseline_parity.py   # Verifikasi paritas <1e-12 terhadap 8 material baseline Excel
│   │   ├── test_dynamic_scale.py     # Uji skala dinamis 8, 20, 50, dan 100 material
│   │   ├── test_parser.py            # Uji parsing .xlsx/.xlsm/.csv, agregasi duplikat, zero-fill, error handling
│   │   └── test_api_security.py      # Uji isolasi UID, wajib token Auth, & lifecycle background job
│   ├── Dockerfile                    # Container production untuk Render / Cloud Run
│   ├── requirements.txt
│   └── .env.example
├── firebase/
│   ├── firestore.rules               # Security Rules ketat berbasis request.auth.uid
│   ├── storage.rules                 # Storage Security Rules ketat berbasis request.auth.uid
│   └── firestore.indexes.json        # Index Firestore untuk urutan history dataset
├── .github/
│   └── workflows/
│       └── deploy-pages.yml          # GitHub Actions otomatis untuk deploy frontend/ ke GitHub Pages
├── render.yaml                       # Render Blueprint untuk backend Python FastAPI
├── .gitignore                        # Mengecualikan .env, service account JSON, file Excel upload, cache, venv, log
└── README.md
```

---

## 2. Metode & Audit Formula Forecasting

Seluruh perhitungan backend (`backend/app/forecasting.py`) dan verifikasi JS frontend (`frontend/index.html`) menggunakan **rumus acuan Excel yang telah diaudit** (`formulaVersion: "excel-verified-v1"`):

1. **Perbaikan Perbedaan Croston & SBA**:
   - Implementasi dashboard lama sebelumnya memperbarui $P_t$ setiap bulan (termasuk saat $X_t = 0$) dan tidak melakukan inisialisasi kejadian pertama.
   - Implementasi terverifikasi Excel memperbarui $Z_t$ dan $P_t$ **hanya saat $X_t > 0$** dengan inisialisasi kejadian demand pertama ($Z = X_t$, $P = q_{t-1} + 1$), sehingga seluruh parameter ($\alpha_C, \alpha_S, \beta_T$), seluruh metrik error (MAD, MSE, MAPE, SMAPE, WAPE, MASE, RMSSE), `nextForecast`, `interval`, dan proyeksi cocok hingga presisi mesin ($\approx 10^{-15}$) terhadap ke-8 material baseline Excel.
2. **Grid Search**: $\alpha, \beta \in [0{,}0001; 1{,}0000]$ dengan step `0,0001` (10.000 kandidat per metode), objektif MSE *one-step-ahead* minimum, tie memilih nilai terkecil pertama.
3. **Aturan Pembagian Evaluasi (Zero Data Leakage)**:
   - **Data $> 60$ bulan ($> 5$ tahun)**: pembagian kronologis **Train/Test 50:50** ($\lceil N/2 \rceil$ Train, sisanya Test). Parameter dioptimasi ulang **hanya pada data Train**, penyebut MASE/RMSSE dihitung hanya dari Train.
   - **Data $\le 60$ bulan ($\le 5$ tahun)**: data awal (mis. 2020) hingga bulan tepat sebelum demand aktual terakhir sebagai data Train (bulan nol dipertahankan), lalu dievaluasi terhadap demand aktual terakhir.
4. **Baseline 8 Material Excel**: tetap utuh dan permanen, tidak dapat dihapus, dan selalu dapat diaktifkan kembali kapan saja.

---

## 3. Arsitektur Data Firebase (Aman Batas 1 MiB & Sinkron Lintas Perangkat)

```text
Cloud Firestore:
  users/{uid}
    ├── profile: { email, lastSeenAt }
    ├── activeDatasetId: "baseline" | "<datasetId>"
    ├── activeDatasetVersion: number
    └── activeDatasetUpdatedAt: timestamp

  users/{uid}/datasets/{datasetId}
    ├── datasetId, uid, name, originalFilename, fileType, storagePath
    ├── status: "uploaded" | "processing" | "completed" | "failed"
    ├── materialCount, periodMin, periodMax, validationReport
    ├── forecastingConfig, summaryResults
    └── uploadedAt, completedAt, updatedAt

  users/{uid}/datasets/{datasetId}/materials/{stockCode}
    └── hasil lengkap per material (chunked per dokumen agar aman untuk 100-500+ material)

Firebase Storage:
  users/{uid}/datasets/{datasetId}/original/<nama_file_asli>
```

---

## 4. Konfigurasi Firebase Console (Langkah Manual yang Diperlukan)

Buka [Firebase Console](https://console.firebase.google.com/) untuk project **`dashboard-forecast-demand`**:

1. **Aktifkan Layanan**:
   - **Authentication** → Sign-in method → pastikan **Email/Password** aktif.
   - **Authentication** → Settings → **Authorized domains** → tambahkan domain GitHub Pages Anda (mis. `<username>.github.io`).
   - **Cloud Firestore** → pastikan database sudah dibuat.
   - **Firebase Storage** → pastikan bucket `dashboard-forecast-demand.firebasestorage.app` sudah aktif.
2. **Pasang Security Rules**:
   - Salin isi [`firebase/firestore.rules`](firebase/firestore.rules) ke **Firestore → Rules** lalu klik **Publish**.
   - Salin isi [`firebase/storage.rules`](firebase/storage.rules) ke **Storage → Rules** lalu klik **Publish**.
3. **Buat Service Account untuk Backend (JANGAN commit ke GitHub!)**:
   - Buka **Project Settings → Service accounts → Generate new private key**.
   - Simpan file JSON tersebut di komputer lokal (untuk dev lokal) atau salin isinya sebagai secret environment variable `FIREBASE_SERVICE_ACCOUNT_JSON` di Render (untuk production).

---

## 5. Menjalankan Secara Lokal (Development)

### A. Backend Python FastAPI
Prasyarat: Python 3.11+

```bash
cd backend
python -m venv .venv

# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS / Linux:
# source .venv/bin/activate

pip install -r requirements.txt
copy .env.example .env
```

Edit `backend/.env`:
- Isi `GOOGLE_APPLICATION_CREDENTIALS=C:\path\ke\service-account.json` (atau `FIREBASE_SERVICE_ACCOUNT_JSON`).
- Pastikan `REQUIRE_AUTH=true`.

Jalankan server:
```bash
uvicorn app.main:app --reload --port 8000
```
Cek kesehatan backend di `http://localhost:8000/`.

### B. Frontend
Jalankan static server lokal pada folder `frontend/`:
```bash
cd frontend
python -m http.server 5500
```
Buka `http://localhost:5500` di browser. Secara otomatis `config.js` akan menggunakan `http://localhost:8000` saat dijalankan di `localhost`.

---

## 6. Menjalankan Pengujian Otomatis Backend

```bash
cd backend
pytest -v
```

Cakupan pengujian:
- `test_baseline_parity.py`: memverifikasi seluruh 8 material baseline cocok dengan Excel (`<1e-12`).
- `test_dynamic_scale.py`: menguji upload & forecasting dinamis untuk **8, 20, 50, dan 100 material**.
- `test_parser.py`: menguji `.xlsx`, `.xlsm`, `.csv`, duplikat, zero-fill, dan penolakan kolom tidak valid.
- `test_api_security.py`: menguji kewajiban autentikasi (401), isolasi UID antar akun (403), dan background job.

---

## 7. Deployment Backend ke Render (HTTPS Public URL)

1. Push repository ini ke GitHub (pastikan `.env` dan file service account JSON **tidak** ikut ter-commit).
2. Buka [Render Dashboard](https://dashboard.render.com/) → **New → Blueprint** (atau **Web Service** menggunakan `backend/Dockerfile`).
3. Atur Environment Variables berikut di Render:
   - `ENVIRONMENT`: `production`
   - `REQUIRE_AUTH`: `true`
   - `FIREBASE_PROJECT_ID`: `dashboard-forecast-demand`
   - `FIREBASE_STORAGE_BUCKET`: `dashboard-forecast-demand.firebasestorage.app`
   - `FIREBASE_SERVICE_ACCOUNT_JSON`: tempel seluruh isi file JSON Service Account Firebase Admin (sebagai Secret).
   - `CORS_ORIGINS`: URL GitHub Pages Anda, misal `https://<username>.github.io` (tanpa garis miring di akhir).
4. Setelah deploy berhasil, salin URL HTTPS backend Render Anda, contoh: `https://forecast-studio-api.onrender.com`.
5. Verifikasi dengan membuka `https://forecast-studio-api.onrender.com/` — pastikan `status: "ok"`, `firebase_auth_ready: true`, `firestore_ready: true`, `storage_ready: true`.

---

## 8. Deployment Frontend ke GitHub Pages

1. Di repository GitHub Anda, buka **Settings → Secrets and variables → Actions → Variables** → klik **New repository variable**:
   - Name: `PRODUCTION_API_BASE_URL`
   - Value: `https://forecast-studio-api.onrender.com` *(URL Render Anda, tanpa slash akhir)*
2. Buka **Settings → Pages**:
   - Source: pilih **GitHub Actions**.
3. Push perubahan ke branch `main` (atau jalankan workflow **Deploy Frontend to GitHub Pages** secara manual dari tab **Actions**).
4. GitHub Actions otomatis menyuntikkan `PRODUCTION_API_BASE_URL` ke `frontend/config.js` dan mempublikasikan folder `frontend/` ke URL GitHub Pages Anda (`https://<username>.github.io/<repo>/`).
5. Pastikan domain `<username>.github.io` sudah terdaftar di:
   - **Firebase Console → Authentication → Settings → Authorized domains**
   - **`CORS_ORIGINS`** di Render.

---

## 9. Daftar Periksa Pengujian End-to-End Lintas Perangkat

1. Buka URL GitHub Pages di **Browser A**, login menggunakan **Akun Firebase A**.
2. Unggah file `ABCD.xlsx` → periksa ringkasan validasi → klik **Jalankan Forecasting & Simpan ke Firebase**.
3. Amati progress bar aktual (tahap forecasting per material → tahap menyimpan ke Firebase → selesai 100%).
4. Refresh browser → pastikan dataset `ABCD` tetap aktif tanpa perlu upload ulang.
5. Buka URL GitHub Pages di **Browser B / Perangkat 2**, login **Akun Firebase A** → dataset `ABCD`, hasil forecasting, dan history otomatis muncul identik.
6. Unggah dataset baru dari **Browser B** → pastikan **Browser A** otomatis mendeteksi perubahan `activeDatasetId` secara real-time melalui Firestore `onSnapshot`.
7. Login menggunakan **Akun Firebase B** → pastikan history kosong (hanya Baseline Awal) dan Akun B tidak dapat melihat maupun mengakses dataset milik Akun A.
