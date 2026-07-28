# -*- coding: utf-8 -*-
"""SHP 좌표변환·병합·분할 도구 — 공통 처리 엔진(데스크톱용).

웹판(`../app.py`)의 GDAL 처리 로직을 그대로 가져오되, streamlit·pandas 의존을 없애
tkinter 앱과 스크립트 어디서나 쓸 수 있게 정리한 모듈입니다.
실제 좌표변환/병합/분할은 모두 GDAL CLI(ogr2ogr, ogrinfo)가 수행합니다.
"""
from __future__ import annotations

import csv
import io
import os
import re
import shutil
import struct
import subprocess
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

APP_TITLE = "SHP 좌표변환·병합·분할 도구"

COMMON_EPSG = {
    "EPSG:5186 - 중부원점 TM": "5186",
    "EPSG:5185 - 서부원점 TM": "5185",
    "EPSG:5187 - 동부원점 TM": "5187",
    "EPSG:5179 - Korea 2000 / Unified CS": "5179",
    "EPSG:4326 - WGS84 위경도": "4326",
    "직접 입력": "custom",
}
ENCODINGS = ["UTF-8", "CP949", "EUC-KR", "ISO-8859-1"]
CSV_ENCODINGS = ["UTF-8-SIG", "CP949", "EUC-KR", "UTF-8"]
SHAPEFILE_PARTS = [".shp", ".shx", ".dbf", ".prj", ".cpg", ".qix", ".sbn", ".sbx"]

DISSOLVE_AGG_FUNCS = {
    "제외": None,
    "합계(SUM)": "SUM",
    "평균(AVG)": "AVG",
    "최대(MAX)": "MAX",
    "최소(MIN)": "MIN",
    "개수(COUNT)": "COUNT",
}
# 도형을 유지하며 그룹 집계값을 각 피처에 붙이는 모드용(텍스트 이어붙이기 포함)
KEEP_AGG_FUNCS = dict(DISSOLVE_AGG_FUNCS, **{"텍스트 이어붙이기": "GROUP_CONCAT"})
# 집계 결과 컬럼명에 붙일 짧은 꼬리표(원본 컬럼은 보존, 새 컬럼으로 추가)
AGG_SUFFIX = {"SUM": "합계", "AVG": "평균", "MAX": "최대", "MIN": "최소",
              "COUNT": "개수", "GROUP_CONCAT": "묶음"}

BAD_PATTERNS = ["\ufffd", "Ã", "Â", "ì", "í", "ê", "¤"]

# 창 없는 실행(windowed exe에서 콘솔 깜빡임 방지)
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# GUI에서 지정한 GDAL bin 경로(자동 탐색 실패 시 사용)
GDAL_BIN_HINT: str | None = None
# 실행 로그를 받아갈 콜백(GUI가 설정). 설정되면 모든 GDAL 명령이 여기로 흘러갑니다.
LOG_HOOK: Callable[[str], None] | None = None


def set_gdal_bin_hint(path: str | None) -> None:
    global GDAL_BIN_HINT
    GDAL_BIN_HINT = (path or "").strip() or None


def set_log_hook(hook: Callable[[str], None] | None) -> None:
    global LOG_HOOK
    LOG_HOOK = hook


def _log(message: str) -> None:
    if LOG_HOOK:
        try:
            LOG_HOOK(message)
        except Exception:
            pass


# ────────────────────────────── 문자열/식별자 ──────────────────────────────
def safe_name(value: object, fallback: str = "value") -> str:
    text = str(value).strip() if value is not None else ""
    if not text:
        text = fallback
    text = re.sub(r"[\\/:*?\"<>|]+", "_", text)
    text = re.sub(r"\s+", "_", text)
    return text[:80]


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def sql_literal(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


# ────────────────────────────── GDAL 탐색/실행 ──────────────────────────────
def _normalize_bin_path(value: str | os.PathLike | None) -> Path | None:
    if not value:
        return None
    text = str(value).strip().strip('"')
    if not text:
        return None
    path = Path(os.path.expandvars(os.path.expanduser(text)))
    if path.is_file():
        path = path.parent
    return path if path.exists() and path.is_dir() else None


def _exe_in_bin(bin_dir: Path, name: str) -> str | None:
    names = [name, f"{name}.exe"] if os.name == "nt" else [name]
    for candidate_name in names:
        candidate = bin_dir / candidate_name
        if candidate.exists() and candidate.is_file():
            return str(candidate)
    return None


def _gdal_bin_candidates(manual_bin: str | None = None) -> list[Path]:
    candidates: list[Path] = []

    manual = _normalize_bin_path(manual_bin)
    if manual:  # 사용자가 직접 넣은 경로를 최우선으로
        candidates.append(manual)

    for root in [os.environ.get("OSGEO4W_ROOT"), os.environ.get("QGIS_PREFIX_PATH")]:
        base = _normalize_bin_path(root)
        if base:
            candidates.append(base / "bin" if base.name.lower() != "bin" else base)

    for env_name in ["GDAL_BIN", "QGIS_BIN"]:
        path = _normalize_bin_path(os.environ.get(env_name))
        if path:
            candidates.append(path)

    program_roots = [
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        r"C:\Program Files",
        r"C:\Program Files (x86)",
    ]
    for root_text in program_roots:
        root = _normalize_bin_path(root_text)
        if root:
            candidates.extend(sorted((path / "bin" for path in root.glob("QGIS*")), reverse=True))

    for path_text in [r"C:\OSGeo4W\bin", r"C:\OSGeo4W64\bin"]:
        path = _normalize_bin_path(path_text)
        if path:
            candidates.append(path)

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def gdals(manual_bin: str | None = None) -> tuple[str | None, str | None]:
    """(ogr2ogr, ogrinfo) 실행 파일 경로를 찾습니다. 없으면 None."""
    hint = manual_bin if manual_bin is not None else GDAL_BIN_HINT
    for bin_dir in _gdal_bin_candidates(hint):
        ogr2ogr = _exe_in_bin(bin_dir, "ogr2ogr")
        ogrinfo = _exe_in_bin(bin_dir, "ogrinfo")
        if ogr2ogr and ogrinfo:
            return ogr2ogr, ogrinfo
    return shutil.which("ogr2ogr"), shutil.which("ogrinfo")


def _gdal_env(exe_path: str | None) -> dict[str, str]:
    env = os.environ.copy()
    if not exe_path:
        return env
    bin_dir = Path(exe_path).resolve().parent
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")

    root = bin_dir.parent
    proj_dir = root / "share" / "proj"
    gdal_data_dir = root / "share" / "gdal"
    if proj_dir.exists():
        env.setdefault("PROJ_LIB", str(proj_dir))
    if gdal_data_dir.exists():
        env.setdefault("GDAL_DATA", str(gdal_data_dir))
    return env


def run_cmd(args: list[str]) -> tuple[bool, str]:
    _log("$ " + " ".join(f'"{a}"' if " " in str(a) else str(a) for a in args[:1] + args[1:]))
    try:
        completed = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_gdal_env(args[0] if args else None),
            creationflags=_NO_WINDOW,
        )
    except FileNotFoundError as exc:
        _log(f"[실패] {exc}")
        return False, str(exc)
    output = "\n".join(part for part in [completed.stdout, completed.stderr] if part).strip()
    if output:
        _log(output)
    return completed.returncode == 0, output


