"""
SIM Availability Checker for All 4 Major Vietnamese Carriers:
Viettel, MobiFone, VinaPhone, Vietnamobile
Direct Fast Backend APIs (Runs 100% in Background)
"""

import sys
import re
import ssl
import time
import json
import uuid
import argparse
import threading
import concurrent.futures
import urllib3
import urllib.request
import urllib.parse
from typing import Dict, Any, List, Optional, Callable
from curl_cffi import requests

# Suppress SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Import Proxy Manager
from proxy_hunter import ProxyPoolManager, VietnamobileProxyPoolManager

import functools
print = functools.partial(print, flush=True)

# Ensure UTF-8 output on Windows console with line buffering
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    except Exception:
        pass


class CarrierDetector:
    """Helper to detect mobile carrier from Vietnamese phone prefix."""

    VIETTEL_PREFIXES = ["086", "096", "097", "098", "032", "033", "034", "035", "036", "037", "038", "039"]
    MOBIFONE_PREFIXES = ["089", "090", "093", "070", "079", "077", "076", "078"]
    VINAPHONE_PREFIXES = ["088", "091", "094", "081", "082", "083", "084", "085"]
    VIETNAMOBILE_PREFIXES = ["092", "056", "058", "052"]

    @classmethod
    def get_carrier(cls, formatted_phone: str) -> str:
        for prefix in cls.VIETTEL_PREFIXES:
            if formatted_phone.startswith(prefix):
                return "VIETTEL"
        for prefix in cls.MOBIFONE_PREFIXES:
            if formatted_phone.startswith(prefix):
                return "MOBIFONE"
        for prefix in cls.VINAPHONE_PREFIXES:
            if formatted_phone.startswith(prefix):
                return "VINAPHONE"
        for prefix in cls.VIETNAMOBILE_PREFIXES:
            if formatted_phone.startswith(prefix):
                return "VIETNAMOBILE"
        return "UNKNOWN"


