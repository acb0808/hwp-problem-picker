@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo.
echo  hwp-problem-picker 설치를 시작합니다. 이 창을 닫지 말고 기다려 주세요.
echo.

rem 에이전트가 대신 실행할 때는 HWPICK_NO_PAUSE=1 을 주면 끝에서 멈추지 않는다
rem ---- 1. 설치 도구 uv
where uv >nul 2>nul
if not errorlevel 1 goto :have_uv
echo  [1/4] 설치 도구를 내려받습니다. 인터넷 연결이 필요합니다.
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
where uv >nul 2>nul
if errorlevel 1 goto :no_uv
goto :venv
:have_uv
echo  [1/4] 설치 도구 확인됨

rem ---- 2. 파이썬 환경과 필요한 프로그램 모듈
:venv
echo  [2/4] 필요한 프로그램 모듈을 설치합니다. 몇 분 걸릴 수 있습니다.
if exist ".venv\pyvenv.cfg" if exist ".venv\Scripts\python.exe" goto :install
rem 설치가 도중에 끊겨 반쯤 만들어진 환경이 남아 있으면 지우고 새로 만든다(그대로 두면 다음 설치가 계속 실패한다)
if exist ".venv" rmdir /s /q ".venv"
uv venv .venv --python 3.12
if errorlevel 1 goto :fail
:install
uv pip install --python ".venv\Scripts\python.exe" -e . "vendor\latex_to_hwpeqn-0.3.0-py3-none-any.whl"
if errorlevel 1 goto :fail
rem 한글 자동화 보안 모듈(한컴의 DLL)은 이 스킬에 넣어 배포하지 않는다. 그 파일이 든 pyhwpx 꾸러미만 받아 둔다(딸린 꾸러미는 받지 않는다)
uv pip install --python ".venv\Scripts\python.exe" --no-deps pyhwpx==1.7.2
if errorlevel 1 echo  (보안 모듈이 든 꾸러미를 받지 못했습니다. 아래 점검에서 안내합니다.)

rem ---- 3. 글자 인식 모델(처음 한 번 내려받는다)
echo  [3/4] 글자 인식 모델을 준비합니다.
".venv\Scripts\python.exe" -c "from hwpick import ocr; ocr._engine()"
if errorlevel 1 goto :fail

rem ---- 4. 점검. 한글 자동화 보안 모듈이 등록되어 있지 않으면 등록한다(이미 있으면 그대로 둔다)
echo  [4/4] 설치 상태를 점검합니다.
echo.
".venv\Scripts\python.exe" -m hwpick doctor --fix
echo.
echo  설치가 끝났습니다.
echo  안티그래비티에서 쓰려면 install_skill.bat 을 한 번 실행하세요.
echo.
if not defined HWPICK_NO_PAUSE pause
exit /b 0

:no_uv
echo.
echo  설치 도구를 받지 못했습니다. 인터넷 연결을 확인한 뒤 다시 실행해 주세요.
if not defined HWPICK_NO_PAUSE pause
exit /b 1

:fail
echo.
echo  설치 중 오류가 났습니다. 위에 보이는 오류 내용을 그대로 복사해 알려 주세요.
if not defined HWPICK_NO_PAUSE pause
exit /b 1
