"""
Telegram Bot SIM Checker for 4 Major Vietnamese Carriers:
Viettel, MobiFone, VinaPhone, Vietnamobile
Clean & Grouped Summary Report Layout (Carrier-grouped Available & Unavailable)
Zero External Dependencies (Uses Built-in HTTP Long-Polling)
"""

import os
import sys
import re
import html
import json
import time
import datetime
import threading
import concurrent.futures
from typing import Dict, Any, List, Optional
from curl_cffi import requests

# Import the core SIM checker hub
from simchecker import SimCheckerHub, CarrierDetector
from proxy_hunter import ProxyPoolManager, VietnamobileProxyPoolManager, set_telegram_notify_callback

# Configuration and Database Filepaths
CONFIG_FILE = "config.json"
WATCHLIST_FILE = "sim_watchlist.json"

import functools
print = functools.partial(print, flush=True)

# Ensure UTF-8 output on Windows console with line buffering
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    except Exception:
        pass


class ConfigManager:
    """Manage bot configuration and persistence with Environment Variable support."""

    DEFAULT_CONFIG = {
        "bot_token": "",
        "admin_chat_ids": [],
        "proxy": "",
        "delay_seconds": 1.5,
        "max_workers": 4,
        "scheduled_times": ["08:00", "12:00", "19:00"],
        "interval_minutes": 0,
        "auto_scan_enabled": True,
        "auto_proxy_refresh": True,       # Auto-refresh proxy pool before each scan/health-check
        "probe_numbers": {
            "VIETTEL_PREPAID": "",
            "VIETTEL_POSTPAID": "",
            "MOBIFONE": "",
            "VINAPHONE": "",
            "VIETNAMOBILE": ""
        }
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

        env_proxy = os.environ.get("PROXY") or os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")
        if env_proxy:
            cfg["proxy"] = env_proxy.strip()

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
        self._search_mode_users: set = set()  # Track users in SIM search mode
        set_telegram_notify_callback(lambda msg: self.broadcast(msg))

    @property
    def api_url(self) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}"

    def get_main_keyboard(self) -> Dict[str, Any]:
        """Create main interactive persistent reply keyboard."""
        return {
            "keyboard": [
                [{"text": "🚀 Quét Toàn Bộ SIM"}, {"text": "📋 Xem Danh Sách"}],
                [{"text": "➕ Thêm Số Theo Dõi"}, {"text": "🗑 Xóa Số"}],
                [{"text": "🔬 Xem Probe"}, {"text": "▶️ Chạy Health Check"}],
                [{"text": "🔄 Trạng Thái Proxy"}, {"text": "⚙️ Trạng Thái Bot"}],
                [{"text": "⏰ Cài Đặt Hẹn Giờ"}, {"text": "🔍 Tra Cứu SIM"}]
            ],
            "resize_keyboard": True,
            "persistent": True
        }

    def get_search_mode_keyboard(self) -> Dict[str, Any]:
        """Inline keyboard shown while user is in SIM search mode."""
        return {
            "inline_keyboard": [
                [{"text": "❌ Thoát Tra Cứu", "callback_data": "action_exit_search"}]
            ]
        }

    def send_message(self, chat_id: int | str, text: str, reply_markup: Optional[Dict[str, Any]] = None, parse_mode: str = "HTML") -> bool:
        """Send formatted message with optional keyboard, auto-chunking long messages (>4000 chars)."""
        if not self.bot_token or self.bot_token.startswith("YOUR_"):
            return False

        # Telegram message length limit is 4096. Chunk text safely at 4000 chars.
        if len(text) > 4000:
            chunks = []
            curr_chunk = []
            curr_len = 0
            for line in text.split("\n"):
                if curr_len + len(line) + 1 > 3800:
                    chunks.append("\n".join(curr_chunk))
                    curr_chunk = [line]
                    curr_len = len(line)
                else:
                    curr_chunk.append(line)
                    curr_len += len(line) + 1
            if curr_chunk:
                chunks.append("\n".join(curr_chunk))

            success = True
            for idx, ch in enumerate(chunks):
                # Only attach keyboard to the last chunk
                markup = reply_markup if idx == len(chunks) - 1 else None
                res = self._send_single_message(chat_id, ch, reply_markup=markup, parse_mode=parse_mode)
                if not res:
                    success = False
            return success

        return self._send_single_message(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)

    def _send_single_message(self, chat_id: int | str, text: str, reply_markup: Optional[Dict[str, Any]] = None, parse_mode: str = "HTML") -> bool:
        """Helper to send a single message payload to Telegram API."""
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
            if r.status_code == 200:
                print(f"[+] [TELEGRAM SENT OK] Message dispatched to Chat ID {chat_id}", flush=True)
                return True
            else:
                print(f"[!] [TELEGRAM ERROR {r.status_code}] Failed for Chat ID {chat_id}: {r.text}", flush=True)
                if "can't parse entities" in r.text and parse_mode:
                    fallback_text = re.sub(r'<[^>]+>', '', text)
                    payload_fallback = {
                        "chat_id": chat_id,
                        "text": fallback_text,
                        "disable_web_page_preview": True
                    }
                    if reply_markup:
                        payload_fallback["reply_markup"] = reply_markup
                    r_retry = self.session.post(url, json=payload_fallback, timeout=10)
                    print(f"[+] [TELEGRAM RETRY OK] Fallback plain-text sent: {r_retry.status_code == 200}", flush=True)
                    return r_retry.status_code == 200
            return False
        except Exception as e:
            print(f"[!] [TELEGRAM EXCEPTION] Failed sending to {chat_id}: {e}", flush=True)
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

    def broadcast(self, text: str, reply_markup: Optional[Dict[str, Any]] = None, parse_mode: str = "HTML", exclude_id: Optional[int | str] = None):
        """Broadcast message to all admin chat IDs."""
        admin_ids = self.config.get("admin_chat_ids", [])
        for cid in admin_ids:
            if exclude_id and str(cid) == str(exclude_id):
                continue
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

        # --- Search Mode: intercept phone numbers while user is in search mode ---
        if chat_id in self._search_mode_users:
            # Allow exit commands / menu buttons to pass through normally
            if clean_text in ["❌ Thoát Tra Cứu", "/exit_search"]:
                self._search_mode_users.discard(chat_id)
                self.send_message(
                    chat_id,
                    "✅ <b>Đã thoát chế độ Tra Cứu SIM.</b>\nBạn có thể dùng menu bên dưới để tiếp tục.",
                    reply_markup=self.get_main_keyboard()
                )
                return
            # If it looks like a phone number, handle as search
            phone_match = re.match(r'^[0-9]{9,11}$', re.sub(r'[\s\-.]', '', clean_text))
            if phone_match:
                self.handle_search_sim(chat_id, clean_text)
                return
            # If it's another keyboard button or command, exit search mode first then handle normally
            self._search_mode_users.discard(chat_id)

        if clean_text in ["🔍 Tra Cứu SIM", "/search"]:
            self._search_mode_users.add(chat_id)
            msg = (
                "🔍 <b>CHẾ ĐỘ TRA CỨU SIM ĐÃ BẬT!</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "📲 <b>Gửi số điện thoại</b> bất kỳ để kiểm tra tức thì:\n\n"
                "▫ Bot sẽ <b>tự động nhận diện nhà mạng</b> từ đầu số\n"
                "▫ Tra kho và trả kết quả <b>ngay lập tức</b>\n"
                "▫ Gửi <b>nhiều số liên tiếp</b> để tra nhiều SIM\n\n"
                "💡 <i>Nhấn nút bên dưới hoặc gõ /exit_search để thoát.</i>"
            )
            self.send_message(chat_id, msg, reply_markup=self.get_search_mode_keyboard())
            return
        elif clean_text in ["🚀 Quét Toàn Bộ SIM", "Quét Ngay"]:
            self.handle_command(chat_id, "/scan", [])
        elif clean_text in ["📋 Xem Danh Sách", "Danh Sách"]:
            self.handle_command(chat_id, "/list", [])
        elif clean_text in ["🔬 Xem Probe", "Xem Probe"]:
            self.handle_command(chat_id, "/probe", [])
        elif clean_text in ["▶️ Chạy Health Check", "Chạy Health Check", "🔬 Health Check", "Health Check"]:
            self.handle_command(chat_id, "/probe", ["test"])
        elif clean_text in ["➕ Thêm Số Theo Dõi", "➕ Hướng Dẫn Thêm Số", "Thêm Số"]:
            msg = (
                "➕ <b>CÁCH THÊM SỐ VÀO DANH SÁCH THEO DÕI:</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "👉 <b>Gửi lệnh:</b>\n"
                "▫ <code>/add viettel 0981945794 0365141705</code>\n"
                "▫ <code>/add mobifone 0703608734</code>\n"
                "▫ <code>/add vinaphone 0812225033</code>\n"
                "▫ <code>/add vietnamobile 0564441185</code>\n\n"
                "💡 <i>Bạn cũng có thể chỉ gửi <code>/add 0981945794...</code> bot sẽ tự nhận diện nhà mạng!</i>"
            )
            self.send_message(chat_id, msg, reply_markup=self.get_main_keyboard())
        elif clean_text in ["🗑 Xóa Số", "🗑 Hướng Dẫn Xóa Số", "Xóa"]:
            msg = (
                "🗑 <b>CÁCH XÓA SỐ KHỎI THEO DÕI:</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "👉 <b>Gửi lệnh:</b>\n"
                "▫ <code>/del 0981945794</code>\n"
                "▫ Hoặc xóa nhiều số: <code>/del 0981945794 0703608734</code>"
            )
            self.send_message(chat_id, msg, reply_markup=self.get_main_keyboard())
        elif clean_text in ["⏰ Cài Đặt Hẹn Giờ", "Hẹn Giờ"]:
            times = ", ".join([f"<code>{t}</code>" for t in self.config.get("scheduled_times", [])])
            interval = self.config.get("interval_minutes", 0)
            msg = (
                "⏰ <b>CÀI ĐẶT LẬP LỊCH QUÉT TỰ ĐỘNG:</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"🕒 <b>Mốc giờ quét mỗi ngày:</b> {times}\n"
                f"🔄 <b>Quét định kỳ:</b> {interval} phút/lần\n\n"
                "👉 <b>Cách điều chỉnh:</b>\n"
                "▫ Đổi mốc giờ: <code>/set_time 08:00, 12:00, 19:30</code>\n"
                "▫ Quét mỗi X phút: <code>/set_interval 30</code> (hoặc <code>0</code> để tắt)\n"
                "▫ Đặt delay: <code>/set_delay 2.0</code>"
            )
            self.send_message(chat_id, msg, reply_markup=self.get_main_keyboard())
        elif clean_text in ["🔄 Trạng Thái Proxy", "Proxy"]:
            self.handle_command(chat_id, "/proxy", [])
        elif clean_text in ["⚙️ Trạng Thái Bot", "Trạng Thái"]:
            self.handle_command(chat_id, "/status", [])
        elif clean_text.startswith("/"):
            parts = clean_text.split()
            cmd = parts[0].split("@")[0]
            args = parts[1:]
            self.handle_command(chat_id, cmd, args)
        elif re.match(r'^0\d{9}$', clean_text):
            self.handle_command(chat_id, "/check", [clean_text])
        else:
            self.handle_command(chat_id, "/start", [])

    def handle_search_sim(self, chat_id: int | str, raw_phone: str):
        """Handle SIM lookup in search mode: detect carrier, query stock, reply with result."""
        # Normalize phone number
        clean_num = re.sub(r'\D', '', raw_phone)
        if clean_num.startswith("84") and len(clean_num) == 11:
            clean_num = "0" + clean_num[2:]
        elif not clean_num.startswith("0") and len(clean_num) == 9:
            clean_num = "0" + clean_num

        if len(clean_num) != 10:
            self.send_message(
                chat_id,
                "⚠️ <b>Số điện thoại không hợp lệ!</b>\n"
                "Vui lòng nhập đúng định dạng 10 chữ số (VD: <code>0981945794</code>).",
                reply_markup=self.get_search_mode_keyboard()
            )
            return

        # Detect carrier from prefix
        carrier = CarrierDetector.get_carrier(clean_num)
        carrier_display = {
            "VIETTEL":     "Viettel",
            "MOBIFONE":    "MobiFone",
            "VINAPHONE":   "VinaPhone",
            "VIETNAMOBILE": "Vietnamobile",
        }.get(carrier, carrier)

        if carrier == "UNKNOWN":
            self.send_message(
                chat_id,
                f"❓ <b>Không nhận diện được nhà mạng</b> cho số <code>{clean_num}</code>\n"
                f"Đầu số <code>{clean_num[:3]}</code> chưa nằm trong danh sách nhận diện.",
                reply_markup=self.get_search_mode_keyboard()
            )
            return

        # Notify user we're checking (with carrier info)
        carrier_emoji = {
            "VIETTEL":     "🔴",
            "MOBIFONE":    "🟠",
            "VINAPHONE":   "🔵",
            "VIETNAMOBILE": "🟢",
        }.get(carrier, "📱")
        self.send_message(
            chat_id,
            f"{carrier_emoji} Nhận diện: <b>{carrier_display}</b> — Đang tra kho..."
        )

        # Run check in background thread to avoid blocking polling loop
        def _do_search():
            try:
                hub = SimCheckerHub(
                    proxy=self.config.get("proxy"),
                    delay=self.config.get("delay_seconds", 1.5)
                )
                res = hub.check_sim(clean_num)

                if res.get("available"):
                    item = res["items"][0]
                    raw_type = item.get("type", "N/A")
                    clean_type = html.escape(re.sub(r'<[^>]+>', ' ', str(raw_type)).strip())
                    clean_price = html.escape(str(item.get("price", "")))
                    result_text = (
                        f"🎉 <b>KẾT QUẢ TRA CỨU</b>\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"{carrier_emoji} <b>Nhà mạng:</b> {carrier_display}\n"
                        f"📱 <b>Số SIM:</b> <code>{item['phone']}</code>\n"
                        f"✅ <b>Trạng thái:</b> <b>CÒN BÁN TRÊN KHO</b>\n"
                        f"🏷 <b>Loại:</b> {clean_type}\n"
                        f"💰 <b>Giá:</b> {clean_price}\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"💡 <i>Gửi số tiếp theo để tra cứu thêm.</i>"
                    )
                else:
                    raw_note = res.get("note") or res.get("error") or "Đã bán hoặc chưa lên kho"
                    clean_note = html.escape(re.sub(r'<[^>]+>', ' ', str(raw_note)).strip())
                    result_text = (
                        f"❌ <b>KẾT QUẢ TRA CỨU</b>\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"{carrier_emoji} <b>Nhà mạng:</b> {carrier_display}\n"
                        f"📱 <b>Số SIM:</b> <code>{clean_num}</code>\n"
                        f"🚫 <b>Trạng thái:</b> KHÔNG CÓ TRONG KHO\n"
                        f"ℹ️ <b>Ghi chú:</b> {clean_note}\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"💡 <i>Gửi số tiếp theo để tra cứu thêm.</i>"
                    )

                self.send_message(chat_id, result_text, reply_markup=self.get_search_mode_keyboard())

            except Exception as e:
                self.send_message(
                    chat_id,
                    f"⚠️ <b>Lỗi khi tra cứu số <code>{clean_num}</code>:</b> {html.escape(str(e))}",
                    reply_markup=self.get_search_mode_keyboard()
                )

        threading.Thread(target=_do_search, daemon=True).start()

    def handle_command(self, chat_id: int | str, command: str, args: List[str]):
        """Process incoming bot command."""
        cmd = command.lower()

        if cmd in ["/start", "/help"]:
            help_text = (
                "🤖 <b>BOT CHECK SIM 4 NHÀ MẠNG TỰ ĐỘNG</b>\n"
                "<i>(Viettel • MobiFone • VinaPhone • Vietnamobile)</i>\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                "📋 <b>Danh sách lệnh điều khiển:</b>\n\n"
                "▫ <code>/scan</code> — Quét toàn bộ danh sách theo dõi ngay\n"
                "▫ <code>/list</code> — Xem danh sách các số đang theo dõi\n"
                "▫ <code>/check &lt;số&gt;</code> — Kiểm tra ngay 1 số bất kỳ\n"
                "▫ <code>/add &lt;số_1&gt; &lt;số_2&gt;...</code> — Thêm số (Tự động nhận diện nhà mạng)\n"
                "▫ <code>/del &lt;số&gt;</code> — Xóa số khỏi theo dõi\n"
                "▫ <code>/set_time &lt;HH:MM...&gt;</code> — Hẹn giờ quét tự động hàng ngày\n"
                "▫ <code>/set_interval &lt;phút&gt;</code> — Quét định kỳ lặp lại\n"
                "▫ <code>/set_delay &lt;giây&gt;</code> — Cài đặt delay an toàn\n"
                "▫ <code>/probe</code> — Cấu hình & test số Probe Health Check\n"
                "▫ <code>/proxy</code> — Xem danh sách Proxy live & Cào mới\n"
                "▫ <code>/status</code> — Xem trạng thái hệ thống"
            )
            self.send_message(chat_id, help_text, reply_markup=self.get_main_keyboard())

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
                msg = (
                    "⚠️ <b>Cú pháp thêm số theo dõi:</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "👉 <b>Cách 1: Tự động nhận diện nhà mạng (Khuyên dùng):</b>\n"
                    "  <code>/add 0981945794 0703141705 0564141705</code>\n\n"
                    "👉 <b>Cách 2: Gõ kèm tên nhà mạng:</b>\n"
                    "  <code>/add viettel 0981945794 0365141705</code>"
                )
                self.send_message(chat_id, msg, reply_markup=self.get_main_keyboard())
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
            
            hub = SimCheckerHub(proxy=self.config.get("proxy"), delay=self.config.get("delay_seconds", 1.5))
            res = hub.check_sim(phone)
            carrier = res.get("carrier", "UNKNOWN")

            if res.get("available"):
                item = res["items"][0]
                raw_type = item.get('type', 'N/A')
                clean_type = html.escape(re.sub(r'<[^>]+>', ' ', str(raw_type)).strip())
                clean_price = html.escape(str(item.get('price', '')))
                text = (
                    f"🎉 <b>KẾT QUẢ: CÒN BÁN TRÊN KHO {carrier}!</b>\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"📱 <b>Số SIM:</b> <code>{item['phone']}</code>\n"
                    f"🏷 <b>Loại:</b> {clean_type}\n"
                    f"💰 <b>Giá:</b> {clean_price}\n"
                    f"━━━━━━━━━━━━━━━━━━"
                )
            else:
                raw_note = res.get('note', 'Đã bán hoặc chưa lên kho')
                clean_note = html.escape(re.sub(r'<[^>]+>', ' ', str(raw_note)).strip())
                text = (
                    f"❌ <b>KẾT QUẢ: KHÔNG CÓ TRÊN KHO {carrier}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"📱 <b>Số SIM:</b> <code>{phone}</code>\n"
                    f"ℹ️ <b>Ghi chú:</b> {clean_note}\n"
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

        elif cmd == "/proxy":
            vtl_mgr = ProxyPoolManager()
            vnmb_mgr = VietnamobileProxyPoolManager()
            if args and args[0].lower() in ["fetch", "refresh", "hunt"]:
                self.send_message(chat_id, "🌐 <b>Đang kích hoạt Proxy Hunter cào và lọc 10 Proxy live mới cho Viettel & Vietnamobile...</b>\nVui lòng chờ khoảng 15-30 giây.", reply_markup=self.get_main_keyboard())
                def run_hunt():
                    try:
                        vtl_list = []
                        vnmb_list = []
                        def _h_vtl():
                            nonlocal vtl_list
                            vtl_list = vtl_mgr.refresh_proxies(target_count=10, target_name="Viettel")
                        def _h_vnmb():
                            nonlocal vnmb_list
                            vnmb_list = vnmb_mgr.refresh_proxies(target_count=10, max_attempts=5)
                        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
                            ex.submit(_h_vtl)
                            ex.submit(_h_vnmb)
                        self.send_message(
                            chat_id,
                            f"✅ <b>Đã cào xong Proxy live mới nhất!</b>\n"
                            f"▫ <b>Viettel:</b> {len(vtl_list)} IP\n"
                            f"▫ <b>Vietnamobile:</b> {len(vnmb_list)} IP\n\n"
                            f"📊 Kho Proxy đã sẵn sàng sử dụng.",
                            reply_markup=self.get_main_keyboard()
                        )
                    except Exception as e:
                        self.send_message(chat_id, f"❌ <b>Lỗi cào Proxy:</b> {e}", reply_markup=self.get_main_keyboard())
                threading.Thread(target=run_hunt, daemon=True).start()
            else:
                vtl_count = vtl_mgr.get_proxy_count()
                vnmb_count = vnmb_mgr.get_proxy_count()
                msg = (
                    "🌐 <b>TRẠNG THÁI KHO PROXY:</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"📊 <b>Proxy Viettel live:</b> <code>{vtl_count}</code> IP\n"
                    f"📊 <b>Proxy Vietnamobile live:</b> <code>{vnmb_count}</code> IP\n"
                    "💡 <i>Mỗi đợt quét bot sẽ tự động cào proxy mới để chống chặn IP!</i>\n\n"
                    "👉 <b>Gõ <code>/proxy fetch</code> để cào mới proxy cả 2 nhà mạng ngay bây giờ.</b>"
                )
                self.send_message(chat_id, msg, reply_markup=self.get_main_keyboard())

        elif cmd == "/probe":
            probe_numbers = self.config.setdefault("probe_numbers", {
                "VIETTEL_PREPAID": "", "VIETTEL_POSTPAID": "",
                "MOBIFONE": "", "VINAPHONE": "", "VIETNAMOBILE": ""
            })
            PROBE_LABELS = {
                "VIETTEL_PREPAID":  "Viettel Trả Trước",
                "VIETTEL_POSTPAID": "Viettel Trả Sau",
                "MOBIFONE":         "MobiFone",
                "VINAPHONE":        "VinaPhone",
                "VIETNAMOBILE":     "Vietnamobile",
            }
            KEY_MAP = {
                "viettel_prepaid":  "VIETTEL_PREPAID",
                "viettel_postpaid": "VIETTEL_POSTPAID",
                "mobifone":         "MOBIFONE",
                "vinaphone":        "VINAPHONE",
                "vietnamobile":     "VIETNAMOBILE",
            }

            if not args:
                # Show current probe numbers and usage
                lines = ["🔬 <b>CẤU HÌNH SỐ PROBE HEALTH CHECK:</b>", "━━━━━━━━━━━━━━━━━━"]
                for key, label in PROBE_LABELS.items():
                    num = probe_numbers.get(key, "")
                    val = f"<code>{num}</code>" if num else "<i>(chưa cài)</i>"
                    lines.append(f"▫ <b>{label}:</b> {val}")
                lines += [
                    "",
                    "👉 <b>Cách cài số probe:</b>",
                    "▫ <code>/probe viettel_prepaid 0981xxxxxx</code>",
                    "▫ <code>/probe viettel_postpaid 0365xxxxxx</code>",
                    "▫ <code>/probe mobifone 0703xxxxxx</code>",
                    "▫ <code>/probe vinaphone 0812xxxxxx</code>",
                    "▫ <code>/probe vietnamobile 0564xxxxxx</code>",
                    "▫ <code>/probe test</code> — Chạy Health Check thử ngay",
                ]
                self.send_message(chat_id, "\n".join(lines), reply_markup=self.get_main_keyboard())

            elif args[0].lower() == "test":
                self.send_message(chat_id, "🔬 <b>Đang chạy Health Check...</b>\nVui lòng chờ vài giây.")
                def _run_hc():
                    self.run_health_check(initiator_chat_id=chat_id)
                threading.Thread(target=_run_hc, daemon=True).start()

            else:
                config_key = KEY_MAP.get(args[0].lower())
                if not config_key:
                    self.send_message(chat_id,
                        f"❌ Carrier không hợp lệ. Dùng: <code>{', '.join(KEY_MAP.keys())}</code>",
                        reply_markup=self.get_main_keyboard())
                    return
                if len(args) < 2:
                    self.send_message(chat_id,
                        f"⚠️ <b>Cú pháp:</b> <code>/probe {args[0]} 0xxxxxxxxx</code>",
                        reply_markup=self.get_main_keyboard())
                    return
                num = re.sub(r'\D', '', args[1])
                if num.startswith("84") and len(num) == 11:
                    num = "0" + num[2:]
                elif not num.startswith("0") and len(num) == 9:
                    num = "0" + num
                if len(num) != 10:
                    self.send_message(chat_id, "❌ Số điện thoại không hợp lệ (cần 10 chữ số).",
                        reply_markup=self.get_main_keyboard())
                    return
                probe_numbers[config_key] = num
                self.config["probe_numbers"] = probe_numbers
                ConfigManager.save(self.config)
                label = PROBE_LABELS.get(config_key, config_key)
                self.send_message(chat_id,
                    f"✅ <b>Đã cập nhật số probe {label}:</b> <code>{num}</code>",
                    reply_markup=self.get_main_keyboard())

        elif cmd == "/status":
            watchlist = WatchlistManager.load()
            total_count = sum(len(nums) for nums in watchlist.values())
            times = ", ".join([f"<code>{t}</code>" for t in self.config.get("scheduled_times", [])]) or "Chưa cài đặt"
            interval = self.config.get("interval_minutes", 0)
            interval_str = f"Mỗi <code>{interval}</code> phút" if interval > 0 else "Đang tắt"
            delay = self.config.get("delay_seconds", 1.5)
            max_workers = self.config.get("max_workers", 4)
            vtl_proxy_count = ProxyPoolManager().get_proxy_count()
            vnmb_proxy_count = VietnamobileProxyPoolManager().get_proxy_count()
            probe_numbers = self.config.get("probe_numbers", {})
            active_probes = sum(1 for v in probe_numbers.values() if v and v.strip())

            status_text = (
                "⚙️ <b>TRẠNG THÁI HỆ THỐNG:</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📊 <b>Tổng số SIM theo dõi:</b> <code>{total_count}</code> SIM\n"
                f"⚡ <b>Luồng quét song song:</b> <code>{max_workers}</code> luồng\n"
                f"🌐 <b>Kho Proxy Viettel:</b> <code>{vtl_proxy_count}</code> IP\n"
                f"🌐 <b>Kho Proxy Vietnamobile:</b> <code>{vnmb_proxy_count}</code> IP\n"
                f"🔬 <b>Số probe Health Check:</b> <code>{active_probes}/5</code> đã cài\n"
                f"⏰ <b>Mốc giờ quét mỗi ngày:</b> {times}\n"
                f"🔄 <b>Quét chu kỳ phút:</b> {interval_str}\n"
                f"⏳ <b>Delay an toàn:</b> <code>{delay}s</code> / lượt\n"
                f"🚀 <b>Trạng thái Bot:</b> Đang hoạt động 🟢\n"
                f"━━━━━━━━━━━━━━━━━━"
            )
            self.send_message(chat_id, status_text, reply_markup=self.get_main_keyboard())

    def run_health_check(self, initiator_chat_id: Optional[int | str] = None, refresh_vnmb_proxies: bool = True) -> Dict[str, bool]:
        """
        Run health checks against configured probe numbers for all carriers.
        Sends a formatted Telegram report of results.
        refresh_vnmb_proxies: If True, scrapes fresh Vietnamobile proxies before probe check.
                              Set to False when called from run_full_scan which pre-loads proxies.
        Returns: dict of probe_key -> passed (True/False).
        """
        probe_numbers = self.config.get("probe_numbers", {})
        active_probes = {k: v.strip() for k, v in probe_numbers.items() if v and v.strip()}

        if not active_probes:
            print("[*] Health Check: Chưa cài số probe nào, bỏ qua.", flush=True)
            if initiator_chat_id:
                msg = (
                    "⚠️ <b>CHƯA CẤU HÌNH SỐ PROBE HEALTH CHECK!</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "Hiện chưa có số probe nào được cài đặt.\n\n"
                    "👉 <b>Hướng dẫn cài đặt số probe:</b>\n"
                    "▫ <code>/probe viettel_prepaid 0981xxxxxx</code>\n"
                    "▫ <code>/probe viettel_postpaid 0365xxxxxx</code>\n"
                    "▫ <code>/probe mobifone 0703xxxxxx</code>\n"
                    "▫ <code>/probe vinaphone 0812xxxxxx</code>\n"
                    "▫ <code>/probe vietnamobile 0564xxxxxx</code>"
                )
                self.send_message(initiator_chat_id, msg, reply_markup=self.get_main_keyboard())
            return {}

        print(f"[*] Health Check: Đang kiểm tra {len(active_probes)} probe số...", flush=True)

        # When triggered manually via Telegram, refresh Viettel and Vietnamobile proxy pools in parallel
        if initiator_chat_id and self.config.get("auto_proxy_refresh", True):
            print("[*] Health Check: Cào proxy mới song song trước khi kiểm tra...", flush=True)
            def _fetch_vtl_hc():
                try:
                    ProxyPoolManager().reset_attempts()
                    ProxyPoolManager().refresh_proxies(target_count=10, target_name="Viettel")
                    print("[*] Health Check: Proxy Viettel sẵn sàng.", flush=True)
                except Exception as proxy_err:
                    print(f"[!] Health Check Viettel proxy notice: {proxy_err}", flush=True)

            def _fetch_vnmb_hc():
                if refresh_vnmb_proxies and "VIETNAMOBILE" in active_probes:
                    try:
                        VietnamobileProxyPoolManager().refresh_proxies(target_count=10, max_attempts=5)
                        print("[*] Health Check: Proxy Vietnamobile sẵn sàng.", flush=True)
                    except Exception as proxy_err:
                        print(f"[!] Health Check VNMB proxy notice: {proxy_err}", flush=True)

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as px_executor:
                px_executor.submit(_fetch_vtl_hc)
                px_executor.submit(_fetch_vnmb_hc)

        hub = SimCheckerHub(proxy=self.config.get("proxy"), delay=0.5)
        hc_results = hub.health_check(active_probes)

        CARRIER_LABELS = {
            "VIETTEL_PREPAID":  "Viettel Trả Trước",
            "VIETTEL_POSTPAID": "Viettel Trả Sau",
            "MOBIFONE":         "MobiFone",
            "VINAPHONE":        "VinaPhone",
            "VIETNAMOBILE":     "Vietnamobile",
        }

        all_passed = all(r["passed"] for r in hc_results.values())
        any_failed = any(not r["passed"] for r in hc_results.values())

        lines = ["🔬 <b>HEALTH CHECK — TRẠNG THÁI API NHÀ MẠNG:</b>", "━━━━━━━━━━━━━━━━━━"]
        for key, res in hc_results.items():
            label = CARRIER_LABELS.get(key, key)
            icon = "✅" if res["passed"] else "❌"
            status_text = "READY" if res["passed"] else "NOT READY"
            # Terminal log vẫn giữ chi tiết để debug
            print(f"  [{icon}] {label} ({res['probe']}): {'PASS' if res['passed'] else 'FAIL — ' + res['note']}", flush=True)
            lines.append(f"{icon} <b>{label}:</b> {status_text}")

        lines.append("")
        if all_passed:
            lines.append("🚀 <i>Tất cả API sẵn sàng — Bắt đầu quét SIM...</i>")
        elif any_failed:
            lines.append("⚠️ <i>Một số API chưa sẵn sàng — Sẽ bỏ qua carrier đó khi quét.</i>")

        msg = "\n".join(lines)
        if initiator_chat_id:
            self.send_message(initiator_chat_id, msg, reply_markup=self.get_main_keyboard())
            self.broadcast(msg, exclude_id=initiator_chat_id)
        else:
            self.broadcast(msg)

        return {k: v["passed"] for k, v in hc_results.items()}

    def run_full_scan(self, initiator_chat_id: Optional[int | str] = None):
        """Execute full scan and send carrier-grouped summary report."""
        if self.is_scanning:
            print("[!] Scan already in progress, skipping duplicate scan request.")
            return

        self.is_scanning = True
        try:
            # Pre-scan: Refresh Viettel and Vietnamobile proxy pools in parallel
            if self.config.get("auto_proxy_refresh", True):
                print("[*] Pre-scan: Cào proxy live mới song song cho Viettel và Vietnamobile...", flush=True)
                def _fetch_vtl_scan():
                    try:
                        ProxyPoolManager().reset_attempts()
                        fast_list = ProxyPoolManager().refresh_proxies(target_count=10, target_name="Viettel")
                        print(f"[*] Pre-scan: Đã cào xong {len(fast_list)} proxy Viettel live.", flush=True)
                    except Exception as err:
                        print(f"[!] Pre-scan Viettel proxy notice: {err}", flush=True)

                def _fetch_vnmb_scan():
                    try:
                        VietnamobileProxyPoolManager().refresh_proxies(target_count=10, max_attempts=5)
                        print("[*] Pre-scan: Proxy Vietnamobile sẵn sàng.", flush=True)
                    except Exception as err:
                        print(f"[!] Pre-scan Vietnamobile proxy notice: {err}", flush=True)

                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as px_executor:
                    px_executor.submit(_fetch_vtl_scan)
                    px_executor.submit(_fetch_vnmb_scan)

            # Step 2: Health check — skip internal Vietnamobile proxy refresh (already done above)
            health_results = self.run_health_check(initiator_chat_id=initiator_chat_id, refresh_vnmb_proxies=False)

            # Determine which carriers to skip based on failed health checks
            skip_carriers: set = set()
            active_probes = {k: v.strip() for k, v in self.config.get("probe_numbers", {}).items() if v and v.strip()}
            if active_probes:
                for carrier in ["MOBIFONE", "VINAPHONE", "VIETNAMOBILE"]:
                    if carrier in active_probes and not health_results.get(carrier, True):
                        skip_carriers.add(carrier)
                # Viettel: skip only if ALL configured Viettel probes fail
                has_pre  = "VIETTEL_PREPAID"  in active_probes
                has_post = "VIETTEL_POSTPAID" in active_probes
                if has_pre or has_post:
                    pre_ok  = health_results.get("VIETTEL_PREPAID",  not has_pre)
                    post_ok = health_results.get("VIETTEL_POSTPAID", not has_post)
                    if not pre_ok and not post_ok:
                        skip_carriers.add("VIETTEL")

            if skip_carriers:
                print(f"[!] Health Check FAIL — Bỏ qua carrier: {skip_carriers}", flush=True)

            watchlist = WatchlistManager.load()
            delay = self.config.get("delay_seconds", 1.5)
            proxy = self.config.get("proxy")
            hub = SimCheckerHub(proxy=proxy, delay=delay)

            total_sims = []
            for carrier, nums in watchlist.items():
                if carrier in skip_carriers:
                    print(f"[!] Bỏ qua {carrier} do Health Check FAIL.", flush=True)
                    continue
                for num in nums:
                    total_sims.append({"phone": num, "carrier": carrier})

            if not total_sims:
                if initiator_chat_id:
                    self.send_message(initiator_chat_id, "⚠️ Danh sách theo dõi đang trống! Hãy dùng <code>/add</code> để thêm số.", reply_markup=self.get_main_keyboard())
                else:
                    print("[!] Scheduled scan triggered but watchlist is empty.")
                return

            hits = []
            bads = []

            max_workers = self.config.get("max_workers", 4)
            print(f"[*] Starting parallel scan of {len(total_sims)} SIMs ({max_workers} workers) at {datetime.datetime.now().strftime('%H:%M:%S')}...")

            def scan_progress_cb(res, current_idx, total_count):
                carrier = res.get("carrier", "UNKNOWN")
                num = res.get("phone", "")
                if res.get("available"):
                    hits.append(res)
                    first_item = res["items"][0]
                    print(f"  [{current_idx}/{total_count}] [+] HIT: {num} [{carrier}] - {first_item['price']}", flush=True)
                else:
                    bads.append(res)
                    print(f"  [{current_idx}/{total_count}] [-] BAD: {num} [{carrier}] - {res.get('note', 'Không có trong kho')}", flush=True)

            hub.check_sims_parallel(total_sims, max_workers=max_workers, progress_callback=scan_progress_cb)

            # Summary Header
            summary_lines = [
                "📊 <b>BÁO CÁO KẾT QUẢ QUÉT SIM TỔNG HỢP</b>",
                f"⏰ <i>Thời gian: {datetime.datetime.now().strftime('%H:%M:%S - %d/%m/%Y')}</i>",
                "━━━━━━━━━━━━━━━━━━",
                f"▫ Tổng số đã quét: <b>{len(total_sims)}</b> SIM",
                f"▫ Số SIM CÒN BÁN: <b>{len(hits)}</b> SIM",
                f"▫ Số SIM CHƯA CÓ / LỖI: <b>{len(bads)}</b> SIM\n"
            ]

            # 1. PHẦN 1: CÓ TRONG KHO (NHÓM THEO NHÀ MẠNG)
            if hits:
                summary_lines.append("🎉 <b>DANH SÁCH SIM CÒN BÁN (CÓ TRONG KHO):</b>\n")
                for c_key in ["VIETTEL", "MOBIFONE", "VINAPHONE", "VIETNAMOBILE"]:
                    c_hits = [h for h in hits if h.get("carrier") == c_key]
                    if c_hits:
                        summary_lines.append(f"📱 <b>{c_key}:</b>")
                        for h in c_hits:
                            item = h["items"][0]
                            raw_type = item.get('type')
                            clean_type = re.sub(r'<[^>]+>', ' ', str(raw_type)).strip() if raw_type else ""
                            type_str = f" ({html.escape(clean_type)})" if clean_type else ""
                            clean_price = html.escape(str(item.get('price', '')))
                            summary_lines.append(f" • <code>{item['phone']}</code>: {clean_price}{type_str}")
                        summary_lines.append("")
            else:
                summary_lines.append("🎉 <b>DANH SÁCH SIM CÒN BÁN (CÓ TRONG KHO):</b>")
                summary_lines.append("<i>(Hiện tại chưa có SIM nào trong kho)</i>\n\n")

            # 2. PHẦN 2: CHƯA CÓ TRONG KHO (NHÓM THEO NHÀ MẠNG)
            if bads:
                summary_lines.append("❌ <b>DANH SÁCH SIM CHƯA CÓ TRONG KHO / LỖI TRUY VẤN:</b>\n")
                for c_key in ["VIETTEL", "MOBIFONE", "VINAPHONE", "VIETNAMOBILE"]:
                    c_bads = [b for b in bads if b.get("carrier") == c_key]
                    if c_bads:
                        summary_lines.append(f"📱 <b>{c_key}:</b>")
                        for b in c_bads:
                            raw_note = b.get("note") or b.get("error") or "Không có trong kho"
                            clean_note = re.sub(r'<[^>]+>', ' ', str(raw_note))
                            clean_note = re.sub(r'\(.*?\)', '', clean_note).strip()
                            if not clean_note:
                                clean_note = "Không có trong kho"
                            summary_lines.append(f" • <code>{b['phone']}</code>: <i>{html.escape(clean_note)}</i>")
                        summary_lines.append("")

            summary_lines.append("━━━━━━━━━━━━━━━━━━")
            report_msg = "\n".join(summary_lines)

            if initiator_chat_id:
                self.send_message(initiator_chat_id, report_msg, reply_markup=self.get_main_keyboard())
            self.broadcast(report_msg, reply_markup=self.get_main_keyboard(), exclude_id=initiator_chat_id)

            print(f"[*] Scan finished. Found {len(hits)} available SIMs.")

        except Exception as e:
            print(f"[!] Error during run_full_scan: {e}")
        finally:
            self.is_scanning = False

    def scheduler_loop(self):
        """Background thread for automated scheduled scans."""
        while True:
            try:
                # Reload config every loop iteration so manual edits to config.json take effect immediately
                self.config = ConfigManager.load()
                
                # Skip scheduler checks if automated scanning is disabled
                if not self.config.get("auto_scan_enabled", True):
                    time.sleep(10)
                    continue

                now = datetime.datetime.now()
                now_str = now.strftime("%H:%M")
                
                # Check fixed daily scheduled times
                scheduled_times = self.config.get("scheduled_times", [])
                if now_str != self.last_checked_minute:
                    self.last_checked_minute = now_str
                    if now_str in scheduled_times:
                        if not self.is_scanning:
                            print(f"⏰ [Scheduler] Triggering scheduled scan for {now_str}...", flush=True)
                            threading.Thread(target=self.run_full_scan, daemon=True).start()

                # Check interval minutes
                interval_min = self.config.get("interval_minutes", 0)
                if interval_min > 0:
                    elapsed = time.time() - self.last_interval_scan
                    if elapsed >= interval_min * 60:
                        self.last_interval_scan = time.time()
                        if not self.is_scanning:
                            print(f"🔄 [Scheduler] Triggering interval scan (every {interval_min}m)...", flush=True)
                            threading.Thread(target=self.run_full_scan, daemon=True).start()

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
                        elif cb_data == "action_search":
                            # Activate search mode via inline button
                            self.handle_text_or_command(cb_chat_id, "🔍 Tra Cứu SIM")
                        elif cb_data == "action_exit_search":
                            # Deactivate search mode and return to main menu
                            self._search_mode_users.discard(cb_chat_id)
                            self.send_message(
                                cb_chat_id,
                                "✅ <b>Đã thoát chế độ Tra Cứu SIM.</b>\nBạn có thể dùng menu bên dưới để tiếp tục.",
                                reply_markup=self.get_main_keyboard()
                            )
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
