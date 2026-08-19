"""
Public Proxy Hunter & Pool Manager
Collects public proxies from multiple GitHub repositories, filters live proxies against Viettel Telecom API/Web,
and manages dynamic proxy rotation for SIM Checker Bot.
"""

import sys
import re
import time
import json
import random
import threading
import concurrent.futures
from pathlib import Path
from typing import List, Dict, Any, Optional, Set
import requests

import functools
print = functools.partial(print, flush=True)

# Ensure UTF-8 output on Windows console with line buffering
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    except Exception:
        pass

def set_telegram_notify_callback(callback):
    ProxyPoolManager().set_notify_callback(callback)

def notify_telegram(message: str):
    ProxyPoolManager().trigger_notify(message)

# ============================================================
# CONFIG
# ============================================================

PROXY_SOURCES = [
    # =========================
    # ProxyScrape - ~5 minutes
    # =========================
    "https://raw.githubusercontent.com/ProxyScrape/free-proxy-list/main/proxies/protocols/http/data.txt",
    "https://raw.githubusercontent.com/ProxyScrape/free-proxy-list/main/proxies/protocols/https/data.txt",
    "https://raw.githubusercontent.com/ProxyScrape/free-proxy-list/main/proxies/protocols/socks4/data.txt",
    "https://raw.githubusercontent.com/ProxyScrape/free-proxy-list/main/proxies/protocols/socks5/data.txt",
    "https://raw.githubusercontent.com/ProxyScrape/free-proxy-list/main/proxies/all/data.txt",

    # =========================
    # Proxifly - ~5 minutes
    # =========================
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/https/data.txt",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks4/data.txt",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks5/data.txt",

    # =========================
    # prxchk - ~10 minutes
    # =========================
    "https://raw.githubusercontent.com/prxchk/proxy-list/main/all.txt",
    "https://raw.githubusercontent.com/prxchk/proxy-list/main/http.txt",
    "https://raw.githubusercontent.com/prxchk/proxy-list/main/socks4.txt",
    "https://raw.githubusercontent.com/prxchk/proxy-list/main/socks5.txt",

    # =========================
    # gproxynet - ~30 minutes
    # =========================
    "https://raw.githubusercontent.com/gproxynet/free-proxy-list/main/all.txt",
    "https://raw.githubusercontent.com/gproxynet/free-proxy-list/main/http.txt",
    "https://raw.githubusercontent.com/gproxynet/free-proxy-list/main/socks4.txt",
    "https://raw.githubusercontent.com/gproxynet/free-proxy-list/main/socks5.txt",

    # =========================
    # iplocate - ~30 minutes
    # =========================
    "https://raw.githubusercontent.com/iplocate/free-proxy-list/main/http.txt",
    "https://raw.githubusercontent.com/iplocate/free-proxy-list/main/https.txt",
    "https://raw.githubusercontent.com/iplocate/free-proxy-list/main/socks4.txt",
    "https://raw.githubusercontent.com/iplocate/free-proxy-list/main/socks5.txt",

    # =========================
    # hproxy - continuously scanned
    # =========================
    "https://raw.githubusercontent.com/hproxy-com/free-proxy-list/main/all.txt",
    "https://raw.githubusercontent.com/hproxy-com/free-proxy-list/main/http.txt",
    "https://raw.githubusercontent.com/hproxy-com/free-proxy-list/main/https.txt",
    "https://raw.githubusercontent.com/hproxy-com/free-proxy-list/main/socks4.txt",
    "https://raw.githubusercontent.com/hproxy-com/free-proxy-list/main/socks5.txt",

    # =========================
    # dinoz0rg - frequent updates
    # =========================
    "https://raw.githubusercontent.com/dinoz0rg/proxy-list/main/checked_proxies/http.txt",
    "https://raw.githubusercontent.com/dinoz0rg/proxy-list/main/checked_proxies/socks4.txt",
    "https://raw.githubusercontent.com/dinoz0rg/proxy-list/main/checked_proxies/socks5.txt",

    # =========================
    # clarketm - daily
    # =========================
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",

    # =========================
    # TheSpeedX - frequent/daily
    # =========================
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",

    # =========================
    # Monosans
    # =========================
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks4.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/all.txt",

    # =========================
    # roosterkid
    # =========================
    "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt",
    "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTP_RAW.txt",
    "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS4_RAW.txt",
    "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS5_RAW.txt",
]

TEST_URL = "https://vietteltelecom.vn/vx/di-dong/sim-so/#"

FETCH_TIMEOUT = 15
PROXY_TIMEOUT = 5

MAX_WORKERS = 80

