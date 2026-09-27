"""
csfloat_pro_monitor.py

A feature-rich CSFloat monitor with:
- GUI (Tkinter) + log panel
- Discord notifications (discord.py)
- Selenium login (manual Steam/CSFloat), cookie + header harvesting
- Newest listings scraping (HTML -> BeautifulSoup)
- Filters: price range, float range, include/exclude keywords, min discount vs Skinport (optional)
- Duplicate suppression (seen items persisted to JSON)
- CSV export for listings/deals
- Config load/save, .env support (optional)
- Robust logging, exponential backoff on errors
- Graceful start/stop controls

"""

# =========================== Imports ===========================
import os
import re
import sys
import csv
import json
import time
import math
import atexit
import queue
import signal
import asyncio
import logging
import threading
import dataclasses
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple, Set

# GUI
import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox

# Web / Async
import aiohttp
from bs4 import BeautifulSoup

# Discord
import discord

# Selenium
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

# Optional .env (safe if missing)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


# ======================= Constants / Defaults =======================
CSFLOAT_NEWEST_URL = "https://csfloat.com/search?sort_by=most_recent"
DEFAULT_REFRESH_SEC = 15
SEEN_DB_FILE = "seen_items.json"
CONFIG_FILE = "config.json"
LOG_FILE = "csfloat_monitor.log"
CSV_EXPORT_DEFAULT = "csfloat_deals.csv"

# ======================= Data Models / Config =======================
@dataclass
class Filters:
    min_price: float = 0.0
    max_price: float = 999999.0
    min_float: float = 0.0
    max_float: float = 1.0
    include_keywords: List[str] = field(default_factory=list)   # ["AK-47", "M4A1-S"]
    exclude_keywords: List[str] = field(default_factory=list)   # ["Souvenir", "StatTrak"]
    min_market_discount: float = 0.0   # If you wire Skinport later
    dedupe_window: int = 5000          # Keep up to N seen entries

@dataclass
class AppConfig:
    discord_token: str = os.getenv("DISCORD_TOKEN", "")
    discord_channel_id: int = int(os.getenv("DISCORD_CHANNEL_ID", "0"))
    refresh_interval_sec: int = int(os.getenv("REFRESH_INTERVAL_SEC", str(DEFAULT_REFRESH_SEC)))
    headless: bool = False
    use_discord: bool = True
    send_embeds: bool = True
    log_level: str = "INFO"

@dataclass
class Listing:
    name: str
    price_str: str
    float_str: str
    link: Optional[str]
    seen_key: str

# ======================= Logging Setup =======================
class TkinterLogHandler(logging.Handler):
    """Custom log handler that writes to a Tkinter ScrolledText widget."""
    def __init__(self, text_widget_getter):
        super().__init__()
        self.text_widget_getter = text_widget_getter

    def emit(self, record):
        try:
            msg = self.format(record)
            text_widget = self.text_widget_getter()
            if text_widget and text_widget.winfo_exists():
                text_widget.after(0, lambda: (text_widget.insert(tk.END, msg + "\n"), text_widget.see(tk.END)))
        except Exception:
            pass

def setup_logging(gui_text_getter, level_name="INFO"):
    logger = logging.getLogger("csfloat")
    logger.setLevel(getattr(logging, level_name.upper(), logging.INFO))

    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%H:%M:%S")

    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    th = logging.StreamHandler(sys.stdout)
    th.setFormatter(fmt)
    logger.addHandler(th)

    gh = TkinterLogHandler(gui_text_getter)
    gh.setFormatter(fmt)
    logger.addHandler(gh)

    return logger

# ======================= Persistence =======================
class SeenDB:
    def __init__(self, path: str, max_size: int = 5000):
        self.path = path
        self.max_size = max_size
        self._seen: List[str] = []
        self._load()

    def _load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    self._seen = data[-self.max_size:]
        except Exception:
            self._seen = []

    def add(self, key: str):
        if key in self._seen:
            return
        self._seen.append(key)
        if len(self._seen) > self.max_size:
            self._seen = self._seen[-self.max_size:]
        self._save()

    def contains(self, key: str) -> bool:
        return key in self._seen

    def _save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._seen[-self.max_size:], f, ensure_ascii=False, indent=2)
        except Exception:
            pass

