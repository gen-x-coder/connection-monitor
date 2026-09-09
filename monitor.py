#!/usr/bin/env python3
"""Configurable connection monitor with Tkinter GUI and optional RouterOS logs."""

from __future__ import annotations

import csv, json, os, queue, re, subprocess, sys, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

try:
    import paramiko
except ImportError:
    paramiko = None
try:
    import keyring
except ImportError:
    keyring = None

APP_NAME = "Connection Monitor"
CONFIG_FILE = Path(__file__).resolve().with_name("connection_monitor_config.json")
ICON_FILE = Path(__file__).resolve().with_name("monitor_icon.ico")
HOST_KEYS_FILE = Path(__file__).resolve().with_name("routeros_known_hosts")
KEYRING_SERVICE = "ConnectionMonitor-RouterOS"
LOG_PREFIX = "PYCONMON"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
DEFAULTS = {
    "targets": [
        {"name": "Router", "host": "192.168.178.1"},
        {"name": "Ziggo modem", "host": "192.168.88.1"},
        {"name": "Internet (Cloudflare)", "host": "1.1.1.1"},
    ],
    "interval_seconds": 2.0,
    "timeout_ms": 1500,
    "failures_before_down": 2,
    "log_folder": str(Path(__file__).resolve().with_name("network_monitor_logs")),
    "timeline_minutes": 360,
    "routeros": {
        "enabled": False,
        "host": "192.168.178.1",
        "port": 22,
        "username": "",
        "auto_snapshot": True,
        "save_password": True,
    },
}


def now():
    return datetime.now().astimezone()


def stamp():
    return now().isoformat(timespec="milliseconds")


def file_stamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def parse_time(value):
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def display_time(value):
    moment = parse_time(value)
    return moment.strftime("%H:%M:%S.") + f"{moment.microsecond//1000:03d}" if moment else ""


