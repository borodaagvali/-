#!/usr/bin/env python3
"""
GUI for bulk upscaling using Cloudinary.

Особенности:
- Исправлены синтаксические ошибки.
- Фоновая обработка (поток) чтобы GUI не зависал.
- Прогресс-бар и лог в окне.
- Кликабельная ссылка на Cloudinary (открывает https://cloudinary.com).
- Учет ресурсов при сборке PyInstaller (resource_path) — корректная загрузка иконок внутри .exe/.app.
- Правильное вычисление целевой ширины через Pillow (целое число = orig_width * scale_factor).
"""
import os
import sys
import json
import logging
import threading
import webbrowser
from queue import Queue, Empty
from pathlib import Path
import requests
from PIL import Image
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import tkinter.font as tkfont

import cloudinary
import cloudinary.uploader

CONFIG_FILE = "config.json"
LOG_FILE = "bulk_upscale.log"

# Setup logging
logging.basicConfig(filename=LOG_FILE,
                    level=logging.INFO,
                    format="%(asctime)s %(levelname)s: %(message)s")


def resource_path(relative):
    """
    Return path to resource, works for normal execution and for PyInstaller (_MEIPASS).
    """
    if getattr(sys, "_MEIPASS", None):
        base = sys._MEIPASS
    else:
        base = Path(__file__).parent
    return os.path.join(base, relative)


# ---------------- Config helpers ----------------
def save_config(data):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logging.exception("Failed saving config: %s", e)


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            logging.exception("Failed loading config")
    # default
    return {"cloud_name": "", "api_key": "", "api_secret": ""}


def configure_cloudinary(cfg):
    cloudinary.config(
        cloud_name=cfg.get("cloud_name", ""),
        api_key=cfg.get("api_key", ""),
        api_secret=cfg.get("api_secret", ""),
        secure=True
    )


# ---------------- Image processing ----------------
def upscale_image(image_path, output_path, scale_factor=2.0, timeout=60):
    """
    Upscale a single image using Cloudinary. Returns (True, message) on success, (False, error_message) on failure.
    scale_factor: float, e.g. 2.0 to double size.
    """
    try:
        # Read original size using Pillow to compute integer width
        with Image.open(image_path) as im:
            orig_w, orig_h = im.size

        target_w = max(1, int(orig_w * float(scale_factor)))

        result = cloudinary.uploader.upload(
            str(image_path),
            transformation=[{
                "crop": "scale",
                "width": target_w,
                "quality": "auto:good"
            }]
        )

        url = result.get("secure_url") or result.get("url")
        if not url:
            msg = "Cloudinary did not return a URL"
            logging.error(msg + " for %s; result=%s", image_path, result)
            return False, msg

        response = requests.get(url, timeout=timeout)
        if response.status_code == 200:
            # Ensure parent directory exists
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "wb") as f:
                f.write(response.content)
            return True, "OK"
        else:
            msg = f"HTTP {response.status_code}"
            logging.error("Failed to download %s: %s", url, msg)
            return False, msg

    except Exception as e:
        logging.exception("Error processing %s", image_path)
        return False, str(e)


def process_all_images_worker(source_folder, output_folder, scale_factor, queue: Queue, stop_flag):
    """
    Worker function run in a background thread. Communicates progress & messages via queue.
    """
    supported_formats = {'.jpg', '.jpeg', '.png', '.JPG', '.JPEG', '.PNG'}
    src = Path(source_folder)
    out = Path(output_folder)
    out.mkdir(parents=True, exist_ok=True)

    image_files = [f for f in src.iterdir() if f.is_file() and f.suffix in supported_formats]
    total = len(image_files)
    queue.put(("total", total))

    successful = 0
    for idx, image_path in enumerate(image_files, start=1):
        if stop_flag.is_set():
            queue.put(("stopped", "Processing stopped by user"))
            break

        out_name = "u" + image_path.name
        out_path = out / out_name

        queue.put(("status", f"[{idx}/{total}] {image_path.name}"))
        ok, msg = upscale_image(image_path, out_path, scale_factor)
        if ok:
            successful += 1
            queue.put(("progress", (idx, True, image_path.name)))
        else:
            queue.put(("progress", (idx, False, image_path.name + " -> " + msg)))
            logging.warning("Failed %s: %s", image_path, msg)

    queue.put(("done", (successful, total)))


# ---------------- Context menu for entries ----------------
def add_context_menu(entry):
    menu = tk.Menu(entry, tearoff=0)
    menu.add_command(label="Копировать", command=lambda: entry.event_generate("<<Copy>>"))
    menu.add_command(label="Вставить", command=lambda: entry.event_generate("<<Paste>>"))
    menu.add_command(label="Вырезать", command=lambda: entry.event_generate("<<Cut>>"))

    def show_menu(event):
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    entry.bind("<Button-3>", show_menu)  # ПКМ
    entry.bind("<Control-c>", lambda e: entry.event_generate("<<Copy>>"))
    entry.bind("<Control-v>", lambda e: entry.event_generate("<<Paste>>"))
    entry.bind("<Control-x>", lambda e: entry.event_generate("<<Cut>>"))


