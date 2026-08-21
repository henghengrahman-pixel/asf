# Marketing 8008 CRM Terpadu v1.0.0

Gabungan basis `ym-main` (CRM/In-Out), `asf-main` (DP Checker multi website), dan aturan absensi satu shift dari `4553432-main`.

## Akses Leader / SPV / Master
Dashboard, Master Website, Website dinamis, Semua Data Staf, Tambah Staf, Laporan Leader (IN/OUT + Absensi), IN OUT Real Time, Shift Global, Tambah Penugasan, Board Penugasan Aktif, Kantor, Master Jobdesk, Pengguna dan Akses, Log Login & Akses, Audit Perubahan, Pengaturan Kantor, Absensi Staf, IN/OUT Izin Keluar, Cek Nawala, Akun Saya.

## Akses Staff
Dashboard, Absensi Staf, IN/OUT Izin Keluar, Cek Nawala, Akun Saya.

## Absensi satu shift
Buka 10:15 WIB. Tepat waktu sampai 11:15:59. Mulai 11:16:00 dihitung telat Rp50.000/menit. Tutup 13:15:00.

## DP Checker
DP Checker berjalan sebagai service internal Node/Playwright pada port 3001 dan diakses dari CRM melalui reverse proxy `/dp/*`. Website yang ditambah pada Master Website tersimpan di `/data/dp_checker/sites.json` dan otomatis muncul pada Dashboard/sidebar Leader/SPV/Master.

## Railway
Menggunakan Dockerfile Playwright v1.55.0, Python Flask/Gunicorn sebagai service utama dan Node DP Checker sebagai service internal.