def ping_once(host, timeout_ms):
    start = time.perf_counter()
    cmd = (
        ["ping", "-n", "1", "-w", str(timeout_ms), host]
        if os.name == "nt"
        else ["ping", "-c", "1", "-W", str(max(1, (timeout_ms + 999) // 1000)), host]
    )
    try:
        done = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_ms / 1000 + 2,
            creationflags=NO_WINDOW,
        )
        output = (done.stdout or "") + (done.stderr or "")
        if done.returncode == 0:
            match = re.search(r"(?:time|tijd)[=<]\s*(\d+(?:[.,]\d+)?)\s*ms", output, re.I)
            return (
                True,
                (
                    float(match.group(1).replace(",", "."))
                    if match
                    else round((time.perf_counter() - start) * 1000, 1)
                ),
                "",
            )
        return (
            False,
            None,
            output.strip().splitlines()[-1] if output.strip() else f"ping exit {done.returncode}",
        )
    except Exception as exc:
        return False, None, str(exc)


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    if not CONFIG_FILE.exists():
        return cfg
    try:
        saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        cfg.update({k: v for k, v in saved.items() if k != "routeros"})
        if "timeline_minutes" not in saved:
            cfg["timeline_minutes"] = int(saved.get("timeline_hours", 6)) * 60
        cfg["routeros"].update(saved.get("routeros", {}))
        return cfg
    except Exception as exc:
        messagebox.showwarning(
            APP_NAME, f"Configuratie is beschadigd; standaardwaarden worden gebruikt.\n\n{exc}"
        )
        return cfg


def save_config(cfg):
    temporary = CONFIG_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(CONFIG_FILE)


@dataclass
class TargetState:
    name: str
    host: str
    online: bool | None = None
    down_since: str | None = None
    down_started: float | None = None
    failures: int = 0
    sent: int = 0
    received: int = 0
    outages: int = 0
    last_outage: str = ""


class Monitor:
    def __init__(self, cfg, messages):
        self.cfg, self.q = cfg, messages
        self.folder = Path(cfg["log_folder"])
        self.folder.mkdir(parents=True, exist_ok=True)
        self.samples = self.folder / "connection_samples.csv"
        self.events = self.folder / "connection_events.csv"
        self.states = {t["host"]: TargetState(t["name"], t["host"]) for t in cfg["targets"]}
        self.stop_event = threading.Event()
        self.thread = None
        self._headers()
        self._history()

    def _headers(self):
        if not self.samples.exists():
            with self.samples.open("w", newline="", encoding="utf-8-sig") as f:
                csv.writer(f).writerow(
                    ["timestamp", "name", "host", "online", "latency_ms", "error"]
                )
        if not self.events.exists():
            with self.events.open("w", newline="", encoding="utf-8-sig") as f:
                csv.writer(f).writerow(
                    [
                        "timestamp",
                        "event",
                        "name",
                        "host",
                        "outage_started",
                        "duration_seconds",
                        "detail",
                    ]
                )

    def _history(self):
        open_outages = {}
        try:
            with self.events.open(encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    state = self.states.get(row["host"])
                    if state and row["event"] in ("DOWN", "INITIAL_DOWN"):
                        started = row["outage_started"] or row["timestamp"]
                        state.outages += 1
                        state.last_outage = started
                        open_outages[row["host"]] = started
                    elif state and row["event"] == "UP":
                        open_outages.pop(row["host"], None)
        except (OSError, KeyError):
            pass
        for host, started in open_outages.items():
            state = self.states[host]
            started_at = parse_time(started)
            elapsed = max(0.0, (now() - started_at).total_seconds()) if started_at else 0.0
            state.online = False
            state.down_since = started
            state.down_started = time.monotonic() - elapsed

    def start(self):
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def _write_event(self, kind, state, duration="", detail=""):
        row = [stamp(), kind, state.name, state.host, state.down_since or "", duration, detail]
        with self.events.open("a", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow(row)
        self.q.put(("event", row))

    def _run(self):
        self.q.put(("running", True))
        interval = float(self.cfg["interval_seconds"])
        timeout = int(self.cfg["timeout_ms"])
        limit = int(self.cfg["failures_before_down"])
        try:
            with ThreadPoolExecutor(
                max_workers=len(self.states), thread_name_prefix="ping"
            ) as executor:
                while not self.stop_event.is_set():
                    cycle = time.monotonic()
                    futures = {
                        executor.submit(ping_once, host, timeout): host for host in self.states
                    }
                    results = {}
                    for future in as_completed(futures):
                        host = futures[future]
                        ok, latency, error = future.result()
                        results[host] = (ok, latency, error, stamp())
                    self._process_results(results, limit)
                    self.stop_event.wait(max(0.1, interval - (time.monotonic() - cycle)))
        except Exception as exc:
            self.q.put(("error", f"Monitoring gestopt door een fout: {exc}"))
        finally:
            self.q.put(("running", False))

    def _process_results(self, results, limit):
        rows = []
        for host, state in self.states.items():
            ok, latency, error, measured_at = results[host]
            state.sent += 1
            state.received += int(ok)
            state.failures = 0 if ok else state.failures + 1
            rows.append(
                [
                    measured_at,
                    state.name,
                    host,
                    int(ok),
                    latency if latency is not None else "",
                    error,
                ]
            )
            if not ok and state.online is not False and state.failures >= limit:
                state.online = False
                state.down_since = measured_at
                state.down_started = time.monotonic()
                state.outages += 1
                state.last_outage = measured_at
                event = "DOWN" if state.sent > limit else "INITIAL_DOWN"
                self._write_event(event, state, detail=error)
                self.q.put(("outage", state.name))
            elif ok and state.online is False:
                duration = round(time.monotonic() - (state.down_started or time.monotonic()), 1)
                self._write_event("UP", state, duration, "connection restored")
                state.online = True
                state.down_since = None
                state.down_started = None
            elif state.online is None and ok:
                state.online = True
                self._write_event("INITIAL_UP", state)
            loss = 100 * (state.sent - state.received) / state.sent
            self.q.put(
                (
                    "status",
                    host,
                    ok,
                    latency,
                    loss,
                    state.down_since,
                    state.outages,
                    state.last_outage,
                )
            )
        with self.samples.open("a", newline="", encoding="utf-8-sig") as handle:
            csv.writer(handle).writerows(rows)


class RouterOS:
    def __init__(self, cfg, password):
        self.cfg, self.password = cfg, password

    def run(self, command, timeout=20):
        if paramiko is None:
            raise RuntimeError("Installeer requirements.txt opnieuw (Paramiko ontbreekt).")
        client = paramiko.SSHClient()
        if HOST_KEYS_FILE.exists():
            client.load_host_keys(str(HOST_KEYS_FILE))
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                self.cfg["host"],
                port=int(self.cfg["port"]),
                username=self.cfg["username"],
                password=self.password,
                timeout=timeout,
                banner_timeout=timeout,
                auth_timeout=timeout,
                look_for_keys=False,
                allow_agent=False,
            )
            client.save_host_keys(str(HOST_KEYS_FILE))
            _, out, err = client.exec_command(command, timeout=timeout)
            result = out.read().decode("utf-8", "replace")
            error = err.read().decode("utf-8", "replace").strip()
            if error:
                raise RuntimeError(error)
            return result
        finally:
            client.close()


class Settings(tk.Toplevel):
    def __init__(self, parent, cfg, first=False):
        super().__init__(parent)
        self.parent = parent
        self.cfg = json.loads(json.dumps(cfg))
        self.saved = False
        self.title("Eerste configuratie" if first else "Instellingen")
        width = min(920, max(680, self.winfo_screenwidth() - 80))
        height = min(860, max(580, self.winfo_screenheight() - 100))
        x = max(0, (self.winfo_screenwidth() - width) // 2)
        y = max(0, (self.winfo_screenheight() - height) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.minsize(680, 580)
        self.resizable(True, True)
        self.transient(parent)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._build()

    @staticmethod
    def field(parent, label, value, row):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
        entry = ttk.Entry(parent, width=28)
        entry.insert(0, str(value))
        entry.grid(row=row, column=1, sticky="w", pady=3)
        return entry

    def _build(self):
        container = ttk.Frame(self)
        container.pack(fill="both", expand=True)
        canvas = tk.Canvas(container, highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        area = ttk.Frame(canvas, padding=18)
        window = canvas.create_window((0, 0), window=area, anchor="nw")
        area.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        self.settings_canvas = canvas
        self.bind("<MouseWheel>", self._scroll)
        ttk.Label(area, text="Meetdoelen (maximaal 10)", font=("Segoe UI", 11, "bold")).pack(
            anchor="w"
        )
        ttk.Label(
            area, text="Lege regels worden genegeerd. Naam en adres zijn beide verplicht."
        ).pack(anchor="w", pady=(2, 8))
        grid = ttk.Frame(area)
        grid.pack(fill="x")
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)
        ttk.Label(grid, text="Naam", width=26).grid(row=0, column=0, sticky="w")
        ttk.Label(grid, text="IP-adres / hostnaam", width=32).grid(row=0, column=1, sticky="w")
        self.target_entries = []
        configured = self.cfg.get("targets", [])
        for i in range(10):
            name, host = ttk.Entry(grid, width=31), ttk.Entry(grid, width=38)
            name.grid(row=i + 1, column=0, padx=(0, 10), pady=2, sticky="ew")
            host.grid(row=i + 1, column=1, pady=2, sticky="ew")
            if i < len(configured):
                name.insert(0, configured[i]["name"])
                host.insert(0, configured[i]["host"])
            self.target_entries.append((name, host))
        general = ttk.LabelFrame(area, text="Metingen", padding=10)
        general.pack(fill="x", pady=12)
        general.columnconfigure(1, weight=1)
        self.interval = self.field(general, "Interval (seconden)", self.cfg["interval_seconds"], 0)
        self.timeout = self.field(general, "Ping-timeout (ms)", self.cfg["timeout_ms"], 1)
        self.failure_limit = self.field(
            general, "Mislukkingen vóór storing", self.cfg["failures_before_down"], 2
        )
        ttk.Label(general, text="Logmap").grid(row=3, column=0, sticky="w", pady=3)
        self.folder = ttk.Entry(general, width=57)
        self.folder.insert(0, self.cfg["log_folder"])
        self.folder.grid(row=3, column=1, pady=3, sticky="ew")
        ttk.Button(general, text="Kiezen…", command=self.choose_folder).grid(
            row=3, column=2, padx=5
        )
        rcfg = self.cfg["routeros"]
        router = ttk.LabelFrame(area, text="RouterOS (optioneel)", padding=10)
        router.pack(fill="x")
        router.columnconfigure(1, weight=1)
        self.renabled = tk.BooleanVar(value=rcfg["enabled"])
        ttk.Checkbutton(router, text="RouterOS-functies gebruiken", variable=self.renabled).grid(
            row=0, column=0, columnspan=4, sticky="w"
        )
        self.rhost = self.field(router, "Router", rcfg["host"], 1)
        self.rport = self.field(router, "SSH-poort", rcfg["port"], 2)
        self.ruser = self.field(router, "Gebruikersnaam", rcfg["username"], 3)
        ttk.Label(router, text="Wachtwoord").grid(row=4, column=0, sticky="w", pady=3)
        self.rpass = ttk.Entry(router, width=28, show="*")
        self.rpass.grid(row=4, column=1, sticky="w")
        if keyring and rcfg.get("save_password") and rcfg.get("username"):
            try:
                self.rpass.insert(0, keyring.get_password(KEYRING_SERVICE, rcfg["username"]) or "")
            except Exception:
                pass
        self.remember = tk.BooleanVar(value=rcfg.get("save_password", False))
        ttk.Checkbutton(router, text="Veilig onthouden in Windows", variable=self.remember).grid(
            row=4, column=2, columnspan=2, sticky="w"
        )
        self.auto = tk.BooleanVar(value=rcfg.get("auto_snapshot", True))
        ttk.Checkbutton(router, text="Logmomentopname bij storing", variable=self.auto).grid(
            row=5, column=0, columnspan=4, sticky="w", pady=(4, 0)
        )
        buttons = ttk.Frame(area)
        buttons.pack(fill="x", pady=(14, 0))
        ttk.Button(buttons, text="Annuleren", command=self.close).pack(side="right")
        ttk.Button(buttons, text="Opslaan", command=self.save).pack(side="right", padx=8)
        ttk.Label(area, text="").pack(pady=2)

    def close(self):
        self.destroy()

    def _scroll(self, event):
        self.settings_canvas.yview_scroll(int(-event.delta / 120), "units")

    def choose_folder(self):
        chosen = filedialog.askdirectory(initialdir=self.folder.get() or str(Path.cwd()))
        if chosen:
            self.folder.delete(0, "end")
            self.folder.insert(0, chosen)

    def save(self):
        targets = []
        for name_box, host_box in self.target_entries:
            name, host = name_box.get().strip(), host_box.get().strip()
            if name or host:
                if not name or not host:
                    messagebox.showerror(
                        APP_NAME, "Vul bij ieder doel naam én adres in.", parent=self
                    )
                    return
                targets.append({"name": name, "host": host})
        if not targets:
            messagebox.showerror(APP_NAME, "Voeg minimaal één doel toe.", parent=self)
            return
        if len({t["host"].lower() for t in targets}) != len(targets):
            messagebox.showerror(APP_NAME, "Ieder adres mag maar één keer voorkomen.", parent=self)
            return
        try:
            interval = float(self.interval.get())
            timeout = int(self.timeout.get())
            limit = int(self.failure_limit.get())
            port = int(self.rport.get())
            if interval < 0.5 or timeout < 100 or not 1 <= limit <= 10 or not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            messagebox.showerror(
                APP_NAME,
                "Controleer de getallen (interval ≥ 0,5; timeout ≥ 100; storingsgrens 1–10).",
                parent=self,
            )
            return
        self.cfg.update(
            {
                "targets": targets,
                "interval_seconds": interval,
                "timeout_ms": timeout,
                "failures_before_down": limit,
                "log_folder": self.folder.get().strip(),
            }
        )
        self.cfg["routeros"] = {
            "enabled": self.renabled.get(),
            "host": self.rhost.get().strip(),
            "port": port,
            "username": self.ruser.get().strip(),
            "auto_snapshot": self.auto.get(),
            "save_password": self.remember.get(),
        }
        if self.remember.get():
            if keyring is None:
                messagebox.showerror(
                    APP_NAME,
                    "Installeer requirements.txt opnieuw (keyring ontbreekt).",
                    parent=self,
                )
                return
            try:
                keyring.set_password(KEYRING_SERVICE, self.ruser.get().strip(), self.rpass.get())
            except Exception as exc:
                messagebox.showerror(APP_NAME, f"Wachtwoord opslaan mislukte:\n{exc}", parent=self)
                return
        self.parent.session_password = self.rpass.get()
        save_config(self.cfg)
        self.saved = True
        self.close()


class Timeline(ttk.Frame):
    OPTIONS = [("10 minuten", 10), ("60 minuten", 60)] + [
        (f"{hour} uur", hour * 60) for hour in range(2, 25)
    ]

    def __init__(self, parent, on_scale_change):
        super().__init__(parent)
        self.on_scale_change = on_scale_change
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Label(bar, text="Toon:").pack(side="left")
        self.scale_var = tk.StringVar(value="6 uur")
        self.scale = ttk.Combobox(
            bar,
            textvariable=self.scale_var,
            values=[label for label, _ in self.OPTIONS],
            state="readonly",
            width=14,
        )
        self.scale.pack(side="left", padx=(6, 18))
        self.scale.bind("<<ComboboxSelected>>", self.change_scale)
        ttk.Label(bar, text="Groen: bereikbaar     Rood: storing", foreground="#444").pack(
            side="left"
        )
        self.canvas = tk.Canvas(self, height=160, background="white", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _e: self.redraw())
        self.events_file = None
        self.targets = []
        self.minutes = 360
        self.cache_signature = None
        self.cached_intervals = {}
        self.after(30000, self.refresh)

    def set_data(self, path, targets, minutes):
        self.events_file, self.targets, self.minutes = path, targets, int(minutes)
        label = next((label for label, value in self.OPTIONS if value == self.minutes), "6 uur")
        self.scale_var.set(label)
        self.canvas.configure(height=max(155, 34 + 22 * len(targets)))
        self.redraw()

    def change_scale(self, _event=None):
        self.minutes = dict(self.OPTIONS)[self.scale_var.get()]
        self.on_scale_change(self.minutes)
        self.redraw()

    def intervals(self):
        result = {t["host"]: [] for t in self.targets}
        opened = {}
        if not self.events_file or not self.events_file.exists():
            return result
        signature = (
            self.events_file.stat().st_mtime_ns,
            self.events_file.stat().st_size,
            tuple(result),
        )
        if signature == self.cache_signature:
            return self.cached_intervals
        try:
            with self.events_file.open(encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    host = row.get("host", "")
                    event = row.get("event", "")
                    at = parse_time(row.get("timestamp", ""))
                    begin = parse_time(row.get("outage_started", "")) or at
                    if host not in result or not at:
                        continue
                    if event in ("DOWN", "INITIAL_DOWN"):
                        opened[host] = begin
                    elif event == "UP" and host in opened:
                        result[host].append((opened.pop(host), at))
            current = now()
            for host, begin in opened.items():
                result[host].append((begin, current))
        except OSError:
            pass
        self.cache_signature = signature
        self.cached_intervals = result
        return result

    def redraw(self):
        c = self.canvas
        c.delete("all")
        width = max(c.winfo_width(), 600)
        left, right, top = 155, 18, 22
        end = now()
        begin = end - timedelta(minutes=self.minutes)
        plot = width - left - right
        divisions = 5 if self.minutes == 10 else 6
        for i in range(divisions + 1):
            x = left + plot * i / divisions
            moment = begin + (end - begin) * i / divisions
            tick = moment.strftime("%H:%M:%S") if self.minutes == 10 else moment.strftime("%H:%M")
            c.create_line(x, top, x, top + len(self.targets) * 22, fill="#e5e5e5")
            c.create_text(x, top - 5, anchor="s", text=tick, fill="#555", font=("Segoe UI", 8))
        data = self.intervals()
        for index, target in enumerate(self.targets):
            y = top + index * 22 + 10
            c.create_text(8, y, anchor="w", text=target["name"][:23], fill="#222")
            c.create_line(left, y, left + plot, y, width=7, fill="#cfe8cf")
            for a, b in data[target["host"]]:
                a = max(a, begin)
                b = min(b, end)
                if b < a:
                    continue
                x1 = left + plot * (a - begin).total_seconds() / (end - begin).total_seconds()
                x2 = left + plot * (b - begin).total_seconds() / (end - begin).total_seconds()
                c.create_line(x1, y, max(x1 + 2, x2), y, width=9, fill="#d13438")

    def refresh(self):
        self.redraw()
        self.after(30000, self.refresh)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1120x850")
        self.minsize(900, 700)
        self.cfg = load_config()
        self.session_password = ""
        self.q = queue.Queue()
        self.monitor = None
        self._set_icon()
        self._style()
        self._build()
        self.after(150, self._drain)
        self.protocol("WM_DELETE_WINDOW", self.close)
        if CONFIG_FILE.exists():
            self.apply_config()
        else:
            self.after(100, lambda: self.open_settings(True))

    def _style(self):
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Action.TButton", font=("Segoe UI", 11, "bold"), padding=(22, 9))
        style.configure("Title.TLabel", font=("Segoe UI", 16, "bold"))

    def _set_icon(self):
        if ICON_FILE.exists():
            try:
                self.iconbitmap(str(ICON_FILE))
            except tk.TclError:
                pass

    def _build(self):
        root = ttk.Frame(self, padding=14)
        root.pack(fill="both", expand=True)
        header = ttk.Frame(root)
        header.pack(fill="x")
        ttk.Label(header, text="Connection Monitor", style="Title.TLabel").pack(side="left")
        self.state = ttk.Label(header, text="● Gestopt", foreground="#777")
        self.state.pack(side="left", padx=18)
        ttk.Button(header, text="Instellingen", command=self.open_settings).pack(side="right")
        ttk.Button(header, text="Logmap openen", command=self.open_folder).pack(
            side="right", padx=8
        )
        self.toggle = ttk.Button(
            header, text="Start", style="Action.TButton", command=self.toggle_monitor
        )
        self.toggle.pack(side="right")
        columns = ("target", "status", "latency", "loss", "outages", "last", "since")
        self.tree = ttk.Treeview(root, columns=columns, show="headings", height=10)
        for col, label, width in (
            ("target", "Doel", 235),
            ("status", "Status", 95),
            ("latency", "Latency", 85),
            ("loss", "Verlies sessie", 95),
            ("outages", "Storingen", 75),
            ("last", "Laatste storing", 115),
            ("since", "Storing sinds", 115),
        ):
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, anchor="w")
        self.tree.pack(fill="x", pady=(14, 10))
        chart = ttk.LabelFrame(root, text="Tijdlijn", padding=8)
        chart.pack(fill="x")
        self.timeline = Timeline(chart, self.set_timeline_scale)
        self.timeline.pack(fill="x")
        lower = ttk.Panedwindow(root, orient="horizontal")
        lower.pack(fill="both", expand=True, pady=(10, 0))
        router = ttk.LabelFrame(lower, text="RouterOS", padding=10)
        logbox = ttk.LabelFrame(lower, text="Gebeurtenissen", padding=6)
        lower.add(router, weight=1)
        lower.add(logbox, weight=2)
        self.router_status = ttk.Label(router, text="Uitgeschakeld", wraplength=290)
        self.router_status.pack(anchor="w", fill="x")
        for label, action in (
            ("Verbinding testen", "test"),
            ("Extra logging aan", "enable"),
            ("Extra logging uit", "disable"),
            ("Logs nu ophalen", "snapshot"),
        ):
            ttk.Button(router, text=label, command=lambda a=action: self.router_action(a)).pack(
                fill="x", pady=3
            )
        self.console = tk.Text(
            logbox, height=9, wrap="word", state="disabled", font=("Consolas", 9)
        )
        self.console.pack(fill="both", expand=True)

    def apply_config(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        for target in self.cfg["targets"]:
            self.tree.insert(
                "",
                "end",
                iid=target["host"],
                values=(
                    f'{target["name"]} — {target["host"]}',
                    "Nog niet getest",
                    "",
                    "",
                    "0",
                    "",
                    "",
                ),
            )
        folder = Path(self.cfg["log_folder"])
        self.timeline.set_data(
            folder / "connection_events.csv",
            self.cfg["targets"],
            int(self.cfg.get("timeline_minutes", 360)),
        )
        r = self.cfg["routeros"]
        self.router_status.config(
            text=(
                f'SSH: {r["username"]}@{r["host"]}:{r["port"]}'
                if r["enabled"]
                else "Uitgeschakeld in Instellingen"
            )
        )

    def set_timeline_scale(self, minutes):
        self.cfg["timeline_minutes"] = minutes
        save_config(self.cfg)

    def open_settings(self, first=False):
        if self.monitor and self.monitor.thread and self.monitor.thread.is_alive():
            messagebox.showinfo(APP_NAME, "Stop eerst de monitor.")
            return
        dialog = Settings(self, self.cfg, first)
        self.wait_window(dialog)
        if dialog.saved:
            self.cfg = dialog.cfg
            self.apply_config()
            self.log("Instellingen opgeslagen.")
        elif first and not CONFIG_FILE.exists():
            self.after(50, lambda: self.open_settings(True))

    def toggle_monitor(self):
        if self.monitor and self.monitor.thread and self.monitor.thread.is_alive():
            self.monitor.stop()
            self.toggle.config(state="disabled")
        else:
            if not CONFIG_FILE.exists():
                self.open_settings(True)
                return
            self.monitor = Monitor(self.cfg, self.q)
            self.monitor.start()
            self.toggle.config(text="Stop")

    def open_folder(self):
        folder = Path(self.cfg["log_folder"])
        folder.mkdir(parents=True, exist_ok=True)
        try:
            if os.name == "nt":
                os.startfile(folder)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))

    def log(self, text):
        self.console.configure(state="normal")
        self.console.insert("end", f"{stamp()}  {text}\n")
        self.console.see("end")
        self.console.configure(state="disabled")

    def password(self, allow_prompt=True):
        r = self.cfg["routeros"]
        if self.session_password:
            return self.session_password
        if keyring and r.get("save_password") and r.get("username"):
            try:
                return keyring.get_password(KEYRING_SERVICE, r["username"]) or ""
            except Exception:
                pass
        if allow_prompt:
            password = simpledialog.askstring(
                APP_NAME, "RouterOS-wachtwoord:", show="*", parent=self
            )
            if password is not None:
                self.session_password = password
            return password or ""
        return ""

    def router_action(self, action, quiet=False):
        r = self.cfg["routeros"]
        if not r["enabled"]:
            if not quiet:
                messagebox.showinfo(APP_NAME, "Schakel RouterOS eerst in bij Instellingen.")
            return
        password = self.password(allow_prompt=not quiet)
        if not password:
            if not quiet:
                self.log("RouterOS-actie geannuleerd: geen wachtwoord ingevoerd.")
            return

        def job():
            try:
                client = RouterOS(r, password)
                if action == "test":
                    msg = (
                        "SSH werkt.\n"
                        + client.run("/system resource print; /system clock print").strip()
                    )
                elif action == "enable":
                    client.run(
                        f'/system logging remove [find where prefix="{LOG_PREFIX}"]; /system logging add topics=interface action=memory prefix="{LOG_PREFIX}"; /system logging add topics=route action=memory prefix="{LOG_PREFIX}"; /system logging add topics=dhcp action=memory prefix="{LOG_PREFIX}"'
                    )
                    msg = "Extra logging staat aan."
                elif action == "disable":
                    client.run(f'/system logging remove [find where prefix="{LOG_PREFIX}"]')
                    msg = "Extra logregels van deze tool zijn verwijderd."
                else:
                    folder = Path(self.cfg["log_folder"])
                    folder.mkdir(parents=True, exist_ok=True)
                    output = client.run(
                        "/system clock print; /system resource print; /ip route print detail without-paging; /ip dhcp-client print detail without-paging; /interface ethernet print detail without-paging; /log print detail without-paging",
                        30,
                    )
                    path = folder / f"routeros_snapshot_{file_stamp()}.txt"
                    path.write_text(
                        f"Collected: {stamp()}\nRouter: {r['host']}\n\n{output}", encoding="utf-8"
                    )
                    msg = f"RouterOS-log opgeslagen: {path.name}"
                self.q.put(("router", msg))
            except Exception as exc:
                self.q.put(
                    (
                        "router",
                        ("Automatische momentopname mislukt" if quiet else "MikroTik-fout")
                        + f": {exc}",
                    )
                )

        threading.Thread(target=job, daemon=True).start()

    def _drain(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "running":
                    running = item[1]
                    self.toggle.config(text="Stop" if running else "Start", state="normal")
                    self.state.config(
                        text="● Actief" if running else "● Gestopt",
                        foreground="#107c10" if running else "#777",
                    )
                    self.log("Monitoring gestart." if running else "Monitoring gestopt.")
                elif kind == "status":
                    _, host, ok, latency, loss, since, count, last = item
                    values = list(self.tree.item(host, "values"))
                    values[1:] = [
                        "Online" if ok else "Geen antwoord",
                        f"{latency:.1f} ms" if latency is not None else "—",
                        f"{loss:.1f}%",
                        count,
                        display_time(last),
                        display_time(since),
                    ]
                    self.tree.item(host, values=values)
                elif kind == "event":
                    row = item[1]
                    self.log(
                        f"{row[1]}: {row[2]} ({row[3]})"
                        + (f", duur {row[5]} sec" if row[5] else "")
                    )
                    self.timeline.redraw()
                elif (
                    kind == "outage"
                    and self.cfg["routeros"].get("enabled")
                    and self.cfg["routeros"].get("auto_snapshot")
                ):
                    self.router_action("snapshot", True)
                elif kind == "router":
                    self.log(item[1])
                elif kind == "error":
                    self.log(item[1])
                    messagebox.showerror(APP_NAME, item[1])
        except queue.Empty:
            pass
        self.after(150, self._drain)

    def close(self):
        if self.monitor:
            self.monitor.stop()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
