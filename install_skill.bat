@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem 에이전트의 스킬 폴더에 이 폴더로 가는 연결(바로가기 폴더)을 만든다.
rem 파일을 복사하지 않으므로, 이 폴더를 고치면 스킬에도 바로 반영된다.
rem
rem   install_skill.bat               설치되어 있는 에이전트 모두에 연결
rem   install_skill.bat antigravity   안티그래비티에만
rem   install_skill.bat opencode      opencode에만
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
if /i "%WHO%"=="opencode" goto :opencode
if not "%WHO%"=="" goto :usage

if exist "%USERPROFILE%\.gemini" call :link "안티그래비티" "%USERPROFILE%\.gemini\config\skills"
if exist "%USERPROFILE%\.gemini" call :agent
if exist "%USERPROFILE%\.config\opencode" call :link "opencode" "%USERPROFILE%\.config\opencode\skills"
goto :summary

:custom
call :link "지정한 폴더" "%HWPICK_SKILLS_DIR%"
if exist "%USERPROFILE%\.gemini" call :agent
goto :summary

:antigravity
call :link "안티그래비티" "%USERPROFILE%\.gemini\config\skills"
call :agent
goto :summary

:opencode
call :link "opencode" "%USERPROFILE%\.config\opencode\skills"
goto :summary

:summary
echo.
if "%DONE%"=="0" if "%BAD%"=="0" goto :none
if not "%BAD%"=="0" goto :fail
echo  에이전트를 다시 시작한 뒤 "HWPick" 이라고 말하면 문제 선택 화면이 열립니다.
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

rem ---- 스킬 폴더 하나에 연결한다: call :link "이름" "스킬 폴더"
:link
set "TARGET=%~2\hwpick"
if exist "%TARGET%\SKILL.md" goto :link_already
if exist "%TARGET%" goto :link_occupied
if not exist "%~2" mkdir "%~2"
mklink /J "%TARGET%" "%HERE%" >nul
if errorlevel 1 goto :link_fail
echo  %~1: 연결했습니다.
set /a DONE+=1
exit /b 0
:link_already
echo  %~1: 이미 연결되어 있습니다.
set /a DONE+=1
exit /b 0
:link_occupied
echo  %~1: %TARGET% 에 다른 폴더가 있어 연결하지 않았습니다. 그 폴더를 확인해 주세요.
set /a BAD+=1
exit /b 0
:link_fail
echo  %~1: 연결하지 못했습니다. 위의 오류 내용을 알려 주세요.
set /a BAD+=1
exit /b 0

:need_setup
echo.
echo  먼저 setup.bat 을 실행해 설치를 마쳐 주세요.
if not defined HWPICK_NO_PAUSE pause
exit /b 1

:usage
echo  알 수 없는 이름입니다: %WHO%
echo  antigravity 또는 opencode 를 쓰거나, 아무것도 붙이지 않으면 모두에 연결합니다.
if not defined HWPICK_NO_PAUSE pause
exit /b 1

:none
echo  안티그래비티도 opencode도 찾지 못했습니다. 에이전트를 먼저 설치해 주세요.
if not defined HWPICK_NO_PAUSE pause
exit /b 1

:fail
echo  연결하지 못한 곳이 있습니다. 위의 내용을 확인해 주세요.
if not defined HWPICK_NO_PAUSE pause
exit /b 1