class ViettelApiChecker:
    """Fast backend checker for Viettel Telecom SIM inventory with configurable delay and auto proxy rotation."""

    BASE_URL = "https://apigami.viettel.vn/mvt-api/myviettel.php/omiSearchSimV2"

    def __init__(self, proxy: Optional[str] = None, delay: float = 1.5, auto_proxy_rotation: bool = True):
        self.session = requests.Session(impersonate="chrome131")
        self.delay = delay
        self.auto_proxy_rotation = auto_proxy_rotation
        self.proxy_pool = ProxyPoolManager()
        self.headers = {
            "accept": "application/json, text/plain, */*",
            "origin": "https://vietteltelecom.vn",
            "referer": "https://vietteltelecom.vn/vx/di-dong/sim-so/",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "content-type": "application/x-www-form-urlencoded",
        }
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}

    def _rotate_proxy(self) -> Optional[str]:
        """Fetch next live proxy from pre-hunted pool when Viettel rate limits local IP."""
        new_proxy = self.proxy_pool.get_next_proxy()
        if not new_proxy:
            print("[!] 10 Proxy trong nhóm hiện tại đều bị chặn/hết ➔ Cào đợt 10 Proxy live tiếp theo cho Viettel...", flush=True)
            try:
                self.proxy_pool.fetch_next_batch(target_count=10, max_attempts=10, target_name="Viettel")
                new_proxy = self.proxy_pool.get_next_proxy()
            except RuntimeError as err:
                print(f"[!] {err}", flush=True)
                return None

        if new_proxy:
            print(f"[*] Viettel bị chặn IP ➔ Đổi sang Proxy live: {new_proxy}", flush=True)
            try:
                self.session = requests.Session(impersonate="chrome131")
                self.session.proxies = {"http": new_proxy, "https": new_proxy}
            except Exception as e:
                print(f"[!] Error setting session proxy: {e}", flush=True)
        else:
            print("[!] Không tìm thấy Proxy live khả dụng.", flush=True)
        return new_proxy

    def _query(self, search_key: str, isdn_type: str, max_retries: int = 6) -> Dict[str, Any]:
        """Query Viettel API for a specific isdn_type with automatic proxy rotation and retries."""
        for attempt in range(max_retries + 1):
            if self.delay > 0:
                time.sleep(self.delay)

            params = {
                "isdn_type": isdn_type,
                "page_type": "",
                "page": "1",
                "page_size": "45",
                "key_search": search_key,
                "total_record": "1",
                "captcha": "",
                "sid": uuid.uuid4().hex[:16]
            }

            try:
                response = self.session.post(self.BASE_URL, params=params, headers=self.headers, timeout=10, verify=False)
                if response.status_code != 200:
                    if self.auto_proxy_rotation and attempt < max_retries:
                        self._rotate_proxy()
                        continue
                    return {"items": [], "error": f"HTTP {response.status_code}", "note": f"Lỗi kết nối HTTP {response.status_code}"}

                data = response.json()
                msg = data.get("message", "")
                err_code_tracing = data.get("errorCodeTracing", "")

                if "quá nhanh" in msg or "vui lòng chờ" in msg or "vượt quá hạn mức" in msg or err_code_tracing == "ERR_000505":
                    print(f"[!] Viettel Rate Limit hit ({msg or err_code_tracing}). Rotating proxy...")
                    if self.auto_proxy_rotation and attempt < max_retries:
                        self._rotate_proxy()
                        time.sleep(1.0)
                        continue
                    return {"items": [], "rate_limited": True, "note": msg or "Bị giới hạn IP"}

                if data.get("errorCode") == 0:
                    return {"items": data.get("data") or [], "rate_limited": False}

                return {"items": [], "rate_limited": False, "note": msg or "Không tìm thấy dữ liệu"}

            except Exception as e:
                err_str = str(e)
                if self.auto_proxy_rotation and attempt < max_retries:
                    print(f"[!] Viettel network error ({err_str}). Rotating proxy...", flush=True)
                    self._rotate_proxy()
                    time.sleep(0.5)
                    continue

                # Final fallback attempt: Try direct IP connection without proxy if proxy failed
                if getattr(self.session, "proxies", None):
                    print(f"[!] Viettel proxy error ({err_str}). Attempting direct IP fallback...", flush=True)
                    try:
                        direct_session = requests.Session(impersonate="chrome131")
                        resp = direct_session.post(self.BASE_URL, params=params, headers=self.headers, timeout=10)
                        if resp.status_code == 200:
                            d = resp.json()
                            if d.get("errorCode") == 0:
                                return {"items": d.get("data") or [], "rate_limited": False}
                    except Exception:
                        pass

                return {"items": [], "rate_limited": False, "error": err_str, "note": f"Lỗi mạng: {err_str}"}

        return {"items": [], "rate_limited": True, "note": "Thao tác quá nhanh"}

    def search_sim(self, clean_num: str) -> Dict[str, Any]:
        """Check Viettel SIM in 2 steps: Step 1 (Prepaid) -> Step 2 (Postpaid)."""
        rate_limit_notes = []

        # --- 1. CHECK KHO TRẢ TRƯỚC (isdn_type=2) ---
        res_pre = self._query(clean_num, isdn_type="2")
        if res_pre.get("rate_limited"):
            rate_limit_notes.append(res_pre.get("note", "Thao tác quá nhanh"))

        for item in res_pre.get("items", []):
            raw_isdn = str(item.get("isdn", "")).strip()
            full_isdn = "0" + raw_isdn if not raw_isdn.startswith("0") else raw_isdn
            
            if clean_num == full_isdn or full_isdn.endswith(clean_num) or clean_num in full_isdn:
                pre_price = item.get("pre_price", "50000")
                unit = item.get("unit", "VNĐ")
                price_str = f"{int(pre_price):,} {unit}" if (pre_price and str(pre_price).isdigit()) else f"{pre_price} {unit}"
                
                return {
                    "phone": full_isdn,
                    "carrier": "VIETTEL",
                    "available": True,
                    "count": 1,
                    "items": [{
                        "phone": full_isdn,
                        "type": "Trả trước",
                        "price": price_str,
                        "raw": item
                    }]
                }

        # --- 2. CHECK KHO TRẢ SAU (isdn_type=22) ---
        res_pos = self._query(clean_num, isdn_type="22")
        if res_pos.get("rate_limited"):
            rate_limit_notes.append(res_pos.get("note", "Thao tác quá nhanh"))

        for item in res_pos.get("items", []):
            raw_isdn = str(item.get("isdn", "")).strip()
            full_isdn = "0" + raw_isdn if not raw_isdn.startswith("0") else raw_isdn
            
            if clean_num == full_isdn or full_isdn.endswith(clean_num) or clean_num in full_isdn:
                pos_price = item.get("pos_price", "60000")
                unit = item.get("unit", "VNĐ")
                price_str = f"{int(pos_price):,} {unit}" if (pos_price and str(pos_price).isdigit()) else f"{pos_price} {unit}"
                
                pledge_amount = item.get("pledge_amount")
                pledge_time = item.get("pledge_time")
                pledge_str = f" | Cam kết: {int(pledge_amount):,} VNĐ/{pledge_time}T" if pledge_amount and str(pledge_amount).isdigit() else ""

                return {
                    "phone": full_isdn,
                    "carrier": "VIETTEL",
                    "available": True,
                    "count": 1,
                    "items": [{
                        "phone": full_isdn,
                        "type": "Trả sau",
                        "price": f"{price_str}{pledge_str}",
                        "raw": item
                    }]
                }

        # If both prepaid and postpaid were rate limited, return rate limit note
        if len(rate_limit_notes) >= 2:
            return {"phone": clean_num, "carrier": "VIETTEL", "available": False, "note": rate_limit_notes[0]}

        return {"phone": clean_num, "carrier": "VIETTEL", "available": False, "note": "Không có trong kho"}


