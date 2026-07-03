# SHP 좌표변환/분할/병합 앱

Streamlit 기반 SHP/GPKG 처리 도구입니다.

## 기능

- 여러 SHP zip 또는 SHP 구성 파일 업로드
- DBF 속성 미리보기 및 인코딩 후보 비교
- 좌표계 일괄 변환
- 한 SHP 내부 컬럼값 기준 병합
- 여러 레이어 병합
- 한 SHP 내부 컬럼값 기준 분할
- 여러 레이어 컬럼값 기준 일괄 분할
- SHP 결과는 누락 방지를 위해 zip으로 다운로드

## 필요 프로그램

앱 UI는 Python 패키지 `streamlit`, `pandas`만 필요합니다.
실제 공간 변환/분할/병합 작업은 GDAL CLI가 필요합니다.

정확히는 QGIS 프로그램 창을 연동하는 것이 아니라, QGIS 설치 폴더에 함께 들어있는
GDAL/OGR 실행파일을 앱이 호출합니다.

필수 실행파일:

```text
ogr2ogr.exe  좌표계 변환, SHP/GPKG 변환, 병합, 분할 실행
ogrinfo.exe  레이어 정보 확인
```

QGIS가 설치된 PC라면 보통 아래 같은 폴더에 이미 들어 있습니다.

```text
C:\Program Files\QGIS 3.30.3\bin
C:\Program Files\QGIS 3.34.0\bin
C:\Program Files\QGIS 3.36.0\bin
```

앱은 다음 순서로 GDAL을 찾습니다.

1. Windows PATH에 등록된 `ogr2ogr`, `ogrinfo`
2. `C:\Program Files\QGIS *\bin` 자동 탐색
3. `C:\Program Files (x86)\QGIS *\bin` 자동 탐색
4. `C:\OSGeo4W\bin`, `C:\OSGeo4W64\bin` 자동 탐색
5. 앱 사이드바의 `GDAL/QGIS bin 경로` 직접 입력값

따라서 QGIS가 일반 경로에 설치되어 있으면 사용자가 별도 설정을 하지 않아도 됩니다.
특수한 위치에 설치된 경우에만 사이드바에 `bin` 폴더 경로를 입력하면 됩니다.

QGIS가 없는 PC의 Windows 권장 설치:

```powershell
winget install OSGeo.GDAL
```

또는 QGIS/OSGeo4W를 설치하면 됩니다. PATH 등록은 필수가 아니며, 앱이 자동 탐색하거나
사용자가 `bin` 경로를 직접 입력할 수 있습니다.

## 실행

```powershell
pip install -r requirements.txt
streamlit run app.py
```

실행 후 브라우저에서 보통 아래 주소로 접속합니다.

```text
http://127.0.0.1:8501
http://localhost:8501
```

이미 다른 Streamlit 앱이 8501 포트를 쓰고 있으면 Streamlit이 8502, 8503처럼 다음 포트를
사용할 수 있습니다. 터미널에 출력되는 `Local URL`을 우선 확인합니다.

특정 포트로 고정 실행하려면:

```powershell
streamlit run app.py --server.port 8501
```

## 배포/사용환경 기준

다른 사람 컴퓨터에서도 사용할 수 있습니다. 다만 "아무 환경 준비 없이 무조건 실행"되는 구조는
아닙니다. 앱은 UI와 실제 공간처리 실행부가 분리되어 있습니다.

```text
UI 실행                  Python + streamlit + pandas
좌표변환/병합/분할 실행   GDAL CLI(ogr2ogr.exe, ogrinfo.exe)
```

QGIS가 설치된 사용자라면 대부분 QGIS 안에 포함된 GDAL을 그대로 사용할 수 있습니다.
앱이 QGIS 기본 설치 경로를 자동으로 찾고, 실패하면 사이드바에 QGIS `bin` 경로를 직접 입력합니다.

### 각 사용자 PC에서 직접 실행하는 방식

사용자 PC마다 아래가 필요합니다.

- Python
- `streamlit`, `pandas`
- QGIS 또는 GDAL/OSGeo4W

QGIS가 일반 경로에 설치되어 있으면 앱이 자동 탐색합니다.