# ────────────────────────────── 레이어 탐색 ──────────────────────────────
@dataclass
class LayerInfo:
    name: str
    path: Path
    kind: str
    folder: Path
    has_shx: bool = False
    has_dbf: bool = False
    has_prj: bool = False
    cpg: str = ""
    prj_preview: str = ""
    sublayer: str | None = None  # GPKG 내부 레이어명(SHP은 None)

    def __str__(self) -> str:  # 콤보박스/리스트박스 표시용
        return f"{self.name} ({self.kind})"


def gpkg_layer_names(path: Path) -> list[str]:
    """GPKG 내부 레이어명을 ogrinfo로 나열합니다."""
    _, ogrinfo = gdals()
    if not ogrinfo or not path.exists():
        return []
    ok, output = run_cmd([ogrinfo, str(path)])
    if not ok:
        return []
    names: list[str] = []
    for line in output.splitlines():
        match = re.match(r"^\s*\d+:\s+(.+)$", line)
        if match:
            name = re.sub(r"\s*\([^)]*\)\s*$", "", match.group(1)).strip()
            if name:
                names.append(name)
    return names


def ogr_source_args(layer: LayerInfo) -> list[str]:
    """ogr2ogr 소스 인자. GPKG 내부 레이어는 레이어명을 함께 지정합니다."""
    if layer.kind == "GPKG" and layer.sublayer:
        return [str(layer.path), layer.sublayer]
    return [str(layer.path)]


def _shp_layer(shp: Path) -> LayerInfo:
    stem = shp.with_suffix("")
    prj = stem.with_suffix(".prj")
    cpg = stem.with_suffix(".cpg")
    return LayerInfo(
        name=shp.stem,
        path=shp,
        kind="SHP",
        folder=shp.parent,
        has_shx=stem.with_suffix(".shx").exists(),
        has_dbf=stem.with_suffix(".dbf").exists(),
        has_prj=prj.exists(),
        cpg=cpg.read_text(encoding="utf-8", errors="replace").strip() if cpg.exists() else "",
        prj_preview=prj.read_text(encoding="utf-8", errors="replace")[:400] if prj.exists() else "",
    )


def _gpkg_layers(gpkg: Path) -> list[LayerInfo]:
    sublayers = gpkg_layer_names(gpkg)
    if len(sublayers) > 1:
        return [LayerInfo(name=f"{gpkg.stem}:{sub}", path=gpkg, kind="GPKG", folder=gpkg.parent, sublayer=sub)
                for sub in sublayers]
    if len(sublayers) == 1:
        return [LayerInfo(name=gpkg.stem, path=gpkg, kind="GPKG", folder=gpkg.parent, sublayer=sublayers[0])]
    # ogrinfo 미탐색/실패 시 파일 단위 단일 레이어로 처리
    return [LayerInfo(name=gpkg.stem, path=gpkg, kind="GPKG", folder=gpkg.parent)]


def discover_layers(root: Path) -> list[LayerInfo]:
    """폴더를 훑어 SHP/GPKG 레이어를 모두 찾습니다(하위 폴더 포함)."""
    layers: list[LayerInfo] = []
    for shp in sorted(root.rglob("*.shp")):
        layers.append(_shp_layer(shp))
    for gpkg in sorted(root.rglob("*.gpkg")):
        layers.extend(_gpkg_layers(gpkg))
    return layers


def extract_zip(zip_path: Path, work_dir: Path) -> Path:
    """zip을 작업 폴더에 풀고 풀린 폴더 경로를 돌려줍니다."""
    target = work_dir / safe_name(zip_path.stem, "zip")
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(target)
    return target


def layers_from_paths(paths: Iterable[Path], work_dir: Path) -> list[LayerInfo]:
    """사용자가 고른 파일/폴더 목록에서 레이어를 만듭니다(zip은 풀어서 처리)."""
    layers: list[LayerInfo] = []
    seen: set[str] = set()

    def add(items: Sequence[LayerInfo]) -> None:
        for item in items:
            key = f"{item.path}|{item.sublayer or ''}".casefold()
            if key not in seen:
                seen.add(key)
                layers.append(item)

    for path in paths:
        path = Path(path)
        if path.is_dir():
            add(discover_layers(path))
        elif path.suffix.lower() == ".zip":
            add(discover_layers(extract_zip(path, work_dir)))
        elif path.suffix.lower() == ".shp":
            add([_shp_layer(path)])
        elif path.suffix.lower() == ".gpkg":
            add(_gpkg_layers(path))
        elif path.suffix.lower() in {".shx", ".dbf", ".prj", ".cpg"}:
            shp = path.with_suffix(".shp")
            if shp.exists():
                add([_shp_layer(shp)])
    return layers


def shp_sidecars(shp_path: Path) -> list[Path]:
    stem = shp_path.with_suffix("")
    return [stem.with_suffix(ext) for ext in SHAPEFILE_PARTS if stem.with_suffix(ext).exists()]


# ────────────────────────────── DBF 읽기(속성 미리보기) ──────────────────────────────
def dbf_fields(dbf_path: Path) -> list[dict[str, object]]:
    with dbf_path.open("rb") as fp:
        header = fp.read(32)
        if len(header) < 32:
            raise ValueError("DBF header is too short.")
        header_len = struct.unpack("<H", header[8:10])[0]
        fields: list[dict[str, object]] = []
        while fp.tell() < header_len:
            desc = fp.read(32)
            if not desc or desc[0] == 0x0D:
                break
            raw_name = desc[:11].split(b"\x00", 1)[0]
            fields.append({"raw_name": raw_name, "type": chr(desc[11]),
                           "length": desc[16], "decimals": desc[17]})
        return fields


@dataclass
class Table:
    """가벼운 표 자료(판다스 대체)."""
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, str]] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.rows)

    def column(self, name: str) -> list[str]:
        return [row.get(name, "") for row in self.rows]