class MobifoneApiChecker:
    """Fast backend checker for MobiFone SIM inventory with configurable delay."""

    BASE_URL = "https://khosim.mobifone.vn/api/sim/getPages"

    def __init__(self, proxy: Optional[str] = None, delay: float = 1.0):
        self.session = requests.Session(impersonate="chrome131")
        self.delay = delay
        self.headers = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "origin": "https://simso.mobifone.vn",
            "referer": "https://simso.mobifone.vn/",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        }
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}

    def search_sim(self, clean_num: str) -> Dict[str, Any]:
        if self.delay > 0:
            time.sleep(self.delay)

        prefix = clean_num[:3]
        suffix = clean_num[3:]

        payload = {
            "type": "",
            "msisdnPrefix": prefix,
            "msisdn": suffix,
            "status": "HIEN_THI",
            "page": 0,
            "size": 10,
            "feeRegisterFrom": None,
            "feeRegisterTo": None
        }

        try:
            response = self.session.post(self.BASE_URL, json=payload, headers=self.headers, timeout=8, verify=False)
            if response.status_code != 200:
                return {"phone": clean_num, "carrier": "MOBIFONE", "available": False, "error": f"HTTP {response.status_code}"}

            data = response.json()
            content_list = data.get("data", {}).get("content", [])

            matched_items = []
            for item in content_list:
                raw_msisdn = str(item.get("msisdn", "")).strip()
                full_msisdn = "0" + raw_msisdn if not raw_msisdn.startswith("0") else raw_msisdn
                
                if clean_num == full_msisdn or full_msisdn.endswith(clean_num) or clean_num in full_msisdn:
                    fee = item.get("feeRegister", 0)
                    sim_type = item.get("type", "N/A")
                    status = item.get("status", "Hiển thị")
                    
                    matched_items.append({
                        "phone": full_msisdn,
                        "type": sim_type,
                        "price": f"{int(fee):,} VNĐ" if isinstance(fee, (int, float)) else str(fee),
                        "status": status,
                        "raw": item
                    })

            if matched_items:
                return {"phone": clean_num, "carrier": "MOBIFONE", "available": True, "count": len(matched_items), "items": matched_items}
            else:
                return {"phone": clean_num, "carrier": "MOBIFONE", "available": False, "note": "Số không có trong kho hoặc đã được bán"}

        except Exception as e:
            return {"phone": clean_num, "carrier": "MOBIFONE", "available": None, "error": str(e)}


