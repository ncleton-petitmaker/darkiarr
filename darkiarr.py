#!/usr/bin/env python3
"""
Darkiarr - DarkiWorld Torznab Indexer + qBittorrent Download Client.

A bridge between DarkiWorld (French DDL indexer) and the *arr stack.
Exposes two standard APIs on one port:
  - Torznab API  (/torznab/api)  -> Radarr/Sonarr see it as an indexer
  - qBittorrent API (/api/v2/)   -> Radarr/Sonarr see it as a download client

Requires: requests, undetected-chromedriver, chromium, xvfb
"""

import hashlib
import http.server
import json
import os
import re
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

# =========================================================================
# Configuration
# =========================================================================
DARKIWORLD_DOMAIN = os.environ.get("DW_DOMAIN", "darkiworld2026.com")
DARKIWORLD_IP = os.environ.get("DW_IP", "188.114.97.2")
ALLDEBRID_KEY = os.environ.get("ALLDEBRID_KEY", "")
ALLDEBRID_AGENT = os.environ.get("ALLDEBRID_AGENT", "darkiarr")
TURNSTILE_SITEKEY = os.environ.get("DW_TURNSTILE_SITEKEY", "0x4AAAAAAAcZdZg-G_cmB9SW")
DW_EMAIL = os.environ.get("DW_EMAIL", "")
DW_PASSWORD = os.environ.get("DW_PASSWORD", "")
MOUNT_PATH = Path(os.environ.get("MOUNT_PATH", "/mnt/darkiarr/links"))
STAGING_PATH = Path(os.environ.get("STAGING_PATH", "/mnt/darkiarr/qbit"))
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8720"))
LISTEN_HOST = os.environ.get("LISTEN_HOST", "0.0.0.0")
DARKIARR_API_KEY = os.environ.get("DARKIARR_API_KEY", "darkiarr")
# Base URL that Radarr/Sonarr use to reach this server (for .torrent download links).
# Must be reachable from the Radarr/Sonarr containers/processes.
DARKIARR_BASE_URL = os.environ.get("DARKIARR_BASE_URL", "").rstrip("/")

def _validate_config():
    errors = []
    if not DW_EMAIL:
        errors.append("DW_EMAIL is required")
    if not DW_PASSWORD:
        errors.append("DW_PASSWORD is required")
    if not ALLDEBRID_KEY:
        errors.append("ALLDEBRID_KEY is required")
    if errors:
        for e in errors:
            print(f"[config] ERROR: {e}")
        sys.exit(1)

# DNS override for DarkiWorld (Tailscale / some networks block the domain)
try:
    import urllib3.util.connection as urllib3_cn
    _orig_create_conn = urllib3_cn.create_connection
    def _patched_create_conn(address, *args, **kwargs):
        host, port = address
        if host == DARKIWORLD_DOMAIN:
            host = DARKIWORLD_IP
        return _orig_create_conn((host, port), *args, **kwargs)
    urllib3_cn.create_connection = _patched_create_conn
except ImportError:
    pass

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: pip3 install requests")


# =========================================================================
# Bencode encoder/decoder (for .torrent file generation/parsing)
# =========================================================================
def bencode(obj):
    """Encode a Python object to bencode bytes."""
    if isinstance(obj, int):
        return f"i{obj}e".encode()
    if isinstance(obj, bytes):
        return f"{len(obj)}:".encode() + obj
    if isinstance(obj, str):
        b = obj.encode("utf-8")
        return f"{len(b)}:".encode() + b
    if isinstance(obj, list):
        return b"l" + b"".join(bencode(i) for i in obj) + b"e"
    if isinstance(obj, dict):
        items = sorted(obj.items(), key=lambda x: x[0] if isinstance(x[0], bytes) else x[0].encode())
        result = b"d"
        for k, v in items:
            result += bencode(k if isinstance(k, bytes) else k.encode("utf-8"))
            result += bencode(v)
        return result + b"e"
    raise TypeError(f"Cannot bencode {type(obj)}")


def bdecode(data, idx=0):
    """Decode bencode bytes to Python objects. Returns (obj, next_idx)."""
    if data[idx:idx+1] == b"i":
        end = data.index(b"e", idx)
        return int(data[idx+1:end]), end + 1
    if data[idx:idx+1] == b"l":
        result = []
        idx += 1
        while data[idx:idx+1] != b"e":
            obj, idx = bdecode(data, idx)
            result.append(obj)
        return result, idx + 1
    if data[idx:idx+1] == b"d":
        result = {}
        idx += 1
        while data[idx:idx+1] != b"e":
            key, idx = bdecode(data, idx)
            val, idx = bdecode(data, idx)
            result[key] = val
        return result, idx + 1
    # String: "len:data"
    colon = data.index(b":", idx)
    length = int(data[idx:colon])
    start = colon + 1
    return data[start:start+length], start + length


def make_torrent(lien_id, release_name, size, title_id=0):
    """Generate a minimal valid .torrent file for a DarkiWorld lien."""
    info = {
        b"length": size or 1,
        b"name": release_name.encode("utf-8"),
        b"piece length": 262144,
        b"pieces": hashlib.sha1(f"darkiarr-{lien_id}".encode()).digest(),
    }
    info_encoded = bencode(info)
    info_hash = hashlib.sha1(info_encoded).hexdigest()

    torrent = {
        b"announce": b"https://darkiarr.local/announce",
        b"comment": json.dumps({"lien_id": lien_id, "title_id": title_id}).encode(),
        b"created by": b"darkiarr",
        b"creation date": int(time.time()),
        b"info": info,
    }
    return bencode(torrent), info_hash


def parse_torrent(data):
    """Parse a .torrent file and extract darkiarr metadata + info_hash."""
    torrent, _ = bdecode(data)
    info = torrent.get(b"info", {})
    info_hash = hashlib.sha1(bencode(info)).hexdigest()
    name = info.get(b"name", b"").decode("utf-8", errors="replace")
    size = info.get(b"length", 0)

    comment = torrent.get(b"comment", b"").decode("utf-8", errors="replace")
    lien_id = 0
    title_id = 0
    try:
        meta = json.loads(comment)
        lien_id = meta.get("lien_id", 0)
        title_id = meta.get("title_id", 0)
    except (json.JSONDecodeError, TypeError):
        m = re.search(r"darkiarr[:\-](\d+)", comment)
        if m:
            lien_id = int(m.group(1))

    return {
        "info_hash": info_hash,
        "name": name,
        "size": size,
        "lien_id": lien_id,
        "title_id": title_id,
    }