```text
C:\Program Files\QGIS 3.xx\bin
```

특수 경로에 설치되어 있으면 사용자가 앱 사이드바의 `GDAL/QGIS bin 경로`에 위 `bin` 폴더를
직접 입력합니다.

### 한 대의 서버에 올리고 여러 사람이 브라우저로 접속하는 방식

이 방식에서는 사용자 PC의 QGIS 설치 여부는 중요하지 않습니다. 실제 변환/병합/분할은
서버 PC에서 실행되기 때문입니다.

서버 PC에 아래가 설치되어 있어야 합니다.

- Python
- `streamlit`, `pandas`
- QGIS 또는 GDAL/OSGeo4W

서버에서 GDAL 상태가 정상으로 잡히면 사용자는 브라우저만으로 앱을 사용할 수 있습니다.

---

# 상세 구현 명세 / 인수인계

이 문서는 다른 개발자가 이 폴더만 보고도 같은 앱을 재현하거나 이어서 구현할 수 있도록 작성한 인수인계 문서입니다.

## 목표

QGIS를 직접 열지 않아도 사용자가 브이월드, 지자체, 공공데이터포털 등에서 받은 SHP/GPKG 파일을 업로드하고 다음 작업을 처리할 수 있게 합니다.

- 여러 SHP/GPKG 일괄 업로드
- SHP 구성 파일 누락 확인
- DBF 속성 미리보기
- 한글 인코딩 깨짐 확인
- 좌표계 변환
- 한 SHP 내부 컬럼값 기준 병합
- 여러 레이어 병합
- 한 SHP 내부 컬럼값 기준 분할
- 여러 레이어 일괄 분할
- SHP zip 또는 GPKG 저장

브이월드 API는 필수 아닙니다. 이미 받은 SHP/GPKG 파일을 변환, 병합, 분할하는 작업은 GDAL/OGR 기반 로컬 처리로 충분합니다. 브이월드 API는 파일을 직접 내려받거나 배경지도, 주소검색, 지오코딩을 붙일 때만 필요합니다.

## 전체 처리 철학

SHP는 오래된 포맷이라 쉽게 “깨져 보이는” 문제가 생깁니다. 실제로는 도형 손상보다 다음 문제가 더 흔합니다.

- `.shp`, `.shx`, `.dbf`, `.prj`, `.cpg` 중 일부 파일 누락
- DBF 인코딩 불일치로 한글 깨짐
- `.prj` 누락 또는 잘못된 CRS
- 필드명 10자 제한
- 긴 문자열 잘림
- geometry 타입 혼합
- SHP/DBF 용량 제한

따라서 앱은 원본을 직접 덮어쓰지 않고, 임시 작업 폴더에서 복사본을 처리한 뒤 결과만 다운로드하게 설계합니다. SHP 결과는 반드시 zip으로 묶습니다.

권장 처리 흐름:

```text
업로드
→ ZIP 압축 해제
→ SHP/GPKG 탐색
→ SHP 구성 파일 검사
→ DBF 인코딩 미리보기
→ 좌표계/기준 컬럼/출력 형식 선택
→ GDAL 처리
→ 결과 zip 또는 gpkg 다운로드
```

## 화면 구조

앱 제목:

```text
SHP 좌표변환·병합·분할 도구
```

큰 탭은 4개입니다.

```text
1. 좌표계 변환
2. 레이어 병합
3. 레이어 분할
4. 코드 결합
```

공통 영역은 사이드바와 상단 확장 패널입니다.

## 사이드바

사이드바에는 다음 기능이 있습니다.

- 파일 업로드
- 업로드 파일 읽기
- 작업 초기화
- 입력 DBF 인코딩 선택
- SHP 출력 인코딩 선택
- GDAL/QGIS bin 경로 직접 입력
- GDAL 상태 표시

업로드 허용 확장자:

```text
zip, shp, shx, dbf, prj, cpg, gpkg
```

입력 DBF 인코딩 후보:

```text
UTF-8
CP949
EUC-KR
ISO-8859-1
```

SHP 출력 인코딩 후보:

```text
UTF-8
CP949
```

