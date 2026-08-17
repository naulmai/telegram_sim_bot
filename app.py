"""
Telegram Bot SIM Checker for 4 Major Vietnamese Carriers:
Viettel, MobiFone, VinaPhone, Vietnamobile
Interactive Buttons Support (Reply Keyboards & Inline Buttons)
Zero External Dependencies (Uses Built-in HTTP Long-Polling)
"""

import os
import sys
import re
import json
import time
import datetime
import threading
from typing import Dict, Any, List, Optional
from curl_cffi import requests

# Import the core SIM checker hub
from simchecker import SimCheckerHub, CarrierDetector

# Configuration and Database Filepaths
CONFIG_FILE = "config.json"
WATCHLIST_FILE = "sim_watchlist.json"

# Ensure UTF-8 output on Windows console
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")


class ConfigManager:
    """Manage bot configuration and persistence with Environment Variable support."""

    DEFAULT_CONFIG = {
        "bot_token": "",
        "admin_chat_ids": [],
        "delay_seconds": 1.5,
        "scheduled_times": ["08:00", "12:00", "19:00"],
        "interval_minutes": 0,
        "auto_scan_enabled": True
    }

    @classmethod
    def load(cls) -> Dict[str, Any]:
        cfg = cls.DEFAULT_CONFIG.copy()
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    cfg.update(json.load(f))
            except Exception:
                pass
        elif os.path.exists("config.example.json"):
            try:
                with open("config.example.json", "r", encoding="utf-8") as f:
                    cfg.update(json.load(f))
            except Exception:
                pass

        # Environment variables override (for Cloud Deploy)
        env_token = os.environ.get("BOT_TOKEN")
        if env_token:
            cfg["bot_token"] = env_token.strip()

        env_admin = os.environ.get("ADMIN_CHAT_ID")
        if env_admin and env_admin.isdigit():
            admin_int = int(env_admin)
            if admin_int not in cfg.setdefault("admin_chat_ids", []):
                cfg["admin_chat_ids"].append(admin_int)

        return cfg

    @classmethod
    def save(cls, data: Dict[str, Any]):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


class WatchlistManager:
    """Manage phone numbers categorized by carrier."""

    DEFAULT_DATA = {
        "VIETTEL": [],
        "MOBIFONE": [],
        "VINAPHONE": [],
        "VIETNAMOBILE": []
    }

    @classmethod
    def load(cls) -> Dict[str, List[str]]:
        if not os.path.exists(WATCHLIST_FILE):
            cls.save(cls.DEFAULT_DATA)
            return cls.DEFAULT_DATA.copy()
        try:
            with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return cls.DEFAULT_DATA.copy()

    @classmethod
    def save(cls, data: Dict[str, List[str]]):
        with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    @classmethod
    def add_number(cls, phone: str, carrier: Optional[str] = None) -> tuple[bool, str, str]:
        """Add a phone number to the appropriate carrier list."""
        clean_num = re.sub(r'\D', '', phone)
        if clean_num.startswith("84") and len(clean_num) == 11:
            clean_num = "0" + clean_num[2:]
        elif not clean_num.startswith("0") and len(clean_num) == 9:
            clean_num = "0" + clean_num

        if len(clean_num) != 10:
            return False, clean_num, "Số điện thoại không hợp lệ (cần 10 chữ số)"

        target_carrier = carrier.upper() if carrier else CarrierDetector.get_carrier(clean_num)
        if target_carrier not in ["VIETTEL", "MOBIFONE", "VINAPHONE", "VIETNAMOBILE"]:
            target_carrier = "VIETTEL"

        data = cls.load()
        if clean_num in data.setdefault(target_carrier, []):
            return False, clean_num, f"Số {clean_num} đã có trong danh sách {target_carrier}"

        data[target_carrier].append(clean_num)
        cls.save(data)
        return True, clean_num, target_carrier

    @classmethod
    def remove_number(cls, phone: str) -> tuple[bool, str, str]:
        """Remove a phone number from watchlist."""
        clean_num = re.sub(r'\D', '', phone)
        if clean_num.startswith("84") and len(clean_num) == 11:
            clean_num = "0" + clean_num[2:]
        elif not clean_num.startswith("0") and len(clean_num) == 9:
            clean_num = "0" + clean_num

        data = cls.load()
        found_carrier = None
        for c, nums in data.items():
            if clean_num in nums:
                nums.remove(clean_num)
                found_carrier = c
                break

        if found_carrier:
            cls.save(data)
            return True, clean_num, found_carrier
        return False, clean_num, "Không tìm thấy số trong danh sách theo dõi"


