# -*- coding: utf-8 -*-
"""SHP 좌표변환·병합·분할 도구 — 데스크톱(Windows) 버전.

웹판(https://gis-shp-tool.streamlit.app)과 같은 GDAL 엔진을 쓰지만,
업로드·다운로드 없이 내 PC의 파일을 바로 읽고 결과를 지정 폴더에 저장합니다.
대용량 SHP에서 업로드 제한·연결 끊김이 없고, GPKG 중간 파일도 로컬 디스크에서 처리합니다.

실행: python app.py     (GDAL은 QGIS 설치본을 자동으로 찾습니다)
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import core

# 드래그앤드롭(tkinterdnd2)은 없으면 버튼 방식으로 자동 대체됩니다.
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    BASE_TK = TkinterDnD.Tk
    HAS_DND = True
except Exception:  # 라이브러리 미설치 등
    DND_FILES = None
    BASE_TK = tk.Tk
    HAS_DND = False

APP_TITLE = "SHP 좌표변환·병합·분할 도구 (데스크톱)"
APP_VERSION = "1.3.0"
QGIS_URL = "https://qgis.org/download/"

# ----- 밝은 테마 색상 -----
BG = "#e1e9ef"
CARD = "#ffffff"
INK = "#2c3e50"
MUTED = "#7e8ea0"
ACCENT = "#5b8db5"
ACCENT_D = "#487aa3"
LINE = "#d4dde6"
LOGBG = "#f5f8fb"
TAB_OFF_BG = "#f3f6f9"
DROP_BG = "#eef5fb"
DROP_HOVER = "#d6e8f7"

FONT_PREFER = ["Pretendard", "본고딕", "Noto Sans KR", "Malgun Gothic", "맑은 고딕"]
UI_FONT = "맑은 고딕"


def app_dir() -> Path:
    """exe로 묶였을 때도 '실행 파일이 있는 폴더'를 돌려줍니다(설정 파일 위치)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_path(name: str) -> Path:
    """exe에 묶인 리소스(아이콘 등) 경로."""
    base = Path(getattr(sys, "_MEIPASS", app_dir()))
    return base / name


def _writable(folder: Path) -> bool:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".write_test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def resolve_config_path() -> Path:
    """설정 파일 위치. exe 폴더가 쓰기 금지(Program Files 등)면 사용자 폴더로 대피합니다."""
    if _writable(app_dir()):
        return app_dir() / "config.txt"
    fallback = Path(os.environ.get("APPDATA") or Path.home()) / "SHP도구"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback / "config.txt"


CONFIG_PATH = resolve_config_path()
DEFAULT_CONFIG = """# ===== SHP 도구 설정 =====
# GDAL(QGIS) bin 폴더. 비워두면 자동으로 찾습니다.
GDAL_BIN=
# 결과 저장 폴더. 비워두면 바탕화면\\SHP도구_결과
OUT_DIR=
# 입력 DBF 인코딩 / SHP 출력 인코딩
IN_ENC=CP949
OUT_ENC=UTF-8
"""


def load_config() -> dict[str, str]:
    if not CONFIG_PATH.exists():
        try:
            CONFIG_PATH.write_text(DEFAULT_CONFIG, encoding="utf-8")
        except OSError:
            pass
    config: dict[str, str] = {}
    try:
        for line in CONFIG_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            config[key.strip().upper()] = value.strip()
    except OSError:
        pass
    return config


def save_config(config: dict[str, str]) -> None:
    lines = ["# ===== SHP 도구 설정 =====",
             "# GDAL(QGIS) bin 폴더. 비워두면 자동으로 찾습니다.",
             f"GDAL_BIN={config.get('GDAL_BIN', '')}",
             "# 결과 저장 폴더",
             f"OUT_DIR={config.get('OUT_DIR', '')}",
             "# 입력 DBF 인코딩 / SHP 출력 인코딩",
             f"IN_ENC={config.get('IN_ENC', 'CP949')}",
             f"OUT_ENC={config.get('OUT_ENC', 'UTF-8')}"]
    try:
        CONFIG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def default_out_dir() -> Path:
    desktop = Path.home() / "Desktop"
    base = desktop if desktop.exists() else Path.home()
    return base / "SHP도구_결과"


def stamped_dir(base: Path, job: str) -> Path:
    """작업별 결과 폴더(덮어쓰기 방지용 시각 표시)."""
    path = base / f"{job}_{time.strftime('%Y%m%d_%H%M%S')}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def open_folder(path: Path) -> None:
    try:
        if os.name == "nt":
            os.startfile(str(path))  # noqa: S606
        else:
            webbrowser.open(path.as_uri())
    except Exception:
        pass