GDAL 상태는 앱이 찾은 `ogr2ogr`, `ogrinfo`의 실제 경로를 보여줍니다. PATH에 없어도
QGIS/OSGeo4W 기본 설치 폴더나 직접 입력한 `bin` 폴더에서 찾으면 정상 동작합니다.
둘 중 하나라도 `없음`이면 UI는 열리지만 실제 좌표계 변환, 병합, 분할 실행은 실패합니다.

## 업로드 레이어/속성 미리보기

상단 확장 패널에서 업로드한 레이어 목록과 DBF 미리보기를 보여줍니다.

레이어 목록 표시 항목:

- 레이어명
- 형식: `SHP` 또는 `GPKG`
- `.shx` 존재 여부
- `.dbf` 존재 여부
- `.prj` 존재 여부
- `.cpg` 내용
- 실제 파일 경로

SHP이고 DBF가 있으면 DBF 미리보기를 제공합니다.

DBF 미리보기 기능:

- 선택한 레이어의 DBF 첫 30행 표시
- 인코딩 후보별 컬럼명 표시
- 깨짐 의심 점수 표시

깨짐 의심 점수는 단순 휴리스틱입니다. 다음 문자가 많으면 깨진 것으로 의심합니다.

```text
�, Ã, Â, ì, í, ê, ¤
```

완벽한 자동 판정은 아닙니다. 사용자가 미리보기 표를 보고 `입력 DBF 인코딩`을 직접 선택하는 방식입니다.

## 1. 좌표계 변환 탭

목적:

여러 SHP/GPKG 레이어를 한 번에 원하는 EPSG 좌표계로 변환합니다.

UI 요소:

- 변환할 레이어 다중 선택
- 목표 좌표계 선택
- 원본 EPSG 강제 지정 입력
- 저장 형식 선택: `SHP`, `GPKG`
- 좌표계 변환 실행 버튼

기본 목표 좌표계 목록:

```text
EPSG:5186 - 중부원점 TM
EPSG:5185 - 서부원점 TM
EPSG:5187 - 동부원점 TM
EPSG:5179 - Korea 2000 / Unified CS
EPSG:4326 - WGS84 위경도
직접 입력
```

처리 방식:

- `ogr2ogr` 사용
- 입력 SHP 인코딩은 `-oo ENCODING=...`로 전달
- 목표 좌표계는 `-t_srs EPSG:{번호}`로 전달
- 원본 CRS가 없거나 잘못된 경우 `-s_srs EPSG:{번호}`로 강제 지정 가능
- SHP 출력 시 `-lco ENCODING=...` 사용
- SHP 결과는 sidecar 파일까지 zip에 포함

개념 명령:

```powershell
ogr2ogr -overwrite -f "ESRI Shapefile" `
  -oo ENCODING=CP949 `
  -s_srs EPSG:4326 `
  -t_srs EPSG:5186 `
  -lco ENCODING=UTF-8 `
  output.shp input.shp
```

주의:

- `.prj`가 있으면 GDAL이 원본 CRS를 자동으로 읽습니다.
- `.prj`가 없거나 틀리면 자동 인식은 불완전합니다.
- 좌표값 범위만으로 EPSG를 완벽하게 판별하지 않습니다.
- 원본 EPSG 강제 지정은 사용자가 알고 있을 때만 사용해야 합니다.

## 2. 레이어 병합 탭

레이어 병합 탭은 2가지 방식을 제공합니다.

```text
한 SHP 내 컬럼값 기준 병합
여러 레이어 병합
```

### 한 SHP 내 컬럼값 기준 병합

목적:

하나의 SHP 안에서 같은 속성값을 가진 도형들을 하나로 묶습니다. 예를 들어 `시군구명`, `읍면동`, `지역명`, `사업구분` 같은 컬럼을 기준으로 같은 값끼리 도형을 dissolve합니다.

예:

```text
원본
전주시 완산구 행 20개
전주시 덕진구 행 15개

