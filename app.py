from __future__ import annotations

import csv
import io
import os
import re
import shutil
import struct
import subprocess
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd
import streamlit as st


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
SHAPEFILE_PARTS = [".shp", ".shx", ".dbf", ".prj", ".cpg", ".qix", ".sbn", ".sbx"]
RUNTIME_DIR = Path(__file__).resolve().parent / ".runtime"


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

    for path_text in [r"C:\OSGeo4W\bin", r"C:\OSGeo4W64\bin", manual_bin]:
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


def _manual_gdal_bin() -> str | None:
    try:
        return st.session_state.get("gdal_bin_path") or None
    except Exception:
        return None


def gdals() -> tuple[str | None, str | None]:
    found_ogr2ogr = shutil.which("ogr2ogr")
    found_ogrinfo = shutil.which("ogrinfo")
    if found_ogr2ogr and found_ogrinfo:
        return found_ogr2ogr, found_ogrinfo

    for bin_dir in _gdal_bin_candidates(_manual_gdal_bin()):
        ogr2ogr = _exe_in_bin(bin_dir, "ogr2ogr")
        ogrinfo = _exe_in_bin(bin_dir, "ogrinfo")
        if ogr2ogr and ogrinfo:
            return ogr2ogr, ogrinfo
    return found_ogr2ogr, found_ogrinfo


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
    try:
        completed = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_gdal_env(args[0] if args else None),
        )
    except FileNotFoundError as exc:
        return False, str(exc)
    output = "\n".join(part for part in [completed.stdout, completed.stderr] if part)
    return completed.returncode == 0, output.strip()