class TelegramBot:
    """Telegram Bot Controller with interactive buttons and long polling."""

    def __init__(self):
        self.config = ConfigManager.load()
        self.bot_token = self.config.get("bot_token", "").strip()
        self.session = requests.Session(impersonate="chrome131")
        self.is_scanning = False
        self.last_interval_scan = time.time()
        self.last_checked_minute = ""

    @property
    def api_url(self) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}"

    def get_main_keyboard(self) -> Dict[str, Any]:
        """Create main interactive persistent reply keyboard."""
        return {
            "keyboard": [
                [{"text": "🚀 Quét Toàn Bộ SIM"}, {"text": "📋 Xem Danh Sách"}],
                [{"text": "➕ Hướng Dẫn Thêm Số"}, {"text": "🗑 Hướng Dẫn Xóa Số"}],
                [{"text": "⏰ Cài Đặt Hẹn Giờ"}, {"text": "⚙️ Trạng Thái Bot"}]
            ],
            "resize_keyboard": True,
            "persistent": True
        }

    def get_inline_action_keyboard(self) -> Dict[str, Any]:
        """Create inline buttons for quick actions under messages."""
        return {
            "inline_keyboard": [
                [
                    {"text": "🚀 Bắt Đầu Quét Ngay", "callback_data": "action_scan"},
                    {"text": "📋 Xem Danh Sách", "callback_data": "action_list"}
                ],
                [
                    {"text": "⚙️ Trạng Thái & Lịch", "callback_data": "action_status"},
                    {"text": "❓ Hướng Dẫn Chi Tiết", "callback_data": "action_help"}
                ]
            ]
        }

    def send_message(self, chat_id: int | str, text: str, reply_markup: Optional[Dict[str, Any]] = None, parse_mode: str = "HTML") -> bool:
        """Send formatted message with optional keyboard."""
        if not self.bot_token or self.bot_token.startswith("YOUR_"):
            return False
        url = f"{self.api_url}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        try:
            r = self.session.post(url, json=payload, timeout=10)
            if r.status_code != 200:
                print(f"[!] Telegram Send Error ({r.status_code}): {r.text}")
            return r.status_code == 200
        except Exception as e:
            print(f"[!] Error sending message to {chat_id}: {e}")
            return False

    def answer_callback_query(self, callback_query_id: str, text: Optional[str] = None):
        """Acknowledge button click in inline keyboard."""
        url = f"{self.api_url}/answerCallbackQuery"
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        try:
            self.session.post(url, json=payload, timeout=5)
        except Exception:
            pass

    def broadcast(self, text: str, reply_markup: Optional[Dict[str, Any]] = None, parse_mode: str = "HTML"):
        """Broadcast message to all admin chat IDs."""
        admin_ids = self.config.get("admin_chat_ids", [])
        for cid in admin_ids:
            self.send_message(cid, text, reply_markup=reply_markup, parse_mode=parse_mode)

    def is_admin(self, chat_id: int | str) -> bool:
        """Check if user is admin, auto-register first user if empty."""
        admin_ids = self.config.setdefault("admin_chat_ids", [])
        if chat_id not in admin_ids:
            admin_ids.append(chat_id)
            ConfigManager.save(self.config)
        return True

    def handle_text_or_command(self, chat_id: int | str, text: str):
        """Process incoming command or text from buttons."""
        clean_text = text.strip()
        print(f"[+] User {chat_id} sent: {clean_text}")

        # Map Button labels to commands
        if clean_text in ["🚀 Quét Toàn Bộ SIM", "Quét Ngay"]:
            self.handle_command(chat_id, "/scan", [])
        elif clean_text in ["📋 Xem Danh Sách", "Danh Sách"]:
            self.handle_command(chat_id, "/list", [])
        elif clean_text in ["➕ Hướng Dẫn Thêm Số", "Thêm Số"]:
            msg = (
                "➕ <b>CÁCH THÊM SỐ VÀO DANH SÁCH THEO DÕI:</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "👉 Gửi lệnh kèm danh sách số:\n"
                "▫ <code>/add viettel 0981945794 0365141705</code>\n"
                "▫ <code>/add mobifone 0703608734</code>\n"
                "▫ <code>/add vinaphone 0812225033</code>\n"
                "▫ <code>/add vietnamobile 0564441185</code>\n\n"
                "💡 <i>Bạn cũng có thể chỉ cần gửi <code>/add &lt;danh_sách_số&gt;</code> bot sẽ tự động nhận diện đúng nhà mạng!</i>"
            )
            self.send_message(chat_id, msg)
        elif clean_text in ["🗑 Hướng Dẫn Xóa Số", "Xóa Số"]:
            msg = (
                "🗑 <b>CÁCH XÓA SỐ KHỎI THEO DÕI:</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "👉 Gửi lệnh:\n"
                "▫ <code>/del 0981945794</code>\n"
                "▫ Hoặc xóa nhiều số: <code>/del 0981945794 0703608734</code>"
            )
            self.send_message(chat_id, msg)
        elif clean_text in ["⏰ Cài Đặt Hẹn Giờ", "Hẹn Giờ"]:
            times = ", ".join([f"<code>{t}</code>" for t in self.config.get("scheduled_times", [])])
            interval = self.config.get("interval_minutes", 0)
            msg = (
                "⏰ <b>CÀI ĐẶT LẬP LỊCH QUÉT TỰ ĐỘNG:</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"🕒 <b>Mốc giờ quét mỗi ngày hiện tại:</b> {times}\n"
                f"🔄 <b>Quét định kỳ:</b> {interval} phút/lần\n\n"
                "👉 <b>Cách thay đổi:</b>\n"
                "▫ Đổi giờ quét cố định: <code>/set_time 08:00, 12:00, 19:30</code>\n"
                "▫ Quét lặp lại mỗi X phút: <code>/set_interval 30</code> (hoặc <code>/set_interval 0</code> để tắt)\n"
                "▫ Cài đặt delay an toàn: <code>/set_delay 2.0</code>"
            )
            self.send_message(chat_id, msg)
        elif clean_text in ["⚙️ Trạng Thái Bot", "Trạng Thái"]:
            self.handle_command(chat_id, "/status", [])
        elif clean_text.startswith("/"):
            parts = clean_text.split()
            cmd = parts[0].split("@")[0]
            args = parts[1:]
            self.handle_command(chat_id, cmd, args)
        elif re.match(r'^0\d{9}$', clean_text):
            # Quick check if user just sent a 10-digit number
            self.handle_command(chat_id, "/check", [clean_text])
        else:
            self.handle_command(chat_id, "/start", [])

    def handle_command(self, chat_id: int | str, command: str, args: List[str]):
        """Process incoming bot command."""
        cmd = command.lower()

        if cmd in ["/start", "/help"]:
            help_text = (
                "🤖 <b>CHÀO MỪNG BẠN ĐẾN VỚI BOT CHECK SIM 4 NHÀ MẠNG!</b>\n"
                "<i>(Hỗ trợ Viettel • MobiFone • VinaPhone • Vietnamobile)</i>\n\n"
                "🔘 <i>Bạn có thể bấm trực tiếp các nút bấm ở thanh điều khiển bên dưới hoặc dùng lệnh:</i>\n\n"
                "📋 <b>Các lệnh nhanh:</b>\n"
                "▫ <code>/scan</code> — Bắt đầu quét kiểm tra toàn bộ danh sách\n"
                "▫ <code>/list</code> — Xem danh sách các số đang theo dõi\n"
                "▫ <code>/check &lt;số&gt;</code> — Kiểm tra nhanh 1 số bất kỳ\n"
                "▫ <code>/add &lt;mạng&gt; &lt;số&gt;</code> — Thêm số vào theo dõi\n"
                "▫ <code>/del &lt;số&gt;</code> — Xóa số khỏi theo dõi\n"
                "▫ <code>/status</code> — Xem trạng thái hệ thống"
            )
            self.send_message(chat_id, help_text, reply_markup=self.get_main_keyboard())
            self.send_message(chat_id, "👇 <b>Bấm nút bên dưới để thao tác nhanh:</b>", reply_markup=self.get_inline_action_keyboard())

        elif cmd == "/list":
            watchlist = WatchlistManager.load()
            total_count = sum(len(nums) for nums in watchlist.values())

            lines = ["📋 <b>DANH SÁCH SIM ĐANG THEO DÕI:</b>", f"Tổng cộng: <b>{total_count}</b> SIM\n"]
            for carrier in ["VIETTEL", "MOBIFONE", "VINAPHONE", "VIETNAMOBILE"]:
                nums = watchlist.get(carrier, [])
                lines.append(f"📱 <b>{carrier}</b> ({len(nums)} số):")
                if nums:
                    num_str = ", ".join([f"<code>{n}</code>" for n in nums])
                    lines.append(f"   {num_str}")
                else:
                    lines.append("   <i>(Chưa có số nào)</i>")
                lines.append("")

            self.send_message(chat_id, "\n".join(lines), reply_markup=self.get_main_keyboard())

        elif cmd == "/add":
            if not args:
                self.send_message(chat_id, "⚠️ <b>Cú pháp:</b> <code>/add &lt;nhà_mạng&gt; &lt;số_1&gt; &lt;số_2&gt;...</code>\n<b>Ví dụ:</b> <code>/add viettel 0981945794 0365141705</code>", reply_markup=self.get_main_keyboard())
                return

            carrier = None
            raw_numbers = []

            if args[0].upper() in ["VIETTEL", "MOBIFONE", "VINAPHONE", "VIETNAMOBILE"]:
                carrier = args[0].upper()
                raw_numbers = args[1:]
            else:
                raw_numbers = args

            all_extracted = []
            for item in raw_numbers:
                nums = re.findall(r'0\d{9}', item)
                if nums:
                    all_extracted.extend(nums)
                else:
                    clean = re.sub(r'\D', '', item)
                    if len(clean) in [9, 10, 11]:
                        all_extracted.append(clean)

            if not all_extracted:
                self.send_message(chat_id, "❌ Không tìm thấy số điện thoại hợp lệ để thêm.", reply_markup=self.get_main_keyboard())
                return

            added_list = []
            failed_list = []
            for num in all_extracted:
                success, phone, note = WatchlistManager.add_number(num, carrier=carrier)
                if success:
                    added_list.append(f"<code>{phone}</code> ({note})")
                else:
                    failed_list.append(f"<code>{phone}</code>: {note}")

            msg_parts = []
            if added_list:
                msg_parts.append("✅ <b>Đã thêm thành công:</b>\n" + "\n".join([f" • {item}" for item in added_list]))
            if failed_list:
                msg_parts.append("\n⚠️ <b>Không thêm được:</b>\n" + "\n".join([f" • {item}" for item in failed_list]))

            self.send_message(chat_id, "\n".join(msg_parts), reply_markup=self.get_main_keyboard())

        elif cmd == "/del":
            if not args:
                self.send_message(chat_id, "⚠️ <b>Cú pháp:</b> <code>/del &lt;số_điện_thoại&gt;</code>\n<b>Ví dụ:</b> <code>/del 0981945794</code>", reply_markup=self.get_main_keyboard())
                return

            all_extracted = []
            for item in args:
                nums = re.findall(r'0\d{9}', item)
                if nums:
                    all_extracted.extend(nums)
                else:
                    all_extracted.append(item)

            deleted = []
            failed = []
            for num in all_extracted:
                success, phone, note = WatchlistManager.remove_number(num)
                if success:
                    deleted.append(f"<code>{phone}</code> (Mạng {note})")
                else:
                    failed.append(f"<code>{phone}</code>: {note}")

            msg_parts = []
            if deleted:
                msg_parts.append("🗑 <b>Đã xóa thành công:</b>\n" + "\n".join([f" • {item}" for item in deleted]))
            if failed:
                msg_parts.append("\n⚠️ <b>Không tìm thấy:</b>\n" + "\n".join([f" • {item}" for item in failed]))

            self.send_message(chat_id, "\n".join(msg_parts), reply_markup=self.get_main_keyboard())

        elif cmd == "/check":
            if not args:
                self.send_message(chat_id, "⚠️ <b>Cú pháp:</b> <code>/check &lt;số_điện_thoại&gt;</code>\n<b>Ví dụ:</b> <code>/check 0981945794</code>", reply_markup=self.get_main_keyboard())
                return

            phone = args[0]
            self.send_message(chat_id, f"🔍 <b>Đang kiểm tra số:</b> <code>{phone}</code>...")
            
            hub = SimCheckerHub(delay=self.config.get("delay_seconds", 1.5))
            res = hub.check_sim(phone)
            carrier = res.get("carrier", "UNKNOWN")

            if res.get("available"):
                item = res["items"][0]
                text = (
                    f"🎉 <b>KẾT QUẢ: CÒN BÁN TRÊN KHO {carrier}!</b>\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"📱 <b>Số SIM:</b> <code>{item['phone']}</code>\n"
                    f"🏷 <b>Loại:</b> {item.get('type', 'N/A')}\n"
                    f"💰 <b>Giá:</b> {item['price']}\n"
                    f"━━━━━━━━━━━━━━━━━━"
                )
            else:
                text = (
                    f"❌ <b>KẾT QUẢ: KHÔNG CÓ TRÊN KHO {carrier}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"📱 <b>Số SIM:</b> <code>{phone}</code>\n"
                    f"ℹ️ <b>Ghi chú:</b> {res.get('note', 'Đã bán hoặc chưa lên kho')}\n"
                    f"━━━━━━━━━━━━━━━━━━"
                )

            self.send_message(chat_id, text, reply_markup=self.get_main_keyboard())

        elif cmd == "/scan":
            if self.is_scanning:
                self.send_message(chat_id, "⏳ Bot đang trong quá trình quét danh sách, vui lòng chờ hoàn thành!", reply_markup=self.get_main_keyboard())
                return

            self.send_message(chat_id, "🚀 <b>Bắt đầu quét toàn bộ danh sách theo dõi...</b>", reply_markup=self.get_main_keyboard())
            threading.Thread(target=self.run_full_scan, args=(chat_id,), daemon=True).start()

        elif cmd == "/set_time":
            if not args:
                self.send_message(chat_id, "⚠️ <b>Cú pháp:</b> <code>/set_time 08:00, 12:00, 19:30</code>\n(Nhập các mốc giờ cách nhau bởi dấu phẩy hoặc khoảng trắng)", reply_markup=self.get_main_keyboard())
                return

            raw_input = " ".join(args)
            time_matches = re.findall(r'\b([01]?[0-9]|2[0-3]):([0-5][0-9])\b', raw_input)

            if not time_matches:
                self.send_message(chat_id, "❌ Định dạng giờ không hợp lệ! Vui lòng nhập định dạng HH:MM (Ví dụ: <code>08:30</code>, <code>19:00</code>)", reply_markup=self.get_main_keyboard())
                return

            formatted_times = [f"{int(h):02d}:{int(m):02d}" for h, m in time_matches]
            formatted_times = sorted(list(set(formatted_times)))

            self.config["scheduled_times"] = formatted_times
            ConfigManager.save(self.config)

            time_str = ", ".join([f"<code>{t}</code>" for t in formatted_times])
            self.send_message(chat_id, f"✅ <b>Đã cập nhật lịch quét hàng ngày:</b>\n⏰ {time_str}", reply_markup=self.get_main_keyboard())

        elif cmd == "/set_interval":
            if not args or not args[0].isdigit():
                self.send_message(chat_id, "⚠️ <b>Cú pháp:</b> <code>/set_interval &lt;số_phút&gt;</code>\n(Ví dụ: <code>/set_interval 30</code> để quét mỗi 30 phút, hoặc <code>0</code> để tắt)", reply_markup=self.get_main_keyboard())
                return

            minutes = int(args[0])
            self.config["interval_minutes"] = minutes
            ConfigManager.save(self.config)

            if minutes > 0:
                self.send_message(chat_id, f"✅ <b>Đã bật quét định kỳ:</b> Mỗi <code>{minutes}</code> phút/lần.", reply_markup=self.get_main_keyboard())
            else:
                self.send_message(chat_id, "⏹ <b>Đã tắt quét theo chu kỳ phút.</b>", reply_markup=self.get_main_keyboard())

        elif cmd == "/set_delay":
            if not args:
                self.send_message(chat_id, "⚠️ <b>Cú pháp:</b> <code>/set_delay &lt;số_giây&gt;</code>\n<b>Ví dụ:</b> <code>/set_delay 2.0</code>", reply_markup=self.get_main_keyboard())
                return

            try:
                delay = float(args[0])
                if delay < 0.5:
                    delay = 0.5
                self.config["delay_seconds"] = delay
                ConfigManager.save(self.config)
                self.send_message(chat_id, f"✅ <b>Đã cập nhật delay giữa các lần check:</b> <code>{delay}</code> giây.", reply_markup=self.get_main_keyboard())
            except ValueError:
                self.send_message(chat_id, "❌ Giá trị delay phải là số hợp lệ (ví dụ: <code>1.5</code>, <code>2.0</code>).", reply_markup=self.get_main_keyboard())

        elif cmd == "/status":
            watchlist = WatchlistManager.load()
            total_count = sum(len(nums) for nums in watchlist.values())
            times = ", ".join([f"<code>{t}</code>" for t in self.config.get("scheduled_times", [])]) or "Chưa cài đặt"
            interval = self.config.get("interval_minutes", 0)
            interval_str = f"Mỗi <code>{interval}</code> phút" if interval > 0 else "Đang tắt"
            delay = self.config.get("delay_seconds", 1.5)

            status_text = (
                "⚙️ <b>TRẠNG THÁI HỆ THỐNG:</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📊 <b>Tổng số SIM theo dõi:</b> <code>{total_count}</code> SIM\n"
                f"⏰ <b>Mốc giờ quét mỗi ngày:</b> {times}\n"
                f"🔄 <b>Quét chu kỳ phút:</b> {interval_str}\n"
                f"⏳ <b>Delay an toàn:</b> <code>{delay}s</code> / lượt\n"
                f"🚀 <b>Trạng thái Bot:</b> Đang hoạt động 🟢\n"
                f"━━━━━━━━━━━━━━━━━━"
            )
            self.send_message(chat_id, status_text, reply_markup=self.get_main_keyboard())

    def run_full_scan(self, initiator_chat_id: Optional[int | str] = None):
        """Execute full scan of all watchlisted numbers and alert immediately."""
        self.is_scanning = True
        watchlist = WatchlistManager.load()
        delay = self.config.get("delay_seconds", 1.5)
        hub = SimCheckerHub(delay=delay)

        total_sims = []
        for carrier, nums in watchlist.items():
            for num in nums:
                total_sims.append({"phone": num, "carrier": carrier})

        if not total_sims:
            if initiator_chat_id:
                self.send_message(initiator_chat_id, "⚠️ Danh sách theo dõi đang trống! Hãy dùng <code>/add</code> để thêm số.", reply_markup=self.get_main_keyboard())
            self.is_scanning = False
            return

        hits = []
        bads = []

        print(f"[*] Starting scan of {len(total_sims)} SIMs at {datetime.datetime.now().strftime('%H:%M:%S')}...")

        for idx, item in enumerate(total_sims, 1):
            num = item["phone"]
            carrier = item["carrier"]
            res = hub.check_sim(num, specified_carrier=carrier)

            if res.get("available"):
                hits.append(res)
                first_item = res["items"][0]
                print(f"  [+] HIT: {num} [{carrier}] - {first_item['price']}")
                
                # IMMEDIATE ALERT NOTIFICATION
                alert_text = (
                    "🚨 <b>BÁO ĐỘNG: PHÁT HIỆN SIM CÒN BÁN TRÊN KHO!</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"📱 <b>Số SIM:</b> <code>{first_item['phone']}</code>\n"
                    f"🏢 <b>Nhà mạng:</b> <b>{carrier}</b>\n"
                    f"🏷 <b>Gói/Loại:</b> {first_item.get('type', 'N/A')}\n"
                    f"💰 <b>Giá bán:</b> {first_item['price']}\n"
                    f"⏰ <b>Thời gian:</b> <code>{datetime.datetime.now().strftime('%d/%m/%Y %H:%M:%S')}</code>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "⚡ <i>Hãy vào trang chủ nhà mạng mua ngay!</i>"
                )
                self.broadcast(alert_text, reply_markup=self.get_main_keyboard())
            else:
                bads.append(res)

        # Summary Report
        summary_lines = [
            "📊 <b>BÁO CÁO KẾT QUẢ QUÉT SIM TỔNG HỢP</b>",
            f"⏰ <i>Thời gian: {datetime.datetime.now().strftime('%H:%M:%S - %d/%m/%Y')}</i>",
            "━━━━━━━━━━━━━━━━━━",
            f"▫ Tổng số đã quét: <b>{len(total_sims)}</b> SIM",
            f"▫ Số SIM CÒN BÁN: <b>{len(hits)}</b> SIM",
            f"▫ Số SIM ĐÃ BÁN: <b>{len(bads)}</b> SIM\n"
        ]

        if hits:
            summary_lines.append("🎉 <b>DANH SÁCH SIM CÒN BÁN:</b>")
            for h in hits:
                item = h["items"][0]
                summary_lines.append(f" • <code>{item['phone']}</code> [{h['carrier']}]: {item['price']}")
            summary_lines.append("")

        if bads:
            summary_lines.append("❌ <b>DANH SÁCH SIM KHÔNG CÓ TRONG KHO:</b>")
            for b in bads:
                note_str = b.get("note") or b.get("error") or "Không có trong kho hoặc đã được bán"
                summary_lines.append(f" • <code>{b['phone']}</code> [{b.get('carrier', 'UNKNOWN')}]: <i>{note_str}</i>")
            summary_lines.append("")

        if not hits and not bads:
            summary_lines.append("ℹ️ <i>Không có dữ liệu quét.</i>\n")

        summary_lines.append("━━━━━━━━━━━━━━━━━━")
        report_msg = "\n".join(summary_lines)

        if initiator_chat_id:
            self.send_message(initiator_chat_id, report_msg, reply_markup=self.get_main_keyboard())
        else:
            self.broadcast(report_msg, reply_markup=self.get_main_keyboard())

        self.is_scanning = False
        print(f"[*] Scan finished. Found {len(hits)} available SIMs.")

    def scheduler_loop(self):
        """Background thread for automated scheduled scans."""
        while True:
            try:
                now = datetime.datetime.now()
                now_str = now.strftime("%H:%M")
                
                # Check fixed daily scheduled times
                scheduled_times = self.config.get("scheduled_times", [])
                if now_str in scheduled_times and now_str != self.last_checked_minute:
                    self.last_checked_minute = now_str
                    if not self.is_scanning:
                        print(f"[*] Triggering scheduled scan for {now_str}...")
                        self.run_full_scan()

                # Check interval minutes
                interval_min = self.config.get("interval_minutes", 0)
                if interval_min > 0:
                    elapsed = time.time() - self.last_interval_scan
                    if elapsed >= interval_min * 60:
                        self.last_interval_scan = time.time()
                        if not self.is_scanning:
                            print(f"[*] Triggering interval scan (every {interval_min}m)...")
                            self.run_full_scan()

            except Exception as e:
                print(f"[!] Error in scheduler loop: {e}")

            time.sleep(10)

    def start_polling(self):
        """Start Telegram Bot long-polling loop."""
        if not self.bot_token or self.bot_token.startswith("YOUR_"):
            print("\n" + "=" * 60)
            print("  [!] CHƯA CẤU HÌNH TELEGRAM BOT TOKEN!")
            print("=" * 60 + "\n")
            return

        print("\n" + "=" * 60)
        print("  🤖 BOT TELEGRAM CHECK SIM ĐANG CHẠY (HỖ TRỢ BUTTONS)...")
        print("=" * 60 + "\n")

        # Start Scheduler Thread
        scheduler_thread = threading.Thread(target=self.scheduler_loop, daemon=True)
        scheduler_thread.start()

        last_update_id = 0

        while True:
            try:
                url = f"{self.api_url}/getUpdates"
                params = {"offset": last_update_id + 1, "timeout": 20}
                response = self.session.get(url, params=params, timeout=25)
                
                if response.status_code != 200:
                    time.sleep(2)
                    continue

                data = response.json()
                updates = data.get("result", [])

                for update in updates:
                    update_id = update.get("update_id", 0)
                    if update_id > last_update_id:
                        last_update_id = update_id

                    # 1. Handle Inline Button Click (Callback Query)
                    if "callback_query" in update:
                        cb = update["callback_query"]
                        cb_id = cb.get("id")
                        cb_data = cb.get("data")
                        cb_chat_id = cb.get("message", {}).get("chat", {}).get("id")
                        
                        self.answer_callback_query(cb_id)
                        
                        if cb_data == "action_scan":
                            self.handle_command(cb_chat_id, "/scan", [])
                        elif cb_data == "action_list":
                            self.handle_command(cb_chat_id, "/list", [])
                        elif cb_data == "action_status":
                            self.handle_command(cb_chat_id, "/status", [])
                        elif cb_data == "action_help":
                            self.handle_command(cb_chat_id, "/help", [])
                        continue

                    # 2. Handle Regular Message
                    msg = update.get("message") or update.get("edited_message")
                    if not msg:
                        continue

                    chat_id = msg.get("chat", {}).get("id")
                    text = msg.get("text", "").strip()

                    if not text or not chat_id:
                        continue

                    # Auto-register admin ID
                    self.is_admin(chat_id)

                    self.handle_text_or_command(chat_id, text)

            except Exception as e:
                print(f"[!] Polling exception: {e}")
                time.sleep(2)


def main():
    bot = TelegramBot()
    bot.start_polling()


if __name__ == "__main__":
    main()