결과
전주시 완산구 1개 멀티파트 도형
전주시 덕진구 1개 멀티파트 도형
```

UI 요소:

- 대상 SHP 선택
- 묶을 기준 컬럼 선택
- 병합 전 목표 EPSG 통일 입력
- 결과 저장 형식 선택: `SHP`, `GPKG`
- 내부 컬럼값 기준 병합 실행 버튼

처리 방식:

- `ogr2ogr`
- SQLite SQL dialect
- `ST_Union(geometry)`
- `GROUP BY 기준컬럼`
- `-nlt PROMOTE_TO_MULTI`

개념 SQL:

```sql
SELECT 기준컬럼, ST_Union(geometry) AS geometry
FROM 레이어명
GROUP BY 기준컬럼
```

주의:

- 현재 구현은 기준 컬럼과 geometry 중심으로 결과를 만듭니다.
- 다른 속성 컬럼을 유지하려면 집계 규칙이 필요합니다.
- 예: 첫 값 유지, 합계, 평균, 최댓값, 최솟값 등
- 향후 버전에서 속성 집계 옵션을 추가하면 좋습니다.

### 여러 레이어 병합

목적:

여러 SHP/GPKG 레이어를 하나의 레이어로 합칩니다.

UI 요소:

- 병합할 레이어 다중 선택
- 병합 전 목표 EPSG 통일 입력
- 결과 저장 형식 선택: `SHP`, `GPKG`
- 여러 레이어 병합 실행 버튼

처리 방식:

- 첫 레이어는 새 GPKG 생성
- 두 번째 레이어부터 `-append`
- 필드가 다를 경우 `-addfields`
- 최종 출력이 SHP이면 GPKG에서 SHP로 한 번 더 변환
- 레이어명은 `merged`로 통일

개념 명령:

```powershell
ogr2ogr -overwrite -f GPKG -nln merged merged.gpkg input1.shp
ogr2ogr -update -append -f GPKG -addfields -nln merged merged.gpkg input2.shp
ogr2ogr -update -append -f GPKG -addfields -nln merged merged.gpkg input3.shp
```

주의:

- 여러 레이어의 geometry 타입이 다르면 실패하거나 예기치 않은 결과가 날 수 있습니다.
- 현재는 `-nlt PROMOTE_TO_MULTI`로 단일/멀티 타입 차이를 완화합니다.
- Point/Line/Polygon처럼 기본 geometry 종류가 다르면 별도 병합 정책이 필요합니다.
- CRS가 서로 다르면 목표 EPSG를 지정해 통일한 뒤 병합해야 합니다.

## 3. 레이어 분할 탭

레이어 분할 탭은 2가지 방식을 제공합니다.

```text
한 SHP 내 컬럼값 기준 분할
여러 레이어 분할
```

또한 두 조건 방식을 지원합니다.

```text
정확히 일치: 컬럼 = '값'
포함 조건: 컬럼 LIKE '%값%'
```

### 한 SHP 내 컬럼값 기준 분할

목적:

하나의 SHP를 속성값별로 여러 개의 SHP/GPKG로 나눕니다.

예:

```text
기준 컬럼: 읍면동

input.shp
→ input_중앙동.shp
→ input_서신동.shp
→ input_효자동.shp
```

UI 요소:

- 대상 SHP 선택
- 분할 기준 컬럼 선택
- 포함 조건 사용 여부
- 분할할 값 입력
- 결과 저장 형식 선택
- 분할 실행 버튼

분할할 값 입력 규칙:

- 한 줄에 하나씩 입력
- 비워두면 앱이 DBF 미리보기에서 감지한 고유값을 사용
- 포함 조건을 켜면 `%값%` 방식으로 검색

정확히 일치 조건:

```sql
"읍면동" = '중앙동'
```

포함 조건:

```sql
"지역명" LIKE '%전주%'
```

### 여러 레이어 분할

목적:

여러 SHP를 같은 컬럼명과 같은 분할 조건으로 한 번에 나눕니다.

예:

```text
A.shp, B.shp, C.shp 업로드
기준 컬럼: 지역명
값: 전주, 군산