def read_dbf_preview(dbf_path: Path, encoding: str, limit: int = 30) -> Table:
    """DBF를 직접 파싱해 앞부분 레코드를 읽습니다(GDAL 없이도 동작)."""
    with dbf_path.open("rb") as fp:
        header = fp.read(32)
        if len(header) < 32:
            return Table()
        record_count = struct.unpack("<I", header[4:8])[0]
        header_len = struct.unpack("<H", header[8:10])[0]
        record_len = struct.unpack("<H", header[10:12])[0]
        fields = dbf_fields(dbf_path)
        names: list[str] = []
        for index, field_info in enumerate(fields, start=1):
            raw_name = field_info["raw_name"]
            assert isinstance(raw_name, bytes)
            name = raw_name.decode(encoding, errors="replace").strip() or f"field_{index}"
            names.append(name)

        fp.seek(header_len)
        rows: list[dict[str, str]] = []
        for _ in range(min(record_count, limit)):
            record = fp.read(record_len)
            if len(record) < record_len:
                break
            if record[:1] == b"*":  # 삭제 표시된 레코드
                continue
            offset = 1
            row: dict[str, str] = {}
            for name, field_info in zip(names, fields):
                width = int(field_info["length"])
                raw = record[offset: offset + width]
                offset += width
                row[name] = raw.decode(encoding, errors="replace").strip()
            rows.append(row)
    return Table(columns=names, rows=rows)


def dbf_record_count(dbf_path: Path) -> int:
    with dbf_path.open("rb") as fp:
        header = fp.read(32)
    return struct.unpack("<I", header[4:8])[0] if len(header) >= 32 else 0


def text_garble_score(text: str) -> int:
    return sum(text.count(pattern) for pattern in BAD_PATTERNS)


def encoding_score(table: Table) -> int:
    text = " ".join(str(value) for row in table.rows[:20] for value in row.values())
    return text_garble_score(text)


def encoding_report(dbf_path: Path) -> list[dict[str, object]]:
    """인코딩 후보별 '깨짐 의심 점수'. 점수가 낮을수록 정상입니다."""
    report: list[dict[str, object]] = []
    for enc in ENCODINGS:
        try:
            table = read_dbf_preview(dbf_path, enc)
            score = encoding_score(table) + text_garble_score(" ".join(table.columns))
            report.append({"인코딩": enc, "깨짐 의심 점수": score, "컬럼": ", ".join(table.columns[:8])})
        except Exception as exc:
            report.append({"인코딩": enc, "깨짐 의심 점수": 9999, "컬럼": str(exc)[:60]})
    return report


def best_encoding(report: list[dict[str, object]], candidates: list[str]) -> str:
    valid = [row for row in report if isinstance(row["깨짐 의심 점수"], int) and row["깨짐 의심 점수"] < 9999]
    pool = valid or report
    best = str(min(pool, key=lambda row: row["깨짐 의심 점수"])["인코딩"])
    return best if best in candidates else candidates[0]


def columns_for_layer(layer: LayerInfo, encoding: str) -> list[str]:
    if layer.kind != "SHP":
        return gpkg_columns(layer)
    dbf = layer.path.with_suffix(".dbf")
    if not dbf.exists():
        return []
    try:
        return list(read_dbf_preview(dbf, encoding, limit=1).columns)
    except Exception:
        return []


def gpkg_columns(layer: LayerInfo) -> list[str]:
    """GPKG 레이어의 속성 컬럼명을 ogrinfo로 읽습니다."""
    _, ogrinfo = gdals()
    if not ogrinfo:
        return []
    args = [ogrinfo, "-so", str(layer.path)] + ([layer.sublayer] if layer.sublayer else [])
    ok, output = run_cmd(args)
    if not ok:
        return []
    columns: list[str] = []
    for line in output.splitlines():
        match = re.match(r"^(\w[^:]*):\s+(String|Integer|Integer64|Real|Date|DateTime|Binary)\b", line.strip())
        if match:
            columns.append(match.group(1).strip())
    return columns


def unique_values(layer: LayerInfo, column: str, encoding: str, limit: int = 20000) -> list[str]:
    """분할 기준값 후보(고유값)를 뽑습니다. SHP은 DBF에서, GPKG은 ogrinfo SQL로."""
    if layer.kind == "SHP":
        dbf = layer.path.with_suffix(".dbf")
        if not dbf.exists():
            return []
        table = read_dbf_preview(dbf, encoding, limit=limit)
        return sorted({value for value in table.column(column) if value != ""})
    _, ogrinfo = gdals()
    if not ogrinfo:
        return []
    src_layer = layer.sublayer or layer.path.stem
    sql = f"SELECT DISTINCT {quote_ident(column)} FROM {quote_ident(src_layer)}"
    ok, output = run_cmd([ogrinfo, "-q", "-dialect", "SQLite", "-sql", sql, str(layer.path)])
    if not ok:
        return []
    values = re.findall(rf"^\s*{re.escape(column)}\s*\(String\)\s*=\s*(.*)$", output, flags=re.M)
    return sorted({value.strip() for value in values if value.strip() and value.strip() != "(null)"})


# ────────────────────────────── CSV 코드표 ──────────────────────────────
def read_csv_table(raw: bytes, encoding: str) -> Table:
    """코드표 CSV를 지정 인코딩으로 읽습니다. 모든 값은 문자열(앞자리 0 보존)."""
    text = raw.decode(encoding, errors="replace")
    reader = csv.reader(io.StringIO(text, newline=""))
    rows = list(reader)
    if not rows:
        return Table()
    columns = [col.strip() for col in rows[0]]
    records = []
    for raw_row in rows[1:]:
        if not any(cell.strip() for cell in raw_row):
            continue
        records.append({col: (raw_row[i].strip() if i < len(raw_row) else "") for i, col in enumerate(columns)})
    return Table(columns=columns, rows=records)


def csv_encoding_report(raw: bytes) -> list[dict[str, object]]:
    report: list[dict[str, object]] = []
    for enc in CSV_ENCODINGS:
        try:
            table = read_csv_table(raw, enc)
            score = encoding_score(table) + text_garble_score(" ".join(table.columns))
            report.append({"인코딩": enc, "깨짐 의심 점수": score, "컬럼": ", ".join(table.columns[:8])})
        except Exception as exc:
            report.append({"인코딩": enc, "깨짐 의심 점수": 9999, "컬럼": str(exc)[:60]})
    return report


