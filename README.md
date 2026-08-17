# 🤖 Telegram SIM Checker Bot (4 Nhà Mạng)

Hệ thống Bot Telegram tự động theo dõi và kiểm tra tình trạng SIM trong kho của **4 nhà mạng lớn nhất Việt Nam**: **Viettel**, **MobiFone**, **VinaPhone**, và **Vietnamobile**.

---

## 📂 Cấu trúc thư mục:
- `telegram_bot.py`: Mã nguồn chính của Telegram Bot (Long-polling, Scheduler, Watchlist Manager).
- `simchecker.py`: Module tra cứu API ngầm tốc độ cao của 4 nhà mạng.
- `config.json`: Cấu hình Bot Token, Admin Chat ID, lịch hẹn giờ và delay an toàn.
- `sim_watchlist.json`: Cơ sở dữ liệu danh sách số điện thoại theo từng nhà mạng.
- `list number.txt`: Danh sách số mẫu.
- `report_sims.txt`: Báo cáo kết quả quét gần nhất.

---

## 🚀 Cách chạy Bot:
1. Mở file `config.json`, điền Telegram Bot Token vào `bot_token`.
2. Khởi động bot bằng lệnh:
   ```bash
   python telegram_bot.py
   ```

---

## 📱 Các lệnh điều khiển trên Telegram:
- `/start` hoặc `/help` : Xem menu hướng dẫn.
- `/add <mạng> <số>` : Thêm số vào danh sách theo dõi.
- `/del <số>` : Xóa số khỏi danh sách theo dõi.
- `/list` : Xem toàn bộ danh sách số đang theo dõi.
- `/check <số>` : Kiểm tra ngay lập tức 1 số bất kỳ.
- `/scan` : Quét toàn bộ danh sách theo dõi ngay bây giờ.
- `/set_time <HH:MM>` : Hẹn giờ tự động quét mỗi ngày (Ví dụ: `/set_time 08:00, 12:00, 19:30`).
- `/set_interval <phút>` : Quét định kỳ sau mỗi X phút.
- `/set_delay <giây>` : Đặt thời gian delay giữa các lần check.
- `/status` : Xem trạng thái bot.
