"""
Public Proxy Hunter & Pool Manager
Collects public proxies from multiple GitHub repositories, filters live proxies against Viettel Telecom API/Web,
and manages dynamic proxy rotation for SIM Checker Bot.
"""

import re
import time
import json
import random
import threading
import concurrent.futures
from pathlib import Path
from typing import List, Dict, Any, Optional, Set
import requests

# ============================================================
# CONFIG
# ============================================================

PROXY_SOURCES = [
    # TheSpeedX
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",

    # Monosans
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks4.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/all.txt",

    # Proxifly
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/https/data.txt",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks4/data.txt",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks5/data.txt",
]

TEST_URL = "https://vietteltelecom.vn/vx/di-dong/sim-so/#"

FETCH_TIMEOUT = 15
PROXY_TIMEOUT = 5

MAX_WORKERS = 80

MIN_LATENCY = 0
MAX_LATENCY = 5000

OUTPUT_ALL = "live_proxies.txt"
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

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(check_proxy, proxy): proxy for proxy in proxies}
        for future in concurrent.futures.as_completed(futures):
            completed += 1
            result = future.result()
            if result:
                live.append(result)
                print(f"[LIVE {len(live)}/{target_live_count}] {result['proxy']:<22} {result['latency']:>7.2f} ms")
                if len(live) >= target_live_count:
                    print(f"\n[✔] Đã tìm đủ {target_live_count} proxy live! Dừng cào sớm để tiết kiệm thời gian.")
                    break
            if completed % 200 == 0:
                print(f"[PROGRESS] {completed}/{total} | LIVE={len(live)}")

    return live[:target_live_count]


def save_results(live: List[Dict[str, Any]]) -> List[str]:
    live = sorted(live, key=lambda x: x["latency"])

    # Save all live proxies
    with open(OUTPUT_ALL, "w", encoding="utf-8") as f:
        for item in live:
            f.write(item["proxy"] + "\n")

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
        self.load_local_proxies()

    def reset_attempts(self):
        """Reset hunt attempts counter for a fresh scan session."""
        with self._lock:
            self.hunt_attempts = 0

    def load_local_proxies(self) -> List[str]:
        """Load proxies from fast_proxies.txt or live_proxies.txt if available."""
        loaded = []
        for file in [OUTPUT_FAST, OUTPUT_ALL]:
            if Path(file).exists():
                try:
                    with open(file, "r", encoding="utf-8") as f:
                        lines = [line.strip() for line in f if line.strip()]
                        if lines:
                            loaded = lines
                            break
                except Exception:
                    pass

        self.proxies = loaded
        self.current_index = 0
        return self.proxies

    def get_next_proxy(self) -> Optional[str]:
        """Get the next proxy in rotation."""
        with self._lock:
            if not self.proxies:
                return None
            proxy = self.proxies[self.current_index % len(self.proxies)]
            self.current_index += 1
            return f"http://{proxy}" if not proxy.startswith("http") else proxy

    def is_exhausted(self) -> bool:
        """Check if current batch of proxies has been fully rotated."""
        with self._lock:
            if not self.proxies:
                return True
            return self.current_index >= len(self.proxies)

    def get_proxy_count(self) -> int:
        """Return total live proxies available in pool."""
        return len(self.proxies)

    def refresh_proxies(self, target_count: int = 10, max_attempts: int = 10) -> List[str]:
        """Run proxy hunter to collect up to 10 live proxies. Throws RuntimeError if 10 attempts fail."""
        with self._lock:
            if self.hunt_attempts >= max_attempts:
                raise RuntimeError(f"❌ Đã cào thử {max_attempts} đợt Proxy ({max_attempts * target_count} proxy) nhưng tất cả đều bị chặn IP!")

            if self.is_hunting:
                print("[*] Proxy Hunter is already running in background...")
                return self.proxies
            self.is_hunting = True

        try:
            self.hunt_attempts += 1
            print(f"\n[*] [Đợt cào {self.hunt_attempts}/{max_attempts}] Bắt đầu cào {target_count} proxy live tươi cho Viettel...")
            raw_proxies = collect_proxies()
            if raw_proxies:
                live = check_all(raw_proxies, target_live_count=target_count)
                if live:
                    fast_list = save_results(live)
                    with self._lock:
                        self.proxies = fast_list
                        self.current_index = 0
                    return self.proxies
            print(f"[!] Đợt cào {self.hunt_attempts} không tìm thấy đủ {target_count} proxy live.")
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