def write_csv_utf8(table: Table, out_path: Path) -> None:
    with out_path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=table.columns)
        writer.writeheader()
        for row in table.rows:
            writer.writerow({col: row.get(col, "") for col in table.columns})


def substr_key(value: object, start: int, length: int) -> str:
    """SQLite substr(x, start, length)와 동일한 규칙(1-기준)으로 코드 추출."""
    begin = max(int(start) - 1, 0)
    return str(value or "")[begin: begin + int(length)].strip()


# ────────────────────────────── 출력 경로/형식 ──────────────────────────────
def output_dataset_path(base_dir: Path, layer_name: str, output_format: str) -> Path:
    suffix = ".gpkg" if output_format == "GPKG" else ".shp"
    return base_dir / f"{safe_name(layer_name)}{suffix}"


def ogr_output_format(output_format: str) -> str:
    return "GPKG" if output_format == "GPKG" else "ESRI Shapefile"


def ogr_layer_stats(path: Path, input_encoding: str | None = None, sublayer: str | None = None) -> dict[str, object]:
    """ogrinfo로 feature count와 extent를 읽어 딕셔너리로 반환합니다."""
    _, ogrinfo = gdals()
    if not ogrinfo or not path.exists():
        return {}
    args = [ogrinfo, "-so", "-al"]
    if path.suffix.lower() == ".shp" and input_encoding:
        args += ["-oo", f"ENCODING={input_encoding}"]
    args += [str(path)]
    if sublayer:
        args += [sublayer]
    ok, output = run_cmd(args)
    if not ok:
        return {}
    counts = [int(value) for value in re.findall(r"Feature Count:\s*(\d+)", output)]
    extents = re.findall(r"Extent:\s*\(([^)]*)\)\s*-\s*\(([^)]*)\)", output)
    stats: dict[str, object] = {}
    if counts:
        stats["features"] = sum(counts)
    if extents:
        stats["extent"] = f"({extents[0][0].strip()}) - ({extents[0][1].strip()})"
    return stats


# ogrinfo -so -al 의 속성 컬럼 줄 형식: `NM: String (80.0)` / `area_m2: Real (24.15)`
FIELD_LINE_RE = re.compile(r"^([^\s:][^:]*):\s+\w+(?:\([^)]*\))?\s+\(\d+\.\d+\)\s*$")


def layer_field_names(path: Path, layer: str | None = None) -> list[str]:
    """레이어의 속성 컬럼명을 ogrinfo로 실제 확인해 순서대로 돌려줍니다(SHP·GPKG 공통)."""
    _, ogrinfo = gdals()
    if not ogrinfo or not path.exists():
        return []
    ok, output = run_cmd([ogrinfo, "-so", "-al", str(path)] + ([layer] if layer else []))
    if not ok:
        return []
    return [m.group(1).strip() for m in (FIELD_LINE_RE.match(line) for line in output.splitlines()) if m]


def layer_epsg(path: Path, sublayer: str | None = None) -> str | None:
    """레이어 좌표계의 EPSG 코드를 돌려줍니다(없으면 None).

    WKT에서 **마지막**에 나오는 EPSG 코드가 좌표계 자체의 코드입니다(앞쪽은 파라미터 코드).
    """
    _, ogrinfo = gdals()
    if not ogrinfo or not path.exists():
        return None
    ok, output = run_cmd([ogrinfo, "-so", "-al", str(path)] + ([sublayer] if sublayer else []))
    if not ok:
        return None
    codes = re.findall(r'(?:ID\["EPSG",\s*(\d+)\]|AUTHORITY\["EPSG",\s*"(\d+)"\])', output)
    for primary, legacy in reversed(codes):
        return primary or legacy
    return None


GEOM_FAMILY = {"POINT": "점", "MULTIPOINT": "점",
               "LINESTRING": "선", "MULTILINESTRING": "선",
               "POLYGON": "면", "MULTIPOLYGON": "면"}


def layer_geom_family(path: Path, sublayer: str | None = None) -> str:
    """레이어 도형 타입을 '점/선/면/기타'로 돌려줍니다(SHP 혼합 저장 불가 판정용)."""
    _, ogrinfo = gdals()
    if not ogrinfo or not path.exists():
        return "기타"
    ok, output = run_cmd([ogrinfo, "-so", "-al", str(path)] + ([sublayer] if sublayer else []))
    if not ok:
        return "기타"
    match = re.search(r"^Geometry:\s*(.+)$", output, re.MULTILINE)
    if not match:
        return "기타"
    raw = re.sub(r"^(3D|Measured|3D Measured)\s+", "", match.group(1).strip(), flags=re.I)
    return GEOM_FAMILY.get(raw.upper().replace(" ", ""), "기타")


def log_problems(log: str) -> list[str]:
    """처리 로그에서 ERROR/Warning 줄만 뽑아냅니다(조용히 지나가는 실패를 알리기 위함)."""
    seen: list[str] = []
    for line in (log or "").splitlines():
        text = line.strip()
        if not text or not re.search(r"\b(ERROR|Warning)\b", text):
            continue
        if "tms_NZTM2000" in text or "GDAL_DATA is not defined" in text:
            continue
        if text not in seen:
            seen.append(text)
    return seen


def decimal_cast_args(
    gpkg_path: Path,
    source_layer: str | None,
    decimal_fields: dict[str, int] | None,
) -> tuple[list[str], list[str]]:
    """SHP 저장 시 지정 컬럼의 소수점 자리수를 고정하는 ogr2ogr 인자를 만듭니다.

    `-dialect OGRSQL`을 반드시 명시해야 합니다. GPKG를 소스로 `-sql`을 주면 GDAL이 GPKG
    내장 SQLite로 넘겨버려 `CAST(... AS numeric(w,d))`의 자리수 지정이 무시됩니다.
    """
    if not decimal_fields:
        return [], []
    fields = layer_field_names(gpkg_path, source_layer)
    targets = {name: dec for name, dec in decimal_fields.items() if name in fields}
    if not targets:
        return [], []
    selects = []
    for name in fields:
        ident = quote_ident(name)
        if name in targets:
            dec = max(int(targets[name]), 0)
            width = min(18 + dec, 24)
            selects.append(f"CAST({ident} AS numeric({width},{dec})) AS {ident}")
        else:
            selects.append(ident)
    sql = f"SELECT {', '.join(selects)} FROM {quote_ident(source_layer or gpkg_path.stem)}"
    return ["-dialect", "OGRSQL", "-sql", sql], [str(gpkg_path)]