class ShpToolApp(BASE_TK):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_TITLE}  v{APP_VERSION}")
        icon = resource_path("icon.ico")
        if icon.exists():
            try:
                self.iconbitmap(str(icon))
            except tk.TclError:
                pass
        # 사원 노트북(1366x768)에서도 창이 화면을 벗어나지 않도록 화면 크기에 맞춥니다.
        width = min(self.winfo_screenwidth() - 60, 1180)
        height = min(self.winfo_screenheight() - 90, 880)
        self.geometry(f"{width}x{height}+20+20")
        self.minsize(960, 600)
        self.configure(bg=BG)

        self.config_data = load_config()
        self.layers: list[core.LayerInfo] = []
        self.work_dir = Path(os.environ.get("TEMP", str(Path.home()))) / "shp_tool_work"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.busy = False
        self.last_out_dir: Path | None = None
        self.code_table: core.Table | None = None
        self.code_raw: bytes | None = None
        self.code_csv_path: Path | None = None

        self._pick_font()
        self._build_style()
        self._build_ui()

        core.set_log_hook(lambda text: self.log_queue.put(text))
        core.set_gdal_bin_hint(self.config_data.get("GDAL_BIN", ""))
        self._log_job = self.after(120, self._drain_log)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.refresh_gdal_status()
        self.log(f"준비 완료 (v{APP_VERSION}). ① 파일을 끌어다 놓거나 [파일 선택] → ② 탭에서 작업을 고르세요.")
        self.log(f"설정 파일: {CONFIG_PATH}")
        if not HAS_DND:
            self.log("ℹ️ 드래그앤드롭 모듈(tkinterdnd2)이 없어 버튼으로만 파일을 넣을 수 있습니다.")
        ogr2ogr, _ = core.gdals()
        if not ogr2ogr:
            self.after(400, self.show_gdal_help)

    # ────────────────────────── 기본 외형 ──────────────────────────
    def _pick_font(self) -> None:
        global UI_FONT
        try:
            from tkinter import font as tkfont
            families = set(tkfont.families())
            for name in FONT_PREFER:
                if name in families:
                    UI_FONT = name
                    break
        except Exception:
            pass

    @staticmethod
    def _indicator_image(size: int, fill: str, border: str, mark: str | None) -> tk.PhotoImage:
        """체크박스 네모칸 그림 하나를 픽셀 단위로 그려서 돌려줍니다."""
        rows = [[fill] * size for _ in range(size)]
        edge = max(1, size // 8)                       # 테두리 두께
        for y in range(size):
            for x in range(size):
                if x < edge or y < edge or x >= size - edge or y >= size - edge:
                    rows[y][x] = border
        if mark:
            # ✓ 모양: (짧은 아래획) → (긴 위획). 굵기는 크기에 비례.
            thick = max(2, size // 7)
            segments = [((0.28, 0.52), (0.44, 0.70)), ((0.44, 0.70), (0.75, 0.30))]
            for (x0, y0), (x1, y1) in segments:
                steps = size * 2
                for step in range(steps + 1):
                    t = step / steps
                    px = round((x0 + (x1 - x0) * t) * size)
                    py = round((y0 + (y1 - y0) * t) * size)
                    for dx in range(thick):
                        for dy in range(thick):
                            mx, my = px + dx, py + dy
                            if 0 <= mx < size and 0 <= my < size:
                                rows[my][mx] = mark
        image = tk.PhotoImage(width=size, height=size)
        image.put(" ".join("{" + " ".join(row) + "}" for row in rows))
        return image

    def _apply_check_indicator(self, style: ttk.Style) -> None:
        """체크박스의 체크 표시를 직접 그린 ✓로 바꿉니다.

        clam 테마는 켜진 체크박스를 **✓가 아니라 X(☒)로** 그립니다. 사용자가 "선택이 안 된
        줄 알았다"고 할 만큼 헷갈리는 모양이라, 네모칸과 체크표시를 이미지로 만들어
        indicator 요소를 교체합니다(웹판이 CSS로 직접 그리는 것과 같은 이유·같은 모양).
        실패하면 조용히 기본 모양으로 남겨 둡니다.
        """
        try:
            size = 16
            self._check_images = {
                "off": self._indicator_image(size, "#ffffff", "#98a2b3", None),
                "on": self._indicator_image(size, ACCENT_D, ACCENT_D, "#ffffff"),
                "off_disabled": self._indicator_image(size, "#f1f4f7", "#c8d2dc", None),
                "on_disabled": self._indicator_image(size, "#b9c6d2", "#b9c6d2", "#ffffff"),
            }
            style.element_create(
                "Check.indicator", "image", self._check_images["off"],
                ("disabled", "selected", self._check_images["on_disabled"]),
                ("disabled", self._check_images["off_disabled"]),
                ("selected", self._check_images["on"]),
                border=0, sticky="", padding=(0, 0, 6, 0),
            )
            style.layout("TCheckbutton", [
                ("Checkbutton.padding", {"sticky": "nswe", "children": [
                    ("Check.indicator", {"side": "left", "sticky": ""}),
                    ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [
                        ("Checkbutton.label", {"sticky": "nswe"}),
                    ]}),
                ]}),
            ])
        except tk.TclError:
            pass

    def _build_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        base = (UI_FONT, 10)
        style.configure(".", background=BG, foreground=INK, font=base)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD, relief="flat")
        style.configure("TLabel", background=BG, foreground=INK, font=base)
        style.configure("Card.TLabel", background=CARD, foreground=INK, font=base)
        style.configure("Muted.TLabel", background=CARD, foreground=MUTED, font=(UI_FONT, 9))
        style.configure("Head.TLabel", background=CARD, foreground=INK, font=(UI_FONT, 11, "bold"))
        style.configure("TCheckbutton", background=CARD, foreground=INK, font=base)
        style.configure("TRadiobutton", background=CARD, foreground=INK, font=base)
        self._apply_check_indicator(style)
        style.configure("TButton", font=base, padding=(10, 5))
        style.configure("Accent.TButton", font=(UI_FONT, 10, "bold"), padding=(12, 6),
                        background=ACCENT, foreground="#ffffff")
        style.map("Accent.TButton", background=[("active", ACCENT_D), ("disabled", "#b9c6d2")])
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=TAB_OFF_BG, foreground=MUTED,
                        padding=(16, 8), font=(UI_FONT, 10, "bold"))
        style.map("TNotebook.Tab", background=[("selected", CARD)], foreground=[("selected", INK)])
        style.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=INK,
                        rowheight=24, font=(UI_FONT, 9))
        style.configure("Treeview.Heading", background=TAB_OFF_BG, foreground=INK, font=(UI_FONT, 9, "bold"))
        style.configure("TProgressbar", background=ACCENT, troughcolor=LINE)

    def card(self, parent: tk.Misc, **kwargs) -> ttk.Frame:
        frame = ttk.Frame(parent, style="Card.TFrame", padding=12, **kwargs)
        return frame

    # ────────────────────────── UI 구성 ──────────────────────────
    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)

        # 순서 주의: 위 카드 → 로그(아래 고정) → 탭(남는 공간 전부).
        # 로그를 먼저 아래에 붙여야 창이 작아도 로그가 화면 밖으로 밀리지 않습니다.
        self._build_top(root)
        self._build_log(root)
        self._build_tabs(root)

    def _build_top(self, parent: tk.Misc) -> None:
        top = ttk.Frame(parent)
        top.pack(fill="x")

        # ① 파일
        files_card = self.card(top)
        files_card.pack(side="left", fill="both", expand=True)
        ttk.Label(files_card, text="① 파일 불러오기", style="Head.TLabel").pack(anchor="w")
        ttk.Label(files_card, text="SHP·GPKG·ZIP 파일 또는 폴더를 넣으면 안에 있는 레이어를 모두 찾습니다.",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 6))

        self.drop_zone = tk.Label(
            files_card,
            text=("📂  여기로 파일을 끌어다 놓으세요   ·   .shp / .gpkg / .zip / 폴더 / 코드표 .csv"
                  if HAS_DND else "아래 [파일 선택] 또는 [폴더 선택] 버튼을 눌러 주세요"),
            bg=DROP_BG, fg=ACCENT_D, font=(UI_FONT, 10, "bold"),
            relief="ridge", bd=1, padx=10, pady=9, cursor="hand2")
        self.drop_zone.pack(fill="x", pady=(0, 6))
        self.drop_zone.bind("<Button-1>", lambda _e: self.pick_files())
        self._register_drop(self.drop_zone)

        buttons = ttk.Frame(files_card, style="Card.TFrame")
        buttons.pack(fill="x")
        ttk.Button(buttons, text="파일 선택", command=self.pick_files).pack(side="left")
        ttk.Button(buttons, text="폴더 선택", command=self.pick_folder).pack(side="left", padx=6)
        ttk.Button(buttons, text="목록 비우기", command=self.clear_layers).pack(side="left")

        tree_wrap = ttk.Frame(files_card, style="Card.TFrame")
        tree_wrap.pack(fill="both", expand=True, pady=(8, 0))
        columns = ("kind", "prj", "cpg", "path")
        self.layer_tree = ttk.Treeview(tree_wrap, columns=columns, show="tree headings", height=4)
        self.layer_tree.heading("#0", text="레이어")
        self.layer_tree.heading("kind", text="형식")
        self.layer_tree.heading("prj", text="prj")
        self.layer_tree.heading("cpg", text="cpg")
        self.layer_tree.heading("path", text="경로")
        self.layer_tree.column("#0", width=250, anchor="w")
        self.layer_tree.column("kind", width=55, anchor="center")
        self.layer_tree.column("prj", width=45, anchor="center")
        self.layer_tree.column("cpg", width=70, anchor="center")
        self.layer_tree.column("path", width=330, anchor="w")
        scroll = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.layer_tree.yview)
        self.layer_tree.configure(yscrollcommand=scroll.set)
        self.layer_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self._register_drop(self.layer_tree)
        self._register_drop(files_card)

        # ② 설정
        conf = self.card(top)
        conf.pack(side="left", fill="y", padx=(10, 0))
        ttk.Label(conf, text="② 공통 설정", style="Head.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")

        ttk.Label(conf, text="입력 DBF 인코딩", style="Card.TLabel").grid(row=1, column=0, sticky="w", pady=(8, 2))
        self.in_enc = tk.StringVar(value=self.config_data.get("IN_ENC", "CP949"))
        ttk.Combobox(conf, textvariable=self.in_enc, values=core.ENCODINGS, width=14,
                     state="readonly").grid(row=1, column=1, sticky="w", pady=(8, 2))

        ttk.Label(conf, text="SHP 출력 인코딩", style="Card.TLabel").grid(row=2, column=0, sticky="w", pady=2)
        self.out_enc = tk.StringVar(value=self.config_data.get("OUT_ENC", "UTF-8"))
        ttk.Combobox(conf, textvariable=self.out_enc, values=["UTF-8", "CP949"], width=14,
                     state="readonly").grid(row=2, column=1, sticky="w", pady=2)

        ttk.Label(conf, text="결과 저장 폴더", style="Card.TLabel").grid(row=3, column=0, sticky="w", pady=2)
        self.out_dir = tk.StringVar(value=self.config_data.get("OUT_DIR") or str(default_out_dir()))
        ttk.Entry(conf, textvariable=self.out_dir, width=26).grid(row=3, column=1, sticky="we", pady=2)
        ttk.Button(conf, text="찾기", width=5, command=self.pick_out_dir).grid(row=3, column=2, padx=(4, 0))

        ttk.Label(conf, text="GDAL/QGIS bin", style="Card.TLabel").grid(row=4, column=0, sticky="w", pady=2)
        self.gdal_bin = tk.StringVar(value=self.config_data.get("GDAL_BIN", ""))
        ttk.Entry(conf, textvariable=self.gdal_bin, width=26).grid(row=4, column=1, sticky="we", pady=2)
        ttk.Button(conf, text="찾기", width=5, command=self.pick_gdal_bin).grid(row=4, column=2, padx=(4, 0))

        self.gdal_label = ttk.Label(conf, text="", style="Muted.TLabel", wraplength=300, justify="left")
        self.gdal_label.grid(row=5, column=0, columnspan=3, sticky="w", pady=(6, 0))
        actions = ttk.Frame(conf, style="Card.TFrame")
        actions.grid(row=6, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Button(actions, text="GDAL 다시 찾기", command=self.refresh_gdal_status).grid(row=0, column=0, sticky="we")
        ttk.Button(actions, text="설정 저장", command=self.save_settings).grid(row=0, column=1, padx=4, sticky="we")
        ttk.Button(actions, text="결과 폴더 열기", command=self.open_result_dir).grid(row=1, column=0, pady=4, sticky="we")
        ttk.Button(actions, text="도움말", command=self.show_gdal_help).grid(row=1, column=1, padx=4, pady=4, sticky="we")

    def _build_tabs(self, parent: tk.Misc) -> None:
        self.notebook = ttk.Notebook(parent)
        self.notebook.pack(fill="both", expand=True, pady=10, side="top")
        self.tab_convert = self.card(self.notebook)
        self.tab_merge = self.card(self.notebook)
        self.tab_split = self.card(self.notebook)
        self.tab_join = self.card(self.notebook)
        self.tab_preview = self.card(self.notebook)
        self.notebook.add(self.tab_convert, text="1. 좌표계 변환")
        self.notebook.add(self.tab_merge, text="2. 레이어 병합")
        self.notebook.add(self.tab_split, text="3. 레이어 분할")
        self.notebook.add(self.tab_join, text="4. 코드 결합")
        self.notebook.add(self.tab_preview, text="5. 속성·인코딩 확인")
        self._build_convert_tab()
        self._build_merge_tab()
        self._build_split_tab()
        self._build_join_tab()
        self._build_preview_tab()

    def _build_log(self, parent: tk.Misc) -> None:
        wrap = self.card(parent)
        wrap.pack(fill="x", expand=False, side="bottom")
        head = ttk.Frame(wrap, style="Card.TFrame")
        head.pack(fill="x")
        ttk.Label(head, text="처리 로그", style="Head.TLabel").pack(side="left")
        ttk.Button(head, text="로그 저장", command=self.save_log).pack(side="right")
        ttk.Button(head, text="지우기", command=lambda: self.log_text.delete("1.0", "end")).pack(side="right", padx=6)
        # 실행 중에만 켜지는 취소 버튼(누르면 GDAL 프로세스를 즉시 종료합니다)
        self.cancel_button = ttk.Button(head, text="■ 작업 취소", command=self.cancel_job, state="disabled")
        self.cancel_button.pack(side="right", padx=6)

        self.progress = ttk.Progressbar(wrap, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(6, 4))
        self.status = ttk.Label(wrap, text="대기 중", style="Muted.TLabel")
        self.status.pack(anchor="w")

        text_wrap = ttk.Frame(wrap, style="Card.TFrame")
        text_wrap.pack(fill="both", expand=True, pady=(6, 0))
        self.log_text = tk.Text(text_wrap, height=5, bg=LOGBG, fg=INK, relief="flat",
                                font=("Consolas", 9), wrap="word", insertbackground=INK)
        scroll = ttk.Scrollbar(text_wrap, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    # ────────────────────────── 탭 1: 좌표계 변환 ──────────────────────────
    def _build_convert_tab(self) -> None:
        tab = self.tab_convert
        left = ttk.Frame(tab, style="Card.TFrame")
        left.pack(side="left", fill="both", expand=True)
        # 실행 버튼을 먼저 아래에 붙여, 창이 작아도 버튼이 잘리지 않게 합니다.
        right_wrap = ttk.Frame(tab, style="Card.TFrame")
        right_wrap.pack(side="left", fill="y", padx=(16, 0))
        ttk.Button(right_wrap, text="좌표계 변환 실행", style="Accent.TButton",
                   command=self.run_convert).pack(side="bottom", fill="x", pady=(10, 0))
        right = ttk.Frame(right_wrap, style="Card.TFrame")
        right.pack(side="top", fill="both", expand=True)

        ttk.Label(left, text="변환할 레이어 (여러 개 선택 가능 · 선택 안 하면 전체)", style="Card.TLabel").pack(anchor="w")

        # 옵션은 왼쪽 아래에 고정 — 작은 화면(1366x768)에서도 잘리지 않게 두 열에 나눠 담습니다.
        # 아래에서 위로 쌓이므로 pack 순서는 면적 → 안전변환 → 저장형식 입니다.
        area_row = ttk.Frame(left, style="Card.TFrame")
        area_row.pack(side="bottom", fill="x", pady=(4, 0))
        self.convert_area = tk.BooleanVar(value=False)
        ttk.Checkbutton(area_row, text="면적 컬럼(area_m2, ㎡) 추가", variable=self.convert_area).pack(side="left")
        ttk.Label(area_row, text="소수점", style="Card.TLabel").pack(side="left", padx=(12, 4))
        self.convert_area_dec = tk.IntVar(value=1)
        ttk.Spinbox(area_row, from_=0, to=6, textvariable=self.convert_area_dec, width=5).pack(side="left")
        ttk.Label(area_row, text="※ 5186 같은 미터 좌표계에서만 ㎡가 정확합니다",
                  style="Muted.TLabel").pack(side="left", padx=10)

        safe_row = ttk.Frame(left, style="Card.TFrame")
        safe_row.pack(side="bottom", fill="x", pady=(6, 0))
        self.convert_safe = tk.BooleanVar(value=True)
        ttk.Checkbutton(safe_row, text="안전 변환 모드(권장): 도형 복구 2단계 + 전후 피처수 검증",
                        variable=self.convert_safe, command=self._toggle_safe).pack(side="left")
        self.convert_makevalid = tk.BooleanVar(value=False)
        self.convert_makevalid_cb = ttk.Checkbutton(safe_row, text="도형 유효화(-makevalid)",
                                                    variable=self.convert_makevalid, state="disabled")
        self.convert_makevalid_cb.pack(side="left", padx=12)

        fmt_row = ttk.Frame(left, style="Card.TFrame")
        fmt_row.pack(side="bottom", fill="x", pady=(6, 0))
        ttk.Label(fmt_row, text="저장 형식", style="Card.TLabel").pack(side="left")
        self.convert_format = tk.StringVar(value="SHP")
        ttk.Radiobutton(fmt_row, text="SHP", value="SHP", variable=self.convert_format).pack(side="left", padx=(8, 0))
        ttk.Radiobutton(fmt_row, text="GPKG", value="GPKG", variable=self.convert_format).pack(side="left", padx=8)

        self.convert_list = self._layer_listbox(left)

        row = 0
        ttk.Label(right, text="목표 좌표계", style="Card.TLabel").grid(row=row, column=0, sticky="w", pady=3)
        self.convert_target = tk.StringVar(value=list(core.COMMON_EPSG.keys())[0])
        combo = ttk.Combobox(right, textvariable=self.convert_target, values=list(core.COMMON_EPSG.keys()),
                             width=30, state="readonly")
        combo.grid(row=row, column=1, sticky="w", pady=3)
        combo.bind("<<ComboboxSelected>>", lambda _e: self._toggle_custom_epsg())

        row += 1
        ttk.Label(right, text="직접 입력 EPSG", style="Card.TLabel").grid(row=row, column=0, sticky="w", pady=3)
        self.convert_custom = tk.StringVar(value="5186")
        self.convert_custom_entry = ttk.Entry(right, textvariable=self.convert_custom, width=32, state="disabled")
        self.convert_custom_entry.grid(row=row, column=1, sticky="w", pady=3)

        row += 1
        ttk.Label(right, text="원본 EPSG 강제 지정", style="Card.TLabel").grid(row=row, column=0, sticky="w", pady=3)
        self.convert_source = tk.StringVar(value="")
        ttk.Entry(right, textvariable=self.convert_source, width=32).grid(row=row, column=1, sticky="w", pady=3)

        row += 1
        ttk.Label(right, text="(.prj가 없거나 틀릴 때만 입력. 예: 5174)", style="Muted.TLabel")\
            .grid(row=row, column=0, columnspan=2, sticky="w")


    def _toggle_custom_epsg(self) -> None:
        is_custom = core.COMMON_EPSG.get(self.convert_target.get()) == "custom"
        self.convert_custom_entry.configure(state="normal" if is_custom else "disabled")

    def _toggle_safe(self) -> None:
        self.convert_makevalid_cb.configure(state="disabled" if self.convert_safe.get() else "normal")

    # ────────────────────────── 탭 2: 병합 ──────────────────────────
    def _build_merge_tab(self) -> None:
        tab = self.tab_merge
        self.merge_mode = tk.StringVar(value="dissolve")
        head = ttk.Frame(tab, style="Card.TFrame")
        head.pack(fill="x")
        ttk.Label(head, text="병합 방식", style="Head.TLabel").pack(side="left", padx=(0, 12))
        for text, value in [("컬럼값 기준 병합(도형도 합침)", "dissolve"),
                            ("속성만 합치기(도형·개수 유지)", "keep"),
                            ("여러 레이어 이어붙이기", "append")]:
            ttk.Radiobutton(head, text=text, value=value, variable=self.merge_mode,
                            command=self._switch_merge_mode).pack(side="left", padx=6)

        common = ttk.Frame(tab, style="Card.TFrame")
        common.pack(fill="x", pady=(8, 4))
        ttk.Label(common, text="목표 EPSG 통일(선택)", style="Card.TLabel").pack(side="left")
        self.merge_epsg = tk.StringVar(value="")
        ttk.Entry(common, textvariable=self.merge_epsg, width=10).pack(side="left", padx=(6, 16))
        ttk.Label(common, text="저장 형식", style="Card.TLabel").pack(side="left")
        self.merge_format = tk.StringVar(value="SHP")
        ttk.Radiobutton(common, text="SHP", value="SHP", variable=self.merge_format).pack(side="left", padx=4)
        ttk.Radiobutton(common, text="GPKG", value="GPKG", variable=self.merge_format).pack(side="left", padx=4)
        self.merge_makevalid = tk.BooleanVar(value=False)
        ttk.Checkbutton(common, text="도형 유효화(-makevalid)", variable=self.merge_makevalid).pack(side="left", padx=12)

        body = ttk.Frame(tab, style="Card.TFrame")
        body.pack(fill="both", expand=True)

        # (a) dissolve / keep 공용: 단일 레이어 + 기준 컬럼 + 집계표
        self.merge_single = ttk.Frame(body, style="Card.TFrame")
        ttk.Button(self.merge_single, text="병합 실행", style="Accent.TButton",
                   command=self.run_merge_single).pack(side="bottom", anchor="e", pady=(10, 0))
        picker = ttk.Frame(self.merge_single, style="Card.TFrame")
        picker.pack(fill="x")
        ttk.Label(picker, text="대상 레이어", style="Card.TLabel").pack(side="left")
        self.merge_layer = tk.StringVar()
        self.merge_layer_combo = ttk.Combobox(picker, textvariable=self.merge_layer, width=44, state="readonly")
        self.merge_layer_combo.pack(side="left", padx=6)
        self.merge_layer_combo.bind("<<ComboboxSelected>>", lambda _e: self._reload_merge_columns())
        ttk.Label(picker, text="기준 컬럼", style="Card.TLabel").pack(side="left", padx=(12, 0))
        self.merge_column = tk.StringVar()
        self.merge_column_combo = ttk.Combobox(picker, textvariable=self.merge_column, width=22, state="readonly")
        self.merge_column_combo.pack(side="left", padx=6)
        self.merge_column_combo.bind("<<ComboboxSelected>>", lambda _e: self._reload_agg_table())

        self.merge_hint = ttk.Label(self.merge_single, text="", style="Muted.TLabel", wraplength=980, justify="left")
        self.merge_hint.pack(anchor="w", pady=(6, 4))

        opts = ttk.Frame(self.merge_single, style="Card.TFrame")
        opts.pack(fill="x")
        self.merge_area = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="면적 컬럼(area_m2, ㎡) 추가", variable=self.merge_area).pack(side="left")
        ttk.Label(opts, text="소수점", style="Card.TLabel").pack(side="left", padx=(12, 4))
        self.merge_area_dec = tk.IntVar(value=1)
        ttk.Spinbox(opts, from_=0, to=6, textvariable=self.merge_area_dec, width=5).pack(side="left")

        agg_wrap = ttk.Frame(self.merge_single, style="Card.TFrame")
        agg_wrap.pack(fill="both", expand=True, pady=(8, 0))
        ttk.Label(agg_wrap, text="속성 집계(선택) — 컬럼을 고르고 방식을 정한 뒤 [적용]", style="Card.TLabel").pack(anchor="w")
        agg_body = ttk.Frame(agg_wrap, style="Card.TFrame")
        agg_body.pack(fill="both", expand=True)
        self.agg_tree = ttk.Treeview(agg_body, columns=("func",), show="tree headings", height=4,
                                     selectmode="extended")
        self.agg_tree.heading("#0", text="컬럼")
        self.agg_tree.heading("func", text="집계 방식")
        self.agg_tree.column("#0", width=280)
        self.agg_tree.column("func", width=160, anchor="center")
        agg_scroll = ttk.Scrollbar(agg_body, orient="vertical", command=self.agg_tree.yview)
        self.agg_tree.configure(yscrollcommand=agg_scroll.set)
        self.agg_tree.pack(side="left", fill="both", expand=True)
        agg_scroll.pack(side="left", fill="y")
        agg_side = ttk.Frame(agg_body, style="Card.TFrame")
        agg_side.pack(side="left", fill="y", padx=8)
        self.agg_func = tk.StringVar(value="합계(SUM)")
        self.agg_func_combo = ttk.Combobox(agg_side, textvariable=self.agg_func, width=18, state="readonly",
                                           values=list(core.KEEP_AGG_FUNCS.keys()))
        self.agg_func_combo.pack(pady=(0, 6))
        ttk.Button(agg_side, text="선택 컬럼에 적용", command=self._apply_agg).pack(fill="x")
        ttk.Button(agg_side, text="모두 제외로", command=self._clear_agg).pack(fill="x", pady=6)

        # (b) append: 여러 레이어
        self.merge_multi = ttk.Frame(body, style="Card.TFrame")
        ttk.Button(self.merge_multi, text="이어붙이기 실행", style="Accent.TButton",
                   command=self.run_merge_append).pack(side="bottom", anchor="e", pady=(10, 0))
        ttk.Label(self.merge_multi, text="이어붙일 레이어 (2개 이상 선택 · 선택 안 하면 전체)",
                  style="Card.TLabel").pack(anchor="w")
        self.merge_list = self._layer_listbox(self.merge_multi)
        ttk.Label(self.merge_multi,
                  text="여러 SHP/GPKG를 하나의 레이어로 이어붙입니다. 컬럼이 서로 달라도 -addfields로 합쳐집니다.",
                  style="Muted.TLabel").pack(anchor="w", pady=4)

        self._switch_merge_mode()

    def _switch_merge_mode(self) -> None:
        mode = self.merge_mode.get()
        self.merge_single.pack_forget()
        self.merge_multi.pack_forget()
        if mode == "append":
            self.merge_multi.pack(fill="both", expand=True)
            return
        self.merge_single.pack(fill="both", expand=True)
        if mode == "dissolve":
            self.merge_hint.configure(
                text="같은 값의 도형을 하나로 합칩니다. 바깥 윤곽선은 그대로 두고 인접 도형의 '공유 내부 경계'만 "
                     "지웁니다(경계선 깨짐 방지를 위해 병합 전 도형 복구 자동 수행).")
            self.agg_func_combo.configure(values=list(core.DISSOLVE_AGG_FUNCS.keys()))
        else:
            self.merge_hint.configure(
                text="도형은 하나도 안 바뀝니다(개수·모양·경계선 그대로). 같은 기준값끼리 묶어 면적 합계·집계값만 "
                     "새 컬럼으로 붙입니다. 정확한 ㎡를 원하면 위 '목표 EPSG 통일'에 5186을 넣으세요.")
            self.agg_func_combo.configure(values=list(core.KEEP_AGG_FUNCS.keys()))

    def _apply_agg(self) -> None:
        func = self.agg_func.get()
        for item in self.agg_tree.selection():
            self.agg_tree.set(item, "func", func)

    def _clear_agg(self) -> None:
        for item in self.agg_tree.get_children():
            self.agg_tree.set(item, "func", "제외")

    def _agg_map(self) -> dict[str, str]:
        table = core.KEEP_AGG_FUNCS if self.merge_mode.get() == "keep" else core.DISSOLVE_AGG_FUNCS
        result: dict[str, str] = {}
        for item in self.agg_tree.get_children():
            column = self.agg_tree.item(item, "text")
            func = table.get(self.agg_tree.set(item, "func"))
            if func:
                result[column] = func
        return result

    # ────────────────────────── 탭 3: 분할 ──────────────────────────
    def _build_split_tab(self) -> None:
        tab = self.tab_split
        left = ttk.Frame(tab, style="Card.TFrame")
        left.pack(side="left", fill="both", expand=True)
        right = ttk.Frame(tab, style="Card.TFrame")
        right.pack(side="left", fill="both", expand=True, padx=(16, 0))

        ttk.Label(left, text="분할할 레이어 (여러 개 선택 가능 · 선택 안 하면 전체)", style="Card.TLabel").pack(anchor="w")
        self.split_list = self._layer_listbox(left, height=7)
        self.split_list.bind("<<ListboxSelect>>", lambda _e: self._reload_split_columns())

        row = ttk.Frame(left, style="Card.TFrame")
        row.pack(fill="x", pady=(8, 0))
        ttk.Label(row, text="분할 기준 컬럼", style="Card.TLabel").pack(side="left")
        self.split_column = tk.StringVar()
        self.split_column_combo = ttk.Combobox(row, textvariable=self.split_column, width=22, state="readonly")
        self.split_column_combo.pack(side="left", padx=6)
        ttk.Button(row, text="고유값 불러오기", command=self.load_split_values).pack(side="left")

        opts = ttk.Frame(left, style="Card.TFrame")
        opts.pack(fill="x", pady=(8, 0))
        ttk.Label(opts, text="목표 EPSG(선택)", style="Card.TLabel").pack(side="left")
        self.split_epsg = tk.StringVar(value="")
        ttk.Entry(opts, textvariable=self.split_epsg, width=10).pack(side="left", padx=(6, 14))
        ttk.Label(opts, text="저장 형식", style="Card.TLabel").pack(side="left")
        self.split_format = tk.StringVar(value="SHP")
        ttk.Radiobutton(opts, text="SHP", value="SHP", variable=self.split_format).pack(side="left", padx=4)
        ttk.Radiobutton(opts, text="GPKG", value="GPKG", variable=self.split_format).pack(side="left", padx=4)

        opts2 = ttk.Frame(left, style="Card.TFrame")
        opts2.pack(fill="x", pady=(4, 0))
        self.split_contains = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts2, text="포함 조건으로 분할(%값% 방식)", variable=self.split_contains).pack(side="left")
        self.split_makevalid = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts2, text="도형 유효화(-makevalid)", variable=self.split_makevalid).pack(side="left", padx=12)

        ttk.Button(right, text="분할 실행", style="Accent.TButton",
                   command=self.run_split).pack(side="bottom", anchor="e", pady=(10, 0))
        ttk.Label(right, text="분할할 값 (한 줄에 하나 · 비우면 감지된 전체 고유값)", style="Card.TLabel").pack(anchor="w")
        text_wrap = ttk.Frame(right, style="Card.TFrame")
        text_wrap.pack(fill="both", expand=True, pady=(4, 0))
        self.split_values = tk.Text(text_wrap, height=8, bg=LOGBG, fg=INK, relief="flat",
                                    font=(UI_FONT, 9), insertbackground=INK)
        scroll = ttk.Scrollbar(text_wrap, orient="vertical", command=self.split_values.yview)
        self.split_values.configure(yscrollcommand=scroll.set)
        self.split_values.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    # ────────────────────────── 탭 4: 코드 결합 ──────────────────────────
    def _build_join_tab(self) -> None:
        tab = self.tab_join
        ttk.Button(tab, text="코드 결합 실행", style="Accent.TButton",
                   command=self.run_join).pack(side="bottom", anchor="e", pady=(10, 0))
        ttk.Label(tab, text="속성의 MNUM 같은 컬럼에서 substr로 코드를 뽑아 용도지역 코드표(CSV)를 결합합니다. "
                            "원본 DBF는 건드리지 않고 결합된 새 SHP/GPKG를 만듭니다.",
                  style="Muted.TLabel", wraplength=1080, justify="left").pack(anchor="w")

        row1 = ttk.Frame(tab, style="Card.TFrame")
        row1.pack(fill="x", pady=(8, 4))
        ttk.Label(row1, text="대상 레이어", style="Card.TLabel").pack(side="left")
        self.join_layer = tk.StringVar()
        self.join_layer_combo = ttk.Combobox(row1, textvariable=self.join_layer, width=40, state="readonly")
        self.join_layer_combo.pack(side="left", padx=6)
        self.join_layer_combo.bind("<<ComboboxSelected>>", lambda _e: self._reload_join_columns())
        ttk.Label(row1, text="코드 컬럼", style="Card.TLabel").pack(side="left", padx=(12, 0))
        self.join_mnum = tk.StringVar()
        self.join_mnum_combo = ttk.Combobox(row1, textvariable=self.join_mnum, width=18, state="readonly")
        self.join_mnum_combo.pack(side="left", padx=6)
        ttk.Label(row1, text="시작", style="Card.TLabel").pack(side="left", padx=(12, 2))
        self.join_start = tk.IntVar(value=21)
        ttk.Spinbox(row1, from_=1, to=200, textvariable=self.join_start, width=5).pack(side="left")
        ttk.Label(row1, text="길이", style="Card.TLabel").pack(side="left", padx=(8, 2))
        self.join_len = tk.IntVar(value=6)
        ttk.Spinbox(row1, from_=1, to=50, textvariable=self.join_len, width=5).pack(side="left")
        ttk.Button(row1, text="코드 미리보기", command=self.preview_join_codes).pack(side="left", padx=10)

        self.join_code_label = ttk.Label(tab, text="추출된 코드 예시: -", style="Muted.TLabel")
        self.join_code_label.pack(anchor="w")

        # 코드표 CSV도 끌어다 놓을 수 있게(버튼으로 고르는 방법도 그대로 둡니다)
        self.csv_drop = tk.Label(
            tab,
            text=("📄  코드표 CSV를 여기로 끌어다 놓으세요   ·   버튼으로 골라도 됩니다"
                  if HAS_DND else "아래 [코드표 CSV 선택] 버튼을 눌러 주세요"),
            bg=DROP_BG, fg=MUTED, font=(UI_FONT, 10), pady=10, relief="flat",
        )
        self.csv_drop.pack(fill="x", pady=(8, 0))
        self._register_drop_csv(self.csv_drop)

        row2 = ttk.Frame(tab, style="Card.TFrame")
        row2.pack(fill="x", pady=(6, 4))
        ttk.Button(row2, text="코드표 CSV 선택", command=self.pick_code_csv).pack(side="left")
        self.join_csv_label = ttk.Label(row2, text="선택된 코드표 없음", style="Card.TLabel")
        self.join_csv_label.pack(side="left", padx=10)
        ttk.Label(row2, text="인코딩", style="Card.TLabel").pack(side="left", padx=(12, 2))
        self.join_csv_enc = tk.StringVar(value="UTF-8-SIG")
        combo = ttk.Combobox(row2, textvariable=self.join_csv_enc, values=core.CSV_ENCODINGS, width=12, state="readonly")
        combo.pack(side="left")
        combo.bind("<<ComboboxSelected>>", lambda _e: self._reload_code_table())

        row3 = ttk.Frame(tab, style="Card.TFrame")
        row3.pack(fill="x", pady=4)
        ttk.Label(row3, text="코드표 키 컬럼", style="Card.TLabel").pack(side="left")
        self.join_key = tk.StringVar()
        self.join_key_combo = ttk.Combobox(row3, textvariable=self.join_key, width=22, state="readonly")
        self.join_key_combo.pack(side="left", padx=6)
        ttk.Button(row3, text="매칭률 확인", command=self.check_match_rate).pack(side="left", padx=6)
        ttk.Label(row3, text="저장 형식", style="Card.TLabel").pack(side="left", padx=(16, 2))
        self.join_format = tk.StringVar(value="GPKG")
        ttk.Radiobutton(row3, text="SHP", value="SHP", variable=self.join_format).pack(side="left", padx=4)
        ttk.Radiobutton(row3, text="GPKG", value="GPKG", variable=self.join_format).pack(side="left", padx=4)
        ttk.Label(row3, text="(SHP은 필드명이 10바이트로 잘립니다 — 한글 컬럼이면 GPKG 권장)",
                  style="Muted.TLabel").pack(side="left", padx=8)

        body = ttk.Frame(tab, style="Card.TFrame")
        body.pack(fill="both", expand=True, pady=(6, 0))
        left = ttk.Frame(body, style="Card.TFrame")
        left.pack(side="left", fill="both", expand=True)
        ttk.Label(left, text="결합할 코드표 컬럼 (선택 안 하면 키 외 전체)", style="Card.TLabel").pack(anchor="w")
        self.join_values = tk.Listbox(left, selectmode="extended", height=6, bg=CARD, fg=INK,
                                      relief="flat", highlightthickness=1, highlightbackground=LINE,
                                      font=(UI_FONT, 9), exportselection=False)
        self.join_values.pack(fill="both", expand=True, pady=(4, 0))

        right = ttk.Frame(body, style="Card.TFrame")
        right.pack(side="left", fill="both", expand=True, padx=(12, 0))
        ttk.Label(right, text="코드표 미리보기", style="Card.TLabel").pack(anchor="w")
        self.join_preview = ttk.Treeview(right, show="headings", height=6)
        self.join_preview.pack(fill="both", expand=True, pady=(4, 0))

    # ────────────────────────── 탭 5: 속성/인코딩 확인 ──────────────────────────
    def _build_preview_tab(self) -> None:
        tab = self.tab_preview
        row = ttk.Frame(tab, style="Card.TFrame")
        row.pack(fill="x")
        ttk.Label(row, text="레이어", style="Card.TLabel").pack(side="left")
        self.preview_layer = tk.StringVar()
        self.preview_combo = ttk.Combobox(row, textvariable=self.preview_layer, width=44, state="readonly")
        self.preview_combo.pack(side="left", padx=6)
        ttk.Button(row, text="속성·인코딩 확인", command=self.run_preview).pack(side="left")
        ttk.Label(row, text="'깨짐 의심 점수'가 가장 낮은 인코딩이 보통 정답입니다.",
                  style="Muted.TLabel").pack(side="left", padx=12)

        self.enc_tree = ttk.Treeview(tab, columns=("score", "cols"), show="tree headings", height=4)
        self.enc_tree.heading("#0", text="인코딩")
        self.enc_tree.heading("score", text="깨짐 의심 점수")
        self.enc_tree.heading("cols", text="컬럼")
        self.enc_tree.column("#0", width=120)
        self.enc_tree.column("score", width=110, anchor="center")
        self.enc_tree.column("cols", width=760, anchor="w")
        self.enc_tree.pack(fill="x", pady=(8, 8))

        ttk.Label(tab, text="속성 미리보기(앞 30행)", style="Card.TLabel").pack(anchor="w")
        wrap = ttk.Frame(tab, style="Card.TFrame")
        wrap.pack(fill="both", expand=True, pady=(4, 0))
        self.attr_tree = ttk.Treeview(wrap, show="headings", height=7)
        xscroll = ttk.Scrollbar(wrap, orient="horizontal", command=self.attr_tree.xview)
        yscroll = ttk.Scrollbar(wrap, orient="vertical", command=self.attr_tree.yview)
        self.attr_tree.configure(xscrollcommand=xscroll.set, yscrollcommand=yscroll.set)
        self.attr_tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="we")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)

    # ────────────────────────── 공통 위젯/헬퍼 ──────────────────────────
    def _layer_listbox(self, parent: tk.Misc, height: int = 6) -> tk.Listbox:
        wrap = ttk.Frame(parent, style="Card.TFrame")
        wrap.pack(fill="both", expand=True, pady=(4, 0))
        listbox = tk.Listbox(wrap, selectmode="extended", height=height, bg=CARD, fg=INK,
                             relief="flat", highlightthickness=1, highlightbackground=LINE,
                             font=(UI_FONT, 9), exportselection=False)
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=listbox.yview)
        listbox.configure(yscrollcommand=scroll.set)
        listbox.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        return listbox

    # ────────────────────────── 드래그앤드롭 ──────────────────────────
    def _register_drop(self, widget: tk.Misc) -> None:
        """위젯에 파일 끌어놓기를 붙입니다(tkinterdnd2가 있을 때만)."""
        if not HAS_DND:
            return
        try:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self._on_drop)
            widget.dnd_bind("<<DragEnter>>", self._on_drag_enter)
            widget.dnd_bind("<<DragLeave>>", self._on_drag_leave)
        except Exception:
            pass

    def _on_drag_enter(self, _event) -> None:
        self.drop_zone.configure(bg=DROP_HOVER, text="📥  놓으면 바로 읽습니다")

    def _on_drag_leave(self, _event) -> None:
        self.drop_zone.configure(bg=DROP_BG,
                                 text="📂  여기로 파일을 끌어다 놓으세요   ·   .shp / .gpkg / .zip / 폴더 / 코드표 .csv")

    def _paths_from_event(self, event) -> list[Path]:
        """끌어놓기 이벤트에서 실제 경로만 뽑아냅니다(공백 있는 경로도 안전하게)."""
        try:
            items = self.tk.splitlist(event.data)
        except Exception:
            items = [part for part in str(event.data).split() if part]
        paths = [Path(item.strip("{}")) for item in items if str(item).strip()]
        return [path for path in paths if path.exists()]

    def _on_drop(self, event) -> None:
        self._on_drag_leave(event)
        paths = self._paths_from_event(event)
        if not paths:
            self.log("끌어놓은 항목에서 파일을 찾지 못했습니다.")
            return
        # CSV는 지도 레이어가 아니라 '코드표'이므로 큰 상자에 놓아도 코드 결합 탭으로 보냅니다.
        csvs = [path for path in paths if path.suffix.lower() == ".csv"]
        others = [path for path in paths if path.suffix.lower() != ".csv"]
        if csvs:
            self.log(f"끌어놓기: 코드표 CSV로 인식 — {csvs[0].name}"
                     + (f" (CSV {len(csvs)}개 중 첫 번째)" if len(csvs) > 1 else ""))
            self.load_code_csv(csvs[0])
        if others:
            self.log(f"끌어놓기: {len(others)}개 항목")
            self.add_paths(others)

    # ── 코드표 CSV 전용 드롭 영역(코드 결합 탭) ──────────────────────────
    def _register_drop_csv(self, widget: tk.Misc) -> None:
        if not HAS_DND:
            return
        try:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self._on_drop_csv)
            widget.dnd_bind("<<DragEnter>>", lambda _e: self.csv_drop.configure(
                bg=DROP_HOVER, text="📥  놓으면 코드표로 읽습니다"))
            widget.dnd_bind("<<DragLeave>>", lambda _e: self._reset_csv_drop())
        except Exception:
            pass

    def _reset_csv_drop(self, filename: str | None = None) -> None:
        if filename:
            self.csv_drop.configure(bg=DROP_BG, text=f"📄  코드표: {filename}   ·   다른 파일을 놓으면 바뀝니다")
        else:
            self.csv_drop.configure(
                bg=DROP_BG, text="📄  코드표 CSV를 여기로 끌어다 놓으세요   ·   버튼으로 골라도 됩니다")

    def _on_drop_csv(self, event) -> None:
        self._reset_csv_drop()
        paths = self._paths_from_event(event)
        csvs = [path for path in paths if path.suffix.lower() in (".csv", ".txt")]
        if not csvs:
            self.log("여기에는 코드표 CSV를 놓아 주세요(.csv). 지도 파일은 위쪽 큰 상자에 놓으면 됩니다.")
            return
        self.load_code_csv(csvs[0])

    def log(self, message: str) -> None:
        self.log_queue.put(message)

    def _drain_log(self) -> None:
        try:
            while True:
                message = self.log_queue.get_nowait()
                self.log_text.insert("end", message.rstrip() + "\n")
                self.log_text.see("end")
        except queue.Empty:
            pass
        except tk.TclError:  # 창이 닫히는 중
            return
        self._log_job = self.after(120, self._drain_log)

    def _on_close(self) -> None:
        """창을 닫을 때 예약된 로그 갱신을 정리하고 종료합니다."""
        if getattr(self, "_log_job", None):
            try:
                self.after_cancel(self._log_job)
            except tk.TclError:
                pass
            self._log_job = None
        self.destroy()

    def set_status(self, text: str, percent: float | None = None) -> None:
        def apply() -> None:
            self.status.configure(text=text)
            if percent is not None:
                self.progress["value"] = max(0, min(100, percent))
        self.after(0, apply)

    def run_task(self, name: str, func) -> None:
        """무거운 작업을 백그라운드 스레드로 돌립니다(창이 멈추지 않게)."""
        if self.busy:
            messagebox.showinfo("작업 중", "이전 작업이 아직 끝나지 않았습니다.")
            return
        ogr2ogr, _ = core.gdals()
        if not ogr2ogr:
            self.show_gdal_help()
            return
        self.busy = True
        core.CANCEL.reset()
        core.PROGRESS.bind(self._on_progress)
        self.job_started = time.time()
        self.after(0, lambda: self.cancel_button.configure(state="normal"))
        self.set_status(f"{name} 실행 중…", 1)
        self.log(f"\n===== {name} 시작 =====")

        def worker() -> None:
            try:
                func()
            except core.Cancelled:
                self.log("⏹ 사용자가 작업을 취소했습니다. 결과 폴더에 미완성 파일이 남아 있을 수 있습니다.")
                self.after(0, lambda: messagebox.showinfo("취소됨", f"{name}을(를) 취소했습니다."))
            except Exception as exc:  # 사용자에게 원인을 그대로 보여줍니다
                self.log("[오류] " + "".join(traceback.format_exception_only(type(exc), exc)).strip())
                self.log(traceback.format_exc())
                self.after(0, lambda: messagebox.showerror("오류", f"{name} 중 오류가 발생했습니다.\n\n{exc}"))
            finally:
                self.busy = False
                core.PROGRESS.bind(None)
                self.after(0, lambda: self.cancel_button.configure(state="disabled"))
                took = time.time() - getattr(self, "job_started", time.time())
                self.set_status(f"대기 중 (직전 작업 {self._pretty_time(took)})", 100)
                self.log(f"===== {name} 종료 · {self._pretty_time(took)} 소요 =====")

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _pretty_time(seconds: float) -> str:
        if seconds < 60:
            return f"{seconds:.0f}초"
        return f"{int(seconds // 60)}분 {int(seconds % 60)}초"

    def _on_progress(self, percent: float, label: str, step: int, total: int) -> None:
        """GDAL이 알려준 진행률을 상태줄·진행 막대에 옮깁니다(작업 스레드에서 호출됨)."""
        elapsed = time.time() - getattr(self, "job_started", time.time())
        eta = ""
        if percent >= 3:
            remain = elapsed * (100 - percent) / percent
            if remain >= 3:
                eta = f" · 남은 시간 약 {self._pretty_time(remain)}"
        head = label or "작업"
        step_text = f" ({step}/{total}단계)" if total > 1 else ""
        self.set_status(f"{head}{step_text} · {percent:.0f}% · 경과 {self._pretty_time(elapsed)}{eta}", percent)

    def cancel_job(self) -> None:
        """실행 중인 GDAL 프로세스를 즉시 종료합니다."""
        if not self.busy:
            return
        self.log("⏹ 취소 요청 — 실행 중인 GDAL 작업을 중단합니다…")
        self.set_status("취소하는 중…", None)
        self.cancel_button.configure(state="disabled")
        core.CANCEL.cancel()

    def selected_layers(self, listbox: tk.Listbox) -> list[core.LayerInfo]:
        indexes = listbox.curselection()
        if not indexes:
            return list(self.layers)
        return [self.layers[i] for i in indexes if i < len(self.layers)]

    def layer_by_label(self, label: str) -> core.LayerInfo | None:
        for layer in self.layers:
            if str(layer) == label:
                return layer
        return None

    def out_base(self) -> Path:
        base = Path(self.out_dir.get().strip() or str(default_out_dir()))
        base.mkdir(parents=True, exist_ok=True)
        return base

    # ────────────────────────── 파일/설정 액션 ──────────────────────────
    def pick_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="SHP · GPKG · ZIP 선택",
            filetypes=[("GIS 파일", "*.shp *.gpkg *.zip"), ("Shapefile", "*.shp"),
                       ("GeoPackage", "*.gpkg"), ("ZIP", "*.zip"), ("모든 파일", "*.*")])
        if paths:
            self.add_paths([Path(p) for p in paths])

    def pick_folder(self) -> None:
        folder = filedialog.askdirectory(title="SHP/GPKG가 들어있는 폴더 선택")
        if folder:
            self.add_paths([Path(folder)])

    def add_paths(self, paths: list[Path]) -> None:
        self.set_status("파일 읽는 중…", 20)
        try:
            found = core.layers_from_paths(paths, self.work_dir)
        except Exception as exc:
            messagebox.showerror("읽기 실패", str(exc))
            self.set_status("대기 중", 0)
            return
        known = {f"{layer.path}|{layer.sublayer or ''}" for layer in self.layers}
        added = [layer for layer in found if f"{layer.path}|{layer.sublayer or ''}" not in known]
        self.layers.extend(added)
        self.refresh_layer_views()
        self.log(f"레이어 {len(added)}개 추가 (전체 {len(self.layers)}개)")
        if not added:
            self.log("추가된 레이어가 없습니다. SHP/GPKG가 들어있는지 확인하세요.")
        self.set_status("대기 중", 0)

    def clear_layers(self) -> None:
        self.layers = []
        self.refresh_layer_views()
        self.log("레이어 목록을 비웠습니다.")

    def refresh_layer_views(self) -> None:
        self.layer_tree.delete(*self.layer_tree.get_children())
        for layer in self.layers:
            self.layer_tree.insert(
                "", "end", text=layer.name,
                values=(layer.kind,
                        ("있음" if layer.has_prj else "없음") if layer.kind == "SHP" else "-",
                        layer.cpg or "-",
                        str(layer.path)))
        labels = [str(layer) for layer in self.layers]
        for listbox in (self.convert_list, self.merge_list, self.split_list):
            listbox.delete(0, "end")
            for label in labels:
                listbox.insert("end", label)
        for combo in (self.merge_layer_combo, self.join_layer_combo, self.preview_combo):
            combo.configure(values=labels)
        if labels:
            if not self.merge_layer.get():
                self.merge_layer.set(labels[0])
                self._reload_merge_columns()
            if not self.join_layer.get():
                self.join_layer.set(labels[0])
                self._reload_join_columns()
            if not self.preview_layer.get():
                self.preview_layer.set(labels[0])
            self.split_list.selection_set(0)
            self._reload_split_columns()

    def pick_out_dir(self) -> None:
        folder = filedialog.askdirectory(title="결과를 저장할 폴더")
        if folder:
            self.out_dir.set(folder)

    def pick_gdal_bin(self) -> None:
        folder = filedialog.askdirectory(title="ogr2ogr.exe가 있는 bin 폴더 선택")
        if folder:
            self.gdal_bin.set(folder)
            core.set_gdal_bin_hint(folder)
            self.refresh_gdal_status()

    def refresh_gdal_status(self) -> None:
        core.set_gdal_bin_hint(self.gdal_bin.get())
        ogr2ogr, ogrinfo = core.gdals()
        if ogr2ogr and ogrinfo:
            self.gdal_label.configure(text=f"✅ GDAL 연결됨\n{ogr2ogr}", foreground="#2f7d4f")
        else:
            self.gdal_label.configure(
                text="❌ GDAL(ogr2ogr)을 찾지 못했습니다.\nQGIS 설치 후 bin 폴더(예: C:\\Program Files\\QGIS 3.30.3\\bin)를 "
                     "위 칸에 넣고 [GDAL 다시 찾기]를 누르세요.", foreground="#b4553f")

    def show_gdal_help(self) -> None:
        """GDAL(QGIS)이 없을 때 뜨는 안내 창 — 사원 배포용."""
        win = tk.Toplevel(self)
        win.title("GDAL(QGIS)이 필요합니다")
        win.configure(bg=CARD)
        win.transient(self)
        win.resizable(False, False)
        wrap = ttk.Frame(win, style="Card.TFrame", padding=18)
        wrap.pack(fill="both", expand=True)

        ttk.Label(wrap, text="좌표변환·병합·분할을 하려면 GDAL이 필요합니다", style="Head.TLabel").pack(anchor="w")
        ttk.Label(wrap, style="Muted.TLabel", justify="left", wraplength=520,
                  text="이 앱은 QGIS에 같이 들어있는 GDAL(ogr2ogr.exe)을 불러다 씁니다.\n"
                       "QGIS를 설치하면 자동으로 잡히고, 그 뒤로는 신경 쓸 일이 없습니다.").pack(anchor="w", pady=(6, 12))

        steps = ("① 아래 [QGIS 내려받기]를 눌러 QGIS 설치 (기본값으로 다음만 눌러도 됩니다)\n"
                 "② 설치가 끝나면 이 앱에서 [GDAL 다시 찾기] 클릭\n"
                 "③ 오른쪽 위에 '✅ GDAL 연결됨'이 뜨면 준비 끝\n\n"
                 "이미 QGIS가 있는데 못 찾는 경우\n"
                 "  → 'GDAL/QGIS bin' 칸에 아래 같은 폴더를 넣고 [GDAL 다시 찾기] → [설정 저장]\n"
                 "     C:\\Program Files\\QGIS 3.30.3\\bin\n"
                 "     C:\\OSGeo4W\\bin")
        box = tk.Label(wrap, text=steps, bg=LOGBG, fg=INK, font=(UI_FONT, 9), justify="left",
                       anchor="w", padx=12, pady=10)
        box.pack(fill="x")

        buttons = ttk.Frame(wrap, style="Card.TFrame")
        buttons.pack(fill="x", pady=(14, 0))
        ttk.Button(buttons, text="QGIS 내려받기", style="Accent.TButton",
                   command=lambda: webbrowser.open(QGIS_URL)).pack(side="left")
        ttk.Button(buttons, text="bin 폴더 직접 지정",
                   command=lambda: (win.destroy(), self.pick_gdal_bin())).pack(side="left", padx=6)
        ttk.Button(buttons, text="다시 찾기",
                   command=lambda: (self.refresh_gdal_status(), self._close_if_found(win))).pack(side="left")
        ttk.Button(buttons, text="닫기", command=win.destroy).pack(side="right")

        win.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - win.winfo_width()) // 2
        y = self.winfo_rooty() + 120
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _close_if_found(self, win: tk.Toplevel) -> None:
        ogr2ogr, _ = core.gdals()
        if ogr2ogr:
            self.log(f"GDAL 연결됨: {ogr2ogr}")
            win.destroy()
        else:
            messagebox.showwarning("아직 못 찾음", "여전히 ogr2ogr을 찾지 못했습니다.\n"
                                                "QGIS 설치를 마쳤는지, 또는 bin 폴더를 직접 지정했는지 확인해 주세요.", parent=win)

    def save_settings(self) -> None:
        self.config_data.update({
            "GDAL_BIN": self.gdal_bin.get().strip(),
            "OUT_DIR": self.out_dir.get().strip(),
            "IN_ENC": self.in_enc.get(),
            "OUT_ENC": self.out_enc.get(),
        })
        save_config(self.config_data)
        core.set_gdal_bin_hint(self.gdal_bin.get())
        self.log(f"설정을 저장했습니다: {CONFIG_PATH}")
        messagebox.showinfo("저장됨", f"설정을 저장했습니다.\n{CONFIG_PATH}")

    def open_result_dir(self) -> None:
        open_folder(self.last_out_dir or self.out_base())

    def save_log(self) -> None:
        path = filedialog.asksaveasfilename(defaultextension=".txt", initialfile="shp_tool_log.txt",
                                            filetypes=[("텍스트", "*.txt")])
        if path:
            Path(path).write_text(self.log_text.get("1.0", "end"), encoding="utf-8")
            self.log(f"로그 저장: {path}")

    def finish(self, out_dir: Path, message: str) -> None:
        self.last_out_dir = out_dir
        self.log(message)
        self.log(f"결과 폴더: {out_dir}")
        core.cleanup_temp(out_dir)
        self.after(0, lambda: self._done_dialog(out_dir, message))

    def _done_dialog(self, out_dir: Path, message: str) -> None:
        if messagebox.askyesno("완료", f"{message}\n\n결과 폴더를 열까요?\n{out_dir}"):
            open_folder(out_dir)

    # ────────────────────────── 컬럼 로딩 ──────────────────────────
    def _reload_merge_columns(self) -> None:
        layer = self.layer_by_label(self.merge_layer.get())
        columns = core.columns_for_layer(layer, self.in_enc.get()) if layer else []
        self.merge_column_combo.configure(values=columns)
        if columns and self.merge_column.get() not in columns:
            self.merge_column.set(columns[0])
        self._reload_agg_table()

    def _reload_agg_table(self) -> None:
        layer = self.layer_by_label(self.merge_layer.get())
        columns = core.columns_for_layer(layer, self.in_enc.get()) if layer else []
        self.agg_tree.delete(*self.agg_tree.get_children())
        for column in columns:
            if column == self.merge_column.get():
                continue
            self.agg_tree.insert("", "end", text=column, values=("제외",))

    def _reload_split_columns(self) -> None:
        layers = self.selected_layers(self.split_list)
        columns = core.columns_for_layer(layers[0], self.in_enc.get()) if layers else []
        self.split_column_combo.configure(values=columns)
        if columns and self.split_column.get() not in columns:
            self.split_column.set(columns[0])

    def _reload_join_columns(self) -> None:
        layer = self.layer_by_label(self.join_layer.get())
        columns = core.columns_for_layer(layer, self.in_enc.get()) if layer else []
        self.join_mnum_combo.configure(values=columns)
        if columns:
            default = next((c for c in columns if c.lower() == "mnum"), columns[0])
            if self.join_mnum.get() not in columns:
                self.join_mnum.set(default)

    # ────────────────────────── 실행: 좌표계 변환 ──────────────────────────
    def run_convert(self) -> None:
        layers = self.selected_layers(self.convert_list)
        if not layers:
            messagebox.showwarning("레이어 없음", "먼저 파일을 불러오세요.")
            return
        target = core.COMMON_EPSG[self.convert_target.get()]
        if target == "custom":
            target = self.convert_custom.get().strip()
            if not target.isdigit():
                messagebox.showwarning("EPSG 확인", "직접 입력 EPSG에 숫자만 넣어 주세요(예: 5186).")
                return
        source = self.convert_source.get().strip() or None
        fmt = self.convert_format.get()
        safe = self.convert_safe.get()
        makevalid = self.convert_makevalid.get()
        add_area = self.convert_area.get()
        decimals = int(self.convert_area_dec.get())
        in_enc, out_enc = self.in_enc.get(), self.out_enc.get()

        if add_area and target == "4326":
            self.log("⚠️ EPSG:4326(경위도)은 면적이 ㎡가 아니라 제곱도로 계산됩니다. 5186 등 미터 좌표계를 쓰세요.")

        def job() -> None:
            out_dir = stamped_dir(self.out_base(), "변환")
            results: list[Path] = []
            dropped = False
            # 레이어당 GDAL 명령 수: 안전 변환 2 + 면적 2(GPKG 경유 저장) — 어긋나도 자동 보정됩니다.
            per_layer = (2 if safe else 1) + (2 if add_area else 0)
            core.PROGRESS.begin(len(layers) * per_layer, "좌표계 변환")
            for index, layer in enumerate(layers, start=1):
                self.log(f"\n--- {layer.name} ({index}/{len(layers)}) ---")
                before = core.ogr_layer_stats(layer.path, in_enc, layer.sublayer)
                out_path = core.output_dataset_path(out_dir, f"{layer.name}_{target}", fmt)
                if safe:
                    ok, out_path, counts, _log = core.convert_layer_safe(
                        layer, out_path, target, source, fmt, in_enc, out_enc)
                    repaired = counts.get("복구후")
                else:
                    ok, out_path, _log = core.convert_layer(
                        layer, out_path, target, source, fmt, in_enc, out_enc, makevalid)
                    repaired = None
                if not ok:
                    self.log(f"❌ {layer.name}: 변환 실패")
                    continue
                if add_area:
                    area_ok, out_path, _ = core.add_area_column(out_path, fmt, out_enc, decimals)
                    self.log("면적 컬럼 추가 " + ("완료(area_m2)" if area_ok else "실패 — 로그 확인"))
                results.append(out_path)
                after = core.ogr_layer_stats(out_path)
                before_n, after_n = before.get("features"), after.get("features")
                lost = (before_n - after_n) if isinstance(before_n, int) and isinstance(after_n, int) else None
                if lost:
                    dropped = True
                summary = f"변환 전 {before_n} → " + (f"복구후 {repaired} → " if repaired is not None else "") + f"변환 후 {after_n}"
                self.log(f"✅ {out_path.name} | {summary} | 손실 " + (f"❌ -{lost}" if lost else "✅ 0"))
                self.log(f"   범위: {before.get('extent', '-')}  →  {after.get('extent', '-')}")
            if dropped:
                self.log("⚠️ 사라진 피처가 있습니다. 보통 (1) 원본 좌표계 오판 또는 (2) 불량 도형이 원인입니다. "
                         "'원본 EPSG 강제 지정'에 실제 좌표계(예: 5174)를 넣고 다시 시도하세요.")
            if results:
                self.finish(out_dir, f"{len(results)}개 레이어 변환 완료")
            else:
                self.log("❌ 변환 결과가 없습니다. 위 로그를 확인하세요.")

        self.run_task("좌표계 변환", job)

    # ────────────────────────── 실행: 병합 ──────────────────────────
    def run_merge_single(self) -> None:
        layer = self.layer_by_label(self.merge_layer.get())
        if not layer:
            messagebox.showwarning("레이어 없음", "대상 레이어를 고르세요.")
            return
        column = self.merge_column.get()
        if not column:
            messagebox.showwarning("컬럼 없음", "기준 컬럼을 고르세요.")
            return
        mode = self.merge_mode.get()
        agg_map = self._agg_map()
        epsg = self.merge_epsg.get().strip() or None
        fmt = self.merge_format.get()
        add_area = self.merge_area.get()
        decimals = int(self.merge_area_dec.get())
        in_enc, out_enc = self.in_enc.get(), self.out_enc.get()

        if mode == "keep" and not agg_map and not add_area:
            messagebox.showwarning("선택 필요", "'면적 컬럼 추가'를 켜거나, 집계할 속성을 1개 이상 고르세요.")
            return

        def job() -> None:
            out_dir = stamped_dir(self.out_base(), "병합")
            if mode == "dissolve":
                core.PROGRESS.begin(3 if add_area else 2, "도형 병합")
                out_path = core.output_dataset_path(out_dir, f"{layer.name}_dissolved_by_{column}", fmt)
                before_n = core.ogr_layer_stats(layer.path, in_enc, layer.sublayer).get("features")
                ok, out_path, _log = core.dissolve_one_layer(
                    layer, column, out_path, fmt, in_enc, epsg, agg_map, out_enc)
                if ok and add_area:
                    area_ok, out_path, _ = core.add_area_column(out_path, fmt, out_enc, decimals)
                    self.log("면적 컬럼 추가 " + ("완료(area_m2)" if area_ok else "실패 — 로그 확인"))
                if not ok:
                    self.log("❌ 병합 실패 — 로그를 확인하세요.")
                    return
                after_n = core.ogr_layer_stats(out_path).get("features")
                self.log(f"✅ {out_path.name} | 피처 {before_n} → {after_n} (같은 '{column}' 값끼리 하나로 합쳐짐)")
                self.finish(out_dir, "컬럼값 기준 병합 완료")
                return

            core.PROGRESS.begin(3, "속성 합치기")
            out_path = core.output_dataset_path(out_dir, f"{layer.name}_agg_by_{column}", fmt)
            before_n = core.ogr_layer_stats(layer.path, in_enc, layer.sublayer).get("features")
            ok, out_path, _log, new_cols = core.aggregate_keep_geometry(
                layer, column, out_path, fmt, in_enc, epsg, agg_map, add_area, decimals, out_enc)
            if not ok:
                self.log("❌ 실패 — 로그를 확인하세요.")
                return
            after_n = core.ogr_layer_stats(out_path).get("features")
            self.log(f"✅ {out_path.name} | 추가된 컬럼: {', '.join(new_cols) if new_cols else '없음'}")
            if isinstance(before_n, int) and isinstance(after_n, int):
                if before_n == after_n:
                    self.log(f"✅ 폴리곤 개수 그대로 유지: {before_n} → {after_n} (도형 안 바뀜)")
                else:
                    self.log(f"⚠️ 개수가 달라졌습니다: {before_n} → {after_n}. 로그를 확인하세요.")
            self.finish(out_dir, "속성 합치기(도형 유지) 완료")

        self.run_task("레이어 병합", job)

    def run_merge_append(self) -> None:
        layers = self.selected_layers(self.merge_list)
        if len(layers) < 2:
            messagebox.showwarning("레이어 부족", "이어붙이려면 2개 이상 선택해야 합니다.")
            return
        epsg = self.merge_epsg.get().strip() or None
        fmt = self.merge_format.get()
        makevalid = self.merge_makevalid.get()
        in_enc, out_enc = self.in_enc.get(), self.out_enc.get()

        def job() -> None:
            out_dir = stamped_dir(self.out_base(), "병합")
            core.PROGRESS.begin(len(layers) + 1, "레이어 이어붙이기")
            out_path = core.output_dataset_path(out_dir, "merged_layers", fmt)
            ok, out_path, _log = core.merge_layers(layers, out_path, fmt, epsg, in_enc, out_enc, makevalid)
            if not ok:
                self.log("❌ 병합 실패 — 로그를 확인하세요.")
                return
            total = sum(int(core.ogr_layer_stats(item.path, in_enc, item.sublayer).get("features", 0) or 0)
                        for item in layers)
            merged_n = core.ogr_layer_stats(out_path).get("features")
            self.log(f"✅ {out_path.name} | 입력 피처 합계 {total} → 병합 결과 {merged_n}")
            self.finish(out_dir, f"{len(layers)}개 레이어 이어붙이기 완료")

        self.run_task("레이어 이어붙이기", job)

    # ────────────────────────── 실행: 분할 ──────────────────────────
    def load_split_values(self) -> None:
        layers = self.selected_layers(self.split_list)
        column = self.split_column.get()
        if not layers or not column:
            messagebox.showwarning("선택 필요", "레이어와 기준 컬럼을 먼저 고르세요.")
            return

        def job() -> None:
            values = core.unique_values(layers[0], column, self.in_enc.get())
            def apply() -> None:
                self.split_values.delete("1.0", "end")
                self.split_values.insert("1.0", "\n".join(values))
            self.after(0, apply)
            self.log(f"'{column}' 고유값 {len(values)}개를 불러왔습니다: {', '.join(values[:10])}"
                     + (" …" if len(values) > 10 else ""))

        self.run_task("고유값 불러오기", job)

    def run_split(self) -> None:
        layers = self.selected_layers(self.split_list)
        column = self.split_column.get()
        if not layers or not column:
            messagebox.showwarning("선택 필요", "레이어와 기준 컬럼을 고르세요.")
            return
        typed = [line.strip() for line in self.split_values.get("1.0", "end").splitlines() if line.strip()]
        epsg = self.split_epsg.get().strip() or None
        fmt = self.split_format.get()
        contains = self.split_contains.get()
        makevalid = self.split_makevalid.get()
        in_enc, out_enc = self.in_enc.get(), self.out_enc.get()

        def job() -> None:
            out_dir = stamped_dir(self.out_base(), "분할")
            produced: list[Path] = []
            for layer in layers:
                columns = core.columns_for_layer(layer, in_enc)
                if columns and column not in columns:
                    self.log(f"[{layer.name}] 기준 컬럼 '{column}' 없음 — 건너뜀")
                    continue
                values = typed or core.unique_values(layer, column, in_enc)
                if not values:
                    self.log(f"[{layer.name}] 분할할 값을 찾지 못했습니다.")
                    continue
                self.log(f"\n--- {layer.name}: {len(values)}개 값으로 분할 ---")
                # 값 1개 = GDAL 1회. 진행률·남은시간은 core.PROGRESS가 상태줄에 표시합니다.
                core.PROGRESS.begin(len(values), f"분할 · {layer.name}")

                def progress(done: int, total: int, value: str) -> None:
                    self.log(f"   [{done}/{total}] {value}")

                outputs, _log = core.split_layer_by_values(
                    layer, column, values, out_dir / core.safe_name(layer.name), fmt,
                    in_enc, out_enc, contains, epsg, makevalid, progress)
                produced.extend(outputs)
                self.log(f"✅ {layer.name}: {len(outputs)}/{len(values)}개 생성")
            if produced:
                self.finish(out_dir, f"{len(produced)}개 파일로 분할 완료")
            else:
                self.log("❌ 분할 결과가 없습니다. 기준 컬럼/값을 확인하세요.")

        self.run_task("레이어 분할", job)

    # ────────────────────────── 실행: 코드 결합 ──────────────────────────
    def preview_join_codes(self) -> None:
        layer = self.layer_by_label(self.join_layer.get())
        column = self.join_mnum.get()
        if not layer or not column:
            messagebox.showwarning("선택 필요", "대상 레이어와 코드 컬럼을 고르세요.")
            return
        if layer.kind != "SHP":
            self.join_code_label.configure(text="추출된 코드 예시: (GPKG는 미리보기 없이 실행됩니다)")
            return
        table = core.read_dbf_preview(layer.path.with_suffix(".dbf"), self.in_enc.get(), limit=200)
        keys = [core.substr_key(value, self.join_start.get(), self.join_len.get()) for value in table.column(column)]
        sample = list(dict.fromkeys([key for key in keys if key]))[:10]
        self.join_code_label.configure(text=f"추출된 코드 예시: {', '.join(sample) or '없음'}  (앞 {len(keys)}행 기준)")

    def pick_code_csv(self) -> None:
        path = filedialog.askopenfilename(title="코드표 CSV 선택", filetypes=[("CSV", "*.csv"), ("모든 파일", "*.*")])
        if path:
            self.load_code_csv(Path(path))

    def load_code_csv(self, path: Path) -> None:
        """코드표 CSV를 읽어 인코딩을 자동 판정합니다(버튼 선택·끌어놓기 공용)."""
        try:
            raw = path.read_bytes()
        except OSError as exc:
            messagebox.showerror("코드표 읽기 실패", f"{path.name}\n\n{exc}")
            return
        self.code_csv_path = path
        self.code_raw = raw
        report = core.csv_encoding_report(self.code_raw)
        for row in report:
            self.log(f"[코드표 인코딩] {row['인코딩']}: 깨짐 점수 {row['깨짐 의심 점수']} · {row['컬럼']}")
        self.join_csv_enc.set(core.best_encoding(report, core.CSV_ENCODINGS))
        self.join_csv_label.configure(text=path.name)
        self._reset_csv_drop(path.name)
        self._reload_code_table()

    def _reload_code_table(self) -> None:
        if not self.code_raw:
            return
        try:
            table = core.read_csv_table(self.code_raw, self.join_csv_enc.get())
        except Exception as exc:
            messagebox.showerror("코드표 읽기 실패", str(exc))
            return
        self.code_table = table
        self.join_key_combo.configure(values=table.columns)
        default = next((c for c in table.columns if any(k in c.lower() for k in ["code", "코드", "ucode"])),
                       table.columns[0] if table.columns else "")
        if self.join_key.get() not in table.columns:
            self.join_key.set(default)
        self.join_values.delete(0, "end")
        for column in table.columns:
            if column != self.join_key.get():
                self.join_values.insert("end", column)
        self._fill_tree(self.join_preview, table, limit=20)
        self.log(f"코드표: {self.join_csv_enc.get()} · {len(table)}행 · 컬럼 {table.columns}")

    def check_match_rate(self) -> None:
        layer = self.layer_by_label(self.join_layer.get())
        if not layer or layer.kind != "SHP" or not self.code_table:
            messagebox.showinfo("확인 불가", "SHP 레이어와 코드표를 먼저 고르세요(GPKG는 실행 후 결과로 확인).")
            return
        key_col = self.join_key.get()
        table = core.read_dbf_preview(layer.path.with_suffix(".dbf"), self.in_enc.get(), limit=500)
        keys = [core.substr_key(v, self.join_start.get(), self.join_len.get()) for v in table.column(self.join_mnum.get())]
        keys = [key for key in keys if key]
        codeset = {value.strip() for value in self.code_table.column(key_col)}
        if not keys:
            self.log("매칭 확인: 추출된 코드가 없습니다. 시작 위치/길이를 확인하세요.")
            return
        matched = [key for key in keys if key in codeset]
        rate = len(matched) / len(keys) * 100
        unmatched = list(dict.fromkeys([key for key in keys if key not in codeset]))[:8]
        self.log(f"매칭률(앞 {len(keys)}행): {rate:.0f}% ({len(matched)}/{len(keys)})"
                 + (f" · 미매칭 예시: {', '.join(unmatched)}" if unmatched else ""))
        if rate == 0:
            messagebox.showwarning("매칭 0%", "substr 시작 위치·길이 또는 코드표 키 컬럼을 다시 확인하세요.")

    def run_join(self) -> None:
        layer = self.layer_by_label(self.join_layer.get())
        if not layer or not self.code_table:
            messagebox.showwarning("선택 필요", "대상 레이어와 코드표 CSV를 고르세요.")
            return
        key_col = self.join_key.get()
        chosen = [self.join_values.get(i) for i in self.join_values.curselection()]
        value_cols = chosen or [c for c in self.code_table.columns if c != key_col]
        if not value_cols:
            messagebox.showwarning("컬럼 없음", "결합할 컬럼이 없습니다.")
            return
        mnum = self.join_mnum.get()
        start, length = int(self.join_start.get()), int(self.join_len.get())
        fmt = self.join_format.get()
        in_enc, out_enc = self.in_enc.get(), self.out_enc.get()
        table = self.code_table

        def job() -> None:
            out_dir = stamped_dir(self.out_base(), "코드결합")
            code_csv = out_dir / "codes_utf8.csv"
            core.write_csv_utf8(table, code_csv)
            core.PROGRESS.begin(3, "코드 결합")
            out_path = core.output_dataset_path(out_dir, f"{layer.name}_joined", fmt)
            ok, out_path, _log = core.join_code_table(
                layer, mnum, start, length, code_csv, key_col, value_cols, out_path, fmt, in_enc, out_enc)
            if not ok:
                self.log("❌ 코드 결합 실패 — 로그를 확인하세요.")
                return
            self.log(f"✅ {out_path.name} | 붙인 컬럼: {', '.join(value_cols)}")
            self.finish(out_dir, "코드 결합 완료")

        self.run_task("코드 결합", job)

    # ────────────────────────── 실행: 속성 미리보기 ──────────────────────────
    def run_preview(self) -> None:
        layer = self.layer_by_label(self.preview_layer.get())
        if not layer:
            messagebox.showwarning("레이어 없음", "레이어를 고르세요.")
            return
        if layer.kind != "SHP" or not layer.has_dbf:
            messagebox.showinfo("미리보기 불가", "DBF가 있는 SHP만 속성 미리보기를 지원합니다.")
            return
        dbf = layer.path.with_suffix(".dbf")
        report = core.encoding_report(dbf)
        self.enc_tree.delete(*self.enc_tree.get_children())
        for row in report:
            self.enc_tree.insert("", "end", text=str(row["인코딩"]),
                                 values=(row["깨짐 의심 점수"], row["컬럼"]))
        recommended = core.best_encoding(report, core.ENCODINGS)
        if recommended != self.in_enc.get():
            self.log(f"💡 '{recommended}'의 깨짐 점수가 더 낮습니다. 한글이 깨져 보이면 입력 DBF 인코딩을 {recommended}로 바꾸세요.")
        table = core.read_dbf_preview(dbf, self.in_enc.get(), limit=30)
        self._fill_tree(self.attr_tree, table, limit=30)
        self.log(f"{layer.name}: 전체 {core.dbf_record_count(dbf)}행 · 컬럼 {len(table.columns)}개 "
                 f"(현재 인코딩 {self.in_enc.get()})")

    def _fill_tree(self, tree: ttk.Treeview, table: core.Table, limit: int = 20) -> None:
        tree.delete(*tree.get_children())
        tree["columns"] = table.columns
        for column in table.columns:
            tree.heading(column, text=column)
            tree.column(column, width=max(80, min(220, len(column) * 14 + 40)), anchor="w")
        for row in table.rows[:limit]:
            tree.insert("", "end", values=[row.get(column, "") for column in table.columns])