# =========================================================================
# Browser session (undetected-chromedriver + Xvfb for Turnstile bypass)
# =========================================================================
class BrowserSession:
    """Maintains an authenticated browser session with Cloudflare Turnstile bypass."""

    def __init__(self):
        self.driver = None
        self.xvfb = None
        self.logged_in = False
        self._lock = threading.Lock()
        self._op_count = 0

    def ensure_alive(self):
        if self.driver and self._op_count >= 10:
            print(f"[browser] Periodic restart after {self._op_count} ops...")
            self._restart()
            return
        if not self.driver:
            print("[browser] Driver is None, restarting...")
            self.start()
            return
        try:
            _ = self.driver.current_url
        except Exception as e:
            print(f"[browser] Driver unresponsive ({e}), restarting...")
            self._restart()

    def _restart(self):
        old_data_dir = None
        try:
            if self.driver:
                old_data_dir = self.driver.user_data_dir if hasattr(self.driver, 'user_data_dir') else None
                self.driver.quit()
        except Exception:
            pass
        if old_data_dir and os.path.isdir(old_data_dir):
            import shutil
            try:
                shutil.rmtree(old_data_dir, ignore_errors=True)
            except Exception:
                pass
        self.driver = None
        self.logged_in = False
        self._op_count = 0
        self.start()

    def start(self):
        try:
            import undetected_chromedriver as uc
        except ImportError:
            sys.exit("Missing dependency: pip3 install undetected-chromedriver")

        subprocess.run(["pkill", "-9", "-f", "Xvfb"], capture_output=True)
        subprocess.run(["pkill", "-9", "-f", "chromedriver"], capture_output=True)
        time.sleep(1)

        display = f":{os.getpid() % 90 + 10}"
        self.xvfb = subprocess.Popen(
            ["Xvfb", display, "-screen", "0", "1920x1080x24"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        os.environ["DISPLAY"] = display
        time.sleep(2)

        if not os.path.exists("/tmp/chromedriver"):
            subprocess.run(["cp", "/usr/bin/chromedriver", "/tmp/chromedriver"])
            subprocess.run(["chmod", "+x", "/tmp/chromedriver"])

        options = uc.ChromeOptions()
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument(f"--host-resolver-rules=MAP {DARKIWORLD_DOMAIN} {DARKIWORLD_IP}")
        options.add_argument("--window-size=1920,1080")
        # Auto-detect chromium binary
        for chromium_bin in ["/usr/bin/chromium-browser", "/usr/bin/chromium", "/usr/bin/google-chrome"]:
            if os.path.exists(chromium_bin):
                options.binary_location = chromium_bin
                break

        self.driver = uc.Chrome(
            options=options, headless=False,
            version_main=143, driver_executable_path="/tmp/chromedriver"
        )
        self.driver.set_page_load_timeout(60)
        self.driver.set_script_timeout(90)

        print("[browser] Chrome started with Xvfb")
        self._login()

    def _get_turnstile_token(self, sitekey=None, timeout=30):
        sk = sitekey or TURNSTILE_SITEKEY
        self.driver.execute_script(f"""
            window.tsToken = null;
            if (typeof turnstile === 'undefined') {{
                var s = document.createElement('script');
                s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit&onload=onTsLoad';
                window.onTsLoad = function() {{
                    var d = document.createElement('div'); d.id = 'ts-' + Date.now();
                    document.body.appendChild(d);
                    turnstile.render('#' + d.id, {{
                        sitekey: '{sk}',
                        callback: function(t) {{ window.tsToken = t; }}
                    }});
                }};
                document.head.appendChild(s);
            }} else {{
                var d = document.createElement('div'); d.id = 'ts-' + Date.now();
                document.body.appendChild(d);
                turnstile.render('#' + d.id, {{
                    sitekey: '{sk}',
                    callback: function(t) {{ window.tsToken = t; }}
                }});
            }}
        """)

        for _ in range(timeout // 2):
            time.sleep(2)
            token = self.driver.execute_script("return window.tsToken")
            if token:
                return token
        return None

    def _do_login(self, token):
        """Attempt login with a Turnstile token. Returns True on success."""
        xsrf_js = self._get_xsrf_token_js()
        result = self.driver.execute_async_script(f"""
            var cb = arguments[arguments.length - 1];
            var xsrf = {xsrf_js};
            if (!xsrf) {{ cb({{error: 'XSRF-TOKEN missing for login'}}); return; }}
            fetch('/auth/login', {{
                method: 'POST',
                headers: {{
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                    'X-XSRF-TOKEN': decodeURIComponent(xsrf),
                    'X-Requested-With': 'XMLHttpRequest'
                }},
                body: JSON.stringify({{
                    email: arguments[0],
                    password: arguments[1],
                    token: arguments[2]
                }})
            }}).then(r => r.json()).then(d => cb(d)).catch(e => cb({{error: e.message}}));
        """, DW_EMAIL, DW_PASSWORD, token)

        if not result or "errors" in result:
            print(f"[browser] Login API failed: {result}")
            return False

        self.driver.get(f"https://{DARKIWORLD_DOMAIN}/")
        time.sleep(3)
        user = self.driver.execute_script("return window.bootstrapData?.user?.email")
        if user:
            print(f"[browser] Logged in as {user}")
            self.logged_in = True
            return True
        return False

    def _login(self):
        print("[browser] Loading DarkiWorld...")
        self.driver.get(f"https://{DARKIWORLD_DOMAIN}/")
        time.sleep(5)

        for attempt in range(1, 4):
            print(f"[browser] Solving Turnstile (attempt {attempt})...")
            token = self._get_turnstile_token()
            if not token:
                print("[browser] Turnstile solve failed")
                self.driver.refresh()
                time.sleep(5)
                continue

            if self._do_login(token):
                return

            print("[browser] Login failed, retrying...")
            self.driver.get(f"https://{DARKIWORLD_DOMAIN}/")
            time.sleep(5)

        print("[browser] WARNING: All login attempts failed, continuing anyway")
        self.logged_in = True  # Allow degraded operation

    def _get_xsrf_token_js(self):
        """JS snippet that safely extracts XSRF-TOKEN from cookies."""
        return "(document.cookie.match(/XSRF-TOKEN=([^;]+)/)||[])[1]"

    def _has_xsrf_token(self):
        """Check if the browser still has an XSRF-TOKEN cookie."""
        token = self.driver.execute_script(
            f"return {self._get_xsrf_token_js()}"
        )
        return bool(token)

    def _ensure_session(self):
        """Re-login if the session has expired (no XSRF-TOKEN cookie)."""
        if not self._has_xsrf_token():
            print("[browser] XSRF-TOKEN missing, session expired - re-logging in...")
            self.logged_in = False
            self._login()

    def api_get(self, path, params=None):
        with self._lock:
            self._ensure_session()
            qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in (params or {}).items())
            url = f"/api/v1/{path}" + (f"?{qs}" if qs else "")
            xsrf_js = self._get_xsrf_token_js()
            result = self.driver.execute_async_script(f"""
                var cb = arguments[arguments.length - 1];
                var token = {xsrf_js};
                if (!token) {{ cb({{error: 'XSRF-TOKEN missing'}}); return; }}
                fetch(arguments[0], {{
                    headers: {{
                        'Accept': 'application/json',
                        'X-XSRF-TOKEN': decodeURIComponent(token),
                        'X-Requested-With': 'XMLHttpRequest'
                    }}
                }}).then(r => r.json()).then(d => cb(d)).catch(e => cb({{error: e.message}}));
            """, url)
            return result

    def api_post(self, path, body=None):
        with self._lock:
            self._ensure_session()
            xsrf_js = self._get_xsrf_token_js()
            result = self.driver.execute_async_script(f"""
                var cb = arguments[arguments.length - 1];
                var token = {xsrf_js};
                if (!token) {{ cb({{error: 'XSRF-TOKEN missing'}}); return; }}
                fetch('/api/v1/' + arguments[0], {{
                    method: 'POST',
                    headers: {{
                        'Content-Type': 'application/json',
                        'Accept': 'application/json',
                        'X-XSRF-TOKEN': decodeURIComponent(token),
                        'X-Requested-With': 'XMLHttpRequest'
                    }},
                    body: JSON.stringify(arguments[1] || {{}})
                }}).then(async r => ({{
                    status: r.status, body: await r.json()
                }})).then(d => cb(d)).catch(e => cb({{error: e.message}}));
            """, path, body or {})
            return result

    def download_lien(self, lien_id):
        self.ensure_alive()
        with self._lock:
            self._op_count += 1
            current = self.driver.current_url
            if "/download" not in current and "/titles/" not in current:
                self.driver.get(f"https://{DARKIWORLD_DOMAIN}/")
                time.sleep(3)

            token = self._get_turnstile_token()
            if not token:
                print(f"[browser] Turnstile failed for download {lien_id}")
                return None

            result = self.driver.execute_async_script("""
                var cb = arguments[arguments.length - 1];
                fetch('/api/v1/liens/' + arguments[0] + '/download', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Accept': 'application/json',
                        'X-XSRF-TOKEN': decodeURIComponent(
                            document.cookie.match(/XSRF-TOKEN=([^;]+)/)[1]
                        ),
                        'X-Requested-With': 'XMLHttpRequest'
                    },
                    body: JSON.stringify({token: arguments[1]})
                }).then(async r => ({status: r.status, body: await r.text()}))
                .then(d => cb(d)).catch(e => cb({error: e.message}));
            """, str(lien_id), token)
            return result

    def resolve_darki_zone(self, darki_url):
        self.ensure_alive()
        with self._lock:
            self._op_count += 1
            print(f"[browser] Resolving darki.zone...")
            self.driver.get(darki_url)
            time.sleep(6)

            for i in range(20):
                ts_value = self.driver.execute_script(
                    'var inp = document.querySelector(\'input[name="cf-turnstile-response"]\');'
                    'return inp ? inp.value : null;'
                )
                if ts_value and len(ts_value) > 10:
                    print(f"[browser] darki.zone Turnstile solved")
                    break
                time.sleep(2)
            else:
                print("[browser] darki.zone Turnstile timeout")
                return None

            self.driver.execute_script('document.querySelector("form").submit()')
            time.sleep(6)

            links = self.driver.execute_script("""
                return Array.from(document.querySelectorAll('a[href]'))
                    .map(a => a.href)
                    .filter(h => h.includes('1fichier') || h.includes('rapidgator')
                              || h.includes('turbobit') || h.includes('nitroflare'));
            """)
            # Navigate back to DarkiWorld for future API calls
            self.driver.get(f"https://{DARKIWORLD_DOMAIN}/")
            time.sleep(3)

            if links:
                url = links[0].replace('&amp;', '&')
                print(f"[browser] Resolved to: {url[:80]}...")
                return url

            print("[browser] No hoster link found after form submit")
            return None

    def stop(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
        if self.xvfb:
            self.xvfb.terminate()


browser = BrowserSession()


# =========================================================================
# DarkiWorld API
# =========================================================================
UA = "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"


def dw_search(query, content_type=None):
    """Search DarkiWorld by text query. Returns list of title dicts."""
    # Prefer browser API (authenticated, no Cloudflare issues)
    if browser.logged_in and browser.driver:
        try:
            search_params = {"query": query, "perPage": 20}
            if content_type == "movie":
                search_params["type"] = "movie"
            elif content_type == "series":
                search_params["type"] = "series"
            result = browser.api_get("titles", search_params)
            if result and "pagination" in result:
                data = result["pagination"].get("data", [])
                results = []
                for item in data:
                    item_is_series = item.get("is_series", False)
                    type_ok = True
                    if content_type == "movie" and item_is_series:
                        type_ok = False
                    elif content_type == "series" and not item_is_series:
                        type_ok = False
                    if type_ok:
                        results.append(item)
                if results:
                    print(f"[dw] Browser search '{query}': {len(results)} results")
                    return results
            print(f"[dw] Browser search '{query}': no results")
        except Exception as e:
            print(f"[dw] Browser search error: {e}")

    # Fallback to public search API
    url = f"https://{DARKIWORLD_DOMAIN}/api/v1/search/{urllib.parse.quote(query)}"
    try:
        r = requests.get(url, params={"limit": 20}, timeout=15,
                         headers={"User-Agent": UA, "Accept": "application/json"})
    except requests.RequestException as e:
        print(f"[dw] Search request failed: {e}")
        return []
    try:
        data = r.json()
    except Exception:
        match = re.search(r'"results":\s*(\[.*?\])\s*,\s*"query"', r.text, re.DOTALL)
        if match:
            data = {"results": json.loads(match.group(1))}
        else:
            print(f"[dw] Search failed: non-JSON response ({r.status_code})")
            return []
    results = []
    for item in data.get("results", []):
        if item.get("model_type") != "title":
            continue
        if content_type and item.get("type") != content_type:
            continue
        results.append(item)
    return results


def dw_search_by_tmdb(tmdb_id, content_type=None):
    """Search DarkiWorld by TMDB ID. Returns a single title dict or None."""
    if browser.logged_in and browser.driver:
        try:
            search_params = {"tmdb_id": tmdb_id}
            if content_type == "movie":
                search_params["type"] = "movie"
            elif content_type == "series":
                search_params["type"] = "series"
            result = browser.api_get("titles", search_params)
            if result and "pagination" in result:
                data = result["pagination"].get("data", [])
                for item in data:
                    item_tmdb = str(item.get("tmdb_id", ""))
                    item_is_series = item.get("is_series", False)
                    # Match TMDB ID AND content type
                    type_ok = True
                    if content_type == "movie" and item_is_series:
                        type_ok = False
                    elif content_type == "series" and not item_is_series:
                        type_ok = False
                    if item_tmdb == str(tmdb_id) and type_ok:
                        print(f"[dw] TMDB {tmdb_id} found via browser API: {item.get('name')}")
                        return item
            if result and isinstance(result, dict) and result.get("id") and "error" not in result:
                print(f"[dw] TMDB {tmdb_id} found via browser API (direct): {result.get('name')}")
                return result
            print(f"[dw] TMDB {tmdb_id} not found via browser API tmdb_id param")
        except Exception as e:
            print(f"[dw] Browser API search error: {e}")

    # Fallback: look up title metadata from *arr APIs and search by name
    arr_info = _lookup_title_from_arr(tmdb_id, content_type)
    if arr_info:
        arr_title = arr_info["title"]
        arr_year = arr_info.get("year")
        arr_imdb = arr_info.get("imdb_id")
        print(f"[dw] Searching DW for '{arr_title}' (from *arr lookup, TMDB {tmdb_id})")
        results = dw_search(arr_title, content_type)
        # Prefer exact TMDB match
        for r in results:
            if str(r.get("tmdb_id", "")) == str(tmdb_id):
                print(f"[dw] TMDB {tmdb_id} matched via tmdb_id: {r.get('name')}")
                return r
        # Prefer exact IMDB match
        if arr_imdb:
            for r in results:
                if r.get("imdb_id") == arr_imdb:
                    print(f"[dw] TMDB {tmdb_id} matched via imdb_id ({arr_imdb}): {r.get('name')}")
                    return r
        # Validate by year (+/-1 tolerance)
        if arr_year:
            year_matches = [r for r in results
                            if r.get("year") and abs(int(r["year"]) - int(arr_year)) <= 1]
            if len(year_matches) == 1:
                print(f"[dw] TMDB {tmdb_id} matched via year ({arr_year}): {year_matches[0].get('name')}")
                return year_matches[0]
            if year_matches:
                print(f"[dw] TMDB {tmdb_id} multiple year matches for '{arr_title}' ({arr_year}), "
                      f"picking first: {year_matches[0].get('name')}")
                return year_matches[0]
            print(f"[dw] TMDB {tmdb_id} no year match for '{arr_title}' ({arr_year}) "
                  f"among {[(r.get('name'), r.get('year')) for r in results]}")
        elif len(results) == 1:
            print(f"[dw] TMDB {tmdb_id} single result via name search: {results[0].get('name')}")
            return results[0]
        else:
            print(f"[dw] TMDB {tmdb_id} {len(results)} results, cannot validate without year")

    print(f"[dw] TMDB {tmdb_id} not found")
    return None


def dw_get_liens(title_id, season=1):
    """Get all download links for a title + season (paginated)."""
    all_liens = []
    page = 1
    while True:
        result = browser.api_get("liens", {
            "title_id": title_id,
            "loader": "linksdl",
            "season": season,
            "perPage": 500,
            "page": page,
            "filters": "",
            "paginate": "lengthAware",
        })
        if not result or "error" in result:
            print(f"[dw] Error getting liens for {title_id} page {page}: {result}")
            break
        pagination = result.get("pagination", {})
        data = pagination.get("data", [])
        if not data:
            break
        all_liens.extend(data)
        last_page = pagination.get("last_page", pagination.get("lastPage", 1))
        if page == 1:
            total = pagination.get("total", "?")
            print(f"[dw] Liens: total={total} last_page={last_page}")
        if page >= last_page:
            break
        page += 1
    print(f"[dw] Got {len(all_liens)} liens for title {title_id} season {season}")
    return all_liens


def dw_get_seasons(title_id):
    """Get list of season numbers for a series."""
    result = browser.api_get(f"titles/{title_id}/seasons")
    if result and "error" not in result:
        seasons = result.get("seasons", result.get("pagination", {}).get("data", []))
        if isinstance(seasons, list) and seasons:
            nums = [s.get("number", s) if isinstance(s, dict) else s for s in seasons]
            nums = sorted([n for n in nums if isinstance(n, int) and n > 0])
            if nums:
                print(f"[dw] Seasons from API: {nums}")
                return nums

    print(f"[dw] Seasons API failed, probing...")
    found = []
    for sn in range(1, 21):
        liens = dw_get_liens(title_id, season=sn)
        if liens:
            found.append(sn)
        elif found:
            break
    print(f"[dw] Found seasons by probing: {found}")
    return found


# =========================================================================
# Quality & language mapping
# =========================================================================
QUALITY_MAP = {
    89: "REMUX UHD", 57: "REMUX BLURAY", 92: "REMUX DVD",
    17: "Blu-Ray 1080p", 76: "Blu-Ray 1080p (x265)", 16: "Blu-Ray 720p", 18: "Blu-Ray 3D",
    52: "HD 1080p", 31: "HD 720p",
    50: "HDLight 1080p", 86: "HDLight 1080p (x265)", 49: "HDLight 720p",
    60: "Ultra HDLight (x265)", 53: "ULTRA HD (x265)",
    55: "WEB 1080p", 83: "WEB 1080p (x265)", 94: "WEB 1080p Light", 54: "WEB 720p", 4: "WEB",
    62: "HDTV 1080p", 61: "HDTV 720p", 14: "HDTV",
    15: "HDRip", 1: "DVDRIP", 51: "DVDRIP MKV",
    13: "ISO", 12: "IMG", 10: "DVD-R", 11: "Full-DVD",
}

# Maps DW quality IDs to scene-style release name components.
# Format: "resolution.source.codec" - must be parseable by Radarr/Sonarr.
DW_QUALITY_TO_RELEASE = {
    86: "1080p.BluRay.x265",       # HDLight 1080p x265
    83: "1080p.WEB-DL.x265",      # WEB 1080p x265
    76: "1080p.BluRay.x265",      # Blu-Ray 1080p x265
    55: "1080p.WEB-DL",            # WEB 1080p
    50: "1080p.BluRay",            # HDLight 1080p
    17: "1080p.BluRay",            # Blu-Ray 1080p
    52: "1080p.BluRay",            # HD 1080p
    94: "1080p.WEB-DL",            # WEB 1080p Light
    53: "2160p.BluRay.x265",      # ULTRA HD x265
    60: "2160p.BluRay.x265.HDR",  # Ultra HDLight x265
    89: "2160p.REMUX.BluRay",     # REMUX UHD
    57: "1080p.REMUX.BluRay",     # REMUX BLURAY
    54: "720p.WEB-DL",             # WEB 720p
    49: "720p.BluRay",             # HDLight 720p
    31: "720p.BluRay",             # HD 720p
    16: "720p.BluRay",             # Blu-Ray 720p
    62: "1080p.HDTV",              # HDTV 1080p
    61: "720p.HDTV",               # HDTV 720p
    14: "HDTV",                     # HDTV
    15: "720p.HDRip",              # HDRip
    1:  "DVDRip",                   # DVDRIP
    51: "DVDRip",                   # DVDRIP MKV
    4:  "WEB-DL",                   # WEB
    92: "DVDRip.REMUX",            # REMUX DVD
    18: "1080p.BluRay.3D",         # Blu-Ray 3D
}


def _get_host_name(lien):
    h = lien.get("host", {})
    return h.get("name", "?") if isinstance(h, dict) else str(h)


def _get_quality_name(lien):
    return QUALITY_MAP.get(lien.get("qualite"), f"id:{lien.get('qualite')}")


def _get_langs(lien):
    return [la.get("name", "") for la in lien.get("langues_compact", [])]


def _get_lang_tag(lien, original_language=None):
    """Build a scene-style language tag (MULTI, FRENCH, VOSTFR, etc.).
    If the content's original language is French and the release is tagged
    FRENCH (VF only), promote it to TRUEFRENCH so Radarr/Sonarr custom
    formats that penalize VF-only releases don't reject it."""
    langs = _get_langs(lien)
    lang_str = " ".join(langs).lower()
    if "multi" in lang_str:
        return "MULTi"
    if "truefrench" in lang_str:
        return "TRUEFRENCH"
    if "french" in lang_str or "vfi" in lang_str:
        # For French-original content, FRENCH *is* the original language,
        # so it's equivalent to TRUEFRENCH (not a dub-only release).
        if original_language and original_language.lower() in ("fr", "french"):
            return "TRUEFRENCH"
        return "FRENCH"
    if "vostfr" in lang_str:
        return "VOSTFR"
    if "vo" in lang_str or "english" in lang_str:
        return "ENGLISH"
    return "MULTi"


def _is_supported_host(lien):
    """Check if the lien's host is supported by AllDebrid."""
    host = _get_host_name(lien).lower()
    return any(h in host for h in ("1fichier", "rapidgator", "turbobit", "nitroflare", "uptobox"))


# =========================================================================
# AllDebrid
# =========================================================================
def alldebrid_unlock(link):
    """Unlock a link via AllDebrid. Returns {link, filename, filesize} or None."""
    try:
        r = requests.get(
            "https://api.alldebrid.com/v4/link/unlock",
            params={"agent": ALLDEBRID_AGENT, "apikey": ALLDEBRID_KEY, "link": link},
            timeout=30
        )
        data = r.json()
        if data.get("status") == "success":
            d = data["data"]
            return {
                "link": d["link"],
                "filename": d.get("filename", ""),
                "filesize": d.get("filesize", 0),
            }
        print(f"[alldebrid] Unlock error: {data}")
    except Exception as e:
        print(f"[alldebrid] Unlock exception: {e}")
    return None


def alldebrid_save_link(fichier_url):
    """Save a link to AllDebrid cloud (persists to WebDAV /links/)."""
    try:
        r = requests.post(
            "https://api.alldebrid.com/v4/user/links/save",
            params={"agent": ALLDEBRID_AGENT, "apikey": ALLDEBRID_KEY},
            data={"links[]": fichier_url},
            timeout=30
        )
        data = r.json()
        if data.get("status") == "success":
            print(f"[alldebrid] Link saved to cloud")
            return True
        print(f"[alldebrid] Save error: {data}")
    except Exception as e:
        print(f"[alldebrid] Save exception: {e}")
    return False


# =========================================================================
# File utilities
# =========================================================================
def wait_for_file(filename, timeout=300):
    """Poll rclone mount until file appears. Returns Path or None."""
    print(f"[mount] Waiting for {filename} in {MOUNT_PATH}...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            target = MOUNT_PATH / filename
            if target.exists() and target.stat().st_size > 0:
                print(f"[mount] File found: {target}")
                return target
        except OSError:
            pass
        try:
            for f in os.listdir(MOUNT_PATH):
                if f == filename:
                    target = MOUNT_PATH / f
                    if target.stat().st_size > 0:
                        print(f"[mount] File found: {target}")
                        return target
        except OSError:
            pass
        time.sleep(15)
    print(f"[mount] Timeout waiting for {filename}")
    return None


def create_symlink(source_path, dest_dir, dest_name=None):
    """Create a symlink. Returns the symlink path."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    link_name = dest_name or source_path.name
    link_path = dest_dir / link_name
    if link_path.exists() or link_path.is_symlink():
        link_path.unlink()
    link_path.symlink_to(source_path)
    print(f"[symlink] {link_path} -> {source_path}")
    return link_path


# =========================================================================
# Resolve hoster URL from DarkiWorld lien ID
# =========================================================================
def _resolve_hoster_url(lien_id, max_retries=3):
    """Get the actual hoster URL (e.g. 1fichier) from a DarkiWorld lien ID."""
    for attempt in range(1, max_retries + 1):
        try:
            if attempt > 1:
                print(f"[resolve] Retry {attempt}/{max_retries} for lien {lien_id}")
                time.sleep(5)

            result = browser.download_lien(lien_id)
            if not result:
                continue

            status = result.get("status")
            body = result.get("body", "")

            if status == 429:
                print(f"[resolve] Rate limited (429), waiting 30s...")
                time.sleep(30)
                continue

            try:
                data = json.loads(body)
            except (json.JSONDecodeError, TypeError):
                print(f"[resolve] Invalid response for {lien_id}: {str(body)[:200]}")
                continue

            lien_data = data.get("lien", data)
            darki_url = lien_data.get("lien") or data.get("url") or data.get("link", "")
            if not darki_url:
                print(f"[resolve] No URL in response: {json.dumps(data)[:300]}")
                continue

            print(f"[resolve] Got URL: {darki_url[:80]}...")

            if "darki.zone" in darki_url:
                hoster_url = browser.resolve_darki_zone(darki_url)
                if not hoster_url:
                    print(f"[resolve] Failed to resolve darki.zone")
                    continue
                return hoster_url

            return darki_url
        except Exception as e:
            print(f"[resolve] Exception for lien {lien_id} (attempt {attempt}): {e}")

    print(f"[resolve] All {max_retries} attempts failed for lien {lien_id}")
    try:
        browser._restart()
    except Exception as e:
        print(f"[resolve] Browser restart failed: {e}")
    return None


# =========================================================================
# Torznab Indexer API
# =========================================================================
RADARR_URL = os.environ.get("RADARR_URL", "")
RADARR_KEY = os.environ.get("RADARR_KEY", "")
SONARR_URL = os.environ.get("SONARR_URL", "")
SONARR_KEY = os.environ.get("SONARR_KEY", "")


def _lookup_title_from_arr(tmdb_id, content_type=None):
    """Look up title metadata from Radarr/Sonarr by TMDB ID.
    Returns a dict with title, year, imdb_id (or None)."""
    tmdb_str = str(tmdb_id)
    # Try Radarr lookup (works even if movie isn't in library)
    if content_type != "series" and RADARR_URL and RADARR_KEY:
        try:
            r = requests.get(f"{RADARR_URL}/api/v3/movie/lookup/tmdb",
                             params={"tmdbId": tmdb_str},
                             headers={"X-Api-Key": RADARR_KEY}, timeout=10)
            data = r.json()
            if isinstance(data, list) and data:
                data = data[0]
            if isinstance(data, dict) and data.get("title"):
                info = {
                    "title": data.get("title") or data.get("originalTitle", ""),
                    "year": data.get("year"),
                    "imdb_id": data.get("imdbId"),
                }
                print(f"[arr] Radarr lookup: '{info['title']}' ({info['year']}) for TMDB {tmdb_id}")
                return info
        except Exception as e:
            print(f"[arr] Radarr lookup error: {e}")
    # Try Sonarr lookup
    if content_type != "movie" and SONARR_URL and SONARR_KEY:
        try:
            r = requests.get(f"{SONARR_URL}/api/v3/series/lookup",
                             params={"term": f"tmdb:{tmdb_str}"},
                             headers={"X-Api-Key": SONARR_KEY}, timeout=10)
            data = r.json()
            if isinstance(data, list) and data:
                d = data[0]
                title = d.get("title", "")
                if title:
                    info = {
                        "title": title,
                        "year": d.get("year"),
                        "imdb_id": d.get("imdbId"),
                    }
                    print(f"[arr] Sonarr lookup: '{info['title']}' ({info['year']}) for TMDB {tmdb_id}")
                    return info
        except Exception as e:
            print(f"[arr] Sonarr lookup error: {e}")
    print(f"[arr] TMDB {tmdb_id} not found via *arr lookup APIs")
    return None


def _lookup_original_language(tmdb_id, content_type=None):
    """Look up the original language of a title from Radarr/Sonarr by TMDB ID.
    Returns a language string like 'French', 'English', etc. or None."""
    tmdb_str = str(tmdb_id)
    if content_type != "series" and RADARR_URL and RADARR_KEY:
        try:
            r = requests.get(f"{RADARR_URL}/api/v3/movie/lookup/tmdb",
                             params={"tmdbId": tmdb_str},
                             headers={"X-Api-Key": RADARR_KEY}, timeout=10)
            data = r.json()
            if isinstance(data, list) and data:
                data = data[0]
            if isinstance(data, dict):
                orig_lang = data.get("originalLanguage", {})
                if isinstance(orig_lang, dict):
                    return orig_lang.get("name", "")
        except Exception:
            pass
    if content_type != "movie" and SONARR_URL and SONARR_KEY:
        try:
            r = requests.get(f"{SONARR_URL}/api/v3/series/lookup",
                             params={"term": f"tmdb:{tmdb_str}"},
                             headers={"X-Api-Key": SONARR_KEY}, timeout=10)
            data = r.json()
            if isinstance(data, list) and data:
                orig_lang = data[0].get("originalLanguage", {})
                if isinstance(orig_lang, dict):
                    return orig_lang.get("name", "")
        except Exception:
            pass
    return None


def _normalize(s):
    """Normalize a title for comparison."""
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r'[^a-z0-9 ]', '', s.lower()).strip()


def _build_release_name(title_name, year, quality_str, lang_tag, episode_tag=""):
    """Build a scene-style release name parseable by Radarr/Sonarr.
    Example: The.Movie.2024.MULTi.1080p.WEB-DL.x265-DarkiWorld"""
    # Replace spaces and special chars with dots
    name = re.sub(r'[^a-zA-Z0-9àâäéèêëïîôùûüÿçœæÀÂÄÉÈÊËÏÎÔÙÛÜŸÇŒÆ]+', '.', title_name or "Unknown")
    name = name.strip('.')
    parts = [name]
    if year:
        parts.append(str(year))
    if episode_tag:
        parts.append(episode_tag)
    parts.append(lang_tag)
    parts.append(quality_str)
    base = ".".join(parts)
    return f"{base}-DarkiWorld"


def _get_base_url():
    """Get the base URL for download links, reachable from Radarr/Sonarr."""
    if DARKIARR_BASE_URL:
        return DARKIARR_BASE_URL
    return f"http://localhost:{LISTEN_PORT}"


def _xml_escape(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;")


def _torznab_caps_xml():
    return """<?xml version="1.0" encoding="UTF-8"?>
<caps>
  <server title="Darkiarr" />
  <limits max="100" default="50" />
  <searching>
    <search available="yes" supportedParams="q" />
    <movie-search available="yes" supportedParams="q,tmdbid,imdbid" />
    <tv-search available="yes" supportedParams="q,tmdbid,season,ep" />
  </searching>
  <categories>
    <category id="2000" name="Movies">
      <subcat id="2030" name="Movies/HD" />
      <subcat id="2040" name="Movies/BluRay" />
      <subcat id="2045" name="Movies/UHD" />
    </category>
    <category id="5000" name="TV">
      <subcat id="5030" name="TV/HD" />
      <subcat id="5040" name="TV/SD" />
      <subcat id="5045" name="TV/UHD" />
    </category>
  </categories>
</caps>"""


def _torznab_error_xml(code, description):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<error code="{code}" description="{_xml_escape(description)}" />"""


def _lien_to_torznab_item(lien, title_name, title_year, title_type, title_id,
                           tmdb_id=None, imdb_id=None, episode_tag="",
                           original_language=None):
    """Convert a DarkiWorld lien to a Torznab XML <item>."""
    lien_id = lien.get("id", 0)
    quality_id = lien.get("qualite", 0)
    quality_str = DW_QUALITY_TO_RELEASE.get(quality_id, "WEB-DL")
    lang_tag = _get_lang_tag(lien, original_language=original_language)
    size = lien.get("taille", 0) or 0

    release_name = _build_release_name(title_name, title_year, quality_str, lang_tag, episode_tag)

    # Download URL: Radarr will GET this to download a .torrent file
    dl_params = urllib.parse.urlencode({
        "apikey": DARKIARR_API_KEY,
        "name": release_name,
        "size": size,
        "title_id": title_id,
    })
    download_url = f"{_get_base_url()}/torznab/download/{lien_id}?{dl_params}"

    cat = "2000" if title_type == "movie" else "5000"

    # Determine sub-category based on quality
    if "2160p" in quality_str or "REMUX" in quality_str:
        subcat = "2045" if title_type == "movie" else "5045"
    elif "1080p" in quality_str or "720p" in quality_str:
        subcat = "2030" if title_type == "movie" else "5030"
    else:
        subcat = "2040" if title_type == "movie" else "5040"

    # Publication date from lien or current time
    pub_ts = lien.get("created_at") or lien.get("updated_at")
    if pub_ts and isinstance(pub_ts, str):
        try:
            pub_date = datetime.fromisoformat(pub_ts.replace("Z", "+00:00")).strftime("%a, %d %b %Y %H:%M:%S %z")
        except Exception:
            pub_date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")
    else:
        pub_date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")

    attrs = [
        f'    <torznab:attr name="category" value="{cat}" />',
        f'    <torznab:attr name="category" value="{subcat}" />',
        f'    <torznab:attr name="seeders" value="100" />',
        f'    <torznab:attr name="peers" value="100" />',
        f'    <torznab:attr name="grabs" value="50" />',
        f'    <torznab:attr name="size" value="{size}" />',
        f'    <torznab:attr name="files" value="1" />',
        '    <torznab:attr name="downloadvolumefactor" value="0" />',
        '    <torznab:attr name="uploadvolumefactor" value="1" />',
    ]
    if tmdb_id:
        attrs.append(f'    <torznab:attr name="tmdbid" value="{tmdb_id}" />')
    if imdb_id:
        attrs.append(f'    <torznab:attr name="imdbid" value="{_xml_escape(str(imdb_id))}" />')

    attrs_xml = "\n".join(attrs)

    return f"""  <item>
    <title>{_xml_escape(release_name)}</title>
    <guid>darkiarr-{lien_id}</guid>
    <link>{_xml_escape(download_url)}</link>
    <enclosure url="{_xml_escape(download_url)}" length="{size}" type="application/x-bittorrent" />
    <pubDate>{pub_date}</pubDate>
    <size>{size}</size>
    <category>{cat}</category>
{attrs_xml}
  </item>"""


def _wrap_torznab_results(items_xml):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:torznab="http://torznab.com/schemas/2015/feed">
  <channel>
    <title>Darkiarr - DarkiWorld</title>
    <description>DarkiWorld Torznab Indexer via Darkiarr</description>
    <link>{_xml_escape(_get_base_url())}</link>
{items_xml}
  </channel>
</rss>"""


def handle_torznab_search(params):
    """Handle Torznab search requests. Returns (content_type, body)."""
    t = params.get("t", [""])[0]

    if t == "caps":
        return "application/xml", _torznab_caps_xml()

    if t not in ("search", "movie", "tvsearch"):
        return "application/xml", _torznab_error_xml(202, f"No such function: {t}")

    tmdb_id = params.get("tmdbid", [None])[0]
    imdb_id = params.get("imdbid", [None])[0]
    query = params.get("q", [""])[0]

    # RSS/validation: Radarr/Sonarr/Prowlarr send queries with no search terms
    # to validate the indexer. Return a dummy item so categories are detected.
    if not query and not tmdb_id and not imdb_id:
        from email.utils import formatdate
        now_rfc2822 = formatdate(timeval=None, localtime=False, usegmt=True)
        dummy = f"""  <item>
    <title>Darkiarr.Test.2020.MULTi.1080p.WEB-DL.DarkiWorld</title>
    <guid>darkiarr-rss-test</guid>
    <link>http://localhost</link>
    <pubDate>{now_rfc2822}</pubDate>
    <enclosure url="http://localhost" length="1000000000" type="application/x-bittorrent" />
    <category>2000</category>
    <category>5000</category>
    <size>1000000000</size>
  </item>"""
        return "application/xml", _wrap_torznab_results(dummy)
    season = params.get("season", [None])[0]
    ep = params.get("ep", [None])[0]
    offset = int(params.get("offset", [0])[0])
    limit = int(params.get("limit", [100])[0])

    content_type_filter = None
    if t == "movie":
        content_type_filter = "movie"
    elif t == "tvsearch":
        content_type_filter = "series"

    # Find the title(s) on DarkiWorld
    dw_titles = []

    if tmdb_id:
        title = dw_search_by_tmdb(tmdb_id, content_type_filter)
        if title:
            dw_titles.append(title)

    if not dw_titles and query:
        results = dw_search(query, content_type_filter)
        if tmdb_id:
            for r in results:
                if str(r.get("tmdb_id", "")) == str(tmdb_id):
                    dw_titles.append(r)
                    break
        if not dw_titles:
            dw_titles = results[:5]

    # Note: dw_search_by_tmdb already handles *arr fallback + validation

    if not dw_titles:
        print(f"[torznab] No results for tmdb={tmdb_id} q={query}")
        return "application/xml", _wrap_torznab_results("")

    # Collect items from all matched titles
    all_items = []
    for dw_title in dw_titles:
        title_id = dw_title["id"]
        title_name = dw_title.get("name", "Unknown")
        title_year = dw_title.get("year", "")
        title_type = dw_title.get("type", content_type_filter or "movie")
        title_tmdb = dw_title.get("tmdb_id") or tmdb_id
        title_imdb = dw_title.get("imdb_id") or imdb_id

        print(f"[torznab] {t}: {title_name} ({title_year}) [DW:{title_id}] tmdb={title_tmdb}")

        # Look up original language so FRENCH releases on French-original
        # content get promoted to TRUEFRENCH (avoids custom format rejection).
        orig_lang = None
        if title_tmdb:
            orig_lang = _lookup_original_language(title_tmdb, content_type_filter)
            if orig_lang:
                print(f"[torznab] Original language: {orig_lang}")

        season_num = int(season) if season else 1
        liens = dw_get_liens(title_id, season=season_num)
        if not liens:
            continue

        # Filter to supported hosts only
        liens = [l for l in liens if _is_supported_host(l)]

        # For TV with specific episode
        if ep and title_type == "series":
            ep_num = int(ep)
            liens = [l for l in liens if _ep_matches(l, ep_num)]

        # Build items (deduplicated by quality+lang+episode)
        seen_keys = set()
        for lien in liens:
            episode_tag = ""
            if title_type == "series":
                ep_val = lien.get("episode")
                if ep_val is not None:
                    episode_tag = f"S{season_num:02d}E{int(ep_val):02d}"
                elif season:
                    episode_tag = f"S{season_num:02d}"

            key = (title_id, lien.get("qualite"), _get_lang_tag(lien, original_language=orig_lang), episode_tag)
            if key in seen_keys:
                continue
            seen_keys.add(key)

            all_items.append(_lien_to_torznab_item(
                lien, title_name, title_year, title_type, title_id,
                tmdb_id=title_tmdb, imdb_id=title_imdb, episode_tag=episode_tag,
                original_language=orig_lang,
            ))

    # Apply pagination
    paginated = all_items[offset:offset + limit]
    return "application/xml", _wrap_torznab_results("\n".join(paginated))


def _ep_matches(lien, ep_num):
    """Check if a lien matches a specific episode number."""
    ep_val = lien.get("episode")
    if ep_val is None:
        return False
    try:
        return int(ep_val) == ep_num
    except (ValueError, TypeError):
        return False


def handle_torznab_download(lien_id, params):
    """Generate and return a .torrent file for a given lien ID."""
    release_name = params.get("name", [f"darkiarr-{lien_id}"])[0]
    size = int(params.get("size", [0])[0])
    title_id = int(params.get("title_id", [0])[0])

    torrent_data, info_hash = make_torrent(lien_id, release_name, size, title_id)
    print(f"[torznab] Download .torrent for lien {lien_id}: {release_name} (hash={info_hash[:12]})")
    return torrent_data, info_hash


# =========================================================================
# qBittorrent Download Client API
# =========================================================================
jobs = {}  # hash -> job dict
jobs_lock = threading.Lock()


def _qbit_add_from_torrent_file(torrent_data, category="", savepath=""):
    """Add a job from uploaded .torrent file data."""
    try:
        meta = parse_torrent(torrent_data)
    except Exception as e:
        print(f"[qbit] Failed to parse .torrent file: {e}")
        return False

    lien_id = meta["lien_id"]
    info_hash = meta["info_hash"]
    release_name = meta["name"]
    size = meta["size"]

    if not lien_id:
        print(f"[qbit] No lien_id found in .torrent metadata")
        return False

    job = _create_job(info_hash, release_name, lien_id, size, category)
    print(f"[qbit] Job from .torrent: {release_name} (lien={lien_id}, hash={info_hash[:12]})")
    threading.Thread(target=_process_job, args=(info_hash,), daemon=True).start()
    return True


def _qbit_add_from_url(url, category="", tags=""):
    """Add a job from a darkiarr:// or download URL."""
    # Parse lien_id from URL
    m = re.search(r'darkiarr[:/]+(\d+)', url)
    if not m:
        # Try our download URL format: /torznab/download/{lien_id}
        m = re.search(r'/torznab/download/(\d+)', url)
    if not m:
        print(f"[qbit] Cannot extract lien_id from URL: {url[:100]}")
        return False

    lien_id = int(m.group(1))
    release_name = tags or f"darkiarr-{lien_id}"

    # Try to extract name from URL params
    parsed = urllib.parse.urlparse(url)
    url_params = urllib.parse.parse_qs(parsed.query)
    if "name" in url_params:
        release_name = url_params["name"][0]

    info_hash = hashlib.sha1(f"darkiarr-{lien_id}-{release_name}".encode()).hexdigest()
    size = int(url_params.get("size", [0])[0]) if "size" in url_params else 0

    job = _create_job(info_hash, release_name, lien_id, size, category)
    print(f"[qbit] Job from URL: {release_name} (lien={lien_id}, hash={info_hash[:12]})")
    threading.Thread(target=_process_job, args=(info_hash,), daemon=True).start()
    return True


def _create_job(info_hash, release_name, lien_id, size, category):
    """Create a new download job in the tracker."""
    cat = category or "radarr"
    job = {
        "hash": info_hash,
        "name": release_name,
        "lien_id": lien_id,
        "category": cat,
        "state": "downloading",
        "progress": 0.0,
        "content_path": "",
        "save_path": str(STAGING_PATH / cat),
        "size": size,
        "total_size": size,
        "added_on": int(time.time()),
        "completion_on": -1,
        "dlspeed": 10_000_000,
        "eta": 600,
        "num_seeds": 100,
        "num_leechs": 0,
        "ratio": 0,
        "files": [],
        "error": "",
    }
    with jobs_lock:
        jobs[info_hash] = job
    return job


def _process_job(job_hash):
    """Background pipeline: resolve -> debrid -> wait mount -> symlink -> done."""
    with jobs_lock:
        job = jobs.get(job_hash)
    if not job:
        return

    lien_id = job["lien_id"]
    category = job["category"]
    release_name = job["name"]

    try:
        # Step 1: Resolve hoster URL
        print(f"[qbit] [{release_name}] Resolving lien {lien_id}...")
        hoster_url = _resolve_hoster_url(lien_id)
        if not hoster_url:
            _fail_job(job_hash, "Failed to resolve hoster URL")
            return

        with jobs_lock:
            job["progress"] = 0.2
            job["dlspeed"] = 8_000_000

        # Step 2: AllDebrid unlock (get filename + filesize)
        print(f"[qbit] [{release_name}] Unlocking via AllDebrid...")
        unlock = alldebrid_unlock(hoster_url)
        if not unlock:
            _fail_job(job_hash, "AllDebrid unlock failed")
            return

        filename = unlock["filename"]
        filesize = unlock.get("filesize", 0)
        with jobs_lock:
            job["size"] = filesize
            job["total_size"] = filesize
            job["progress"] = 0.4

        # Step 3: Save to AllDebrid cloud
        print(f"[qbit] [{release_name}] Saving to AllDebrid cloud...")
        if not alldebrid_save_link(hoster_url):
            _fail_job(job_hash, "AllDebrid save failed")
            return

        with jobs_lock:
            job["progress"] = 0.6
            job["dlspeed"] = 5_000_000

        # Step 4: Wait for file on rclone mount
        print(f"[qbit] [{release_name}] Waiting for {filename}...")
        mount_file = wait_for_file(filename)
        if not mount_file:
            _fail_job(job_hash, "Timeout waiting for file on rclone mount")
            return

        with jobs_lock:
            job["progress"] = 0.8

        # Step 5: Create symlink in staging directory
        staging_dir = STAGING_PATH / category / release_name
        link = create_symlink(mount_file, staging_dir)

        with jobs_lock:
            job["state"] = "pausedUP"
            job["progress"] = 1.0
            job["content_path"] = str(staging_dir)
            job["save_path"] = str(STAGING_PATH / category)
            job["completion_on"] = int(time.time())
            job["dlspeed"] = 0
            job["eta"] = 0
            job["ratio"] = 1.0
            job["files"] = [{
                "name": f"{release_name}/{link.name}",
                "size": filesize,
                "progress": 1.0,
                "priority": 1,
                "is_seed": False,
                "piece_range": [0, 0],
                "availability": 1.0,
            }]

        print(f"[qbit] [{release_name}] Complete -> {staging_dir}")

    except Exception as e:
        print(f"[qbit] [{release_name}] Error: {e}")
        _fail_job(job_hash, str(e))


def _fail_job(job_hash, error_msg):
    """Mark a job as failed."""
    with jobs_lock:
        job = jobs.get(job_hash)
        if job:
            job["state"] = "error"
            job["error"] = error_msg
            job["dlspeed"] = 0


def _qbit_torrents_info(category=None, hashes=None):
    """Return list of jobs in qBittorrent JSON format."""
    hash_list = hashes.split("|") if hashes else None
    with jobs_lock:
        result = []
        for h, job in jobs.items():
            if category and job.get("category") != category:
                continue
            if hash_list and h not in hash_list:
                continue
            result.append({
                "hash": job["hash"],
                "name": job["name"],
                "state": job["state"],
                "progress": job["progress"],
                "size": job.get("size", 0),
                "total_size": job.get("total_size", 0),
                "dlspeed": job.get("dlspeed", 0),
                "upspeed": 0,
                "eta": job.get("eta", 8640000),
                "num_seeds": job.get("num_seeds", 100),
                "num_leechs": job.get("num_leechs", 0),
                "ratio": job.get("ratio", 0),
                "added_on": job.get("added_on", 0),
                "completion_on": job.get("completion_on", -1),
                "category": job.get("category", ""),
                "tags": "",
                "save_path": job.get("save_path", str(STAGING_PATH)),
                "content_path": job.get("content_path", ""),
                "magnet_uri": "",
                "amount_left": int(job.get("size", 0) * (1 - job.get("progress", 0))),
                "downloaded": int(job.get("size", 0) * job.get("progress", 0)),
                "uploaded": 0,
                "seen_complete": job.get("completion_on", -1),
                "auto_tmm": False,
                "tracker": "darkiarr",
            })
    return result


def _qbit_torrent_files(torrent_hash):
    """Return files list for a job."""
    with jobs_lock:
        job = jobs.get(torrent_hash)
    if not job:
        return []
    return job.get("files", [])


def _qbit_delete_torrents(hashes_str, delete_files=False):
    """Delete jobs. Optionally remove files."""
    hashes = hashes_str.split("|") if hashes_str else []
    with jobs_lock:
        for h in hashes:
            job = jobs.pop(h, None)
            if job:
                print(f"[qbit] Deleted job: {job['name']}")
                if delete_files and job.get("content_path"):
                    try:
                        content = Path(job["content_path"])
                        # Remove all symlinks in the directory
                        if content.is_dir():
                            for f in content.iterdir():
                                if f.is_symlink():
                                    f.unlink()
                            content.rmdir()
                        elif content.is_symlink():
                            content.unlink()
                        print(f"[qbit] Deleted files for {job['name']}")
                    except Exception as e:
                        print(f"[qbit] Error deleting files: {e}")


# =========================================================================
# Multipart form data parser (handles both text fields and binary files)
# =========================================================================
def parse_multipart(body, content_type):
    """Parse multipart/form-data. Returns {field_name: value} for text fields
    and {field_name: (filename, bytes)} for file fields."""
    boundary_match = re.search(r'boundary=([^\s;]+)', content_type)
    if not boundary_match:
        return {}

    boundary = boundary_match.group(1).encode()
    parts = body.split(b"--" + boundary)
    fields = {}

    for part in parts:
        if part in (b"", b"--", b"--\r\n", b"\r\n"):
            continue

        header_end = part.find(b"\r\n\r\n")
        if header_end < 0:
            continue

        headers_raw = part[:header_end].decode("utf-8", errors="replace")
        value_raw = part[header_end + 4:]
        # Strip trailing \r\n and boundary markers
        if value_raw.endswith(b"\r\n"):
            value_raw = value_raw[:-2]

        name_match = re.search(r'name="([^"]+)"', headers_raw)
        if not name_match:
            continue
        field_name = name_match.group(1)

        filename_match = re.search(r'filename="([^"]*)"', headers_raw)
        if filename_match:
            # Binary file field
            fields[field_name] = (filename_match.group(1), value_raw)
        else:
            # Text field
            fields[field_name] = value_raw.decode("utf-8", errors="replace")

    return fields


# =========================================================================
# HTTP Server
# =========================================================================
class Handler(http.server.BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = urllib.parse.parse_qs(parsed.query)

        # === Torznab API ===
        if path in ("/torznab/api", "/torznab"):
            apikey = params.get("apikey", [""])[0]
            if DARKIARR_API_KEY and apikey != DARKIARR_API_KEY:
                return self.send_xml(401, _torznab_error_xml(100, "Invalid API Key"))
            ct, body = handle_torznab_search(params)
            return self.send_xml(200, body)

        # Torznab .torrent download
        m = re.match(r'^/torznab/download/(\d+)$', path)
        if m:
            apikey = params.get("apikey", [""])[0]
            if DARKIARR_API_KEY and apikey != DARKIARR_API_KEY:
                return self.send_error(401)
            lien_id = int(m.group(1))
            torrent_data, info_hash = handle_torznab_download(lien_id, params)
            self.send_response(200)
            self.send_header("Content-Type", "application/x-bittorrent")
            self.send_header("Content-Disposition", f'attachment; filename="{info_hash}.torrent"')
            self.send_header("Content-Length", str(len(torrent_data)))
            self.end_headers()
            self.wfile.write(torrent_data)
            return

        # === qBittorrent API ===
        if path == "/api/v2/auth/login":
            return self._qbit_login_response()

        if path == "/api/v2/app/version":
            return self.send_text("v4.6.7")

        if path == "/api/v2/app/webapiVersion":
            return self.send_text("2.9.3")

        if path == "/api/v2/app/buildInfo":
            return self.send_json({"qt": "6.7.0", "libtorrent": "2.0.10.0", "boost": "1.86", "openssl": "3.3.1", "bitness": 64})

        if path == "/api/v2/app/preferences":
            return self.send_json({
                "save_path": str(STAGING_PATH) + "/",
                "temp_path_enabled": False,
                "temp_path": "",
                "max_connec": 500,
                "max_connec_per_torrent": 100,
                "max_uploads": -1,
                "max_uploads_per_torrent": -1,
                "listen_port": LISTEN_PORT,
                "dht": False,
                "pex": False,
                "lsd": False,
            })

        if path == "/api/v2/transfer/info":
            return self.send_json({
                "dl_info_speed": 0, "dl_info_data": 0,
                "up_info_speed": 0, "up_info_data": 0,
                "dl_rate_limit": 0, "up_rate_limit": 0,
                "dht_nodes": 0, "connection_status": "connected",
            })

        if path == "/api/v2/torrents/info":
            category = params.get("category", [None])[0]
            hashes = params.get("hashes", [None])[0]
            return self.send_json(_qbit_torrents_info(category, hashes))

        if path == "/api/v2/torrents/files":
            h = params.get("hash", [""])[0]
            return self.send_json(_qbit_torrent_files(h))

        if path == "/api/v2/torrents/properties":
            h = params.get("hash", [""])[0]
            with jobs_lock:
                job = jobs.get(h, {})
            return self.send_json({
                "hash": job.get("hash", h),
                "name": job.get("name", ""),
                "save_path": job.get("save_path", str(STAGING_PATH)),
                "content_path": job.get("content_path", ""),
                "total_size": job.get("size", 0),
                "addition_date": job.get("added_on", 0),
                "completion_date": job.get("completion_on", -1),
                "created_by": "darkiarr",
                "dl_speed": job.get("dlspeed", 0),
                "eta": job.get("eta", 8640000),
                "nb_connections": 0,
                "nb_connections_limit": 100,
                "seeds": 100, "seeds_total": 100,
                "peers": 0, "peers_total": 0,
                "share_ratio": job.get("ratio", 0),
                "time_elapsed": 0,
                "total_downloaded": int(job.get("size", 0) * job.get("progress", 0)),
                "total_uploaded": 0,
                "piece_size": 262144,
                "pieces_have": 1 if job.get("progress", 0) >= 1.0 else 0,
                "pieces_num": 1,
            })

        if path == "/api/v2/torrents/categories":
            return self.send_json({
                "radarr": {"name": "radarr", "savePath": str(STAGING_PATH / "radarr")},
                "tv-sonarr": {"name": "tv-sonarr", "savePath": str(STAGING_PATH / "tv-sonarr")},
                "sonarr": {"name": "sonarr", "savePath": str(STAGING_PATH / "sonarr")},
            })

        if path == "/api/v2/torrents/trackers":
            return self.send_json([{"url": "darkiarr", "status": 2, "num_peers": 100, "num_seeds": 100, "num_leeches": 0, "msg": ""}])

        # === Utility endpoints ===
        if path == "/health":
            return self.send_json({
                "status": "ok",
                "browser": browser.logged_in,
                "jobs": len(jobs),
                "staging_path": str(STAGING_PATH),
                "mount_path": str(MOUNT_PATH),
            })

        if path == "/status":
            with jobs_lock:
                return self.send_json({"jobs": {h: {
                    "name": j["name"], "state": j["state"],
                    "progress": j["progress"], "category": j["category"],
                    "error": j.get("error", ""),
                } for h, j in jobs.items()}})

        # Legacy search
        if path == "/search":
            query = params.get("q", [""])[0]
            ctype = params.get("type", [None])[0]
            if not query:
                return self.send_json({"error": "missing q"}, 400)
            results = dw_search(query, ctype)
            return self.send_json({"results": [{
                "id": r["id"], "name": r["name"], "year": r.get("year"),
                "tmdb_id": r.get("tmdb_id"), "type": r.get("type"),
            } for r in results[:20]]})

        m = re.match(r'^/liens/(\d+)$', path)
        if m:
            title_id = int(m.group(1))
            season = int(params.get("season", [1])[0])
            liens = dw_get_liens(title_id, season)
            return self.send_json({"liens": liens})

        # / - index
        self.send_json({
            "service": "Darkiarr - DarkiWorld Torznab Indexer + qBittorrent Client",
            "version": "2.0.0",
            "endpoints": {
                "Torznab": f"{_get_base_url()}/torznab/api?t=caps&apikey={DARKIARR_API_KEY}",
                "qBittorrent": f"{_get_base_url()}/api/v2/app/version",
                "Health": f"{_get_base_url()}/health",
                "Status": f"{_get_base_url()}/status",
            }
        })

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")
        length = int(self.headers.get("Content-Length", 0))
        content_type = self.headers.get("Content-Type", "")

        if path == "/api/v2/auth/login":
            return self._qbit_login_response()

        if path == "/api/v2/torrents/add":
            body = self.rfile.read(length) if length else b""

            if "multipart/form-data" in content_type:
                fields = parse_multipart(body, content_type)

                # Handle .torrent file upload (Radarr sends this after downloading from our /torznab/download/)
                if "torrents" in fields and isinstance(fields["torrents"], tuple):
                    _filename, torrent_data = fields["torrents"]
                    category = fields.get("category", "") if isinstance(fields.get("category"), str) else ""
                    savepath = fields.get("savepath", "") if isinstance(fields.get("savepath"), str) else ""
                    if _qbit_add_from_torrent_file(torrent_data, category=category, savepath=savepath):
                        return self.send_text("Ok.")
                    return self.send_error_response(400, "Failed to parse .torrent file")

                # Handle URL-based add
                urls = fields.get("urls", "")
                if isinstance(urls, str) and urls.strip():
                    category = fields.get("category", "") if isinstance(fields.get("category"), str) else ""
                    tags = fields.get("tags", "") if isinstance(fields.get("tags"), str) else ""
                    for url in urls.strip().split("\n"):
                        url = url.strip()
                        if url:
                            _qbit_add_from_url(url, category=category, tags=tags)
                    return self.send_text("Ok.")

            else:
                # URL-encoded form data
                form = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"))
                urls = form.get("urls", [""])[0]
                category = form.get("category", [""])[0]
                tags = form.get("tags", [""])[0]
                if urls.strip():
                    for url in urls.strip().split("\n"):
                        url = url.strip()
                        if url:
                            _qbit_add_from_url(url, category=category, tags=tags)
                    return self.send_text("Ok.")

            return self.send_error_response(400, "No URLs or torrent files provided")

        if path == "/api/v2/torrents/delete":
            body = self.rfile.read(length) if length else b""
            form = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"))
            hashes = form.get("hashes", [""])[0]
            delete_files = form.get("deleteFiles", ["false"])[0].lower() == "true"
            _qbit_delete_torrents(hashes, delete_files)
            return self.send_text("Ok.")

        # No-op endpoints that Radarr/Sonarr may call
        if path in ("/api/v2/torrents/pause", "/api/v2/torrents/resume",
                     "/api/v2/torrents/recheck", "/api/v2/torrents/setForceStart",
                     "/api/v2/torrents/setSuperSeeding", "/api/v2/torrents/createCategory",
                     "/api/v2/torrents/editCategory", "/api/v2/torrents/removeCategories",
                     "/api/v2/torrents/addTags", "/api/v2/torrents/removeTags",
                     "/api/v2/app/setPreferences"):
            # Read body to clear the stream
            if length:
                self.rfile.read(length)
            return self.send_text("Ok.")

        if path == "/api/v2/torrents/setCategory":
            body = self.rfile.read(length) if length else b""
            form = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"))
            hashes = form.get("hashes", [""])[0]
            category = form.get("category", [""])[0]
            if hashes:
                with jobs_lock:
                    for h in hashes.split("|"):
                        if h in jobs:
                            jobs[h]["category"] = category
            return self.send_text("Ok.")

        if length:
            self.rfile.read(length)
        self.send_error(404)

    def _qbit_login_response(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Set-Cookie", "SID=darkiarr; path=/")
        self.end_headers()
        self.wfile.write(b"Ok.")

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_xml(self, status, body):
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def send_text(self, text):
        encoded = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def send_error_response(self, status, message):
        self.send_json({"error": message}, status)

    def log_message(self, fmt, *args):
        # Skip noisy qBittorrent polling
        msg = fmt % args
        if "/api/v2/torrents/info" in msg or "/api/v2/transfer/info" in msg:
            return
        print(f"[http] {msg}")


# =========================================================================
# Main
# =========================================================================
def init_browser_background():
    try:
        browser.start()
    except Exception as e:
        print(f"[init] Browser start failed: {e}")
        print("[init] Authenticated endpoints won't work until browser is ready")


def main():
    _validate_config()

    print("=" * 60)
    print("  Darkiarr - DarkiWorld Torznab Indexer + qBittorrent Client")
    print("=" * 60)
    print()

    STAGING_PATH.mkdir(parents=True, exist_ok=True)
    (STAGING_PATH / "radarr").mkdir(exist_ok=True)
    (STAGING_PATH / "tv-sonarr").mkdir(exist_ok=True)
    (STAGING_PATH / "sonarr").mkdir(exist_ok=True)

    base = _get_base_url()
    print(f"[init] Server:  {LISTEN_HOST}:{LISTEN_PORT}")
    print(f"[init] Base URL: {base}")
    print(f"[init] Mount:   {MOUNT_PATH}")
    print(f"[init] Staging: {STAGING_PATH}")
    print(f"[init] API key: {DARKIARR_API_KEY}")
    print()
    print("[init] Radarr/Sonarr configuration:")
    print(f"  Torznab Indexer URL:       {base}/torznab/api")
    print(f"  Torznab API Key:           {DARKIARR_API_KEY}")
    print(f"  qBittorrent Host:          {LISTEN_HOST}")
    print(f"  qBittorrent Port:          {LISTEN_PORT}")
    print(f"  qBittorrent Category:      radarr / tv-sonarr")
    print()

    print("[init] Starting browser session in background...")
    threading.Thread(target=init_browser_background, daemon=True).start()

    class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    server = ThreadedServer((LISTEN_HOST, LISTEN_PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[shutdown] Stopping...")
    finally:
        browser.stop()
        server.server_close()


if __name__ == "__main__":
    main()