결과
A/A_전주.shp
A/A_군산.shp
B/B_전주.shp
B/B_군산.shp
C/C_전주.shp
C/C_군산.shp
```

주의:

- 여러 레이어 분할은 모든 대상 레이어에 같은 기준 컬럼이 있어야 합니다.
- 기준 컬럼이 없는 레이어는 로그에 기록하고 건너뜁니다.
- 값 목록은 첫 번째 대상 레이어를 기준으로 감지합니다.

## 파일 처리 규칙

SHP는 단일 파일이 아닙니다. 최소한 다음 파일이 중요합니다.

```text
.shp  도형
.shx  도형 인덱스
.dbf  속성 테이블
.prj  좌표계 정보
.cpg  DBF 인코딩 정보
```

앱은 추가 sidecar도 함께 zip에 넣습니다.

```text
.qix
.sbn
.sbx
```

권장 업로드:

```text
레이어별 zip 업로드
```

허용 업로드:

```text
SHP 구성 파일을 여러 개 직접 업로드
GPKG 직접 업로드
```

SHP 결과:

```text
항상 zip 다운로드
```

GPKG 결과:

```text
단일 .gpkg 다운로드 또는 전체 zip 다운로드
```

## 코드 구조

주요 상수:

- `APP_TITLE`: 앱 제목
- `COMMON_EPSG`: 자주 쓰는 EPSG 목록
- `ENCODINGS`: DBF 입력 인코딩 후보
- `SHAPEFILE_PARTS`: SHP sidecar 확장자 목록

주요 데이터 클래스:

- `LayerInfo`: 업로드 레이어 메타데이터

주요 함수:

- `gdals()`: `ogr2ogr`, `ogrinfo` PATH 확인
- `run_cmd(args)`: GDAL 명령 실행
- `session_root()`: Streamlit 세션별 임시 작업 폴더 생성
- `reset_workspace()`: 작업 폴더 초기화
- `save_uploads(files)`: 업로드 파일 저장 및 zip 압축 해제
- `discover_layers(input_dir)`: `.shp`, `.gpkg` 탐색
- `dbf_fields(dbf_path)`: DBF 필드 헤더 읽기
- `read_dbf_preview(dbf_path, encoding, limit)`: DBF 속성 미리보기
- `encoding_score(df)`: 깨짐 의심 점수 계산
- `zip_paths(paths, zip_name)`: 결과 zip 생성
- `convert_layer(...)`: 좌표계 변환
- `dissolve_one_layer(...)`: 한 SHP 내부 컬럼값 기준 병합
- `merge_layers(...)`: 여러 레이어 병합
- `split_layer_by_values(...)`: 컬럼값 기준 분할
- `render_layer_status(...)`: 업로드 상태/DBF 미리보기 UI
- `render_convert_tab(...)`: 좌표계 변환 탭
- `render_merge_tab(...)`: 레이어 병합 탭
- `render_split_tab(...)`: 레이어 분할 탭
- `main()`: Streamlit 진입점

## GDAL 요구사항

이 앱은 UI 자체는 Python만으로 열리지만, 실제 공간 처리에는 GDAL CLI가 필요합니다.

필수 명령:

```text
ogr2ogr
ogrinfo
```

확인:

```powershell
ogr2ogr --version
ogrinfo --version
```

PATH에 등록하지 않은 QGIS 설치본도 사용할 수 있습니다. 앱 사이드바에 다음처럼
`ogr2ogr.exe`, `ogrinfo.exe`가 들어있는 폴더를 입력하면 됩니다.

```text
C:\Program Files\QGIS 3.30.3\bin
```

Windows 설치 후보:

```powershell
winget install OSGeo.GDAL
```

또는:

- OSGeo4W 설치
- QGIS 설치 후 앱 자동 탐색 사용
- 자동 탐색이 실패하면 QGIS `bin` 경로를 앱 사이드바에 직접 입력

QGIS 설치 경로 예:

```text
C:\Program Files\QGIS 3.xx\bin
```

OSGeo4W 경로 예:

```text
C:\OSGeo4W\bin
```

PATH가 잡히지 않아도 앱이 QGIS/OSGeo4W 기본 설치 경로를 자동 탐색합니다. 그래도
못 찾으면 앱 사이드바의 GDAL 상태에 `없음`으로 표시되며, 이때는 `GDAL/QGIS bin 경로`에
직접 경로를 입력합니다.

### 사용자에게 안내할 문구

QGIS가 설치되어 있는데 GDAL 상태가 `없음`이면 아래 순서로 확인합니다.

1. QGIS 설치 폴더를 엽니다.
2. `bin` 폴더 안에 `ogr2ogr.exe`, `ogrinfo.exe`가 있는지 확인합니다.
3. 앱 사이드바의 `GDAL/QGIS bin 경로`에 그 `bin` 폴더 경로를 입력합니다.
4. GDAL 상태에 두 실행파일 경로가 표시되면 변환/병합/분할을 실행할 수 있습니다.

예:

```text
C:\Program Files\QGIS 3.30.3\bin
```

## 서버/접속 문제 해결

앱이 안 열린다고 느껴질 때는 먼저 실제 열린 포트를 확인합니다.

기본 접속 주소:

```text
http://127.0.0.1:8501
http://localhost:8501
```

터미널에 `Local URL: http://localhost:8502`처럼 표시되면 해당 포트로 접속합니다.