class VinaphoneApiChecker:
    """Fast backend checker for VinaPhone SIM inventory with configurable delay."""

    BASE_URL = "https://digishop.vnpt.vn/apiprod/v2/simso/num_search"

    def __init__(self, proxy: Optional[str] = None, delay: float = 1.0):
        self.session = requests.Session(impersonate="chrome131")
        self.delay = delay
        self.headers = {
            "accept": "*/*",
            "referer": "https://digishop.vnpt.vn/sim-so?tab=c320",
            "origin": "https://digishop.vnpt.vn",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        }
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}

    def search_sim(self, clean_num: str) -> Dict[str, Any]:
        if self.delay > 0:
            time.sleep(self.delay)

        prefix_code = "84" + clean_num[1:3]
        suffix_search = clean_num[3:]

        params = {
            "search": suffix_search,
            "prefix": prefix_code,
            "commit": "0"
        }

        try:
            items = []  # Default in case all retries fail with a non-error path
            for attempt in range(5):
                response = self.session.get(self.BASE_URL, params=params, headers=self.headers, timeout=8)
                if response.status_code != 200:
                    return {"phone": clean_num, "carrier": "VINAPHONE", "available": False, "error": f"HTTP {response.status_code}"}

                data = response.json()
                error_code = data.get("errorCode")
                if error_code is not None and error_code != 0:
                    if attempt < 4:
                        print(f"  [~] VINAPHONE {clean_num}: Lần thử {attempt+1}/5 bị lỗi API ({data.get('message', '')}), thử lại...", flush=True)
                        time.sleep(self.delay or 1.0)
                        continue
                    err_msg = data.get("message", f"API Error {error_code}")
                    print(f"  [!] VINAPHONE {clean_num}: Thất bại sau 5 lần thử — {err_msg}", flush=True)
                    return {"phone": clean_num, "carrier": "VINAPHONE", "available": None, "error": err_msg}

                items = data.get("data", [])
                break

            matched_items = []
            for item in items:
                raw_so_tb = str(item.get("so_tb", "")).strip()
                if raw_so_tb.startswith("84"):
                    full_phone = "0" + raw_so_tb[2:]
                else:
                    full_phone = raw_so_tb

                if clean_num == full_phone or full_phone.endswith(clean_num) or clean_num in full_phone:
                    price = item.get("price", 0)
                    raw_kieuso = str(item.get("kieuso_name") or "N/A")
                    kieuso = re.sub(r'<[^>]+>', ' ', raw_kieuso).strip()
                    kieuso = re.sub(r'\s+', ' ', kieuso)
                    
                    matched_items.append({
                        "phone": full_phone,
                        "type": kieuso,
                        "price": f"{int(price):,} VNĐ" if isinstance(price, (int, float)) else str(price),
                        "raw": item
                    })

            if matched_items:
                return {"phone": clean_num, "carrier": "VINAPHONE", "available": True, "count": len(matched_items), "items": matched_items}
            else:
                return {"phone": clean_num, "carrier": "VINAPHONE", "available": False, "note": "Số không có trong kho hoặc đã được bán"}

        except Exception as e:
            return {"phone": clean_num, "carrier": "VINAPHONE", "available": None, "error": str(e)}