MIN_LATENCY = 0
MAX_LATENCY = 5000

OUTPUT_FAST = "fast_proxies.txt"


PROXY_REGEX = re.compile(
    r"(?<!\d)"
    r"(?:\d{1,3}\.){3}\d{1,3}"
    r":\d{1,5}"
    r"(?!\d)"
)


def valid_ip(ip: str) -> bool:
    try:
        parts = ip.split(".")
        if len(parts) != 4:
            return False
        return all(0 <= int(x) <= 255 for x in parts)
    except ValueError:
        return False


def extract_proxies(text: str) -> Set[str]:
    result = set()
    for match in PROXY_REGEX.findall(text):
        try:
            ip, port = match.split(":")
            if not valid_ip(ip):
                continue
            port_num = int(port)
            if not 1 <= port_num <= 65535:
                continue
            result.add(f"{ip}:{port_num}")
        except Exception:
            continue
    return result


def fetch_source(url: str) -> Set[str]:
    try:
        response = requests.get(
            url,
            timeout=FETCH_TIMEOUT,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                )
            },
        )
        response.raise_for_status()
        proxies = extract_proxies(response.text)
        print(f"[SOURCE] {len(proxies):>6} proxies | {url}")
        return proxies
    except Exception as e:
        print(f"[SOURCE ERROR] {url} -> {e}")
        return set()


def collect_proxies() -> List[str]:
    print("\n" + "=" * 70)
    print("COLLECTING PUBLIC PROXIES")
    print("=" * 70)

    all_proxies = set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(fetch_source, url) for url in PROXY_SOURCES]
        for future in concurrent.futures.as_completed(futures):
            try:
                all_proxies.update(future.result())
            except Exception:
                pass

    print(f"\n[+] UNIQUE PROXIES COLLECTED: {len(all_proxies)}")
    return list(all_proxies)


def check_proxy(proxy: str) -> Optional[Dict[str, Any]]:
    proxy_url = f"http://{proxy}"
    proxies = {
        "http": proxy_url,
        "https": proxy_url,
    }
    start = time.perf_counter()
    try:
        response = requests.get(
            TEST_URL,
            proxies=proxies,
            timeout=PROXY_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        latency = (time.perf_counter() - start) * 1000
        if response.status_code != 200:
            return None
        if latency > MAX_LATENCY:
            return None
        return {
            "proxy": proxy,
            "latency": round(latency, 2),
        }
    except Exception:
        return None


def check_all(proxies: List[str], target_live_count: int = 10) -> List[Dict[str, Any]]:
    print("\n" + "=" * 70)
    print(f"CHECKING LIVE PROXIES FOR VIETTEL TELECOM (TARGET: {target_live_count} LIVE)")
    print("=" * 70)

    live = []
    total = len(proxies)
    completed = 0

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS)
    try:
        futures = {executor.submit(check_proxy, proxy): proxy for proxy in proxies}
        for future in concurrent.futures.as_completed(futures):
            completed += 1
            try:
                result = future.result()
                if result:
                    live.append(result)
                    print(f"[LIVE {len(live)}/{target_live_count}] {result['proxy']:<22} {result['latency']:>7.2f} ms", flush=True)
                    if len(live) >= target_live_count:
                        print(f"\n[+] Da tim du {target_live_count} proxy live! Dung cao som de tiet kiem thoi gian.", flush=True)
                        executor.shutdown(wait=False, cancel_futures=True)
                        return live[:target_live_count]
            except Exception:
                pass
            if completed % 200 == 0:
                print(f"[PROGRESS] {completed}/{total} | LIVE={len(live)}", flush=True)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    return live[:target_live_count]


def save_results(live: List[Dict[str, Any]]) -> List[str]:
    live = sorted(live, key=lambda x: x["latency"])

    fast_proxy_strings = [item["proxy"] for item in live]
    with open(OUTPUT_FAST, "w", encoding="utf-8") as f:
        for p in fast_proxy_strings:
            f.write(p + "\n")

    print("\n" + "=" * 70)
    print(f"LIVE PROXIES FOUND : {len(live)}")
    print(f"SAVED TO           : {OUTPUT_FAST}")
    print("=" * 70)

    return fast_proxy_strings