def selftest() -> int:
    """`SHP도구.exe --selftest` — 사원 PC에서 환경을 점검해 진단결과.txt를 남깁니다.

    앱이 안 될 때 이 파일만 보내주면 원인(대개 GDAL 미설치)을 바로 알 수 있습니다.
    """
    lines = [f"SHP 도구 자가진단 v{APP_VERSION}",
             f"시각: {time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"실행 파일: {sys.executable}",
             f"앱 폴더: {app_dir()}",
             f"설정 파일: {CONFIG_PATH} (쓰기 가능: {_writable(CONFIG_PATH.parent)})",
             f"결과 기본 폴더: {default_out_dir()}",
             ""]

    ogr2ogr, ogrinfo = core.gdals(load_config().get("GDAL_BIN", ""))
    lines.append(f"[GDAL] ogr2ogr: {ogr2ogr or '❌ 없음'}")
    lines.append(f"[GDAL] ogrinfo: {ogrinfo or '❌ 없음'}")
    if ogr2ogr:
        ok, out = core.run_cmd([ogr2ogr, "--version"])
        lines.append(f"[GDAL] 버전: {out.splitlines()[0] if ok and out else '확인 실패'}")
    else:
        lines.append("[GDAL] → QGIS를 설치하거나 앱에서 bin 폴더를 지정하세요. https://qgis.org/download/")

    lines.append("")
    lines.append(f"[드래그앤드롭] tkinterdnd2 모듈: {'있음' if HAS_DND else '❌ 없음'}")
    if HAS_DND:
        try:  # tkdnd 바이너리까지 실제로 붙는지 확인
            root = BASE_TK()
            root.withdraw()
            probe = tk.Label(root, text="probe")
            probe.drop_target_register(DND_FILES)
            root.destroy()
            lines.append("[드래그앤드롭] tkdnd 로드: 정상 (끌어놓기 사용 가능)")
        except Exception as exc:
            lines.append(f"[드래그앤드롭] ❌ tkdnd 로드 실패: {exc}")
    else:
        lines.append("[드래그앤드롭] 버튼([파일 선택])으로만 파일을 넣을 수 있습니다.")

    report = "\n".join(lines)
    target = app_dir() if _writable(app_dir()) else Path(os.environ.get("TEMP", str(Path.home())))
    out_file = target / "진단결과.txt"
    try:
        out_file.write_text(report + "\n", encoding="utf-8")
    except OSError:
        out_file = None
    print(report)
    if out_file:
        print(f"\n저장: {out_file}")
    return 0 if ogr2ogr else 1


def main() -> None:
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    app = ShpToolApp()
    app.mainloop()


if __name__ == "__main__":
    main()