# ======================= Selenium Login Manager =======================
class SeleniumManager:
    def __init__(self, logger: logging.Logger, headless: bool = False):
        self.logger = logger
        self.headless = headless
        self.driver = None
        self.cookies: Dict[str, str] = {}
        self.headers: Dict[str, str] = {}

    def launch_and_login(self):
        self.logger.info("Launching Chrome for CSFloat login...")
        options = webdriver.ChromeOptions()
        if self.headless:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")

        self.driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
        self.driver.get("https://csfloat.com/login")
        self.logger.info("Browser opened. Please login (Steam SSO) in the window, then return here.")

    def confirm_and_capture(self):
        if not self.driver:
            self.logger.error("Driver not launched. Click 'Open Login' first.")
            return

        self.logger.info("Capturing cookies and session details...")
        # Cookies
        try:
            raw = self.driver.get_cookies()
            self.cookies = {c["name"]: c["value"] for c in raw}
            # document.cookie fallback
            try:
                doc_cookie = self.driver.execute_script("return document.cookie || ''")
                if doc_cookie:
                    for part in doc_cookie.split("; "):
                        if "=" in part:
                            k, v = part.split("=", 1)
                            self.cookies.setdefault(k, v)
            except Exception:
                pass
            self.logger.info(f"Collected {len(self.cookies)} cookies.")
        except Exception as e:
            self.logger.error(f"Cookie read error: {e}")

        # Headers (token, UA)
        self.headers = {}
        try:
            token = None
            for key in ("token", "authToken", "accessToken", "jwt", "authorization"):
                try:
                    v = self.driver.execute_script(f"return window.localStorage.getItem('{key}');")
                except Exception:
                    v = None
                if v:
                    token = v
                    self.logger.info(f"Found localStorage key '{key}'.")
                    break
            if token:
                if token.lower().startswith("bearer "):
                    self.headers["Authorization"] = token
                else:
                    self.headers["Authorization"] = f"Bearer {token}"
        except Exception as e:
            self.logger.warning(f"Token read failed: {e}")

        try:
            ua = self.driver.execute_script("return navigator.userAgent;")
            if ua:
                self.headers["User-Agent"] = ua
                self.logger.info(f"Captured UA: {ua}")
        except Exception:
            pass

        # Useful default headers
        self.headers.setdefault("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
        self.headers.setdefault("Referer", "https://csfloat.com/")

        self.logger.info("Session captured. You can keep the browser open or close it.")

    def close(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception as e:
                self.logger.warning(f"Driver close error: {e}")
        self.driver = None
        self.logger.info("Browser closed.")

# ======================= Scraper =======================
class CSFloatScraper:
    def __init__(self, logger: logging.Logger, cookies: Dict[str, str], headers: Dict[str, str]):
        self.logger = logger
        self.cookies = cookies
        self.headers = headers

    async def fetch_newest(self) -> List[Listing]:
        """Scrape newest listings page and parse cards."""
        listings: List[Listing] = []
        try:
            async with aiohttp.ClientSession(cookies=self.cookies) as session:
                async with session.get(CSFLOAT_NEWEST_URL, headers=self.headers) as resp:
                    html = await resp.text()
                    self.logger.debug(f"HTML status: {resp.status}")
                    if resp.status != 200:
                        self.logger.warning(f"CSFloat HTML error {resp.status} — {html[:200]}")
                        return []

            soup = BeautifulSoup(html, "html.parser")

            # Try multiple selectors since CSFloat may change class names
            card_selectors = [
                ".Listing--preview",
                ".listing-card",
                ".item-card",
                ".card"
            ]
            cards = []
            for sel in card_selectors:
                found = soup.select(sel)
                if found:
                    cards = found
                    break

            for card in cards:
                name_tag = card.select_one(".ItemPreview--name, .item-name, .name, .title")
                price_tag = card.select_one(".Listing--price, .price, .Price, .amount")
                float_tag = card.select_one(".FloatValue, .float, .ItemPreview--float")
                link_tag = card.select_one("a")

                name = (name_tag.text.strip() if name_tag else "Unknown")
                price_str = (price_tag.text.strip() if price_tag else "Unknown")
                float_str = (float_tag.text.strip() if float_tag else "N/A")
                link = None
                if link_tag and link_tag.has_attr("href"):
                    href = link_tag["href"]
                    link = href if href.startswith("http") else f"https://csfloat.com{href}"

                seen_key = f"{name}|{price_str}|{link or ''}"
                listings.append(Listing(name=name, price_str=price_str, float_str=float_str, link=link, seen_key=seen_key))

            self.logger.info(f"Scraped {len(listings)} newest listings.")
            return listings

        except Exception as e:
            self.logger.error(f"Scraping error: {e}")
            return []

# ======================= Filter / Utility =======================
def parse_money(m: str) -> Optional[float]:
    """Parse price strings like '$12.34', '€45,67', '12.34 USD' into float USD-like number."""
    if not m or m in ("Unknown", "N/A"):
        return None
    # Remove currency symbols, commas
    x = re.sub(r"[^\d.,]", "", m)
    # Convert comma decimals to dot if needed
    if x.count(",") == 1 and x.count(".") == 0:
        x = x.replace(",", ".")
    x = x.replace(",", "")  # remove thousands
    try:
        return float(x)
    except Exception:
        return None

def parse_float_value(s: str) -> Optional[float]:
    """Parse float strings like '0.1234'."""
    if not s or s == "N/A":
        return None
    # Extract first float
    m = re.search(r"\d+\.\d+", s)
    if m:
        try:
            return float(m.group(0))
        except Exception:
            return None
    return None

def listing_passes_filters(lst: Listing, flt: Filters, logger: logging.Logger) -> bool:
    price = parse_money(lst.price_str)
    fval = parse_float_value(lst.float_str)

    if price is None or not (flt.min_price <= price <= flt.max_price):
        return False
    if fval is not None and not (flt.min_float <= fval <= flt.max_float):
        return False

    name_low = lst.name.lower()

    if flt.include_keywords:
        if not any(k.lower() in name_low for k in flt.include_keywords):
            return False
    if flt.exclude_keywords:
        if any(k.lower() in name_low for k in flt.exclude_keywords):
            return False

    return True

# ======================= Discord Notifier =======================
class DiscordNotifier:
    def __init__(self, cfg: AppConfig, logger: logging.Logger):
        self.cfg = cfg
        self.logger = logger
        intents = discord.Intents.default()
        intents.message_content = True
        self.client = discord.Client(intents=intents)
        self.channel = None

        @self.client.event
        async def on_ready():
            self.logger.info(f"Discord logged in as {self.client.user}")
            ch = self.client.get_channel(self.cfg.discord_channel_id)
            if not ch:
                self.logger.warning("Discord channel not found; notifications disabled.")
            self.channel = ch

    def run_in_background(self):
        if not self.cfg.use_discord:
            self.logger.info("Discord disabled by config.")
            return
        if self.cfg.discord_token.startswith("REPLACE") or len(self.cfg.discord_token.strip()) < 20:
            self.logger.warning("Invalid Discord token; notifications disabled.")
            return

        t = threading.Thread(target=lambda: self.client.run(self.cfg.discord_token), daemon=True)
        t.start()

    async def notify_listing(self, lst: Listing):
        if not self.channel:
            return
        try:
            if self.cfg.send_embeds:
                embed = discord.Embed(title=lst.name, url=lst.link or discord.Embed.Empty, color=discord.Color.green())
                embed.add_field(name="Price", value=lst.price_str, inline=True)
                embed.add_field(name="Float", value=lst.float_str, inline=True)
                if lst.link:
                    embed.set_footer(text="CSFloat newest")
                await self.channel.send(embed=embed)
            else:
                msg = f"**{lst.name}**\nPrice: {lst.price_str}\nFloat: {lst.float_str}\n{lst.link or ''}"
                await self.channel.send(msg)
        except Exception as e:
            self.logger.warning(f"Discord send failed: {e}")

# ======================= CSV Exporter =======================
class CSVExporter:
    def __init__(self, logger: logging.Logger, path: str = CSV_EXPORT_DEFAULT):
        self.logger = logger
        self.path = path
        # ensure header
        if not os.path.exists(self.path):
            try:
                with open(self.path, "w", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    w.writerow(["datetime", "name", "price", "float", "link"])
            except Exception as e:
                self.logger.warning(f"CSV init failed: {e}")

    def append(self, lst: Listing):
        try:
            with open(self.path, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow([datetime.utcnow().isoformat(), lst.name, lst.price_str, lst.float_str, lst.link or ""])
        except Exception as e:
            self.logger.warning(f"CSV append failed: {e}")

# ======================= Main Monitor =======================
class Monitor:
    def __init__(self, logger: logging.Logger, cfg: AppConfig, flt: Filters, seen_db: SeenDB,
                 selenium_mgr: SeleniumManager, notifier: DiscordNotifier, exporter: CSVExporter):
        self.logger = logger
        self.cfg = cfg
        self.flt = flt
        self.seen_db = seen_db
        self.selenium_mgr = selenium_mgr
        self.notifier = notifier
        self.exporter = exporter
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    def build_scraper(self) -> CSFloatScraper:
        return CSFloatScraper(self.logger, cookies=self.selenium_mgr.cookies, headers=self.selenium_mgr.headers)

    async def start(self):
        if self._task and not self._task.done():
            self.logger.info("Monitor already running.")
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run_loop())
        self.logger.info("Monitor started.")

    async def stop(self):
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except Exception:
                pass
        self.logger.info("Monitor stopped.")

    async def _run_loop(self):
        backoff = 1
        while not self._stop.is_set():
            try:
                scraper = self.build_scraper()
                listings = await scraper.fetch_newest()
                new_count = 0
                for lst in listings:
                    if not listing_passes_filters(lst, self.flt, self.logger):
                        continue
                    if self.seen_db.contains(lst.seen_key):
                        continue
                    # mark seen
                    self.seen_db.add(lst.seen_key)
                    new_count += 1
                    # log & export
                    self.logger.info(f"NEW: {lst.name} | {lst.price_str} | {lst.float_str} | {lst.link}")
                    self.exporter.append(lst)
                    # notify
                    await self.notifier.notify_listing(lst)

                if new_count == 0:
                    self.logger.info("No new matching items this cycle.")
                else:
                    self.logger.info(f"Found {new_count} new matching items.")

                backoff = 1  # reset backoff after success
                # sleep until next iteration or stop
                for _ in range(self.cfg.refresh_interval_sec):
                    if self._stop.is_set():
                        break
                    await asyncio.sleep(1)

            except Exception as e:
                self.logger.error(f"Monitor loop error: {e}")
                # exponential backoff
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

# ======================= GUI =======================
class GUI:
    def __init__(self, cfg: AppConfig, flt: Filters):
        self.cfg = cfg
        self.flt = flt

        # Tk
        self.root = tk.Tk()
        self.root.title("CSFloat Pro Monitor")
        self.root.geometry("920x720")

        # Widgets
        self.txt_log = None
        self.var_token = tk.StringVar(value=self.cfg.discord_token)
        self.var_channel = tk.StringVar(value=str(self.cfg.discord_channel_id))
        self.var_refresh = tk.StringVar(value=str(self.cfg.refresh_interval_sec))
        self.var_headless = tk.BooleanVar(value=self.cfg.headless)
        self.var_use_discord = tk.BooleanVar(value=self.cfg.use_discord)
        self.var_send_embeds = tk.BooleanVar(value=self.cfg.send_embeds)
        self.var_log_level = tk.StringVar(value=self.cfg.log_level)

        self.var_min_price = tk.StringVar(value=str(self.flt.min_price))
        self.var_max_price = tk.StringVar(value=str(self.flt.max_price))
        self.var_min_float = tk.StringVar(value=str(self.flt.min_float))
        self.var_max_float = tk.StringVar(value=str(self.flt.max_float))
        self.var_include = tk.StringVar(value=", ".join(self.flt.include_keywords))
        self.var_exclude = tk.StringVar(value=", ".join(self.flt.exclude_keywords))

        self._build_layout()

    def _build_layout(self):
        # Top config frame
        frm_cfg = ttk.LabelFrame(self.root, text="Configuration", padding=8)
        frm_cfg.pack(fill="x", padx=8, pady=6)

        row1 = ttk.Frame(frm_cfg)
        row1.pack(fill="x")
        ttk.Label(row1, text="Discord Token").grid(row=0, column=0, sticky="w")
        ttk.Entry(row1, textvariable=self.var_token, width=60).grid(row=0, column=1, sticky="we", padx=6)
        ttk.Label(row1, text="Channel ID").grid(row=0, column=2, sticky="w", padx=(12,0))
        ttk.Entry(row1, textvariable=self.var_channel, width=18).grid(row=0, column=3, sticky="w")

        row2 = ttk.Frame(frm_cfg)
        row2.pack(fill="x", pady=(6,0))
        ttk.Label(row2, text="Refresh (sec)").grid(row=0, column=0, sticky="w")
        ttk.Entry(row2, textvariable=self.var_refresh, width=10).grid(row=0, column=1, sticky="w", padx=(0,12))
        ttk.Checkbutton(row2, text="Headless Login", variable=self.var_headless).grid(row=0, column=2, sticky="w", padx=(0,12))
        ttk.Checkbutton(row2, text="Use Discord", variable=self.var_use_discord).grid(row=0, column=3, sticky="w", padx=(0,12))
        ttk.Checkbutton(row2, text="Send Embeds", variable=self.var_send_embeds).grid(row=0, column=4, sticky="w", padx=(0,12))
        ttk.Label(row2, text="Log Level").grid(row=0, column=5, sticky="w")
        ttk.Combobox(row2, textvariable=self.var_log_level, values=["DEBUG","INFO","WARNING","ERROR"], width=10).grid(row=0, column=6, sticky="w")

        # Filters
        frm_flt = ttk.LabelFrame(self.root, text="Filters", padding=8)
        frm_flt.pack(fill="x", padx=8, pady=6)

        r1 = ttk.Frame(frm_flt); r1.pack(fill="x")
        ttk.Label(r1, text="Min Price").grid(row=0, column=0, sticky="w")
        ttk.Entry(r1, textvariable=self.var_min_price, width=10).grid(row=0, column=1, padx=6)
        ttk.Label(r1, text="Max Price").grid(row=0, column=2, sticky="w")
        ttk.Entry(r1, textvariable=self.var_max_price, width=10).grid(row=0, column=3, padx=6)

        r2 = ttk.Frame(frm_flt); r2.pack(fill="x", pady=(6,0))
        ttk.Label(r2, text="Min Float").grid(row=0, column=0, sticky="w")
        ttk.Entry(r2, textvariable=self.var_min_float, width=10).grid(row=0, column=1, padx=6)
        ttk.Label(r2, text="Max Float").grid(row=0, column=2, sticky="w")
        ttk.Entry(r2, textvariable=self.var_max_float, width=10).grid(row=0, column=3, padx=6)

        r3 = ttk.Frame(frm_flt); r3.pack(fill="x", pady=(6,0))
        ttk.Label(r3, text="Include Keywords (comma-separated)").grid(row=0, column=0, sticky="w")
        ttk.Entry(r3, textvariable=self.var_include, width=60).grid(row=0, column=1, padx=6)
        ttk.Label(r3, text="Exclude Keywords (comma-separated)").grid(row=0, column=2, sticky="w")
        ttk.Entry(r3, textvariable=self.var_exclude, width=40).grid(row=0, column=3, padx=6)

        # Controls
        frm_ctrl = ttk.Frame(self.root, padding=6)
        frm_ctrl.pack(fill="x")
        ttk.Button(frm_ctrl, text="Open Login", command=self.on_open_login).pack(side="left", padx=6)
        ttk.Button(frm_ctrl, text="Confirm Session", command=self.on_confirm_session).pack(side="left", padx=6)
        ttk.Button(frm_ctrl, text="Close Browser", command=self.on_close_browser).pack(side="left", padx=6)

        ttk.Button(frm_ctrl, text="Start Monitor", command=self.on_start_monitor, style="Accent.TButton").pack(side="left", padx=12)
        ttk.Button(frm_ctrl, text="Stop Monitor", command=self.on_stop_monitor).pack(side="left", padx=6)

        ttk.Button(frm_ctrl, text="Save Config", command=self.on_save_config).pack(side="right", padx=6)
        ttk.Button(frm_ctrl, text="Load Config", command=self.on_load_config).pack(side="right", padx=6)
        ttk.Button(frm_ctrl, text="Export CSV…", command=self.on_export_csv).pack(side="right", padx=6)

        # Log
        frm_log = ttk.LabelFrame(self.root, text="Log", padding=6)
        frm_log.pack(fill="both", expand=True, padx=8, pady=6)
        self.txt_log = scrolledtext.ScrolledText(frm_log, wrap=tk.WORD)
        self.txt_log.pack(fill="both", expand=True)

        # Styles
        try:
            style = ttk.Style(self.root)
            if "azure" in style.theme_names():
                style.theme_use("azure")
            style.configure("Accent.TButton", foreground="black")
        except Exception:
            pass

    # A getter for logger handler
    def get_log_widget(self):
        return self.txt_log

    # Button callbacks
    def on_open_login(self):
        self.apply_to_config()
        APP.selenium_mgr.headless = self.var_headless.get()
        threading.Thread(target=APP.selenium_mgr.launch_and_login, daemon=True).start()

    def on_confirm_session(self):
        threading.Thread(target=APP.selenium_mgr.confirm_and_capture, daemon=True).start()

    def on_close_browser(self):
        threading.Thread(target=APP.selenium_mgr.close, daemon=True).start()

    def on_start_monitor(self):
        self.apply_to_config()
        asyncio.run_coroutine_threadsafe(APP.monitor.start(), APP.loop)

    def on_stop_monitor(self):
        asyncio.run_coroutine_threadsafe(APP.monitor.stop(), APP.loop)

    def on_save_config(self):
        self.apply_to_config()
        cfg = dataclasses.asdict(APP.cfg)
        flt = dataclasses.asdict(APP.flt)
        data = {"config": cfg, "filters": flt}
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            messagebox.showinfo("Saved", f"Configuration saved to {CONFIG_FILE}")
        except Exception as e:
            messagebox.showerror("Error", f"Save failed: {e}")

    def on_load_config(self):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            cfg = data.get("config", {})
            flt = data.get("filters", {})
            for k, v in cfg.items():
                if hasattr(APP.cfg, k):
                    setattr(APP.cfg, k, v)
            for k, v in flt.items():
                if hasattr(APP.flt, k):
                    setattr(APP.flt, k, v)
            # Reflect to GUI
            self.var_token.set(APP.cfg.discord_token)
            self.var_channel.set(str(APP.cfg.discord_channel_id))
            self.var_refresh.set(str(APP.cfg.refresh_interval_sec))
            self.var_headless.set(APP.cfg.headless)
            self.var_use_discord.set(APP.cfg.use_discord)
            self.var_send_embeds.set(APP.cfg.send_embeds)
            self.var_log_level.set(APP.cfg.log_level)

            self.var_min_price.set(str(APP.flt.min_price))
            self.var_max_price.set(str(APP.flt.max_price))
            self.var_min_float.set(str(APP.flt.min_float))
            self.var_max_float.set(str(APP.flt.max_float))
            self.var_include.set(", ".join(APP.flt.include_keywords))
            self.var_exclude.set(", ".join(APP.flt.exclude_keywords))

            # update logger level if changed
            APP.logger.setLevel(getattr(logging, APP.cfg.log_level.upper(), logging.INFO))

            messagebox.showinfo("Loaded", f"Configuration loaded from {CONFIG_FILE}")
        except Exception as e:
            messagebox.showerror("Error", f"Load failed: {e}")

    def on_export_csv(self):
        path = filedialog.asksaveasfilename(defaultextension=".csv", initialfile=CSV_EXPORT_DEFAULT)
        if not path:
            return
        APP.exporter.path = path
        messagebox.showinfo("CSV", f"Export file set to:\n{path}")

    def apply_to_config(self):
        # sync GUI -> config
        APP.cfg.discord_token = self.var_token.get().strip()
        try:
            APP.cfg.discord_channel_id = int(self.var_channel.get().strip() or "0")
        except Exception:
            APP.cfg.discord_channel_id = 0
        try:
            APP.cfg.refresh_interval_sec = max(5, int(self.var_refresh.get().strip() or str(DEFAULT_REFRESH_SEC)))
        except Exception:
            APP.cfg.refresh_interval_sec = DEFAULT_REFRESH_SEC
        APP.cfg.headless = self.var_headless.get()
        APP.cfg.use_discord = self.var_use_discord.get()
        APP.cfg.send_embeds = self.var_send_embeds.get()
        APP.cfg.log_level = self.var_log_level.get()

        # update logger level at runtime
        APP.logger.setLevel(getattr(logging, APP.cfg.log_level.upper(), logging.INFO))

        # Filters
        try:
            APP.flt.min_price = float(self.var_min_price.get())
            APP.flt.max_price = float(self.var_max_price.get())
            APP.flt.min_float = float(self.var_min_float.get())
            APP.flt.max_float = float(self.var_max_float.get())
        except Exception:
            pass

        inc = [x.strip() for x in self.var_include.get().split(",") if x.strip()]
        exc = [x.strip() for x in self.var_exclude.get().split(",") if x.strip()]
        APP.flt.include_keywords = inc
        APP.flt.exclude_keywords = exc


# ======================= Application Orchestrator =======================
class Application:
    def __init__(self):
        # default config/filters
        self.cfg = AppConfig()
        self.flt = Filters()

        # placeholder GUI to feed logger (created after logger)
        self._gui_ref: Optional[GUI] = None

        # setup logger (with placeholder getter)
        self.logger = setup_logging(self._get_gui_text, level_name=self.cfg.log_level)

        # components
        self.selenium_mgr = SeleniumManager(self.logger, headless=self.cfg.headless)
        self.notifier = DiscordNotifier(self.cfg, self.logger)
        self.exporter = CSVExporter(self.logger)
        self.seen_db = SeenDB(SEEN_DB_FILE, max_size=self.flt.dedupe_window)
        self.monitor = Monitor(self.logger, self.cfg, self.flt, self.seen_db,
                               self.selenium_mgr, self.notifier, self.exporter)

        # event loop in background thread
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop_runner, daemon=True).start()

        # start discord (if enabled)
        self.notifier.run_in_background()

        # GUI
        self.gui = GUI(self.cfg, self.flt)
        self._gui_ref = self.gui  # now logger can write to GUI

        # OS signals
        try:
            signal.signal(signal.SIGINT, self._on_signal)
            signal.signal(signal.SIGTERM, self._on_signal)
        except Exception:
            pass
        atexit.register(self._cleanup)

    def _get_gui_text(self):
        return self._gui_ref.txt_log if self._gui_ref else None

    def _loop_runner(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _on_signal(self, *args):
        self.logger.info("Shutting down (signal)...")
        self._cleanup()
        os._exit(0)

    def _cleanup(self):
        try:
            self.selenium_mgr.close()
        except Exception:
            pass
        try:
            if self.loop.is_running():
                self.loop.call_soon_threadsafe(self.loop.stop)
        except Exception:
            pass

    def run(self):
        self.gui.root.mainloop()


# ======================= Entry =======================
if __name__ == "__main__":
    APP = Application()
    APP.run()