def session_root() -> Path:
    if "workdir" not in st.session_state:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        st.session_state.workdir = str(RUNTIME_DIR / f"shp_tool_{uuid.uuid4().hex[:12]}")
    root = Path(st.session_state.workdir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def reset_workspace() -> None:
    old = st.session_state.get("workdir")
    if old and Path(old).exists():
        shutil.rmtree(old, ignore_errors=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    st.session_state.workdir = str(RUNTIME_DIR / f"shp_tool_{uuid.uuid4().hex[:12]}")
    st.session_state.layers = []


def save_uploads(files) -> Path:
    root = session_root()
    input_dir = root / "input"
    if input_dir.exists():
        shutil.rmtree(input_dir)
    input_dir.mkdir(parents=True, exist_ok=True)

    for uploaded in files:
        target = input_dir / uploaded.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(uploaded.getbuffer())
        if target.suffix.lower() == ".zip":
            extract_dir = input_dir / target.stem
            extract_dir.mkdir(exist_ok=True)
            with zipfile.ZipFile(target) as zf:
                zf.extractall(extract_dir)
    return input_dir


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


def discover_layers(input_dir: Path) -> list[LayerInfo]:
    layers: list[LayerInfo] = []
    for shp in sorted(input_dir.rglob("*.shp")):
        stem = shp.with_suffix("")
        prj = stem.with_suffix(".prj")
        cpg = stem.with_suffix(".cpg")
        prj_preview = prj.read_text(encoding="utf-8", errors="replace")[:400] if prj.exists() else ""
        cpg_text = cpg.read_text(encoding="utf-8", errors="replace").strip() if cpg.exists() else ""
        layers.append(
            LayerInfo(
                name=shp.stem,
                path=shp,
                kind="SHP",
                folder=shp.parent,
                has_shx=stem.with_suffix(".shx").exists(),
                has_dbf=stem.with_suffix(".dbf").exists(),
                has_prj=prj.exists(),
                cpg=cpg_text,
                prj_preview=prj_preview,
            )
        )
    for gpkg in sorted(input_dir.rglob("*.gpkg")):
        sublayers = gpkg_layer_names(gpkg)
        if len(sublayers) > 1:
            for sub in sublayers:
                layers.append(
                    LayerInfo(name=f"{gpkg.stem}:{sub}", path=gpkg, kind="GPKG", folder=gpkg.parent, sublayer=sub)
                )
        elif len(sublayers) == 1:
            layers.append(
                LayerInfo(name=gpkg.stem, path=gpkg, kind="GPKG", folder=gpkg.parent, sublayer=sublayers[0])
            )
        else:
            # ogrinfo 미탐색/실패 시 기존처럼 파일 단위 단일 레이어로 처리
            layers.append(LayerInfo(name=gpkg.stem, path=gpkg, kind="GPKG", folder=gpkg.parent))
    return layers


def dbf_fields(dbf_path: Path) -> list[dict[str, object]]:
    with dbf_path.open("rb") as fp:
        header = fp.read(32)
        if len(header) < 32:
            raise ValueError("DBF header is too short.")
        header_len = struct.unpack("<H", header[8:10])[0]
        fields = []
        while fp.tell() < header_len:
            desc = fp.read(32)
            if not desc or desc[0] == 0x0D:
                break
            raw_name = desc[:11].split(b"\x00", 1)[0]
            fields.append(
                {
                    "raw_name": raw_name,
                    "type": chr(desc[11]),
                    "length": desc[16],
                    "decimals": desc[17],
                }
            )
        return fields


def read_dbf_preview(dbf_path: Path, encoding: str, limit: int = 30) -> pd.DataFrame:
    with dbf_path.open("rb") as fp:
        header = fp.read(32)
        if len(header) < 32:
            return pd.DataFrame()
        record_count = struct.unpack("<I", header[4:8])[0]
        header_len = struct.unpack("<H", header[8:10])[0]
        record_len = struct.unpack("<H", header[10:12])[0]
        fields = dbf_fields(dbf_path)
        names = []
        for index, field in enumerate(fields, start=1):
            raw_name = field["raw_name"]
            assert isinstance(raw_name, bytes)
            name = raw_name.decode(encoding, errors="replace").strip() or f"field_{index}"
            names.append(name)

        fp.seek(header_len)
        rows = []
        for _ in range(min(record_count, limit)):
            record = fp.read(record_len)
            if len(record) < record_len:
                break
            if record[:1] == b"*":
                continue
            offset = 1
            row = {}
            for name, field in zip(names, fields):
                width = int(field["length"])
                raw = record[offset : offset + width]
                offset += width
                row[name] = raw.decode(encoding, errors="replace").strip()
            rows.append(row)
    return pd.DataFrame(rows)


def encoding_score(df: pd.DataFrame) -> int:
    text = " ".join(str(v) for v in df.head(20).to_numpy().ravel())
    bad_patterns = ["�", "Ã", "Â", "ì", "í", "ê", "¤"]
    return sum(text.count(pattern) for pattern in bad_patterns)


def layer_options(layers: list[LayerInfo]) -> list[str]:
    return [f"{idx + 1}. {layer.name} ({layer.kind})" for idx, layer in enumerate(layers)]


def selected_layers(labels: list[str], layers: list[LayerInfo]) -> list[LayerInfo]:
    result = []
    all_labels = layer_options(layers)
    for label in labels:
        if label in all_labels:
            result.append(layers[all_labels.index(label)])
    return result


def zip_paths(paths: Iterable[Path], zip_name: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in paths:
            if path.is_dir():
                for child in path.rglob("*"):
                    if child.is_file():
                        zf.write(child, child.relative_to(path.parent))
            elif path.suffix.lower() == ".shp":
                for child in shp_sidecars(path):
                    zf.write(child, child.relative_to(path.parent))
            elif path.exists():
                zf.write(path, path.name)
    buffer.seek(0)
    return buffer.getvalue()


def output_dataset_path(base_dir: Path, layer_name: str, output_format: str) -> Path:
    if output_format == "GPKG":
        return base_dir / f"{safe_name(layer_name)}.gpkg"
    return base_dir / f"{safe_name(layer_name)}.shp"


def ogr_output_format(output_format: str) -> str:
    return "GPKG" if output_format == "GPKG" else "ESRI Shapefile"


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
        return False, out_path, "ogr2ogr을 찾을 수 없습니다. GDAL 설치 후 다시 실행하세요."

    def build(fmt: str, target: Path) -> list[str]:
        a = [ogr2ogr, "-overwrite", "-f", ogr_output_format(fmt)]
        if layer.kind == "SHP" and input_encoding:
            a += ["-oo", f"ENCODING={input_encoding}"]
        if source_epsg:
            a += ["-s_srs", f"EPSG:{source_epsg}"]
        if target_epsg:
            a += ["-t_srs", f"EPSG:{target_epsg}"]
        if makevalid:
            a += ["-makevalid"]
        if fmt == "SHP":
            a += ["-lco", f"ENCODING={output_encoding}"]
        a += [str(target), *ogr_source_args(layer)]
        return a

    ok, log = run_cmd(build(output_format, out_path))
    if ok or output_format == "GPKG":
        return ok, out_path, log
    # SHP 저장 실패 -> GPKG로 자동 대체(빈손 방지)
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
    도형이 재투영 중 붕괴하거나 원본 좌표계 오판으로 유효범위를 벗어나 폐기되는 것을 최소화합니다.
    """
    ogr2ogr, _ = gdals()
    if not ogr2ogr:
        return False, out_path, {}, "ogr2ogr을 찾을 수 없습니다."
    work = out_path.parent / f"_safe_{safe_name(layer.name)}.gpkg"
    if work.exists():
        work.unlink()
    logs: list[str] = []
    counts: dict[str, object] = {"입력": ogr_layer_stats(layer.path, input_encoding, layer.sublayer).get("features")}

    # ① 원본 좌표계 확정 + 도형 복구(재투영 없이 네이티브 좌표에서)
    step_a = [ogr2ogr, "-overwrite", "-f", "GPKG", "-nln", "step", "-makevalid"]
    if layer.kind == "SHP" and input_encoding:
        step_a += ["-oo", f"ENCODING={input_encoding}"]
    if source_epsg:
        step_a += ["-a_srs", f"EPSG:{source_epsg}"]  # 변환 없이 소스 CRS만 확정(=먼저 정의)
    step_a += ["-nlt", "PROMOTE_TO_MULTI", str(work), *ogr_source_args(layer)]
    ok, log = run_cmd(step_a)
    logs.append(f"[① 도형복구/원본CRS확정] {log}")
    if not ok:
        return False, out_path, counts, "\n".join(logs)
    counts["복구후"] = ogr_layer_stats(work).get("features")

    # ② 재투영 + 도형 복구 (SHP 저장 실패 시 GPKG로 자동 대체)
    extra_b = ["-makevalid", "-nlt", "PROMOTE_TO_MULTI"]
    if target_epsg:
        extra_b += ["-t_srs", f"EPSG:{target_epsg}"]
    ok, actual, log = gpkg_to_final(work, "step", out_path, output_format, output_encoding, extra_args=extra_b)
    logs.append(f"[② 재투영] {log}")
    if not ok:
        return False, actual, counts, "\n".join(logs)
    counts["출력"] = ogr_layer_stats(actual).get("features")
    return True, actual, counts, "\n".join(logs)


def get_ogr_summary(layer: LayerInfo) -> str:
    _, ogrinfo = gdals()
    if not ogrinfo:
        return ""
    ok, output = run_cmd([ogrinfo, "-so", "-al", str(layer.path)])
    return output if ok else output


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


def gpkg_to_final(
    gpkg_path: Path,
    source_layer: str | None,
    out_path: Path,
    output_format: str,
    output_encoding: str,
    extra_args: list[str] | None = None,
) -> tuple[bool, Path, str]:
    """UTF-8 GPKG(중간 결과)를 최종 형식으로 저장합니다.

    SHP 저장이 실패하면 자동으로 GPKG로 대체 저장해, 어떤 경우에도 결과물이 남도록 합니다.
    실제 저장된 경로를 함께 반환합니다(대체 시 .gpkg 경로).
    """
    ogr2ogr, _ = gdals()
    if not ogr2ogr:
        return False, out_path, "ogr2ogr을 찾을 수 없습니다."
    src_tail = [str(gpkg_path)] + ([source_layer] if source_layer else [])
    if output_format == "GPKG":
        args = [ogr2ogr, "-overwrite", "-f", "GPKG"] + (extra_args or []) + [str(out_path), *src_tail]
        ok, log = run_cmd(args)
        return ok, out_path, log
    args = [ogr2ogr, "-overwrite", "-f", "ESRI Shapefile", "-lco", f"ENCODING={output_encoding}"] + (extra_args or []) + [str(out_path), *src_tail]
    ok, log = run_cmd(args)
    if ok:
        return True, out_path, log
    # SHP 저장 실패 -> GPKG로 대체(빈손 방지)
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
) -> tuple[bool, Path, str]:
    """SQLite dialect(공간함수 ST_*) 결과를 인코딩 안전하게 저장하고 실제 경로를 반환합니다.

    SQLite dialect로 SHP를 직접 쓰면 일부 GDAL 버전(예: 클라우드 apt gdal-bin)에서
    `-lco ENCODING`이 무시돼 한글이 ISO-8859-1로 손상(???)됩니다. 그래서 공간 SQL 결과는
    먼저 UTF-8이 보장되는 GPKG로 만들고, 최종 형식으로 다시 저장합니다.
    SHP 저장 실패 시 GPKG로 자동 대체합니다.
    """
    ogr2ogr, _ = gdals()
    if not ogr2ogr:
        return False, out_path, "ogr2ogr을 찾을 수 없습니다."
    tmp = out_path.parent / f"_sqltmp_{safe_name(out_path.stem)}.gpkg"
    if tmp.exists():
        tmp.unlink()
    a1 = [ogr2ogr, "-overwrite", "-f", "GPKG", "-dialect", "SQLite", "-sql", sql, "-nln", "result"]
    if oo_encoding:
        a1 += ["-oo", f"ENCODING={oo_encoding}"]
    if extra_args:
        a1 += extra_args
    a1 += [str(tmp), str(src_path)]
    ok, l1 = run_cmd(a1)
    if not ok:
        return False, out_path, l1
    ok2, actual, l2 = gpkg_to_final(tmp, "result", out_path, output_format, output_encoding)
    return ok2, actual, (l1 + "\n" + l2).strip()


def add_area_column(
    src_path: Path,
    output_format: str,
    output_encoding: str,
    decimals: int = 1,
    field: str = "area_m2",
) -> tuple[bool, Path, str]:
    """결과 레이어에 ST_Area 기반 면적(㎡) 컬럼을 추가한 새 파일을 만듭니다.

    좌표계가 미터 기반 투영좌표계(EPSG:5186 등)일 때만 ㎡가 정확합니다.
    src_path는 이 앱이 생성한 결과물이라 레이어명이 파일명과 같다고 가정합니다.
    """
    layer_name = src_path.stem
    dec = max(int(decimals), 0)
    sql = f"SELECT *, ROUND(ST_Area(geometry), {dec}) AS {quote_ident(field)} FROM {quote_ident(layer_name)}"
    out_path = src_path.with_name(f"{src_path.stem}_area{src_path.suffix}")
    ok, actual, log = sqlite_sql_to_output(
        src_path, sql, out_path, output_format, output_encoding,
        oo_encoding=(output_encoding if src_path.suffix.lower() == ".shp" else None),
    )
    return ok, (actual if ok else src_path), log


def shp_sidecars(shp_path: Path) -> list[Path]:
    stem = shp_path.with_suffix("")
    return [stem.with_suffix(ext) for ext in SHAPEFILE_PARTS if stem.with_suffix(ext).exists()]


def shapefile_to_zip_download(path: Path) -> bytes:
    files = shp_sidecars(path) if path.suffix.lower() == ".shp" else [path]
    return zip_paths(files, f"{path.stem}.zip")


def download_for_path(path: Path) -> tuple[str, bytes]:
    """실제 결과 파일 형식에 맞는 (파일명, 바이트)를 돌려줍니다.

    SHP은 sidecar까지 zip으로, GPKG(대체 포함)은 단일 파일로 제공합니다.
    """
    if path.suffix.lower() == ".shp":
        return f"{path.stem}.zip", shapefile_to_zip_download(path)
    return path.name, path.read_bytes()


def unique_values_from_dbf(layer: LayerInfo, column: str, encoding: str, limit: int = 5000) -> list[str]:
    dbf = layer.path.with_suffix(".dbf")
    if not dbf.exists():
        return []
    df = read_dbf_preview(dbf, encoding, limit=limit)
    if column not in df.columns:
        return []
    return sorted(v for v in df[column].dropna().astype(str).unique().tolist() if v != "")


def columns_for_layer(layer: LayerInfo, encoding: str) -> list[str]:
    if layer.kind != "SHP":
        return []
    dbf = layer.path.with_suffix(".dbf")
    if not dbf.exists():
        return []
    try:
        return list(read_dbf_preview(dbf, encoding, limit=1).columns)
    except Exception:
        return []


DISSOLVE_AGG_FUNCS = {
    "제외": None,
    "합계(SUM)": "SUM",
    "평균(AVG)": "AVG",
    "최대(MAX)": "MAX",
    "최소(MIN)": "MIN",
    "개수(COUNT)": "COUNT",
}


def dissolve_one_layer(
    layer: LayerInfo,
    column: str,
    out_path: Path,
    output_format: str,
    input_encoding: str,
    target_epsg: str | None,
    agg_map: dict[str, str] | None = None,
    makevalid: bool = False,
    output_encoding: str = "UTF-8",
) -> tuple[bool, Path, str]:
    layer_name = layer.path.stem
    select_parts = [quote_ident(column), "ST_Union(geometry) AS geometry"]
    for col, func in (agg_map or {}).items():
        if col == column or not func:
            continue
        select_parts.append(f"{func}({quote_ident(col)}) AS {quote_ident(col)}")
    sql = (
        f"SELECT {', '.join(select_parts)} "
        f"FROM {quote_ident(layer_name)} GROUP BY {quote_ident(column)}"
    )
    extra: list[str] = ["-nlt", "PROMOTE_TO_MULTI"]
    if target_epsg:
        extra += ["-t_srs", f"EPSG:{target_epsg}"]
    if makevalid:
        extra += ["-makevalid"]
    return sqlite_sql_to_output(
        layer.path, sql, out_path, output_format, output_encoding,
        oo_encoding=(input_encoding if layer.kind == "SHP" else None),
        extra_args=extra,
    )


def merge_layers(
    layers: list[LayerInfo],
    out_path: Path,
    output_format: str,
    target_epsg: str | None,
    input_encoding: str,
    output_encoding: str,
    makevalid: bool = False,
) -> tuple[bool, Path, str]:
    ogr2ogr, _ = gdals()
    if not ogr2ogr:
        return False, out_path, "ogr2ogr을 찾을 수 없습니다."
    temp_gpkg = out_path if output_format == "GPKG" else out_path.with_suffix(".gpkg")
    if temp_gpkg.exists():
        temp_gpkg.unlink()
    logs = []
    for idx, layer in enumerate(layers):
        args = [ogr2ogr]
        if idx == 0:
            args += ["-overwrite", "-f", "GPKG"]
        else:
            args += ["-update", "-append", "-f", "GPKG", "-addfields"]
        if layer.kind == "SHP" and input_encoding:
            args += ["-oo", f"ENCODING={input_encoding}"]
        if target_epsg:
            args += ["-t_srs", f"EPSG:{target_epsg}"]
        if makevalid:
            args += ["-makevalid"]
        args += ["-nln", "merged", "-nlt", "PROMOTE_TO_MULTI", str(temp_gpkg), *ogr_source_args(layer)]
        ok, output = run_cmd(args)
        logs.append(f"[{layer.name}] {output}")
        if not ok:
            return False, out_path, "\n".join(logs)

    if output_format == "SHP":
        # temp_gpkg(UTF-8) -> SHP, 실패 시 GPKG로 자동 대체
        ok, actual, output = gpkg_to_final(temp_gpkg, "merged", out_path, "SHP", output_encoding)
        logs.append(output)
        return ok, actual, "\n".join(logs)
    return True, temp_gpkg, "\n".join(logs)


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
) -> tuple[list[Path], str]:
    ogr2ogr, _ = gdals()
    if not ogr2ogr:
        return [], "ogr2ogr을 찾을 수 없습니다."
    logs = []
    outputs = []
    out_dir.mkdir(parents=True, exist_ok=True)
    for value in values:
        name = f"{safe_name(layer.name)}_{safe_name(value)}"
        out_path = output_dataset_path(out_dir, name, output_format)
        where = (
            f"{quote_ident(column)} LIKE {sql_literal('%' + value + '%')}"
            if contains
            else f"{quote_ident(column)} = {sql_literal(value)}"
        )
        args = [ogr2ogr, "-overwrite", "-f", ogr_output_format(output_format)]
        if layer.kind == "SHP" and input_encoding:
            args += ["-oo", f"ENCODING={input_encoding}"]
        if target_epsg:
            args += ["-t_srs", f"EPSG:{target_epsg}"]
        if makevalid:
            args += ["-makevalid"]
        if output_format == "SHP":
            args += ["-lco", f"ENCODING={output_encoding}"]
        args += ["-where", where, str(out_path), str(layer.path)]
        ok, output = run_cmd(args)
        logs.append(f"[{value}] {output}")
        if ok:
            outputs.append(out_path)
    return outputs, "\n".join(logs)


CSV_ENCODINGS = ["UTF-8-SIG", "CP949", "EUC-KR", "UTF-8"]


def read_code_table_enc(raw: bytes, encoding: str) -> pd.DataFrame:
    """지정한 인코딩으로 코드표 CSV를 읽습니다. 모든 값은 문자열(앞자리 0 보존)."""
    return pd.read_csv(io.BytesIO(raw), dtype=str, encoding=encoding, keep_default_na=False)


def text_garble_score(text: str) -> int:
    bad_patterns = ["�", "Ã", "Â", "ì", "í", "ê", "¤"]
    return sum(text.count(pattern) for pattern in bad_patterns)


def code_table_encoding_rows(raw: bytes) -> list[dict[str, object]]:
    """코드표 CSV를 인코딩 후보별로 읽어 깨짐 의심 점수를 매깁니다(헤더+값 모두 반영)."""
    rows: list[dict[str, object]] = []
    for enc in CSV_ENCODINGS:
        try:
            df = read_code_table_enc(raw, enc)
            score = encoding_score(df) + text_garble_score(" ".join(str(c) for c in df.columns))
            rows.append({"인코딩": enc, "깨짐 의심 점수": score, "컬럼": ", ".join(str(c) for c in df.columns[:8])})
        except Exception as exc:
            rows.append({"인코딩": enc, "깨짐 의심 점수": 9999, "컬럼": str(exc)[:60]})
    return rows


def best_encoding(rows: list[dict[str, object]]) -> str:
    valid = [r for r in rows if isinstance(r["깨짐 의심 점수"], int) and r["깨짐 의심 점수"] < 9999]
    pool = valid or rows
    return str(min(pool, key=lambda r: r["깨짐 의심 점수"])["인코딩"])


def read_code_table(raw: bytes) -> tuple[pd.DataFrame, str]:
    """업로드된 코드표 CSV를 인코딩 자동판별로 읽습니다. 모든 값은 문자열(앞자리 0 보존)."""
    enc = best_encoding(code_table_encoding_rows(raw))
    return read_code_table_enc(raw, enc), enc


def substr_key_series(values: Iterable[object], start: int, length: int) -> pd.Series:
    """SQLite substr(x, start, length)와 동일한 규칙(1-기준)으로 파이썬에서 코드 추출."""
    begin = max(start - 1, 0)
    series = pd.Series([str(v) if v is not None else "" for v in values])
    return series.str.slice(begin, begin + length).str.strip()


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
    """SHP 속성에 substr(mnum, start, length) 키로 코드표를 LEFT JOIN한 새 레이어를 만듭니다."""
    ogr2ogr, _ = gdals()
    if not ogr2ogr:
        return False, out_path, "ogr2ogr을 찾을 수 없습니다."
    work = out_path.parent / "_codejoin_work.gpkg"
    if work.exists():
        work.unlink()
    logs: list[str] = []

    # 1) 대상 SHP -> GPKG 레이어 'src' (읽기 인코딩 지정, 저장은 UTF-8)
    a1 = [ogr2ogr, "-overwrite", "-f", "GPKG", "-nln", "src"]
    if layer.kind == "SHP" and input_encoding:
        a1 += ["-oo", f"ENCODING={input_encoding}"]
    a1 += [str(work), *ogr_source_args(layer)]
    ok, log = run_cmd(a1)
    logs.append(f"[src] {log}")
    if not ok:
        return False, out_path, "\n".join(logs)

    # 2) 정규화된 UTF-8 코드표 CSV -> GPKG 레이어 'codes' (모두 문자열)
    a2 = [ogr2ogr, "-update", "-f", "GPKG", "-nln", "codes", "-oo", "AUTODETECT_TYPE=NO", str(work), str(code_csv_path)]
    ok, log = run_cmd(a2)
    logs.append(f"[codes] {log}")
    if not ok:
        return False, out_path, "\n".join(logs)

    # 3) LEFT JOIN
    select_parts = ["s.*"] + [f"c.{quote_ident(col)} AS {quote_ident(col)}" for col in value_cols]
    sql = (
        f"SELECT {', '.join(select_parts)} FROM src s "
        f"LEFT JOIN codes c "
        f"ON TRIM(substr(s.{quote_ident(mnum_col)}, {int(start)}, {int(length)})) = TRIM(c.{quote_ident(code_key_col)})"
    )
    # work.gpkg는 이미 UTF-8이므로 oo_encoding 불필요. SHP은 GPKG 경유로 인코딩 안전 저장.
    ok, actual, log = sqlite_sql_to_output(work, sql, out_path, output_format, output_encoding)
    logs.append(f"[join] {log}")
    return ok, actual, "\n".join(logs)


def render_layer_status(layers: list[LayerInfo], encoding: str) -> None:
    if not layers:
        st.info("왼쪽에서 SHP zip, SHP 구성 파일, GPKG 파일을 업로드하세요.")
        return
    rows = []
    for layer in layers:
        rows.append(
            {
                "레이어": layer.name,
                "형식": layer.kind,
                "shx": "있음" if layer.has_shx else ("-" if layer.kind != "SHP" else "없음"),
                "dbf": "있음" if layer.has_dbf else ("-" if layer.kind != "SHP" else "없음"),
                "prj": "있음" if layer.has_prj else ("-" if layer.kind != "SHP" else "없음"),
                "cpg": layer.cpg or "-",
                "경로": str(layer.path),
            }
        )
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    shp_layers = [layer for layer in layers if layer.kind == "SHP" and layer.has_dbf]
    if shp_layers:
        layer = st.selectbox("속성 미리보기 레이어", shp_layers, format_func=lambda item: item.name)
        dbf = layer.path.with_suffix(".dbf")
        previews = []
        for enc in ENCODINGS:
            try:
                df = read_dbf_preview(dbf, enc)
                previews.append({"인코딩": enc, "깨짐 의심 점수": encoding_score(df), "컬럼": ", ".join(df.columns[:8])})
            except Exception as exc:
                previews.append({"인코딩": enc, "깨짐 의심 점수": 9999, "컬럼": str(exc)})
        st.dataframe(pd.DataFrame(previews), width="stretch", hide_index=True)
        try:
            st.caption(f"현재 선택 인코딩: {encoding}")
            st.dataframe(read_dbf_preview(dbf, encoding), width="stretch", hide_index=True)
        except Exception as exc:
            st.warning(f"DBF 미리보기를 읽지 못했습니다: {exc}")


def render_convert_tab(layers: list[LayerInfo], encoding: str, output_encoding: str) -> None:
    st.subheader("좌표계 변환")
    if not layers:
        st.stop()
    labels = st.multiselect("변환할 레이어", layer_options(layers), default=layer_options(layers))
    target_choice = st.selectbox("목표 좌표계", list(COMMON_EPSG.keys()), index=0)
    target_epsg = COMMON_EPSG[target_choice]
    if target_epsg == "custom":
        target_epsg = st.text_input("목표 EPSG 번호", value="5186").strip()
    source_override = st.text_input("원본 EPSG 강제 지정(선택, .prj가 없거나 틀릴 때만)", value="").strip()
    output_format = st.radio("저장 형식", ["SHP", "GPKG"], horizontal=True)
    safe_mode = st.checkbox(
        "안전 변환 모드(권장): 도형 복구 2단계 + 전후 피처수 검증",
        value=True,
        help="① 원본 좌표계 확정+도형복구(네이티브 좌표) → ② 재투영+도형복구. 불안정한 SHP에서 재투영 중 폴리곤이 한두 개 사라지는 문제를 막고, 어느 단계에서 사라졌는지 리포트합니다.",
    )
    makevalid = st.checkbox(
        "도형 유효화(-makevalid) 적용",
        value=False,
        disabled=safe_mode,
        help="일반 변환 시 도형 보정. 안전 변환 모드에서는 항상 적용되므로 비활성화됩니다.",
    )
    add_area = st.checkbox("면적 컬럼(area_m2, ㎡) 추가", value=False, help="변환된 결과에 폴리곤 면적을 ㎡로 계산해 넣습니다. 미터 단위 투영좌표계에서만 정확합니다.")
    area_decimals = st.number_input("면적 소수점 자리수", min_value=0, max_value=6, value=1, step=1, key="convert_area_dec", disabled=not add_area) if add_area else 1
    if add_area and target_epsg == "4326":
        st.warning("EPSG:4326(경위도)는 면적이 ㎡가 아니라 제곱도로 계산됩니다. 5186 등 미터 좌표계를 목표로 선택하세요.")
    if safe_mode and not source_override:
        st.caption("⚠️ 원본 .prj가 불안정하면 '원본 EPSG 강제 지정'에 실제 좌표계(예: 5174)를 넣어야 재투영 중 피처 손실을 막습니다.")

    if st.button("좌표계 변환 실행", type="primary"):
        chosen = selected_layers(labels, layers)
        out_dir = session_root() / "converted"
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True)
        results = []
        logs = []
        stats_rows = []
        dropped_any = False
        for layer in chosen:
            before = ogr_layer_stats(layer.path, encoding, layer.sublayer)
            out_path = output_dataset_path(out_dir, f"{layer.name}_{target_epsg}", output_format)
            if safe_mode:
                ok, out_path, counts, log = convert_layer_safe(
                    layer, out_path, target_epsg, source_override or None, output_format, encoding, output_encoding
                )
                repaired_n = counts.get("복구후")
            else:
                ok, out_path, log = convert_layer(layer, out_path, target_epsg, source_override or None, output_format, encoding, output_encoding, makevalid)
                repaired_n = None
            logs.append(f"## {layer.name}\n{log}")
            if ok:
                if add_area:
                    area_ok, out_path, area_log = add_area_column(out_path, output_format, output_encoding, int(area_decimals))
                    logs.append(f"[면적] {area_log}" if not area_ok else "[면적] area_m2 추가 완료")
                results.append(out_path)
                after = ogr_layer_stats(out_path)
                before_n = before.get("features")
                after_n = after.get("features")
                lost = (before_n - after_n) if (isinstance(before_n, int) and isinstance(after_n, int)) else None
                if lost:
                    dropped_any = True
                row = {
                    "레이어": layer.name,
                    "변환 전": before_n if before_n is not None else "-",
                }
                if safe_mode:
                    row["복구후"] = repaired_n if repaired_n is not None else "-"
                row["변환 후"] = after_n if after_n is not None else "-"
                row["손실"] = (f"❌ -{lost}" if lost else "✅ 0") if lost is not None else "?"
                row["변환 전 범위"] = before.get("extent", "-")
                row["변환 후 범위"] = after.get("extent", "-")
                stats_rows.append(row)
        if results:
            st.success(f"{len(results)}개 레이어 변환 완료")
            st.download_button("결과 전체 다운로드(zip)", zip_paths(results, "converted.zip"), "converted.zip")
            if output_format == "SHP" and any(p.suffix.lower() == ".gpkg" for p in results):
                st.warning("일부 레이어는 SHP 저장에 실패해 GPKG로 대체 저장했습니다(zip 안에 .gpkg로 들어있고, QGIS에서 동일하게 열립니다).")
            if dropped_any:
                st.error(
                    "⚠️ 변환 중 사라진 피처가 있습니다(손실 열 확인). 원인은 보통 (1) 원본 좌표계 오판 또는 (2) 불량 도형입니다. "
                    "'원본 EPSG 강제 지정'에 실제 좌표계를 넣거나, 안전 변환 모드가 이미 켜져 있는지 확인하세요."
                )
            if stats_rows:
                st.caption("변환 단계별 feature 개수 / extent 비교" + (" (복구후=①도형복구 직후, 재투영 없이)" if safe_mode else ""))
                st.dataframe(pd.DataFrame(stats_rows), width="stretch", hide_index=True)
        else:
            st.error("변환 결과가 없습니다. 로그를 확인하세요.")
        log_text = "\n\n".join(logs) or "로그 없음"
        with st.expander("처리 로그", expanded=not results or dropped_any):
            st.code(log_text)
        st.download_button("작업 로그 다운로드", log_text, "convert_log.txt", key="convert_log_dl")


def render_merge_tab(layers: list[LayerInfo], encoding: str, output_encoding: str) -> None:
    st.subheader("레이어 병합")
    if not layers:
        st.stop()
    mode = st.radio("병합 방식", ["한 SHP 내 컬럼값 기준 병합", "여러 레이어 병합"], horizontal=True)
    target_epsg = st.text_input("병합 전 목표 EPSG 통일(선택)", value="").strip()
    output_format = st.radio("결과 저장 형식", ["SHP", "GPKG"], horizontal=True, key="merge_format")
    makevalid = st.checkbox("도형 유효화(-makevalid) 적용", value=False, key="merge_makevalid", help="병합 시 잘못된 도형을 자동 보정합니다.")
    out_dir = session_root() / "merged"

    if mode == "한 SHP 내 컬럼값 기준 병합":
        shp_layers = [layer for layer in layers if layer.kind == "SHP" and layer.has_dbf]
        if not shp_layers:
            st.warning("DBF가 있는 SHP 레이어가 필요합니다.")
            return
        layer = st.selectbox("대상 SHP", shp_layers, format_func=lambda item: item.name, key="merge_one_layer")
        columns = columns_for_layer(layer, encoding)
        column = st.selectbox("묶을 기준 컬럼", columns, key="merge_column")
        st.caption("같은 컬럼값을 가진 도형들을 하나의 멀티파트 도형으로 합칩니다.")
        add_area = st.checkbox("면적 컬럼(area_m2, ㎡) 추가", value=False, key="merge_area", help="병합된 구역별 면적을 ㎡로 계산해 넣습니다. 미터 단위 투영좌표계에서만 정확합니다.")
        area_decimals = st.number_input("면적 소수점 자리수", min_value=0, max_value=6, value=1, step=1, key="merge_area_dec", disabled=not add_area) if add_area else 1
        if add_area and target_epsg == "4326":
            st.warning("EPSG:4326(경위도)는 면적이 ㎡가 아니라 제곱도로 계산됩니다. 5186 등 미터 좌표계로 통일하세요.")

        other_columns = [col for col in columns if col != column]
        agg_map: dict[str, str] = {}
        with st.expander("속성 집계 옵션(선택)", expanded=False):
            st.caption("기준 컬럼 외 나머지 속성을 어떻게 요약할지 정합니다. '제외'는 결과에서 뺍니다.")
            if other_columns:
                agg_df = pd.DataFrame({"컬럼": other_columns, "집계": ["제외"] * len(other_columns)})
                edited = st.data_editor(
                    agg_df,
                    hide_index=True,
                    width="stretch",
                    disabled=["컬럼"],
                    column_config={
                        "집계": st.column_config.SelectboxColumn("집계", options=list(DISSOLVE_AGG_FUNCS.keys()), required=True)
                    },
                    key="merge_agg_editor",
                )
                for _, row in edited.iterrows():
                    func = DISSOLVE_AGG_FUNCS.get(str(row["집계"]))
                    if func:
                        agg_map[str(row["컬럼"])] = func
            else:
                st.caption("집계할 다른 속성 컬럼이 없습니다.")

        if st.button("내부 컬럼값 기준 병합 실행", type="primary"):
            if out_dir.exists():
                shutil.rmtree(out_dir)
            out_dir.mkdir(parents=True)
            out_path = output_dataset_path(out_dir, f"{layer.name}_dissolved_by_{column}", output_format)
            ok, out_path, log = dissolve_one_layer(layer, column, out_path, output_format, encoding, target_epsg or None, agg_map, makevalid, output_encoding)
            if ok and add_area:
                area_ok, out_path, area_log = add_area_column(out_path, output_format, output_encoding, int(area_decimals))
                log = f"{log}\n[면적] {'area_m2 추가 완료' if area_ok else area_log}"
            if ok:
                st.success("병합 완료")
                if out_path.suffix.lower() == ".gpkg" and output_format == "SHP":
                    st.warning("SHP 저장에 실패해 GPKG로 대체 저장했습니다(QGIS에서 동일하게 열립니다).")
                filename, data = download_for_path(out_path)
                st.download_button("결과 다운로드", data, filename)
            else:
                st.error("병합 실패")
            st.code(log or "로그 없음")
            st.download_button("작업 로그 다운로드", log or "로그 없음", "merge_dissolve_log.txt", key="merge_one_log_dl")
    else:
        labels = st.multiselect("병합할 레이어", layer_options(layers), default=layer_options(layers), key="merge_many_layers")
        if st.button("여러 레이어 병합 실행", type="primary"):
            chosen = selected_layers(labels, layers)
            if len(chosen) < 2:
                st.warning("두 개 이상 레이어를 선택하세요.")
                return
            if out_dir.exists():
                shutil.rmtree(out_dir)
            out_dir.mkdir(parents=True)
            out_path = output_dataset_path(out_dir, "merged_layers", output_format)
            ok, out_path, log = merge_layers(chosen, out_path, output_format, target_epsg or None, encoding, output_encoding, makevalid)
            if ok:
                st.success("병합 완료")
                if out_path.suffix.lower() == ".gpkg" and output_format == "SHP":
                    st.warning("SHP 저장에 실패해 GPKG로 대체 저장했습니다(QGIS에서 동일하게 열립니다).")
                filename, data = download_for_path(out_path)
                st.download_button("결과 다운로드", data, filename)
                total = sum(int(ogr_layer_stats(item.path, encoding, item.sublayer).get("features", 0) or 0) for item in chosen)
                merged_n = ogr_layer_stats(out_path).get("features")
                if merged_n is not None:
                    st.caption(f"입력 feature 합계: {total} → 병합 결과: {merged_n}")
            else:
                st.error("병합 실패")
            st.code(log or "로그 없음")
            st.download_button("작업 로그 다운로드", log or "로그 없음", "merge_layers_log.txt", key="merge_many_log_dl")


def render_split_tab(layers: list[LayerInfo], encoding: str, output_encoding: str) -> None:
    st.subheader("레이어 분할")
    if not layers:
        st.stop()
    mode = st.radio("분할 방식", ["한 SHP 내 컬럼값 기준 분할", "여러 레이어 분할"], horizontal=True)
    target_epsg = st.text_input("분할 결과 목표 EPSG(선택)", value="", key="split_target").strip()
    output_format = st.radio("결과 저장 형식", ["SHP", "GPKG"], horizontal=True, key="split_format")
    contains = st.checkbox("포함 조건으로 분할(%지역% 방식)", value=False)
    makevalid = st.checkbox("도형 유효화(-makevalid) 적용", value=False, key="split_makevalid", help="분할 시 잘못된 도형을 자동 보정합니다.")
    out_dir = session_root() / "split"

    shp_layers = [layer for layer in layers if layer.kind == "SHP" and layer.has_dbf]
    if not shp_layers:
        st.warning("DBF가 있는 SHP 레이어가 필요합니다.")
        return

    if mode == "한 SHP 내 컬럼값 기준 분할":
        targets = [st.selectbox("대상 SHP", shp_layers, format_func=lambda item: item.name, key="split_one")]
    else:
        labels = st.multiselect("분할할 SHP 레이어", [layer.name for layer in shp_layers], default=[layer.name for layer in shp_layers])
        targets = [layer for layer in shp_layers if layer.name in labels]

    first = targets[0] if targets else shp_layers[0]
    columns = columns_for_layer(first, encoding)
    column = st.selectbox("분할 기준 컬럼", columns, key="split_column")
    detected_values = unique_values_from_dbf(first, column, encoding, limit=1000)[:200] if column else []
    value_text = st.text_area(
        "분할할 값(한 줄에 하나, 비우면 감지된 전체 고유값 사용)",
        value="",
        placeholder="\n".join(detected_values[:5]),
        height=120,
    )
    st.caption(f"감지된 값 예시: {', '.join(detected_values[:10]) if detected_values else '없음'}")

    if st.button("분할 실행", type="primary"):
        values = [line.strip() for line in value_text.splitlines() if line.strip()] or detected_values
        if not values:
            st.warning("분할할 값을 찾지 못했습니다.")
            return
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True)
        all_outputs = []
        logs = []
        for layer in targets:
            layer_columns = columns_for_layer(layer, encoding)
            if column not in layer_columns:
                logs.append(f"[{layer.name}] 기준 컬럼 없음: {column}")
                continue
            outputs, log = split_layer_by_values(
                layer,
                column,
                values,
                out_dir / safe_name(layer.name),
                output_format,
                encoding,
                output_encoding,
                contains,
                target_epsg or None,
                makevalid,
            )
            all_outputs.extend(outputs)
            logs.append(f"## {layer.name}\n{log}")
        if all_outputs:
            st.success(f"{len(all_outputs)}개 결과 생성")
            st.download_button("분할 결과 전체 다운로드(zip)", zip_paths(all_outputs, "split.zip"), "split.zip")
        else:
            st.error("분할 결과가 없습니다.")
        log_text = "\n\n".join(logs) or "로그 없음"
        with st.expander("처리 로그", expanded=not all_outputs):
            st.code(log_text)
        st.download_button("작업 로그 다운로드", log_text, "split_log.txt", key="split_log_dl")


def render_join_tab(layers: list[LayerInfo], encoding: str, output_encoding: str) -> None:
    st.subheader("코드 결합")
    st.caption("SHP 속성의 MNUM 같은 컬럼에서 substr로 코드를 뽑아 용도지역 코드표(CSV)를 결합합니다. 원본 DBF를 직접 고치지 않고 결합된 새 SHP/GPKG를 만듭니다.")
    if not layers:
        st.stop()
    shp_layers = [layer for layer in layers if layer.kind == "SHP" and layer.has_dbf]
    if not shp_layers:
        st.warning("DBF가 있는 SHP 레이어가 필요합니다.")
        return

    layer = st.selectbox("대상 SHP", shp_layers, format_func=lambda item: item.name, key="join_layer")
    columns = columns_for_layer(layer, encoding)
    if not columns:
        st.warning("이 레이어의 컬럼을 읽지 못했습니다. 입력 DBF 인코딩을 확인하세요.")
        return

    # 대상 SHP 인코딩 확인(깨짐 점수) — 사이드바의 '입력 DBF 인코딩'이 맞는지 검증
    dbf_path = layer.path.with_suffix(".dbf")
    with st.expander(f"① 대상 SHP 인코딩 확인 (현재: {encoding})", expanded=False):
        enc_rows = []
        for enc in ENCODINGS:
            try:
                dfp = read_dbf_preview(dbf_path, enc, limit=30)
                enc_rows.append({"인코딩": enc, "깨짐 의심 점수": encoding_score(dfp), "컬럼": ", ".join(dfp.columns[:8])})
            except Exception as exc:
                enc_rows.append({"인코딩": enc, "깨짐 의심 점수": 9999, "컬럼": str(exc)[:60]})
        st.dataframe(pd.DataFrame(enc_rows), width="stretch", hide_index=True)
        best_shp_enc = best_encoding(enc_rows)
        if best_shp_enc != encoding:
            st.warning(f"현재 인코딩({encoding})보다 '{best_shp_enc}'의 깨짐 점수가 낮습니다. 한글이 깨져 보이면 사이드바의 '입력 DBF 인코딩'을 {best_shp_enc}로 바꾸세요.")
        try:
            st.dataframe(read_dbf_preview(dbf_path, encoding, limit=10), width="stretch", hide_index=True)
        except Exception as exc:
            st.warning(f"미리보기를 읽지 못했습니다: {exc}")

    col_a, col_b, col_c = st.columns([2, 1, 1])
    with col_a:
        default_idx = next((i for i, c in enumerate(columns) if c.lower() == "mnum"), 0)
        mnum_col = st.selectbox("코드가 들어있는 컬럼(MNUM 등)", columns, index=default_idx, key="join_mnum")
    with col_b:
        start = st.number_input("substr 시작 위치(1부터)", min_value=1, value=21, step=1, key="join_start")
    with col_c:
        length = st.number_input("길이(자리수)", min_value=1, value=6, step=1, key="join_len")

    # 추출 코드 미리보기(21/6이 맞는지 즉시 확인)
    preview = read_dbf_preview(layer.path.with_suffix(".dbf"), encoding, limit=200)
    keys = pd.Series(dtype=str)
    if mnum_col in preview.columns:
        keys = substr_key_series(preview[mnum_col].tolist(), int(start), int(length))
        sample = ", ".join(dict.fromkeys(keys[keys != ""].tolist()[:10]))
        st.caption(f"추출된 코드 예시: {sample or '없음'}  (미리보기 {len(keys)}행 기준)")

    st.markdown("**용도지역 코드표 CSV 업로드**")
    code_file = st.file_uploader("코드표 CSV (예: ZONE_UCODE.csv)", type=["csv"], key="join_csv")
    if code_file is None:
        st.info("코드표 CSV를 업로드하면 조인 키 컬럼과 가져올 컬럼을 선택할 수 있습니다.")
        return

    raw = code_file.getvalue()
    enc_rows = code_table_encoding_rows(raw)
    with st.expander("② 코드표 CSV 인코딩 확인", expanded=True):
        st.dataframe(pd.DataFrame(enc_rows), width="stretch", hide_index=True)
        st.caption("깨짐 의심 점수가 가장 낮은 인코딩이 보통 정답입니다. 미리보기에서 한글이 정상인지 확인 후 선택하세요.")
    recommended = best_encoding(enc_rows)
    csv_enc = st.selectbox(
        "코드표 인코딩 선택",
        CSV_ENCODINGS,
        index=CSV_ENCODINGS.index(recommended) if recommended in CSV_ENCODINGS else 0,
        key="join_csv_enc",
        help="권장값(깨짐 점수 최저)이 기본 선택됩니다. 미리보기가 깨지면 다른 값을 선택하세요.",
    )
    try:
        code_df = read_code_table_enc(raw, csv_enc)
    except Exception as exc:
        st.error(f"선택한 인코딩({csv_enc})으로 코드표를 읽지 못했습니다: {exc}")
        return
    st.caption(f"코드표: {csv_enc} · {len(code_df)}행 · 컬럼 {list(code_df.columns)}")
    st.dataframe(code_df.head(20), width="stretch", hide_index=True)

    code_cols = list(code_df.columns)
    key_default = next((i for i, c in enumerate(code_cols) if any(k in c.lower() for k in ["code", "코드", "ucode"])), 0)
    join_key_col = st.selectbox("코드표의 조인 키 컬럼(6자리 코드)", code_cols, index=key_default, key="join_keycol")
    value_cols = st.multiselect(
        "결합할 코드표 컬럼(속성에 붙일 값)",
        [c for c in code_cols if c != join_key_col],
        default=[c for c in code_cols if c != join_key_col],
        key="join_valcols",
    )
    output_format = st.radio("결과 저장 형식", ["SHP", "GPKG"], horizontal=True, key="join_format")

    # 매칭률 미리보기
    if not keys.empty and join_key_col in code_df.columns:
        codeset = set(code_df[join_key_col].astype(str).str.strip())
        nonempty = keys[keys != ""]
        if len(nonempty):
            matched = nonempty.isin(codeset)
            rate = matched.mean() * 100
            unmatched_samples = ", ".join(dict.fromkeys(nonempty[~matched].tolist()[:8]))
            st.info(f"미리보기 매칭률: {rate:.0f}%  ({int(matched.sum())}/{len(nonempty)})" + (f" · 미매칭 예시: {unmatched_samples}" if unmatched_samples else ""))
            if rate == 0:
                st.warning("매칭이 0%입니다. substr 시작 위치/길이 또는 조인 키 컬럼을 다시 확인하세요.")

    if output_format == "SHP":
        st.caption("SHP는 필드명이 10바이트로 잘릴 수 있습니다(한글 5자). 컬럼명이 길거나 한글이면 GPKG를 권장합니다.")

    if not value_cols:
        st.warning("결합할 컬럼을 하나 이상 선택하세요.")
        return

    if st.button("코드 결합 실행", type="primary"):
        out_dir = session_root() / "codejoin"
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True)
        # 정규화된 UTF-8 코드표 저장
        code_csv = out_dir / "codes_utf8.csv"
        code_df.to_csv(code_csv, index=False, encoding="utf-8")
        out_path = output_dataset_path(out_dir, f"{layer.name}_joined", output_format)
        ok, out_path, log = join_code_table(
            layer, mnum_col, int(start), int(length), code_csv, join_key_col, value_cols,
            out_path, output_format, encoding, output_encoding,
        )
        if ok:
            st.success("코드 결합 완료")
            if out_path.suffix.lower() == ".gpkg" and output_format == "SHP":
                st.warning("SHP 저장에 실패해 GPKG로 대체 저장했습니다(QGIS에서 동일하게 열립니다).")
            filename, data = download_for_path(out_path)
            st.download_button("결과 다운로드", data, filename)
            try:
                joined_preview = read_dbf_preview(out_path.with_suffix(".dbf"), output_encoding, limit=20) if out_path.suffix.lower() == ".shp" else None
                if joined_preview is not None:
                    st.caption("결합 결과 미리보기")
                    st.dataframe(joined_preview, width="stretch", hide_index=True)
            except Exception:
                pass
        else:
            st.error("코드 결합 실패. 로그를 확인하세요.")
        with st.expander("처리 로그", expanded=not ok):
            st.code(log or "로그 없음")
        st.download_button("작업 로그 다운로드", log or "로그 없음", "codejoin_log.txt", key="join_log_dl")


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    st.title(APP_TITLE)

    with st.sidebar:
        st.header("파일")
        files = st.file_uploader(
            "SHP zip, SHP 구성 파일, GPKG 업로드",
            accept_multiple_files=True,
            type=["zip", "shp", "shx", "dbf", "prj", "cpg", "gpkg"],
        )
        if st.button("업로드 파일 읽기", type="primary", disabled=not files):
            reset_workspace()
            input_dir = save_uploads(files)
            st.session_state.layers = discover_layers(input_dir)
        if st.button("작업 초기화"):
            reset_workspace()
            st.rerun()

        st.header("공통 설정")
        input_encoding = st.selectbox("입력 DBF 인코딩", ENCODINGS, index=1)
        output_encoding = st.selectbox("SHP 출력 인코딩", ["UTF-8", "CP949"], index=0)
        gdal_bin_path = st.text_input(
            "GDAL/QGIS bin 경로",
            value=st.session_state.get("gdal_bin_path", ""),
            placeholder=r"C:\Program Files\QGIS 3.30.3\bin",
            help="자동 탐색이 실패할 때만 입력하세요. ogr2ogr.exe와 ogrinfo.exe가 들어있는 폴더입니다.",
        )
        st.session_state.gdal_bin_path = gdal_bin_path.strip()

        st.header("GDAL 상태")
        ogr2ogr, ogrinfo = gdals()
        st.write(f"ogr2ogr: `{ogr2ogr or '없음'}`")
        st.write(f"ogrinfo: `{ogrinfo or '없음'}`")
        if not ogr2ogr:
            st.warning("실제 변환/병합/분할 실행에는 GDAL CLI가 필요합니다. QGIS bin 경로를 입력하세요.")
            with st.expander("GDAL 설치/연결 방법 보기", expanded=True):
                st.markdown(
                    "**방법 1. 이미 QGIS가 설치된 경우**\n"
                    "1. QGIS 설치 폴더 안 `bin` 폴더를 엽니다.\n"
                    "2. 그 안에 `ogr2ogr.exe`, `ogrinfo.exe`가 있는지 확인합니다.\n"
                    "3. 위 `GDAL/QGIS bin 경로` 칸에 그 `bin` 폴더 경로를 붙여넣습니다.\n\n"
                    "일반적인 경로 예:\n"
                    "```\nC:\\Program Files\\QGIS 3.30.3\\bin\nC:\\OSGeo4W\\bin\n```\n\n"
                    "**방법 2. GDAL만 새로 설치**\n"
                    "```\nwinget install OSGeo.GDAL\n```\n"
                    "설치 후 앱을 다시 실행하거나 `bin` 경로를 직접 입력하세요."
                )
        elif not shutil.which("ogr2ogr"):
            st.caption("PATH에는 없지만 앱이 QGIS/OSGeo4W 또는 입력 경로에서 GDAL을 찾았습니다.")

    layers: list[LayerInfo] = st.session_state.get("layers", [])
    with st.expander("업로드 레이어/속성 미리보기", expanded=True):
        render_layer_status(layers, input_encoding)

    tab_convert, tab_merge, tab_split, tab_join = st.tabs(
        ["1. 좌표계 변환", "2. 레이어 병합", "3. 레이어 분할", "4. 코드 결합"]
    )
    with tab_convert:
        render_convert_tab(layers, input_encoding, output_encoding)
    with tab_merge:
        render_merge_tab(layers, input_encoding, output_encoding)
    with tab_split:
        render_split_tab(layers, input_encoding, output_encoding)
    with tab_join:
        render_join_tab(layers, input_encoding, output_encoding)


if __name__ == "__main__":
    main()