class ProxyPoolManager:
    """Thread-safe Proxy Pool Manager that handles dynamic 10-proxy batching and max 10 hunt attempts."""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(ProxyPoolManager, cls).__new__(cls)
                cls._instance._init_manager()
            return cls._instance

    def _init_manager(self):
        self.proxies: List[str] = []
        self.current_index = 0
        self.is_hunting = False
        self.hunt_attempts = 0
        self.notify_callback = None
        self.load_local_proxies()

    def set_notify_callback(self, callback):
        """Set Telegram notification callback directly on singleton instance."""
        with self._lock:
            self.notify_callback = callback
            print("[*] Registered ProxyPoolManager notify_callback successfully.", flush=True)

    def trigger_notify(self, message: str):
        """Dispatch notification callback directly from singleton instance."""
        print(f"[*] [NOTIFY-HUB] Triggering Telegram notification: '{message}'", flush=True)
        cb = None
        with self._lock:
            cb = self.notify_callback
        if cb:
            try:
                res = cb(message)
                print(f"[+] [NOTIFY-HUB] Dispatch complete (Result: {res})", flush=True)
                return res
            except Exception as e:
                print(f"[!] [NOTIFY-HUB] Exception calling callback: {e}", flush=True)
        else:
            print("[!] [NOTIFY-HUB] WARNING: notify_callback is None on ProxyPoolManager singleton!", flush=True)

    def reset_attempts(self):
        """Reset hunt attempts counter and hunting state for a fresh scan session."""
        with self._lock:
            self.hunt_attempts = 0
            self.current_index = 0
            self.is_hunting = False

    def load_local_proxies(self) -> List[str]:
        """Load proxies from fast_proxies.txt if available."""
        loaded = []
        if Path(OUTPUT_FAST).exists():
            try:
                with open(OUTPUT_FAST, "r", encoding="utf-8") as f:
                    loaded = [line.strip() for line in f if line.strip()]
            except Exception:
                pass

        self.proxies = loaded
        self.current_index = 0
        return self.proxies

    def get_next_proxy(self) -> Optional[str]:
        """Get the next proxy in rotation without looping past pool bounds."""
        with self._lock:
            if not self.proxies or self.current_index >= len(self.proxies):
                return None
            proxy = self.proxies[self.current_index]
            self.current_index += 1
            return f"http://{proxy}" if not proxy.startswith("http") else proxy

    def get_proxy_count(self) -> int:
        """Return total live proxies available in pool."""
        with self._lock:
            self.load_local_proxies()
            return len(self.proxies)

    def ensure_proxies(self, target_count: int = 10) -> List[str]:
        """Ensure we have up to target_count proxies loaded."""
        with self._lock:
            if self.proxies and self.current_index < len(self.proxies):
                return self.proxies
        return self.fetch_next_batch(target_count=target_count)

    def refresh_proxies(self, target_count: int = 10, max_attempts: int = 10) -> List[str]:
        """Reset attempts and fetch a brand new batch of target_count live proxies."""
        with self._lock:
            self.hunt_attempts = 0
            self.current_index = 0
            self.is_hunting = False
        return self.fetch_next_batch(target_count=target_count, max_attempts=max_attempts)

    def fetch_next_batch(self, target_count: int = 10, max_attempts: int = 10) -> List[str]:
        """Fetch next batch of 10 live proxies. Throws RuntimeError if max 10 attempts fail."""
        with self._lock:
            if self.hunt_attempts >= max_attempts:
                raise RuntimeError(f"❌ Đã thử {max_attempts} đợt Proxy ({max_attempts * target_count} proxy) nhưng tất cả đều bị Viettel chặn IP!")

            if self.is_hunting:
                print("[*] Proxy Hunter is already running in background...")
                return self.proxies
            self.is_hunting = True

        try:
            self.hunt_attempts += 1
            print(f"\n[*] [Đợt cào {self.hunt_attempts}/{max_attempts}] 10 Proxy trước đó đều bị chặn ➔ Bắt đầu cào 10 Proxy live mới cho Viettel...")
            raw_proxies = collect_proxies()
            if raw_proxies:
                live = check_all(raw_proxies, target_live_count=target_count)
                if live:
                    fast_list = save_results(live)
                    with self._lock:
                        self.proxies = fast_list
                        self.current_index = 0
                    return self.proxies
            print(f"[!] Đợt cào {self.hunt_attempts} không tìm thấy proxy live mới.")
            return self.proxies
        finally:
            with self._lock:
                self.is_hunting = False


def main():
    start = time.time()
    print("\n" + "=" * 70)
    print("             PUBLIC PROXY HUNTER")
    print("=" * 70)

    proxies = collect_proxies()
    if not proxies:
        print("[!] No proxies found.")
        return

    live = check_all(proxies)
    if not live:
        print("\n[!] No live proxies found.")
        return

    save_results(live)
    print(f"\n[+] Finished in {time.time() - start:.2f}s")


if __name__ == "__main__":
    main()