def resolve_layer_and_geom(path: Path) -> tuple[str, str]:
    """결과 파일의 (레이어명, 지오메트리 컬럼명)을 실제로 확인해 돌려줍니다.

    GPKG는 내부 레이어명이 파일명과 다를 수 있어(예: dissolve 결과는 'result')
    ogrinfo로 실제 레이어명과 지오메트리 컬럼명을 읽습니다.
    """
    if path.suffix.lower() != ".gpkg":
        return path.stem, "geometry"
    names = gpkg_layer_names(path)
    layer = names[0] if names else path.stem
    geom = "geometry"
    _, ogrinfo = gdals()
    if ogrinfo:
        ok, out = run_cmd([ogrinfo, "-so", str(path), layer])
        if ok:
            match = re.search(r"Geometry Column\s*=\s*(\S+)", out)
            if match:
                geom = match.group(1)
    return layer, geom


def gpkg_to_final(
    gpkg_path: Path,
    source_layer: str | None,
    out_path: Path,
    output_format: str,
    output_encoding: str,
    extra_args: list[str] | None = None,
    decimal_fields: dict[str, int] | None = None,
) -> tuple[bool, Path, str]:
    """UTF-8 GPKG(중간 결과)를 최종 형식으로 저장합니다.

    SHP 저장이 실패하면 자동으로 GPKG로 대체 저장해, 어떤 경우에도 결과물이 남도록 합니다.

    decimal_fields: {컬럼명: 소수점자리수}. SQL로 계산해 만든 컬럼은 소수점 자리 정보가 없어
      GDAL이 SHP(DBF) 필드를 기본값(폭 24 / 소수점 15)으로 만듭니다. 그래서 ROUND(x,2)로
      반올림해도 `11027.299999999999272`처럼 15자리가 파일에 박힙니다. 최종 저장 때
      `CAST(x AS numeric(w,d))`로 필드 정의 자체를 바꿔야 지정한 자리수로 저장됩니다.
    """
    ogr2ogr, _ = gdals()
    if not ogr2ogr:
        return False, out_path, "ogr2ogr을 찾을 수 없습니다."
    src_tail = [str(gpkg_path)] + ([source_layer] if source_layer else [])
    if output_format == "GPKG":
        args = [ogr2ogr, "-overwrite", "-f", "GPKG"] + (extra_args or []) + [str(out_path), *src_tail]
        ok, log = run_cmd(args)
        return ok, out_path, log
    cast_args, cast_tail = decimal_cast_args(gpkg_path, source_layer, decimal_fields)
    args = ([ogr2ogr, "-overwrite", "-f", "ESRI Shapefile", "-lco", f"ENCODING={output_encoding}"]
            + cast_args + (extra_args or []) + [str(out_path), *(cast_tail if cast_args else src_tail)])
    ok, log = run_cmd(args)
    if ok:
        return True, out_path, log
    fallback = out_path.with_suffix(".gpkg")
    fargs = [ogr2ogr, "-overwrite", "-f", "GPKG"] + (extra_args or []) + [str(fallback), *src_tail]
    ok2, log2 = run_cmd(fargs)
    note = "⚠️ SHP 저장에 실패해 GPKG로 대체했습니다(QGIS에서 동일하게 열립니다)."
    return ok2, (fallback if ok2 else out_path), (log + "\n" + log2 + "\n" + note).strip()


def sqlite_sql_to_output(
    src_path: Path,
    sql: str,
    out_path: Path,
    output_format: str,
    output_encoding: str,
    oo_encoding: str | None = None,
    extra_args: list[str] | None = None,
    decimal_fields: dict[str, int] | None = None,
) -> tuple[bool, Path, str]:
    """SQLite dialect(공간함수 ST_*) 결과를 인코딩 안전하게 저장합니다.

    SQLite dialect로 SHP를 직접 쓰면 일부 GDAL 버전에서 `-lco ENCODING`이 무시돼
    한글이 손상됩니다. 그래서 먼저 UTF-8이 보장되는 GPKG로 만든 뒤 최종 형식으로 저장합니다.
    """
    ogr2ogr, _ = gdals()
    if not ogr2ogr:
        return False, out_path, "ogr2ogr을 찾을 수 없습니다."
    tmp = out_path.parent / f"_sqltmp_{safe_name(out_path.stem)}.gpkg"
    if tmp.exists():
        tmp.unlink()
    args = [ogr2ogr, "-overwrite", "-f", "GPKG", "-dialect", "SQLite", "-sql", sql, "-nln", "result"]
    if oo_encoding:
        args += ["-oo", f"ENCODING={oo_encoding}"]
    if extra_args:
        args += extra_args
    args += [str(tmp), str(src_path)]
    ok, log1 = run_cmd(args)
    if not ok:
        return False, out_path, log1
    ok2, actual, log2 = gpkg_to_final(
        tmp, "result", out_path, output_format, output_encoding, decimal_fields=decimal_fields
    )
    return ok2, actual, (log1 + "\n" + log2).strip()


def add_area_column(
    src_path: Path,
    output_format: str,
    output_encoding: str,
    decimals: int = 1,
    field_name: str = "area_m2",
) -> tuple[bool, Path, str]:
    """결과 레이어에 ST_Area 기반 면적(㎡) 컬럼을 추가한 새 파일을 만듭니다.

    좌표계가 미터 기반 투영좌표계(EPSG:5186 등)일 때만 ㎡가 정확합니다.
    """
    layer_name, geom_col = resolve_layer_and_geom(src_path)
    dec = max(int(decimals), 0)
    sql = (f"SELECT *, ROUND(ST_Area({quote_ident(geom_col)}), {dec}) AS {quote_ident(field_name)} "
           f"FROM {quote_ident(layer_name)}")
    out_path = src_path.with_name(f"{src_path.stem}_area{src_path.suffix}")
    ok, actual, log = sqlite_sql_to_output(
        src_path, sql, out_path, output_format, output_encoding,
        oo_encoding=(output_encoding if src_path.suffix.lower() == ".shp" else None),
        decimal_fields={field_name: dec},
    )
    return ok, (actual if ok else src_path), log


