@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem 에이전트의 스킬 폴더에 이 스킬을 넣는다(SKILL.md 한 장이 든 폴더. 그 안에 이 폴더의 위치가 적힌다).
rem SKILL.md를 고치거나 업데이트한 뒤에는 이 파일을 다시 실행해야 스킬 폴더에도 반영된다.
rem
rem   install_skill.bat               안티그래비티에
rem   install_skill.bat antigravity   위와 같다
rem
rem 스킬 폴더가 다른 곳에 있으면 HWPICK_SKILLS_DIR 환경 변수로 알려 줄 수 있다.

set "HERE=%~dp0."
set "WHO=%~1"
set "DONE=0"
set "BAD=0"

if not exist ".venv\Scripts\python.exe" goto :need_setup
echo.
if defined HWPICK_SKILLS_DIR goto :custom
if /i "%WHO%"=="antigravity" goto :antigravity
if not "%WHO%"=="" goto :usage

if exist "%USERPROFILE%\.gemini" call :link "안티그래비티" "%USERPROFILE%\.gemini\config\skills"
if exist "%USERPROFILE%\.gemini" call :agent
goto :summary

:custom
call :link "지정한 폴더" "%HWPICK_SKILLS_DIR%"
if exist "%USERPROFILE%\.gemini" call :agent
goto :summary

:antigravity
call :link "안티그래비티" "%USERPROFILE%\.gemini\config\skills"
call :agent
goto :summary

:summary
echo.
if "%DONE%"=="0" if "%BAD%"=="0" goto :none
if not "%BAD%"=="0" goto :fail
echo  에이전트를 다시 시작한 뒤 "hwp-problem-picker 스킬로 문제 선택 화면을 열어 줘" 라고 말해 보세요.
echo.
if not defined HWPICK_NO_PAUSE pause
exit /b 0

rem ---- 안티그래비티에 옮겨 적기 담당 서브에이전트 정의를 넣는다(파일 하나를 복사한다. 명령을 실행하지 못하는 가벼운 모델)
:agent
set "AGENTS=%USERPROFILE%\.gemini\config\agents"
if not exist "%AGENTS%" mkdir "%AGENTS%"
copy /Y "%HERE%\agents\hwpick-reader.md" "%AGENTS%\hwpick-reader.md" >nul
if errorlevel 1 goto :agent_fail
copy /Y "%HERE%\agents\hwpick-dispatch.md" "%AGENTS%\hwpick-dispatch.md" >nul
if errorlevel 1 goto :agent_fail
echo  안티그래비티: 옮겨 적기 담당 서브에이전트(hwpick-reader, hwpick-dispatch)를 넣었습니다.
exit /b 0
:agent_fail
echo  안티그래비티: 옮겨 적기 담당 서브에이전트를 넣지 못했습니다(없어도 스킬은 동작합니다).
exit /b 0

rem ---- 스킬 폴더 하나에 넣는다: call :link "이름" "스킬 폴더"
rem      SKILL.md 한 장이 든 진짜 폴더를 만든다(안티그래비티는 연결된 폴더를 스킬로 읽지 않는다)
:link
".venv\Scripts\python.exe" -m hwpick.skilldir "%~1" "%~2"
if errorlevel 1 goto :link_bad
set /a DONE+=1
exit /b 0
:link_bad
set /a BAD+=1
exit /b 0

:need_setup
echo.
echo  먼저 setup.bat 을 실행해 설치를 마쳐 주세요.
if not defined HWPICK_NO_PAUSE pause
exit /b 1

:usage
echo  알 수 없는 이름입니다: %WHO%
echo  아무것도 붙이지 않거나 antigravity 라고 쓰면 안티그래비티에 넣습니다.
if not defined HWPICK_NO_PAUSE pause
exit /b 1

:none
echo  안티그래비티를 찾지 못했습니다. 안티그래비티를 먼저 설치해 주세요.
if not defined HWPICK_NO_PAUSE pause
exit /b 1

:fail
echo  넣지 못한 곳이 있습니다. 위의 내용을 확인해 주세요.
if not defined HWPICK_NO_PAUSE pause
exit /b 1