현재 열린 Streamlit 포트 확인:

```powershell
netstat -ano | findstr 850
```

8501로 고정 실행:

```powershell
streamlit run app.py --server.port 8501
```

여러 Streamlit 프로세스가 떠 있으면 포트 충돌이나 다른 앱 접속 문제가 생길 수 있습니다.
이 경우 기존 터미널에서 `Ctrl+C`로 중지한 뒤 다시 실행합니다.

앱은 열리지만 변환/병합/분할이 실패하면 사이드바의 GDAL 상태를 봅니다.

```text
ogr2ogr: 없음
ogrinfo: 없음
```

위처럼 나오면 QGIS `bin` 경로를 입력합니다.

```text
C:\Program Files\QGIS 3.xx\bin
```

정상 상태 예:

```text
ogr2ogr: C:\Program Files\QGIS 3.30.3\bin\ogr2ogr.exe
ogrinfo: C:\Program Files\QGIS 3.30.3\bin\ogrinfo.exe
```

## 현재 구현된 것

- Streamlit 앱 기본 구조
- 파일 업로드
- zip 압축 해제
- SHP/GPKG 탐색
- SHP 구성 파일 존재 여부 표시
- DBF 직접 파싱 미리보기
- 인코딩 후보별 깨짐 의심 점수
- 좌표계 변환 탭
- 한 SHP 내부 컬럼값 기준 병합
- 여러 레이어 병합
- 한 SHP 내부 컬럼값 기준 분할
- 여러 레이어 분할
- SHP 결과 zip 다운로드
- GDAL PATH/QGIS/OSGeo4W 자동 탐색
- GDAL/QGIS bin 경로 직접 입력
- GDAL 실행 시 QGIS bin 경로를 프로세스 PATH에 임시 반영
- 변환 전후 feature count/extent 비교(ogrinfo 파싱)
- 안전 변환 모드(변환 탭, 기본 ON): ① 원본 좌표계 확정(-a_srs)+makevalid로 네이티브 좌표에서 도형 복구 → ② 재투영(-t_srs)+makevalid 2단계. 불안정 SHP(예: 5174 .prj 누락)를 다른 좌표계로 재투영할 때 불량/유효범위이탈 폴리곤이 한두 개 사라지는 문제 방지. 입력→복구후→출력 단계별 피처수 리포트 + 손실 발생 시 빨간 경고
- 도형 유효화 `-makevalid` 옵션(변환·병합·분할 공통)
- 내부 컬럼값 기준 병합 시 속성 집계 옵션(SUM/AVG/MAX/MIN/COUNT, 컬럼별 지정)
- 여러 레이어 병합 시 입력 합계 vs 결과 feature count 비교
- 각 탭 작업 로그 텍스트 다운로드
- GDAL 미설치 시 설치/연결 방법 안내 강화
- GPKG 내부 복수 레이어 개별 인식/선택(ogrinfo로 레이어 나열, `file:layer` 단위 처리)
- 면적 컬럼(area_m2, ㎡) 추가 옵션(변환·병합 탭). ST_Area 기반, 투영 후 결과에 후처리로 계산해 미터 좌표계에서 정확. 경위도(4326)는 경고 표시
- 4. 코드 결합 탭: SHP 속성의 MNUM 등에서 `substr(컬럼, 시작, 길이)`(기본 21,6)로 코드를 뽑아 용도지역 코드표 CSV를 LEFT JOIN. 코드표 CSV 업로드+인코딩 후보별 깨짐 점수 표시 및 수동 선택(UTF-8-SIG/CP949/EUC-KR/UTF-8, 권장값 자동 기본선택), 대상 SHP 인코딩 확인(사이드바 입력 인코딩 검증), MNUM 컬럼/시작·길이 지정, 추출코드·매칭률 미리보기, 결합할 컬럼 선택. 원본 DBF를 직접 편집하지 않고 결합된 새 SHP/GPKG 생성. 구현: SHP+CSV를 임시 GPKG 2레이어로 넣고 SQLite dialect join(`substr`,`TRIM`), 출력 SHP은 `-lco ENCODING`로 한글 보존
- 공간 SQL(면적/dissolve/코드결합) 결과의 인코딩 안전 저장(`sqlite_sql_to_output`): SQLite dialect로 SHP를 직접 쓰면 일부 GDAL 버전(클라우드 apt gdal-bin)에서 `-lco ENCODING`이 무시돼 한글이 손상(???)되므로, 항상 UTF-8 GPKG로 만든 뒤 SHP로 재변환
- 결과물 빈손 방지 안전망(`gpkg_to_final`): SHP 저장이 실패하면 자동으로 GPKG로 대체 저장하고 화면에 안내(변환·병합·면적·코드결합 공통). 모든 처리 함수가 실제 저장 경로를 반환, 다운로드는 `download_for_path`가 실제 형식에 맞춰 제공
- 면적 소수점 자리수 선택(변환·병합 탭, 0~6자리)
- 배포: Streamlit Community Cloud(https://gis-shp-tool.streamlit.app), GitHub uurimii16/gis-shp-tool. `packages.txt`(`gdal-bin`,`libsqlite3-mod-spatialite`)로 서버가 GDAL/spatialite 자동 설치. 절전방지 GitHub Actions(6h 방문)

## 현재 한계

- 실제 공간 처리는 GDAL CLI에 의존합니다. QGIS가 설치된 PC라면 보통 QGIS 내장 GDAL을 사용하면 됩니다.
- DBF 미리보기는 앱 내부 단순 파서입니다.
- GPKG의 속성 컬럼 미리보기는 아직 구현하지 않았습니다(DBF 파서 기반이라 GPKG는 미지원).
- GPKG 내부 복수 레이어는 개별 인식/선택됩니다. 단 GPKG는 DBF가 없어 속성 미리보기·컬럼 기준 병합/분할 대상에서는 제외됩니다(SHP 전용).
- SHP 내부 컬럼값 기준 병합은 기준 컬럼과 geometry 중심으로 결과를 만듭니다.
- 병합 시 모든 속성 컬럼을 어떻게 집계할지는 아직 옵션화하지 않았습니다.

### 🔴 알려진 이슈(미해결, 2026-07-03)

- **dissolve(한 SHP 내 컬럼값 기준 병합) 결과 폴리곤 선형이 깨짐.** 텍스트(속성)는 정상인데 도형 경계선이 이상하게 나옴.
  - 원인: **`ST_Union` 연산 자체**(결과를 GPKG로 출력해도 동일하게 깨지므로 GPKG→SHP 변환 문제 아님). 원본 폴리곤의 위상 오류(자기교차/겹침/미세 슬리버)를 복구하지 않고 union하면 GEOS가 깨진 결과를 냄. 현재 `-makevalid`는 union 다음에 적용돼 순서가 비효율적.
  - 수정 방향(다음 작업): dissolve를 "복구 먼저 → union"으로. `ST_Union(ST_MakeValid(geometry))` 또는 2단계(원본 makevalid로 GPKG 생성 → 그 GPKG에서 `ST_Union GROUP BY`). 대상 함수 `dissolve_one_layer`, 참고 패턴 `convert_layer_safe`. 상세는 [작업로그.md](작업로그.md) "미해결/다음 작업" 참고.

## 향후 보강하면 좋은 기능

완료(2026-07-03):

- ~~GPKG 내부 복수 레이어 선택~~ ✅ (`gpkg_layer_names`/`ogr_source_args`로 서브레이어 단위 처리)
- ~~GDAL 미설치 시 설치 경로 안내를 더 친절하게 표시~~ ✅
- ~~병합 시 속성 집계 옵션~~ ✅ (SUM/AVG/MAX/MIN/COUNT)
- ~~geometry validity 검사 및 `-makevalid` 옵션 제공~~ ✅
- ~~변환 전후 feature count 비교~~ ✅
- ~~변환 전후 extent 비교~~ ✅
- ~~작업 로그 다운로드~~ ✅

우선순위 중간:

- 필드명 10자 초과 경고
- SHP 문자열 길이 초과 경고
- 용량 2GB 근접 경고
- CRS `.prj` 내용에서 EPSG 후보 표시
- 좌표값 범위 기반 EPSG 추정
- 결과 파일명 접두어/접미어 사용자 지정

우선순위 낮음:

- 지도 미리보기
- 브이월드 배경지도 연동
- 브이월드 API에서 데이터 직접 검색/다운로드
- 작업 이력 저장
- 대용량 작업 큐

## 사용자 안내 문구

앱 화면에 추가하면 좋은 안내 문구입니다.

```text
SHP 결과는 호환성을 위해 zip으로 제공됩니다. 압축을 풀 때 같은 폴더 안에 shp, shx, dbf, prj, cpg 파일을 함께 보관하세요.
```

```text
한글이 깨져 보이면 입력 DBF 인코딩을 CP949 또는 EUC-KR로 바꿔 미리보기를 확인한 뒤 다시 실행하세요.
```

```text
원본 .prj가 없거나 잘못된 경우 좌표계 자동 인식이 정확하지 않을 수 있습니다. 이때 원본 EPSG 강제 지정을 사용하세요.
```

## 이어서 작업할 때 테스트 순서

1. `streamlit run app.py`로 앱이 열리는지 확인합니다.
2. 사이드바에서 `ogr2ogr`, `ogrinfo` 경로가 잡히는지 확인합니다.
3. 작은 테스트 SHP zip을 업로드합니다.
4. DBF 미리보기에서 한글이 정상인지 확인합니다.
5. 좌표계 변환을 SHP/GPKG 각각 테스트합니다.
6. SHP 결과 zip 안에 `.shp/.shx/.dbf/.prj/.cpg`가 있는지 확인합니다.
7. 내부 컬럼값 기준 병합을 테스트합니다.
8. 여러 레이어 병합을 테스트합니다.
9. 컬럼값 기준 분할을 정확히 일치/포함 조건 모두 테스트합니다.
10. 변환 전후 feature count를 수동으로 비교합니다.

## 현재 검증 상태

작성 시점 검증:

- `python -B -c "compile(...)"` 방식 문법 검증 통과
- `import app` 통과
- Streamlit 서버 기동 확인
- `http://127.0.0.1:8501` 응답 코드 200 확인
- QGIS 설치 경로의 GDAL CLI 직접 실행 확인

현재 로컬 환경 특이사항:

- Python 3.14.6
- `streamlit`: 설치됨
- `pandas`: 설치됨
- `geopandas`, `fiona`, `pyogrio`, `shapely`, `osgeo`: 설치 안 됨
- `ogr2ogr`, `ogrinfo`: PATH에서는 발견 안 됨
- QGIS 내장 GDAL 발견:
  - `C:\Program Files\QGIS 3.30.3\bin\ogr2ogr.exe`
  - `C:\Program Files\QGIS 3.30.3\bin\ogrinfo.exe`
- GDAL 버전: `GDAL 3.7.0, released 2023/05/02`

따라서 이 환경에서는 PATH 등록 없이도 앱의 자동 탐색으로 QGIS 내장 GDAL을 사용할 수 있습니다.
다만 실제 SHP 샘플을 넣은 좌표변환/병합/분할 결과 검증은 별도 테스트 데이터로 확인해야 합니다.