# ────────────────────────────── ① 좌표계 변환 ──────────────────────────────
def convert_layer(
    layer: LayerInfo,
    out_path: Path,
    target_epsg: str | None,
    source_epsg: str | None,
    output_format: str,
    input_encoding: str | None,
    output_encoding: str,
    makevalid: bool = False,
) -> tuple[bool, Path, str]:
    ogr2ogr, _ = gdals()
    if not ogr2ogr:
        return False, out_path, "ogr2ogr을 찾을 수 없습니다. GDAL(QGIS) 설치 후 다시 실행하세요."

    def build(fmt: str, target: Path) -> list[str]:
        args = [ogr2ogr, "-overwrite", "-f", ogr_output_format(fmt)]
        if layer.kind == "SHP" and input_encoding:
            args += ["-oo", f"ENCODING={input_encoding}"]
        if source_epsg:
            args += ["-s_srs", f"EPSG:{source_epsg}"]
        if target_epsg:
            args += ["-t_srs", f"EPSG:{target_epsg}"]
        if makevalid:
            args += ["-makevalid"]
        if fmt == "SHP":
            args += ["-lco", f"ENCODING={output_encoding}"]
        args += [str(target), *ogr_source_args(layer)]
        return args

    ok, log = run_cmd(build(output_format, out_path))
    if ok or output_format == "GPKG":
        return ok, out_path, log
    fallback = out_path.with_suffix(".gpkg")
    ok2, log2 = run_cmd(build("GPKG", fallback))
    note = "⚠️ SHP 저장에 실패해 GPKG로 대체했습니다(QGIS에서 동일하게 열립니다)."
    return ok2, (fallback if ok2 else out_path), (log + "\n" + log2 + "\n" + note).strip()


def convert_layer_safe(
    layer: LayerInfo,
    out_path: Path,
    target_epsg: str | None,
    source_epsg: str | None,
    output_format: str,
    input_encoding: str | None,
    output_encoding: str,
) -> tuple[bool, Path, dict[str, object], str]:
    """2단계 안전 변환.

    ① 원본 좌표계 확정(-a_srs) + makevalid로 네이티브 좌표에서 도형 복구 (재투영 없음)
    ② 재투영(-t_srs) + makevalid
    각 단계 피처 수를 세어 어디서 몇 개가 사라졌는지 리포트합니다.
    """
    ogr2ogr, _ = gdals()
    if not ogr2ogr:
        return False, out_path, {}, "ogr2ogr을 찾을 수 없습니다."
    work = out_path.parent / f"_safe_{safe_name(layer.name)}.gpkg"
    if work.exists():
        work.unlink()
    logs: list[str] = []
    counts: dict[str, object] = {"입력": ogr_layer_stats(layer.path, input_encoding, layer.sublayer).get("features")}

    step_a = [ogr2ogr, "-overwrite", "-f", "GPKG", "-nln", "step", "-makevalid"]
    if layer.kind == "SHP" and input_encoding:
        step_a += ["-oo", f"ENCODING={input_encoding}"]
    if source_epsg:
        step_a += ["-a_srs", f"EPSG:{source_epsg}"]  # 변환 없이 소스 CRS만 확정
    step_a += ["-nlt", "PROMOTE_TO_MULTI", str(work), *ogr_source_args(layer)]
    ok, log = run_cmd(step_a)
    logs.append(f"[① 도형복구/원본CRS확정] {log}")
    if not ok:
        return False, out_path, counts, "\n".join(logs)
    counts["복구후"] = ogr_layer_stats(work).get("features")

    extra_b = ["-makevalid", "-nlt", "PROMOTE_TO_MULTI"]
    if target_epsg:
        extra_b += ["-t_srs", f"EPSG:{target_epsg}"]
    ok, actual, log = gpkg_to_final(work, "step", out_path, output_format, output_encoding, extra_args=extra_b)
    logs.append(f"[② 재투영] {log}")
    if not ok:
        return False, actual, counts, "\n".join(logs)
    counts["출력"] = ogr_layer_stats(actual).get("features")
    return True, actual, counts, "\n".join(logs)


# ────────────────────────────── ② 병합 ──────────────────────────────
def dissolve_one_layer(
    layer: LayerInfo,
    column: str,
    out_path: Path,
    output_format: str,
    input_encoding: str,
    target_epsg: str | None,
    agg_map: dict[str, str] | None = None,
    output_encoding: str = "UTF-8",
) -> tuple[bool, Path, str]:
    """한 레이어 안에서 컬럼값 기준 병합(dissolve).

    도형 깨짐(경계선 스파이크) 방지를 위해 항상 2단계로 처리합니다.
      ① 원본 도형을 -makevalid로 복구해 UTF-8 GPKG 생성 (재투영 없이 네이티브 좌표).
      ② 복구된 GPKG에서 ST_Union ... GROUP BY 로 병합 (+ 필요 시 재투영, 최종 -makevalid).
    union은 인접 폴리곤의 '공유 내부 경계'만 지우고 바깥 윤곽선은 그대로 유지합니다.
    """
    ogr2ogr, _ = gdals()
    if not ogr2ogr:
        return False, out_path, "ogr2ogr을 찾을 수 없습니다."

    repaired = out_path.parent / f"_dissolve_src_{safe_name(layer.name)}.gpkg"
    if repaired.exists():
        repaired.unlink()
    # GPKG 지오메트리 컬럼 기본명은 'geom' → union SQL에서 참조하도록 'geometry'로 고정
    args = [ogr2ogr, "-overwrite", "-f", "GPKG", "-nln", "src",
            "-lco", "GEOMETRY_NAME=geometry", "-makevalid", "-nlt", "PROMOTE_TO_MULTI"]
    if layer.kind == "SHP" and input_encoding:
        args += ["-oo", f"ENCODING={input_encoding}"]
    args += [str(repaired), *ogr_source_args(layer)]
    ok, log1 = run_cmd(args)
    if not ok:
        return False, out_path, f"[① 도형복구 실패] {log1}"

    select_parts = [quote_ident(column), "ST_Union(geometry) AS geometry"]
    for col, func in (agg_map or {}).items():
        if col == column or not func:
            continue
        select_parts.append(f"{func}({quote_ident(col)}) AS {quote_ident(col)}")
    sql = (f"SELECT {', '.join(select_parts)} FROM {quote_ident('src')} "
           f"GROUP BY {quote_ident(column)}")
    extra: list[str] = ["-nlt", "PROMOTE_TO_MULTI", "-makevalid"]
    if target_epsg:
        extra += ["-t_srs", f"EPSG:{target_epsg}"]
    ok2, actual, log2 = sqlite_sql_to_output(
        repaired, sql, out_path, output_format, output_encoding,
        oo_encoding=None,  # 복구본은 이미 UTF-8 GPKG
        extra_args=extra,
    )
    return ok2, actual, f"[① 도형복구] {log1}\n[② 병합] {log2}".strip()


