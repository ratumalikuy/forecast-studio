/* ============================================================
   Template Konfigurasi Environment Frontend (Aman untuk Publik)
   Salin menjadi config.js atau gunakan GitHub Actions Repository
   Variable `PRODUCTION_API_BASE_URL`.
   JANGAN PERNAH menaruh service account key atau secret di sini!
   ============================================================ */
window.APP_CONFIG = {
  DEV_API_BASE_URL: "http://localhost:8000",
  PRODUCTION_API_BASE_URL: "https://YOUR-BACKEND-APP.vercel.app",
  FIREBASE_SDK_VERSION: "12.19.0",
  SESSION: {
    PERSISTENCE: "SESSION", // "SESSION" (browserSessionPersistence) atau "NONE" (inMemoryPersistence)
    INACTIVITY_TIMEOUT_MINUTES: 30,
    WARNING_BEFORE_MINUTES: 2,
  },
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
