"""ПРИП-Синхро: при запуске загружает действующие ПРИП (Белое, Баренцево и Печорское море) и готовит файлы для TimeZero.

TimeZero не имеет API для внешних программ, поэтому связь идёт через импорт GPX:
  prip_timezero.gpx      — все действующие ПРИП (для первого импорта)
  prip_timezero_new.gpx  — только новые с момента последнего «Изменения внесены в TimeZero»
Отменённые ПРИП приложение перечисляет по именам, чтобы их удалили в TimeZero вручную.

Автор: Смирнов В.В., ,
smirnov.ecology@yandex.ru
"""
import datetime as dt
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import prip_update as pu

APP = "ПРИП-Синхро"
VERSION = "1.2"
DATA = Path(os.environ.get("USERPROFILE", Path.home())) / "Documents" / "PRIP-Sync"
STATE = DATA / "state.json"
STALE_HOURS = 24


def load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state):
    DATA.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def find_timezero():
    roots = [os.environ.get(k) for k in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")]
    for root in filter(None, roots):
        for pattern in ("*TimeZero*/TimeZero*.exe", "*TimeZero*/*/TimeZero*.exe", "MaxSea*/*.exe"):
            for exe in Path(root).glob(pattern):
                if "unins" not in exe.name.lower():
                    return str(exe)
    return None


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP)
        self.geometry("980x640")
        self.minsize(760, 480)
        self.state_ = load_state()
        self.result = None       # данные последней синхронизации (или кэш)
        self.new_ids, self.gone_ids = [], []
        self._build()
        self.after(100, self.sync)

    # ---------- интерфейс ----------
    def _build(self):
        top = ttk.Frame(self, padding=(10, 8))
        top.pack(fill="x")
        self.status = tk.Label(top, text="Синхронизация…", anchor="w", font=("Segoe UI", 11, "bold"))
        self.status.pack(side="left", fill="x", expand=True)
        ttk.Button(top, text="⟳ Обновить", command=self.sync).pack(side="right")
        ttk.Button(top, text="О программе", command=self.about).pack(side="right", padx=6)

        self.changes = tk.Label(self, anchor="w", justify="left", padx=10, font=("Segoe UI", 10))
        self.changes.pack(fill="x")

        bar = ttk.Frame(self, padding=(10, 6))
        bar.pack(fill="x")
        ttk.Button(bar, text="Открыть карту", command=self.open_map).pack(side="left")
        ttk.Button(bar, text="Файлы для импорта в TimeZero", command=self.open_folder).pack(side="left", padx=6)
        self.ack_btn = ttk.Button(bar, command=self.acknowledge)
        self.ack_btn.pack(side="left")
        ttk.Button(bar, text="Запустить TimeZero", command=self.launch_tz).pack(side="right")
        self.auto_tz = tk.BooleanVar(value=self.state_.get("auto_launch", False))
        ttk.Checkbutton(bar, text="запускать после синхронизации", variable=self.auto_tz,
                        command=self._save_prefs).pack(side="right", padx=6)

        modes = ttk.Frame(self, padding=(10, 0, 10, 6))
        modes.pack(fill="x")
        ttk.Label(modes, text="Обновление TimeZero:").pack(side="left")
        self.mode = tk.StringVar(value=self.state_.get("mode", "full"))
        ttk.Radiobutton(modes, text="полная замена (удалить все ПРИП и загрузить заново)", value="full",
                        variable=self.mode, command=self._mode_changed).pack(side="left", padx=8)
        ttk.Radiobutton(modes, text="только изменения (добавить новые, удалить отменённые)", value="diff",
                        variable=self.mode, command=self._mode_changed).pack(side="left")

        footer = ttk.Frame(self, padding=(10, 0, 10, 6))
        footer.pack(side="bottom", fill="x")
        tk.Label(footer, text=f"Автор: {pu.AUTHOR}, {pu.AUTHOR_ORG}", fg="#666",
                 font=("Segoe UI", 9)).pack(side="left")
        mail = tk.Label(footer, text=pu.AUTHOR_EMAIL, fg="#1a5fb4", cursor="hand2",
                        font=("Segoe UI", 9, "underline"))
        mail.pack(side="left", padx=6)
        mail.bind("<Button-1>", lambda _: webbrowser.open(f"mailto:{pu.AUTHOR_EMAIL}"))

        pane = ttk.PanedWindow(self, orient="vertical")
        pane.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        frame = ttk.Frame(pane)
        cols = ("id", "title", "cancel", "obj")
        self.tree = ttk.Treeview(frame, columns=cols, show="headings", selectmode="browse")
        for c, w, t in zip(cols, (170, 520, 120, 70), ("ПРИП", "Карты / район", "Отмена (МСК)", "Объектов")):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, stretch=(c == "title"))
        self.tree.tag_configure("new", background="#fff3b0")
        self.tree.tag_configure("gone", foreground="#a0a0a0")
        sb = ttk.Scrollbar(frame, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._show_text)
        pane.add(frame, weight=3)
        self.text = tk.Text(pane, height=10, wrap="word", font=("Consolas", 10))
        pane.add(self.text, weight=2)

    def _save_prefs(self):
        self.state_["auto_launch"] = self.auto_tz.get()
        self.state_["mode"] = self.mode.get()
        save_state(self.state_)

    def _mode_changed(self):
        self._save_prefs()
        if self.result:
            self._diff_and_fill()

    @property
    def full(self):
        return self.mode.get() == "full"

    # ---------- синхронизация ----------
    def sync(self):
        if getattr(self, "_busy", False):
            return
        self._busy = True
        self.status.config(text="Синхронизация с mapm.ru…", fg="black")
        box = queue.Queue()
        threading.Thread(target=self._sync_worker, args=(box,), daemon=True).start()
        self._poll(box)

    @staticmethod
    def _sync_worker(box):
        # tkinter нельзя трогать из фонового потока — результат передаётся через очередь
        try:
            box.put((pu.run(DATA), None))
        except Exception as e:  # нет связи, сайт изменился и т.п. — показываем последние сохранённые данные
            box.put((None, e))

    def _poll(self, box):
        try:
            r, err = box.get_nowait()
        except queue.Empty:
            self.after(200, self._poll, box)
            return
        self._busy = False
        self._on_synced(r, err)

    def _on_synced(self, r, err):
        if r:
            self.result = r
            self.status.config(text=f"✓ Синхронизировано {fmt(r['updated'])} МСК · действующих ПРИП: "
                                    f"{len(r['notices'])}", fg="#1b7f3b")
        else:
            cache = self._load_cache()
            if not cache:
                self.status.config(text=f"✗ Нет связи и нет сохранённых данных: {err}", fg="#b00020")
                return
            self.result = cache
            age = dt.datetime.now(pu.MSK) - dt.datetime.fromisoformat(cache["updated"])
            hours = age.total_seconds() / 3600
            self.status.config(text=f"⚠ Нет связи с mapm.ru — показаны данные от {fmt(cache['updated'])} МСК "
                                    f"({hours:.0f} ч назад)", fg="#b00020" if hours > STALE_HOURS else "#a86400")
        self._diff_and_fill()
        if r and self.auto_tz.get():
            self.launch_tz(quiet=True)

    def _load_cache(self):
        try:
            return json.loads((DATA / "prip_raw.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _diff_and_fill(self):
        notices = self.result["notices"]
        current = [n["id"] for n in notices]
        baseline = self.state_.get("baseline")
        if baseline is None:                     # первый запуск: в TimeZero ещё ничего нет
            self.new_ids, self.gone_ids = current, []
        else:
            self.new_ids = [i for i in current if i not in baseline]
            self.gone_ids = [i for i in baseline if i not in current]
        self._write_new_gpx()

        self.ack_btn.config(text="Замена в TimeZero выполнена ✓" if self.full else "Изменения внесены в TimeZero ✓")
        if baseline is None:
            msg = ("Первый запуск: импортируйте в TimeZero файл prip_timezero.gpx (все действующие ПРИП), "
                   f"затем нажмите «{self.ack_btn.cget('text')}».")
        elif not self.new_ids and not self.gone_ids:
            msg = f"Изменений нет — TimeZero актуален (сверено с состоянием от {fmt(self.state_['baseline_date'])})."
        elif self.full:
            parts = [f"Со времени последней замены ({fmt(self.state_['baseline_date'])}) ПРИП изменились:"]
            if self.new_ids:
                parts.append(f"  новые ({len(self.new_ids)}): {', '.join(pu.tz_name(i) for i in self.new_ids)}")
            if self.gone_ids:
                parts.append(f"  отменены ({len(self.gone_ids)}): {', '.join(pu.tz_name(i) for i in self.gone_ids)}")
            parts.append("→ В TimeZero удалите ВСЕ объекты ПРИП (слой «ПРИП» или метки и треки с именами «ПРИП …»), "
                         "затем импортируйте prip_timezero.gpx.")
            msg = "\n".join(parts)
        else:
            parts = []
            if self.new_ids:
                parts.append(f"НОВЫЕ ({len(self.new_ids)}): {', '.join(pu.tz_name(i) for i in self.new_ids)}  →  импортируйте prip_timezero_new.gpx")
            if self.gone_ids:
                parts.append(f"ОТМЕНЕНЫ ({len(self.gone_ids)}) — удалите в TimeZero объекты с именами: "
                             f"{', '.join(pu.tz_name(i) for i in self.gone_ids)}")
            msg = "\n".join(parts)
        for p in self.result.get("problems", []):
            msg += f"\n⚠ {p}"
        self.changes.config(text=msg, wraplength=self.winfo_width() - 30)

        self.tree.delete(*self.tree.get_children())
        for n in notices:
            title = n["title"].split(" ", 3)[-1] if n["title"].startswith("ПРИП") else n["title"]
            self.tree.insert("", "end", iid=n["id"], tags=("new",) if n["id"] in self.new_ids else (),
                             values=(n["id"], title, fmt(n["cancel"]) if n.get("cancel") else "",
                                     n.get("features", 0) or "текст"))
        for gid in self.gone_ids:
            note = ("ОТМЕНЁН — исчезнет из TimeZero после полной замены" if self.full
                    else f"ОТМЕНЁН — удалить в TimeZero объекты «{pu.tz_name(gid)}…»")
            self.tree.insert("", "end", iid=gid, tags=("gone",), values=(gid, note, "", ""))

    def _write_new_gpx(self):
        feats = self.result.get("features")
        if feats is None:                        # данные из кэша: берём геометрию из geojson
            try:
                feats = json.loads((DATA / "prip.geojson").read_text(encoding="utf-8"))["features"]
            except (OSError, ValueError):
                return
        pu.write_gpx_timezero([f for f in feats if f["properties"]["id"] in self.new_ids],
                              DATA / "prip_timezero_new.gpx")

    def _show_text(self, _):
        sel = self.tree.selection()
        n = next((n for n in self.result["notices"] if sel and n["id"] == sel[0]), None)
        self.text.delete("1.0", "end")
        self.text.insert("1.0", n["text"] if n else "Этот ПРИП больше не действует — удалите его объекты в TimeZero.")

    # ---------- действия ----------
    def about(self):
        messagebox.showinfo(f"О программе {APP}", f"{APP} {VERSION}\n"
                            "Действующие ПРИП Белого, Баренцева и Печорского морей для TimeZero.\n"
                            "Источник: mapm.ru (ФГБУ «АМП Западной Арктики»).\n\n"
                            f"Автор: {pu.AUTHOR}\n{pu.AUTHOR_ORG}\n{pu.AUTHOR_EMAIL}")

    def acknowledge(self):
        if not self.result:
            return
        question = ("Подтвердите, что в TimeZero удалены все старые объекты ПРИП "
                    "и импортирован свежий prip_timezero.gpx." if self.full or self.state_.get("baseline") is None
                    else "Подтвердите, что новые ПРИП импортированы в TimeZero, а отменённые удалены из него.")
        if not messagebox.askyesno(APP, question):
            return
        self.state_["baseline"] = [n["id"] for n in self.result["notices"]]
        self.state_["baseline_date"] = self.result["updated"]
        save_state(self.state_)
        self._diff_and_fill()

    def open_map(self):
        webbrowser.open((DATA / "prip_map.html").as_uri())

    def open_folder(self):
        diff = not self.full and self.state_.get("baseline") is not None
        target = DATA / ("prip_timezero_new.gpx" if diff else "prip_timezero.gpx")
        subprocess.Popen(["explorer", "/select,", str(target)])

    def launch_tz(self, quiet=False):
        exe = self.state_.get("timezero") or find_timezero()
        if not exe or not Path(exe).exists():
            if quiet:
                return
            exe = filedialog.askopenfilename(title="Укажите программу TimeZero", filetypes=[("Программа", "*.exe")])
            if not exe:
                return
        self.state_["timezero"] = exe
        save_state(self.state_)
        subprocess.Popen([exe], cwd=str(Path(exe).parent))


def fmt(iso):
    return dt.datetime.fromisoformat(iso).astimezone(pu.MSK).strftime("%d.%m %H:%M") if iso else ""


if __name__ == "__main__":
    App().mainloop()