def aggregate_keep_geometry(
    layer: LayerInfo,
    column: str,
    out_path: Path,
    output_format: str,
    input_encoding: str,
    target_epsg: str | None,
    agg_map: dict[str, str],
    add_area: bool = False,
    area_decimals: int = 1,
    output_encoding: str = "UTF-8",
) -> tuple[bool, Path, str, list[str]]:
    """도형은 그대로 두고, 기준 컬럼(그룹)별 값을 각 피처에 새 컬럼으로 붙입니다.

    dissolve(도형 합침)가 아니라 '그룹 값 되붙이기'입니다. 폴리곤 개수·모양·경계선은
    전혀 바뀌지 않고, 원본 컬럼도 그대로 남으며 새 컬럼만 추가됩니다.
    """
    ogr2ogr, _ = gdals()
    if not ogr2ogr:
        return False, out_path, "ogr2ogr을 찾을 수 없습니다.", []
    picked = {col: func for col, func in (agg_map or {}).items() if func and col != column}
    if not picked and not add_area:
        return False, out_path, "합칠 속성을 1개 이상 고르거나, '면적 계산'을 켜세요.", []

    # ① 원본 → UTF-8 GPKG(src). 면적 정확도를 위해 여기서 재투영. geometry 컬럼명 고정.
    work = out_path.parent / f"_agg_src_{safe_name(layer.name)}.gpkg"
    if work.exists():
        work.unlink()
    args = [ogr2ogr, "-overwrite", "-f", "GPKG", "-nln", "src", "-lco", "GEOMETRY_NAME=geometry", "-makevalid"]
    if layer.kind == "SHP" and input_encoding:
        args += ["-oo", f"ENCODING={input_encoding}"]
    if target_epsg:
        args += ["-t_srs", f"EPSG:{target_epsg}"]
    args += [str(work), *ogr_source_args(layer)]
    ok, log1 = run_cmd(args)
    if not ok:
        return False, out_path, f"[① 원본읽기 실패] {log1}", []

    # 중복 방지 기준은 ①에서 만든 GPKG의 실제 컬럼으로 확인합니다.
    # (columns_for_layer는 DBF만 읽어 GPKG 입력이면 빈 목록 → 이름 충돌을 못 걸러냄)
    existing = set(layer_field_names(work, "src")) or set(columns_for_layer(layer, input_encoding))
    used: list[str] = []

    def uniq(base: str) -> str:
        name, index = base, 2
        while name in existing or name in used:
            name, index = f"{base}{index}", index + 1
        used.append(name)
        return name

    dec = max(int(area_decimals), 0)
    area_fields: dict[str, int] = {}   # SHP 저장 시 소수점 자리수를 고정할 컬럼
    main_selects: list[str] = []
    group_selects: list[str] = []
    group_out: list[str] = []

    if add_area:
        # 컬럼명은 ASCII로(SHP 10바이트 필드명 제한에서 한글이 잘려 값이 깨지는 것 방지)
        a_each = uniq("area_m2")
        main_selects.append(f"ROUND(ST_Area(s.geometry), {dec}) AS {quote_ident(a_each)}")
        # 그룹 면적은 ST_Union으로 '실제 합쳐진 면적'(겹침 1번만 계산)
        a_sum = uniq("area_sum")
        group_selects.append(f"ROUND(ST_Area(ST_Union(geometry)), {dec}) AS {quote_ident(a_sum)}")
        group_out.append(a_sum)
        area_fields = {a_each: dec, a_sum: dec}

    for col, func in picked.items():
        name = uniq(f"{col}_{AGG_SUFFIX.get(func, func)}")
        expr = f"GROUP_CONCAT({quote_ident(col)})" if func == "GROUP_CONCAT" else f"{func}({quote_ident(col)})"
        group_selects.append(f"{expr} AS {quote_ident(name)}")
        group_out.append(name)

    sub = (f"SELECT {quote_ident(column)} AS _k, {', '.join(group_selects)} "
           f"FROM src GROUP BY {quote_ident(column)}")
    parts = ["s.*"] + main_selects + [f"g.{quote_ident(name)} AS {quote_ident(name)}" for name in group_out]
    sql = (f"SELECT {', '.join(parts)} FROM src s "
           f"LEFT JOIN ({sub}) g ON s.{quote_ident(column)} = g._k")

    ok2, actual, log2 = sqlite_sql_to_output(work, sql, out_path, output_format, output_encoding,
                                             oo_encoding=None, decimal_fields=area_fields)
    return ok2, actual, f"[① 원본읽기/재투영] {log1}\n[② 값 되붙이기] {log2}".strip(), used


def merge_layers(
    layers: list[LayerInfo],
    out_path: Path,
    output_format: str,
    target_epsg: str | None,
    input_encoding: str,
    output_encoding: str,
    makevalid: bool = False,
) -> tuple[bool, Path, str]:
    """여러 레이어를 하나로 이어붙입니다(-append). 컬럼이 다르면 -addfields로 채웁니다.

    조용히 잘못되는 두 가지를 막습니다.
    - **좌표계 통일**: `-append`는 재투영을 하지 않습니다. 목표 EPSG를 안 넣고 좌표계가
      다른 레이어를 붙이면 GDAL이 경고 없이 원본 좌표값을 그대로 밀어넣어, 개수는 맞는데
      도형만 엉뚱한 위치에 찍힙니다. 목표가 없으면 **첫 레이어 좌표계로 자동 통일**합니다.
    - **도형 타입 혼합**: SHP은 한 파일에 점/선/면을 섞어 담지 못해, 첫 레이어와 타입이 다른
      피처가 통째로 버려집니다(순서에 따라 결과가 뒤바뀜). 섞인 경우 GPKG로 저장합니다.
    """
    ogr2ogr, _ = gdals()
    if not ogr2ogr:
        return False, out_path, "ogr2ogr을 찾을 수 없습니다."
    logs: list[str] = []

    # ── 좌표계 통일 대상 결정 ──
    codes = [layer_epsg(item.path, item.sublayer) for item in layers]
    unify = target_epsg or None
    if not unify and len({code for code in codes if code}) > 1:
        unify = codes[0]
        if unify:
            logs.append(f"[좌표계] 입력 좌표계가 서로 다릅니다({', '.join(code or '없음' for code in codes)}). "
                        f"첫 레이어 기준 EPSG:{unify}로 자동 통일합니다.")
        else:
            logs.append("⚠️ [좌표계] 입력 좌표계가 서로 다른데 첫 레이어에 좌표계 정보(.prj)가 없어 "
                        "자동 통일을 못 했습니다. 목표 EPSG에 5186 등을 직접 지정하세요.")
    missing = [item.name for item, code in zip(layers, codes) if not code]
    if missing and unify:
        logs.append(f"⚠️ [좌표계] 좌표계 정보(.prj)가 없는 레이어: {', '.join(missing)} "
                    "→ 재투영이 안 되거나 실패할 수 있습니다.")

    # ── 도형 타입 혼합 여부 ──
    families = [layer_geom_family(item.path, item.sublayer) for item in layers]
    mixed = len(set(families)) > 1
    geom_arg = "GEOMETRY" if mixed else "PROMOTE_TO_MULTI"
    save_format = output_format
    if mixed:
        detail = ", ".join(f"{item.name}={fam}" for item, fam in zip(layers, families))
        logs.append(f"[도형] 도형 타입이 섞여 있습니다({detail}).")
        if output_format == "SHP":
            save_format = "GPKG"
            out_path = out_path.with_suffix(".gpkg")
            logs.append("⚠️ [도형] SHP은 점/선/면을 한 파일에 담지 못해 일부가 버려집니다. "
                        "손실 없이 담기 위해 GPKG로 저장합니다(QGIS에서 동일하게 열립니다).")

    temp_gpkg = out_path if save_format == "GPKG" else out_path.with_suffix(".gpkg")
    if temp_gpkg.exists():
        temp_gpkg.unlink()

    for index, layer in enumerate(layers):
        args = [ogr2ogr]
        args += ["-overwrite", "-f", "GPKG"] if index == 0 else ["-update", "-append", "-f", "GPKG", "-addfields"]
        if layer.kind == "SHP" and input_encoding:
            args += ["-oo", f"ENCODING={input_encoding}"]
        if unify:
            args += ["-t_srs", f"EPSG:{unify}"]
        if makevalid:
            args += ["-makevalid"]
        args += ["-nln", "merged", "-nlt", geom_arg, str(temp_gpkg), *ogr_source_args(layer)]
        ok, output = run_cmd(args)
        logs.append(f"[{layer.name}] {output}")
        if not ok:
            return False, out_path, "\n".join(logs)

    if save_format == "SHP":
        ok, actual, output = gpkg_to_final(temp_gpkg, "merged", out_path, "SHP", output_encoding)
        logs.append(output)
        return ok, actual, "\n".join(logs)
    return True, temp_gpkg, "\n".join(logs)