class VietnamobileApiChecker:
    """Fast backend checker for Vietnamobile SIM inventory with configurable delay, urllib direct fallback, and auto proxy rotation if IP is blocked."""

    BASE_URL = "https://shop.vietnamobile.com.vn/vn/so-dep"

    def __init__(self, proxy: Optional[str] = None, delay: float = 1.0):
        # NOTE: Vietnamobile does NOT use config.proxy by default — direct connection is fastest & reliable.
        # Uses a SEPARATE VietnamobileProxyPoolManager (independent from Viettel's ProxyPoolManager)
        # to prevent race conditions when both checkers run concurrently in parallel threads.
        self.session = requests.Session(impersonate="chrome131")
        self.session.proxies = {}
        self.delay = delay
        self.proxy_pool = VietnamobileProxyPoolManager()  # Separate pool — no shared index with Viettel
        self.ssl_ctx = ssl._create_unverified_context()   # Reuse ssl context
        self.headers = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "content-type": "application/x-www-form-urlencoded",
            "origin": "https://shop.vietnamobile.com.vn",
            "referer": "https://shop.vietnamobile.com.vn/vn/so-dep",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        }

    def _parse_html_response(self, html_text: str, clean_num: str) -> Dict[str, Any]:
        """Parse raw HTML response from Vietnamobile so-dep search."""
        # Detect proxy injected fake pages, Captchas, or general mainpages without search result tables
        if "attrbuyoutprice" not in html_text and 'phone="' not in html_text and "patten" not in html_text:
            raise ValueError("Proxy trả về trang không chứa kết quả tìm kiếm (Trang tĩnh / CAPTCHA / Blocked)")

        matched_items = []
        row_blocks = html_text.split('<tr class="')
        for block in row_blocks[1:]:
            phone_match = re.search(r'phone=["\'](\d+)["\']', block)
            if not phone_match:
                phone_match = re.search(r'<span>(\d{10})</span>', block)

            if phone_match:
                found_phone = phone_match.group(1)
                if clean_num == found_phone or found_phone.endswith(clean_num) or clean_num in found_phone:
                    buyout_m = re.search(r'attrbuyoutprice=["\'](\d+)["\']', block)
                    buyout_price = f"{int(buyout_m.group(1)):,} VNĐ" if buyout_m else "N/A"

                    fee_m = re.search(r'attrprice=["\'](\d+)["\']', block)
                    fee_price = f"{int(fee_m.group(1)):,} VNĐ" if fee_m else "50,000 VNĐ"

                    matched_items.append({
                        "phone": found_phone,
                        "type": "SIM Số Đẹp Vietnamobile",
                        "price": f"Mua đứt: {buyout_price} | Đấu nối: {fee_price}",
                        "buyout": buyout_price,
                        "fee": fee_price
                    })

        if matched_items:
            return {"phone": clean_num, "carrier": "VIETNAMOBILE", "available": True, "count": len(matched_items), "items": matched_items}
        else:
            return {"phone": clean_num, "carrier": "VIETNAMOBILE", "available": False, "note": "Số không có trong kho hoặc đã được bán"}

    def search_sim(self, clean_num: str, max_retries: int = 6) -> Dict[str, Any]:
        data = {
            "patten": clean_num,
            "page": "1",
            "money": "0",
            "commitPrice": "",
            "buyoutPrice": "",
            "numClass": ""
        }

        last_error = None
        encoded_data = urllib.parse.urlencode(data).encode("utf-8")

        # Step 1: Direct attempt via curl_cffi (no proxy)
        try:
            response = self.session.post(self.BASE_URL, data=data, headers=self.headers, timeout=6, verify=False)
            if response.status_code == 200:
                html_text = response.content.decode("utf-8", errors="ignore")
                return self._parse_html_response(html_text, clean_num)
            last_error = f"HTTP {response.status_code}"
        except Exception as e:
            last_error = str(e)

        # Step 2: Direct urllib fallback (bypasses curl_cffi and environment proxies completely)
        try:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                urllib.request.HTTPSHandler(context=self.ssl_ctx)
            )
            req = urllib.request.Request(self.BASE_URL, data=encoded_data, headers=self.headers)
            with opener.open(req, timeout=6) as resp:
                if resp.status == 200:
                    html_text = resp.read().decode("utf-8", errors="ignore")
                    return self._parse_html_response(html_text, clean_num)
        except Exception as urllib_err:
            last_error = str(urllib_err)

        # Step 3: Direct connection failed/timed out (e.g. IP blocked on server/ARM device). Try live proxies!
        print(f"[!] Vietnamobile direct IP error ({last_error}). Rotating live proxy...", flush=True)
        for attempt in range(max_retries):
            px = self.proxy_pool.get_next_proxy()
            if not px:
                print("[!] Proxy pool empty for Vietnamobile. Fetching new batch for Vietnamobile...", flush=True)
                try:
                    self.proxy_pool.fetch_next_batch(target_count=10, max_attempts=5, target_name="Vietnamobile")
                    px = self.proxy_pool.get_next_proxy()
                except RuntimeError as err:
                    print(f"[!] {err}", flush=True)
                    break
            
            if not px:
                break

            print(f"[*] Vietnamobile attempt {attempt+1}/{max_retries} with proxy: {px}", flush=True)
            try:
                s = requests.Session(impersonate="chrome131")
                s.proxies = {"http": px, "https": px}
                resp = s.post(self.BASE_URL, data=data, headers=self.headers, timeout=15, verify=False)
                if resp.status_code == 200:
                    html_text = resp.content.decode("utf-8", errors="ignore")
                    return self._parse_html_response(html_text, clean_num)
            except Exception as px_err:
                last_error = str(px_err)
                # Fallback to urllib with proxy if curl_cffi times out via proxy
                print(f"[*] Vietnamobile curl_cffi proxy failed ({px_err}), trying urllib proxy...", flush=True)
                try:
                    opener = urllib.request.build_opener(
                        urllib.request.ProxyHandler({"http": px, "https": px}),
                        urllib.request.HTTPSHandler(context=self.ssl_ctx)
                    )
                    req = urllib.request.Request(self.BASE_URL, data=encoded_data, headers=self.headers)
                    with opener.open(req, timeout=15) as resp_url:
                        if resp_url.status == 200:
                            html_text = resp_url.read().decode("utf-8", errors="ignore")
                            return self._parse_html_response(html_text, clean_num)
                except Exception as px_url_err:
                    last_error = f"{px_err} | {px_url_err}"

        return {"phone": clean_num, "carrier": "VIETNAMOBILE", "available": None, "error": last_error or "Lỗi kết nối Vietnamobile"}