# ---------------- GUI ----------------
class BulkUpscaleGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Массовое масштабирование фотографий")
        self.root.geometry("600x480")
        self.root.resizable(False, False)

        # Попытка установить иконку приложения/окна (работает и при запуске из PyInstaller)
        try:
            # .ico для Windows
            ico = resource_path("dagtools.ico")
            if os.path.exists(ico):
                try:
                    self.root.iconbitmap(ico)
                except Exception:
                    # иногда iconbitmap не работает в некоторых окружениях, продолжаем
                    pass
        except Exception:
            pass

        try:
            # PNG для iconphoto (кроссплатформенно)
            png = resource_path("dagtools.png")
            if os.path.exists(png):
                img = tk.PhotoImage(file=png)
                self.root.iconphoto(False, img)
                # Keep a reference to avoid GC
                self._icon_img = img
        except Exception:
            pass

        self.cfg = load_config()

        self.queue = Queue()
        self.stop_flag = threading.Event()
        self.worker_thread = None

        self._build_ui()
        self._after_id = None
        self.poll_queue()

    def _build_ui(self):
        pad = {"padx": 10, "pady": 4}

        # Clickable Cloudinary link (opens cloudinary.com)
        lbl = tk.Label(self.root, text="Настройки Cloudinary — Cloudinary.com", fg="blue", cursor="hand2")
        f = tkfont.Font(lbl, lbl.cget("font"))
        f.configure(family="Arial", size=14, weight="bold", underline=False)
        lbl.configure(font=f)
        lbl.pack(pady=10)

        def _open_cloudinary(event=None):
            webbrowser.open_new("https://cloudinary.com")

        lbl.bind("<Button-1>", _open_cloudinary)
        lbl.bind("<Enter>", lambda e: f.configure(underline=True))
        lbl.bind("<Leave>", lambda e: f.configure(underline=False))

        frame_creds = tk.Frame(self.root)
        frame_creds.pack(fill="x", padx=10)

        tk.Label(frame_creds, text="Cloud Name:").grid(row=0, column=0, sticky="w")
        self.entry_name = tk.Entry(frame_creds, width=50)
        self.entry_name.grid(row=0, column=1, sticky="w")
        self.entry_name.insert(0, self.cfg.get("cloud_name", ""))

        tk.Label(frame_creds, text="API Key:").grid(row=1, column=0, sticky="w")
        self.entry_key = tk.Entry(frame_creds, width=50)
        self.entry_key.grid(row=1, column=1, sticky="w")
        self.entry_key.insert(0, self.cfg.get("api_key", ""))

        tk.Label(frame_creds, text="API Secret:").grid(row=2, column=0, sticky="w")
        self.entry_secret = tk.Entry(frame_creds, width=50, show="*")
        self.entry_secret.grid(row=2, column=1, sticky="w")
        self.entry_secret.insert(0, self.cfg.get("api_secret", ""))

        # Save secret checkbox
        self.save_secret_var = tk.BooleanVar(value=False)
        tk.Checkbutton(frame_creds, text="Сохранять секрет в config.json (не рекомендуется)", variable=self.save_secret_var).grid(row=3, column=1, sticky="w")

        for e in (self.entry_name, self.entry_key, self.entry_secret):
            add_context_menu(e)

        # Folders and options
        frame_folders = tk.Frame(self.root)
        frame_folders.pack(fill="x", padx=10, pady=6)

        tk.Label(frame_folders, text="Папка с исходными фото:").grid(row=0, column=0, sticky="w")
        self.entry_source = tk.Entry(frame_folders, width=42)
        self.entry_source.grid(row=0, column=1, sticky="w")
        tk.Button(frame_folders, text="Выбрать...", command=lambda: self._choose_dir(self.entry_source)).grid(row=0, column=2, padx=6)

        tk.Label(frame_folders, text="Папка для сохранения:").grid(row=1, column=0, sticky="w")
        self.entry_output = tk.Entry(frame_folders, width=42)
        self.entry_output.grid(row=1, column=1, sticky="w")
        tk.Button(frame_folders, text="Выбрать...", command=lambda: self._choose_dir(self.entry_output)).grid(row=1, column=2, padx=6)

        for e in (self.entry_source, self.entry_output):
            add_context_menu(e)

        # Scale factor
        frame_scale = tk.Frame(self.root)
        frame_scale.pack(fill="x", padx=10, pady=6)
        tk.Label(frame_scale, text="Множитель масштаба (например, 2.0):").grid(row=0, column=0, sticky="w")
        self.entry_scale = tk.Entry(frame_scale, width=10)
        self.entry_scale.grid(row=0, column=1, sticky="w", padx=(6, 0))
        self.entry_scale.insert(0, "2.0")
        add_context_menu(self.entry_scale)

        # Progress & controls
        frame_ctrl = tk.Frame(self.root)
        frame_ctrl.pack(fill="x", padx=10, pady=(10, 2))

        self.progress = ttk.Progressbar(frame_ctrl, orient="horizontal", length=560, mode="determinate")
        self.progress.grid(row=0, column=0, columnspan=3, pady=(0, 6))

        self.status_var = tk.StringVar(value="Готов")
        tk.Label(frame_ctrl, textvariable=self.status_var).grid(row=1, column=0, sticky="w")

        self.btn_start = tk.Button(frame_ctrl, text="🚀 Запустить обработку", bg="green", fg="white", font=("Arial", 11, "bold"), command=self.start_processing)
        self.btn_start.grid(row=1, column=1, sticky="e", padx=6)
        self.btn_stop = tk.Button(frame_ctrl, text="⛔ Остановить", state="disabled", command=self.stop_processing)
        self.btn_stop.grid(row=1, column=2, sticky="e")

        # Log area
        tk.Label(self.root, text="Лог (последние сообщения):").pack(anchor="w", padx=10)
        self.log_text = tk.Text(self.root, height=8, state="disabled")
        self.log_text.pack(fill="both", padx=10, pady=(0, 10))

    def _choose_dir(self, entry):
        d = filedialog.askdirectory()
        if d:
            entry.delete(0, tk.END)
            entry.insert(0, d)

    def start_processing(self):
        # Validate inputs
        cloud_name = self.entry_name.get().strip()
        api_key = self.entry_key.get().strip()
        api_secret = self.entry_secret.get().strip()
        source = self.entry_source.get().strip()
        output = self.entry_output.get().strip()
        scale = self.entry_scale.get().strip()

        if not cloud_name or not api_key:
            messagebox.showerror("Ошибка", "Введите Cloud Name и API Key")
            return

        if not source or not os.path.isdir(source):
            messagebox.showerror("Ошибка", "Выберите существующую папку с фото")
            return

        if not output:
            messagebox.showerror("Ошибка", "Выберите папку для сохранения")
            return

        try:
            scale_f = float(scale)
            if scale_f <= 0:
                raise ValueError()
        except Exception:
            messagebox.showerror("Ошибка", "Неверный множитель масштаба. Используйте положительное число, например 2.0")
            return

        # Save config (optionally excluding secret)
        cfg = {"cloud_name": cloud_name, "api_key": api_key}
        if self.save_secret_var.get():
            cfg["api_secret"] = api_secret
        else:
            cfg["api_secret"] = ""
        save_config(cfg)

        # Configure Cloudinary with provided secret for runtime use
        configure_cloudinary({"cloud_name": cloud_name, "api_key": api_key, "api_secret": api_secret})

        # Disable UI
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.stop_flag.clear()
        self.progress["value"] = 0
        self.status_var.set("Запуск...")

        # Start worker thread
        self.worker_thread = threading.Thread(target=process_all_images_worker,
                                              args=(source, output, scale_f, self.queue, self.stop_flag),
                                              daemon=True)
        self.worker_thread.start()

    def stop_processing(self):
        if messagebox.askyesno("Подтверждение", "Остановить обработку?"):
            self.stop_flag.set()
            self.status_var.set("Ожидание остановки...")
            self.btn_stop.config(state="disabled")

    def poll_queue(self):
        try:
            while True:
                item = self.queue.get_nowait()
                typ = item[0]
                payload = item[1]
                if typ == "total":
                    total = payload
                    self.progress["maximum"] = max(1, total)
                    self.log("Всего файлов: %d" % total)
                elif typ == "status":
                    self.status_var.set(payload)
                elif typ == "progress":
                    idx, ok, msg = payload
                    self.progress["value"] = idx
                    if ok:
                        self.log(f"[{idx}] OK: {msg}")
                    else:
                        self.log(f"[{idx}] ERROR: {msg}")
                elif typ == "stopped":
                    self.log(str(payload))
                elif typ == "done":
                    successful, total = payload
                    self.log(f"Готово: {successful}/{total}")
                    messagebox.showinfo("Готово", f"✅ Успешно обработано: {successful}/{total} файлов.")
                    self._reset_ui()
        except Empty:
            pass
        finally:
            self._after_id = self.root.after(200, self.poll_queue)

    def log(self, text):
        logging.info(text)
        self.log_text.config(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _reset_ui(self):
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")
        self.status_var.set("Готов")
        self.progress["value"] = 0

    def destroy(self):
        if self._after_id:
            self.root.after_cancel(self._after_id)


def main():
    root = tk.Tk()
    app = BulkUpscaleGUI(root)
    root.protocol("WM_DELETE_WINDOW", lambda: (app.destroy(), root.destroy()))
    root.mainloop()


if __name__ == "__main__":
    main()