# ────────────────────────────── ③ 분할 ──────────────────────────────
def split_layer_by_values(
    layer: LayerInfo,
    column: str,
    values: list[str],
    out_dir: Path,
    output_format: str,
    input_encoding: str,
    output_encoding: str,
    contains: bool,
    target_epsg: str | None,
    makevalid: bool = False,
    progress: Callable[[int, int, str], None] | None = None,
) -> tuple[list[Path], str]:
    ogr2ogr, _ = gdals()
    if not ogr2ogr:
        return [], "ogr2ogr을 찾을 수 없습니다."
    logs: list[str] = []
    outputs: list[Path] = []
    out_dir.mkdir(parents=True, exist_ok=True)
    for index, value in enumerate(values, start=1):
        if progress:
            progress(index, len(values), value)
        name = f"{safe_name(layer.name)}_{safe_name(value)}"
        out_path = output_dataset_path(out_dir, name, output_format)
        where = (f"{quote_ident(column)} LIKE {sql_literal('%' + value + '%')}"
                 if contains else f"{quote_ident(column)} = {sql_literal(value)}")
        args = [ogr2ogr, "-overwrite", "-f", ogr_output_format(output_format)]
        if layer.kind == "SHP" and input_encoding:
            args += ["-oo", f"ENCODING={input_encoding}"]
        if target_epsg:
            args += ["-t_srs", f"EPSG:{target_epsg}"]
        if makevalid:
            args += ["-makevalid"]
        if output_format == "SHP":
            args += ["-lco", f"ENCODING={output_encoding}"]
        args += ["-where", where, str(out_path), *ogr_source_args(layer)]
        ok, output = run_cmd(args)
        logs.append(f"[{value}] {output}")
        if ok:
            outputs.append(out_path)
    return outputs, "\n".join(logs)


# ────────────────────────────── ④ 코드 결합 ──────────────────────────────
def join_code_table(
    layer: LayerInfo,
    mnum_col: str,
    start: int,
    length: int,
    code_csv_path: Path,
    code_key_col: str,
    value_cols: list[str],
    out_path: Path,
    output_format: str,
    input_encoding: str,
    output_encoding: str,
) -> tuple[bool, Path, str]:
    """속성의 코드 컬럼에서 substr로 키를 뽑아 코드표(CSV)를 LEFT JOIN한 새 레이어를 만듭니다."""
    ogr2ogr, _ = gdals()
    if not ogr2ogr:
        return False, out_path, "ogr2ogr을 찾을 수 없습니다."
    work = out_path.parent / "_codejoin_work.gpkg"
    if work.exists():
        work.unlink()
    logs: list[str] = []

    # 1) 대상 레이어 -> GPKG 레이어 'src' (읽기 인코딩 지정, 저장은 UTF-8)
    args = [ogr2ogr, "-overwrite", "-f", "GPKG", "-nln", "src"]
    if layer.kind == "SHP" and input_encoding:
        args += ["-oo", f"ENCODING={input_encoding}"]
    args += [str(work), *ogr_source_args(layer)]
    ok, log = run_cmd(args)
    logs.append(f"[src] {log}")
    if not ok:
        return False, out_path, "\n".join(logs)

    # 2) 정규화된 UTF-8 코드표 CSV -> GPKG 레이어 'codes' (모두 문자열)
    args = [ogr2ogr, "-update", "-f", "GPKG", "-nln", "codes",
            "-oo", "AUTODETECT_TYPE=NO", str(work), str(code_csv_path)]
    ok, log = run_cmd(args)
    logs.append(f"[codes] {log}")
    if not ok:
        return False, out_path, "\n".join(logs)

    # 3) LEFT JOIN
    select_parts = ["s.*"] + [f"c.{quote_ident(col)} AS {quote_ident(col)}" for col in value_cols]
    sql = (f"SELECT {', '.join(select_parts)} FROM src s LEFT JOIN codes c "
           f"ON TRIM(substr(s.{quote_ident(mnum_col)}, {int(start)}, {int(length)})) = TRIM(c.{quote_ident(code_key_col)})")
    ok, actual, log = sqlite_sql_to_output(work, sql, out_path, output_format, output_encoding)
    logs.append(f"[join] {log}")
    return ok, actual, "\n".join(logs)


def cleanup_temp(out_dir: Path) -> None:
    """처리 중 생긴 중간 파일(_safe_, _sqltmp_ 등)을 지웁니다."""
    if not out_dir.exists():
        return
    for path in out_dir.glob("_*"):
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink()
        except OSError:
            pass
