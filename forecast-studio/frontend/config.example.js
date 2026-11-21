/* ============================================================
   Template Konfigurasi Environment Frontend (Aman untuk Publik)
   Salin menjadi config.js atau gunakan GitHub Actions Repository
   Variable `PRODUCTION_API_BASE_URL`.
   JANGAN PERNAH menaruh service account key atau secret di sini!
   ============================================================ */
window.APP_CONFIG = {
  // URL Backend FastAPI untuk development lokal
  DEV_API_BASE_URL: "http://localhost:8000",

  // URL Backend FastAPI production (HTTPS, mis. https://forecast-studio-api.onrender.com)
  PRODUCTION_API_BASE_URL: "https://YOUR-BACKEND-APP.onrender.com",

  // Versi Firebase JS Compat SDK (gstatic CDN)
  FIREBASE_SDK_VERSION: "12.19.0",

  // Konfigurasi publik Web App Firebase (aman di frontend; keamanan dijaga Firestore & Storage Rules)
  FIREBASE: {
    apiKey: "YOUR_FIREBASE_WEB_API_KEY",
    authDomain: "YOUR_PROJECT.firebaseapp.com",
    projectId: "YOUR_PROJECT_ID",
    storageBucket: "YOUR_PROJECT.firebasestorage.app",
    messagingSenderId: "YOUR_SENDER_ID",
    appId: "YOUR_APP_ID",
    measurementId: "YOUR_MEASUREMENT_ID",
  },
};
