"""Autopinger toggle window: start and stop main.py without a terminal.

Settings live in toggle_config.json next to this file:
  {"main_path": "...", "remember": false, "token": ""}
The token is only written to disk when "Remember token" is ticked.
"""
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "toggle_config.json")
DEFAULT_MAIN = os.path.join(HERE, "main.py")


def load_config():
    cfg = {"main_path": DEFAULT_MAIN, "remember": False, "token": ""}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            cfg.update({k: loaded[k] for k in cfg if k in loaded})
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"Couldn't read {CONFIG_FILE}: {e}")
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
    except OSError as e:
        print(f"Couldn't save {CONFIG_FILE}: {e}")


class ToggleWindow(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Autopinger Toggle")
        self.geometry("640x460")
        self.minsize(480, 340)

        self.cfg = load_config()
        self.proc = None
        self.log_queue = queue.Queue()

        self.path_var = tk.StringVar(value=self.cfg["main_path"])
        self.token_var = tk.StringVar(value=self.cfg["token"])
        self.remember_var = tk.BooleanVar(value=bool(self.cfg["remember"]))
        self.show_token_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Stopped")

        self.build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self.drain_log)
        self.after(500, self.poll_process)

    def build_ui(self):
        pad = {"padx": 10, "pady": 4}
        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=6, pady=6)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(5, weight=1)

        ttk.Label(frame, text="main.py").grid(row=0, column=0, sticky="w", **pad)
        self.path_entry = ttk.Entry(frame, textvariable=self.path_var)
        self.path_entry.grid(row=0, column=1, sticky="ew", **pad)
        self.browse_btn = ttk.Button(frame, text="Browse...", command=self.browse)
        self.browse_btn.grid(row=0, column=2, **pad)

        ttk.Label(frame, text="Bot token").grid(row=1, column=0, sticky="w", **pad)
        self.token_entry = ttk.Entry(frame, textvariable=self.token_var, show="*")
        self.token_entry.grid(row=1, column=1, sticky="ew", **pad)
        ttk.Checkbutton(
            frame, text="Show", variable=self.show_token_var, command=self.toggle_show
        ).grid(row=1, column=2, **pad)

        ttk.Checkbutton(
            frame,
            text="Remember token (saved in plain text in toggle_config.json)",
            variable=self.remember_var,
        ).grid(row=2, column=1, columnspan=2, sticky="w", **pad)

        ttk.Label(
            frame,
            text="Leave the token empty to use the one in your .env file.",
            foreground="#666666",
        ).grid(row=3, column=1, columnspan=2, sticky="w", padx=10)

        bar = ttk.Frame(frame)
        bar.grid(row=4, column=0, columnspan=3, sticky="ew", **pad)
        self.toggle_btn = ttk.Button(bar, text="Start bot", command=self.toggle)
        self.toggle_btn.pack(side="left")
        ttk.Button(bar, text="Clear log", command=self.clear_log).pack(
            side="left", padx=8
        )
        self.status_label = ttk.Label(bar, textvariable=self.status_var)
        self.status_label.pack(side="right")

        self.log = scrolledtext.ScrolledText(frame, height=10, state="disabled")
        self.log.grid(row=5, column=0, columnspan=3, sticky="nsew", **pad)

    def toggle_show(self):
        self.token_entry.config(show="" if self.show_token_var.get() else "*")

    def browse(self):
        start = os.path.dirname(self.path_var.get()) or HERE
        chosen = filedialog.askopenfilename(
            title="Select main.py",
            initialdir=start if os.path.isdir(start) else HERE,
            filetypes=[("Python files", "*.py"), ("All files", "*.*")],
        )
        if chosen:
            self.path_var.set(os.path.normpath(chosen))

    def write_log(self, text):
        self.log.config(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.config(state="disabled")

    def clear_log(self):
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")

    def drain_log(self):
        try:
            while True:
                self.write_log(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        self.after(100, self.drain_log)

    def is_running(self):
        return self.proc is not None and self.proc.poll() is None

    def set_running_ui(self, running):
        self.toggle_btn.config(text="Stop bot" if running else "Start bot")
        self.status_var.set("Running" if running else "Stopped")
        state = "disabled" if running else "normal"
        self.path_entry.config(state=state)
        self.browse_btn.config(state=state)
        self.token_entry.config(state=state)

    def save_settings(self):
        self.cfg["main_path"] = self.path_var.get().strip()
        self.cfg["remember"] = bool(self.remember_var.get())
        self.cfg["token"] = self.token_var.get().strip() if self.remember_var.get() else ""
        save_config(self.cfg)

    def toggle(self):
        if self.is_running():
            self.stop_bot()
        else:
            self.start_bot()

    def start_bot(self):
        main_path = self.path_var.get().strip().strip('"')
        if not main_path or not os.path.isfile(main_path):
            messagebox.showerror("Can't start", f"main.py not found:\n{main_path}")
            return

        self.save_settings()

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        token = self.token_var.get().strip()
        if token:
            env["token"] = token  # main.py reads os.getenv("token")

        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NO_WINDOW

        try:
            self.proc = subprocess.Popen(
                [sys.executable, "-u", main_path],
                cwd=os.path.dirname(os.path.abspath(main_path)),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=flags,
            )
        except Exception as e:
            self.proc = None
            messagebox.showerror("Can't start", str(e))
            return

        threading.Thread(target=self.read_output, args=(self.proc,), daemon=True).start()
        self.log_queue.put("--- bot starting ---\n")
        self.set_running_ui(True)

    def read_output(self, proc):
        try:
            for line in proc.stdout:
                self.log_queue.put(line)
        except Exception as e:
            self.log_queue.put(f"[log reader stopped: {e}]\n")

    def stop_bot(self):
        proc = self.proc
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.log_queue.put("--- bot stopped ---\n")
        self.proc = None
        self.set_running_ui(False)

    def poll_process(self):
        # Catches the bot exiting by itself (bad token, crash, etc.)
        if self.proc is not None and self.proc.poll() is not None:
            code = self.proc.returncode
            self.log_queue.put(f"--- bot exited (code {code}) ---\n")
            self.proc = None
            self.set_running_ui(False)
        self.after(500, self.poll_process)

    def on_close(self):
        self.save_settings()
        if self.is_running():
            self.stop_bot()
        self.destroy()


if __name__ == "__main__":
    ToggleWindow().mainloop()