class SimCheckerHub:
    """Central router to check SIM across all 4 major carriers with per-carrier delays."""

    def __init__(self, proxy: Optional[str] = None, delay: float = 1.0):
        self.viettel_checker = ViettelApiChecker(proxy=proxy, delay=delay)
        self.mobifone_checker = MobifoneApiChecker(proxy=proxy, delay=delay)
        self.vinaphone_checker = VinaphoneApiChecker(proxy=proxy, delay=delay)
        self.vietnamobile_checker = VietnamobileApiChecker(proxy=proxy, delay=delay)

    def format_phone(self, phone: str) -> str:
        p = re.sub(r'\D', '', phone)
        if p.startswith("84") and len(p) == 11:
            p = "0" + p[2:]
        elif not p.startswith("0") and len(p) == 9:
            p = "0" + p
        return p

    def check_sim(self, phone: str, specified_carrier: Optional[str] = None) -> Dict[str, Any]:
        clean_num = self.format_phone(phone)
        carrier = specified_carrier.upper() if specified_carrier else CarrierDetector.get_carrier(clean_num)

        if carrier == "VIETTEL":
            return self.viettel_checker.search_sim(clean_num)
        elif carrier == "MOBIFONE":
            return self.mobifone_checker.search_sim(clean_num)
        elif carrier == "VINAPHONE":
            return self.vinaphone_checker.search_sim(clean_num)
        elif carrier == "VIETNAMOBILE":
            return self.vietnamobile_checker.search_sim(clean_num)
        else:
            return {
                "phone": clean_num,
                "carrier": carrier,
                "available": False,
                "note": f"Đầu số {clean_num[:3]} chưa xác định nhà mạng"
            }

    def check_sims_parallel(
        self,
        tasks: List[Dict[str, Any]],
        max_workers: int = 4,
        progress_callback: Optional[Callable[[Dict[str, Any], int, int], None]] = None
    ) -> List[Dict[str, Any]]:
        """
        Check multiple SIMs in parallel across carriers.
        Groups tasks by carrier so each carrier's tasks are processed in a dedicated worker thread
        sequentially with safety delays, while different carriers run simultaneously.
        """
        if not tasks:
            return []

        grouped_tasks: Dict[str, List[Dict[str, Any]]] = {}
        for task in tasks:
            num = task.get("phone", "")
            clean_num = self.format_phone(num)
            specified = task.get("carrier")
            carrier = specified.upper() if specified else CarrierDetector.get_carrier(clean_num)
            if carrier not in grouped_tasks:
                grouped_tasks[carrier] = []
            grouped_tasks[carrier].append({"phone": clean_num, "carrier": carrier, "original_task": task})

        results: List[Dict[str, Any]] = []
        lock = threading.Lock()

        def carrier_worker(carrier_name: str, carrier_tasks: List[Dict[str, Any]]):
            worker_results = []
            for t in carrier_tasks:
                res = self.check_sim(t["phone"], specified_carrier=carrier_name)
                with lock:
                    results.append(res)
                    current_count = len(results)
                    if progress_callback:
                        try:
                            progress_callback(res, current_count, len(tasks))
                        except Exception:
                            pass
                worker_results.append(res)
            return worker_results

        num_workers = min(max_workers, max(1, len(grouped_tasks)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = [
                executor.submit(carrier_worker, c_name, c_tasks)
                for c_name, c_tasks in grouped_tasks.items()
            ]
            concurrent.futures.wait(futures)

        return results

    def health_check(self, probe_numbers: Dict[str, str]) -> Dict[str, Dict[str, Any]]:
        """
        Run health checks using known-active probe numbers for each carrier.
        probe_numbers keys: VIETTEL_PREPAID, VIETTEL_POSTPAID, MOBIFONE, VINAPHONE, VIETNAMOBILE
        Returns: dict of key -> {passed: bool, note: str, probe: str}
        A probe is PASS only if the API responds AND the expected number is found in results.
        """
        results: Dict[str, Dict[str, Any]] = {}
        lock = threading.Lock()

        def _check_viettel_prepaid(clean: str) -> tuple:
            """Check Viettel prepaid (isdn_type=2) probe."""
            res = self.viettel_checker._query(clean, isdn_type="2")
            if res.get("error"):
                return False, f"Lỗi API: {res['error']}"
            for item in res.get("items", []):
                raw_isdn = str(item.get("isdn", "")).strip()
                full = ("0" + raw_isdn) if not raw_isdn.startswith("0") else raw_isdn
                if clean == full or full.endswith(clean[1:]):
                    return True, "OK"
            return False, "Số probe không tìm thấy trong kho Trả Trước Viettel"

        def _check_viettel_postpaid(clean: str) -> tuple:
            """Check Viettel postpaid (isdn_type=22) probe."""
            res = self.viettel_checker._query(clean, isdn_type="22")
            if res.get("error"):
                return False, f"Lỗi API: {res['error']}"
            for item in res.get("items", []):
                raw_isdn = str(item.get("isdn", "")).strip()
                full = ("0" + raw_isdn) if not raw_isdn.startswith("0") else raw_isdn
                if clean == full or full.endswith(clean[1:]):
                    return True, "OK"
            return False, "Số probe không tìm thấy trong kho Trả Sau Viettel"

        def _make_generic_checker(search_fn: Callable) -> Callable:
            """Wrap a carrier search_sim function for health check use."""
            def _fn(clean: str) -> tuple:
                res = search_fn(clean)
                if res.get("available") is True:
                    return True, "OK"
                if res.get("error"):
                    return False, f"Lỗi API: {res['error']}"
                return False, res.get("note", "Số probe không tìm thấy")
            return _fn

        def _check_vietnamobile_probe(clean: str) -> tuple:
            """Check Vietnamobile probe using the pre-loaded VNMB proxy pool.
            Proxy refresh is done by the CALLER (app.py run_health_check / run_full_scan)
            before calling hub.health_check() — no double-scrape needed here.
            """
            res = self.vietnamobile_checker.search_sim(clean)
            if res.get("available") is True:
                return True, "OK"
            if res.get("error"):
                return False, f"Lỗi API: {res['error']}"
            return False, res.get("note", "Số probe không tìm thấy")

        def _worker(key: str, check_fn: Callable[[str], tuple]) -> None:
            """Worker to run a single probe concurrently in its own thread."""
            raw = probe_numbers.get(key, "").strip()
            if not raw:
                return
            clean = re.sub(r'\D', '', raw)
            if clean.startswith("84") and len(clean) == 11:
                clean = "0" + clean[2:]
            elif not clean.startswith("0") and len(clean) == 9:
                clean = "0" + clean
            try:
                passed, note = check_fn(clean)
                with lock:
                    results[key] = {"passed": passed, "note": note, "probe": raw}
            except Exception as exc:
                with lock:
                    results[key] = {"passed": False, "note": f"Exception: {exc}", "probe": raw}

        probe_tasks = [
            ("VIETTEL_PREPAID",  _check_viettel_prepaid),
            ("VIETTEL_POSTPAID", _check_viettel_postpaid),
            ("MOBIFONE",         _make_generic_checker(self.mobifone_checker.search_sim)),
            ("VINAPHONE",        _make_generic_checker(self.vinaphone_checker.search_sim)),
            ("VIETNAMOBILE",     _check_vietnamobile_probe),
        ]

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(_worker, key, fn) for key, fn in probe_tasks]
            concurrent.futures.wait(futures)

        return results


def parse_labeled_file(filepath: str) -> List[Dict[str, str]]:
    """Parse phone numbers with optional carrier labels from text file."""
    tasks = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line_str = line.strip()
            if not line_str:
                continue

            carrier_match = re.search(r'[-*#]?\s*(viettel|mobifone|vinaphone|vietnamobile)\s*:\s*(.*)', line_str, re.IGNORECASE)
            if carrier_match:
                carrier_label = carrier_match.group(1).upper()
                numbers_in_line = re.findall(r'0\d{9}', carrier_match.group(2))
                for num in numbers_in_line:
                    tasks.append({"phone": num, "carrier": carrier_label})
            else:
                numbers_in_line = re.findall(r'0\d{9}', line_str)
                for num in numbers_in_line:
                    tasks.append({"phone": num, "carrier": None})
    return tasks


def main():
    parser = argparse.ArgumentParser(description="Tool Check SIM 4 Nhà Mạng (Viettel, MobiFone, VinaPhone, Vietnamobile)")
    parser.add_argument("phone", nargs="?", default="0365141705", help="Số điện thoại cần kiểm tra")
    parser.add_argument("-c", "--carrier", help="Chỉ định nhà mạng cụ thể (VIETTEL, MOBIFONE, VINAPHONE, VIETNAMOBILE)")
    parser.add_argument("-f", "--file", help="File danh sách số điện thoại")
    parser.add_argument("-o", "--output", default="report_sims.txt", help="File lưu báo cáo (default: report_sims.txt)")
    parser.add_argument("-d", "--delay", type=float, default=1.5, help="Thời gian delay an toàn giữa các lần check (default: 1.5s)")
    parser.add_argument("-w", "--workers", type=int, default=4, help="Số luồng chạy song song đa nhà mạng (default: 4)")

    args = parser.parse_args()
    hub = SimCheckerHub(delay=args.delay)

    if args.file:
        try:
            tasks = parse_labeled_file(args.file)
        except Exception as e:
            print(f"[!] Lỗi đọc file: {e}")
            return

        if not tasks:
            print(f"[!] Không tìm thấy số điện thoại hợp lệ trong file {args.file}")
            return

        print(f"[*] Tìm thấy {len(tasks)} số điện thoại trong file: {args.file}")
        print(f"[*] Cấu hình delay an toàn: {args.delay}s / lần check")
        print(f"[*] Luồng quét song song: {args.workers} luồng (Đa nhà mạng)")
        print(f"[*] Đang tiến hành kiểm tra trên 4 nhà mạng...\n", flush=True)

        results_by_carrier = {"VIETTEL": [], "MOBIFONE": [], "VINAPHONE": [], "VIETNAMOBILE": [], "UNKNOWN": []}
        hits = []
        bads = []

        def cli_progress_cb(res, current_count, total_count):
            carrier = res.get("carrier", "UNKNOWN")
            num = res.get("phone", "")
            results_by_carrier.setdefault(carrier, []).append(res)
            if res.get("available"):
                hits.append(res)
                first_item = res["items"][0]
                print(f"  [{current_count}/{total_count}] \033[92m[HIT] [{carrier}] {num} -> CÒN BÁN [{first_item['type']}] ({first_item['price']})\033[0m", flush=True)
            else:
                bads.append(res)
                print(f"  [{current_count}/{total_count}] \033[91m[BAD] [{carrier}] {num} -> {res.get('note', 'Không có trong kho')}\033[0m", flush=True)

        hub.check_sims_parallel(tasks, max_workers=args.workers, progress_callback=cli_progress_cb)

        # Build full report
        report_lines = []
        report_lines.append("=" * 65)
        report_lines.append("        BÁO CÁO KẾT QUẢ KIỂM TRA SIM 4 NHÀ MẠNG")
        report_lines.append("=" * 65)
        report_lines.append(f"File nguồn       : {args.file}")
        report_lines.append(f"Tổng số đã quét  : {len(tasks)} SIM")
        report_lines.append(f"Số SIM CÒN BÁN   : {len(hits)} SIM")
        report_lines.append(f"Số SIM ĐÃ BÁN     : {len(bads)} SIM\n")

        report_lines.append("-" * 65)
        report_lines.append("  DANH SÁCH SIM CÒN BÁN (THEO TỪNG NHÀ MẠNG):")
        report_lines.append("-" * 65)

        for c_name in ["VIETTEL", "MOBIFONE", "VINAPHONE", "VIETNAMOBILE"]:
            c_hits = [r for r in results_by_carrier.get(c_name, []) if r.get("available")]
            report_lines.append(f"\n[+] {c_name} ({len(c_hits)} SIM còn bán):")
            if c_hits:
                for item_res in c_hits:
                    for item in item_res["items"]:
                        report_lines.append(f"    - {item['phone']} | Loại: {item.get('type', 'N/A')} | Giá: {item['price']}")
            else:
                report_lines.append("    (Không có SIM nào còn trong kho)")

        report_lines.append("\n" + "=" * 65)

        report_text = "\n".join(report_lines)
        print("\n" + report_text)

        with open(args.output, "w", encoding="utf-8") as out_file:
            out_file.write(report_text)
        print(f"\n[✔] Báo cáo chi tiết đã được xuất ra file: {args.output}")

    else:
        # Check single number
        print(f"[*] Đang kiểm tra số: {args.phone} (Chạy ngầm qua API) ...", flush=True)
        res = hub.check_sim(args.phone, specified_carrier=args.carrier)
        carrier = res.get("carrier", args.carrier or "UNKNOWN")

        print("\n" + "=" * 55)
        print(f"  KẾT QUẢ KIỂM TRA SỐ: {args.phone} [{carrier}]")
        print("=" * 55)

        if res.get("available"):
            print(f"  [✔] CÓ ĐANG BÁN TRÊN KHO {carrier}! (Tìm thấy {res['count']} kết quả)")
            for idx, item in enumerate(res["items"], 1):
                print(f"   -> {idx}. Số SIM: {item['phone']} | Loại: {item.get('type', 'N/A')} | Giá: {item['price']}")
        else:
            print(f"  [✘] KHÔNG CÓ TRONG KHO HOẶC ĐÃ ĐƯỢC BÁN! ({res.get('note', '')})")
        print("=" * 55 + "\n")


if __name__ == "__main__":
    main()
