@echo off
chcp 65001 > nul
REM ── SHP 도구 데스크톱 exe 빌드 + 사원 배포용 zip 만들기 ──────────────
REM 사용법: 이 폴더에서 build.bat 더블클릭
REM 결과:
REM   dist\SHP도구.exe              ← 프로그램 본체
REM   dist\SHP도구_배포_v1.1.0.zip  ← 사원에게 전달할 파일(exe + 사용설명서)

cd /d "%~dp0"
set APPNAME=SHP도구
set VER=1.1.0

echo [1/5] 빌드 도구 확인/설치
python -m pip install --quiet --upgrade pyinstaller tkinterdnd2 pillow
if errorlevel 1 goto fail

echo [2/5] 아이콘 확인
if not exist icon.ico (
  python make_icon.py
  if errorlevel 1 goto fail
)

echo [3/5] exe 빌드 (1~2분 걸립니다)
python -m PyInstaller --noconfirm --onefile --windowed ^
  --name "%APPNAME%" ^
  --icon "%cd%\icon.ico" ^
  --version-file "%cd%\version_info.txt" ^
  --add-data "%cd%\icon.ico;." ^
  --collect-all tkinterdnd2 ^
  --distpath dist --workpath build --specpath build ^
  app.py
if errorlevel 1 goto fail

echo [4/5] 배포 폴더 구성
set PKG=dist\%APPNAME%_배포
if exist "%PKG%" rmdir /s /q "%PKG%"
mkdir "%PKG%"
copy /y "dist\%APPNAME%.exe" "%PKG%\" > nul
copy /y "사용설명서.txt" "%PKG%\" > nul

echo [5/5] zip 압축
powershell -NoProfile -Command ^
  "Compress-Archive -Path 'dist\%APPNAME%_배포\*' -DestinationPath 'dist\%APPNAME%_배포_v%VER%.zip' -Force"
if errorlevel 1 goto fail

echo.
echo ✅ 완료
echo    프로그램  : %cd%\dist\%APPNAME%.exe
echo    배포용 zip: %cd%\dist\%APPNAME%_배포_v%VER%.zip
echo.
echo    사원에게는 zip 하나만 주면 됩니다(압축 풀고 exe 실행).
echo    ※ 기존 exe가 실행 중이면 잠겨서 빌드가 실패합니다. 창을 닫고 다시 실행하세요.
pause
exit /b 0

:fail
echo.
echo ❌ 빌드 실패. 위 메시지를 확인하세요.
pause
exit /b 1
