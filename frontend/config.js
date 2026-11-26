/* ============================================================
   Konfigurasi Environment Frontend (Development vs Production)
   - Di localhost / 127.0.0.1 otomatis memakai DEV_API_BASE_URL.
   - Di production (Vercel / GitHub Pages / Custom Domain) otomatis
     memakai PRODUCTION_API_BASE_URL (tersembunyi dari tampilan user).
   - Tidak ada private key atau secret backend di file ini.
   ============================================================ */
(function () {
  var isLocal =
    window.location.hostname === "localhost" ||
    window.location.hostname === "127.0.0.1" ||
    window.location.protocol === "file:";

  var injectedProd = "__PRODUCTION_API_BASE_URL__";
  var defaultProd = "https://forecast-studio-olive.vercel.app";
  var hasInjectedProd =
    injectedProd &&
    injectedProd.indexOf("__PRODUCTION_") !== 0 &&
    /^https?:\/\//i.test(injectedProd);

  var prodUrl = (hasInjectedProd ? injectedProd : defaultProd).replace(/\/+$/, "");
  var devUrl = "http://localhost:8000";

  window.APP_CONFIG = {
    ENVIRONMENT: isLocal ? "development" : "production",
    IS_LOCAL: isLocal,
    DEV_API_BASE_URL: devUrl,
    PRODUCTION_API_BASE_URL: prodUrl,
    API_BASE_URL: isLocal ? devUrl : prodUrl,
    FIREBASE_SDK_VERSION: "12.19.0",
    // Konfigurasi keamanan sesi (dapat disesuaikan)
    SESSION: {
      // "SESSION" = browserSessionPersistence (wajib login ulang saat tab/browser ditutup; refresh tab aktif tetap masuk)
      // "NONE"    = inMemoryPersistence (lebih ketat: wajib login ulang bahkan saat refresh / browser session restore)
      PERSISTENCE: "SESSION",
      // Batas waktu tidak aktif otomatis logout (menit)
      INACTIVITY_TIMEOUT_MINUTES: 30,
      // Peringatan sebelum sesi berakhir otomatis (menit)
      WARNING_BEFORE_MINUTES: 2,
    },
    FIREBASE: {
      apiKey: "AIzaSyBHHAjOqlzs6SU6EqVuuoViHFgDpLCWA6U",
      authDomain: "dashboard-forecast-demand.firebaseapp.com",
      projectId: "dashboard-forecast-demand",
      storageBucket: "dashboard-forecast-demand.firebasestorage.app",
      messagingSenderId: "837710819889",
      appId: "1:837710819889:web:473928afaf0898dccc1b10",
      measurementId: "G-FFKK3CQNRM",
    },
  };
})();
