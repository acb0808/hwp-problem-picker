# 설치를 맡은 에이전트에게

사용자는 코딩을 모른다. 아래 순서를 그대로 따르고, 사용자가 직접 해야 하는 일만 쉬운 말로 알린다. 명령은 윈도우의 PowerShell 기준이다.

## 지킬 것

- 다른 스킬, 다른 에이전트 정의, 사용자의 문서와 교재 파일은 건드리지 않는다.
- 전역 파이썬에 아무것도 설치하지 않는다. 필요한 것은 설치 폴더 안의 `.venv`에만 들어간다.
- 구매, 구독, 보안 기능 해제는 대신 하지 않는다. 비밀 키나 설정 파일 전체를 출력하지 않는다.
- 설치만 요청받았으면 교재를 열거나 문서를 만들지 않는다.
- 실패하면 오류 내용을 그대로 사용자에게 보여 주고 멈춘다. 추측으로 다른 방법을 시도하지 않는다.

## 1. 먼저 확인

1. 윈도우인지 확인한다. 아니면 "한글 파일은 윈도우에서만 만들 수 있다"고 알리고 멈춘다.
2. 한컴오피스 한글이 설치되어 있는지 확인한다.

```powershell
Test-Path "Registry::HKEY_CLASSES_ROOT\HWPFrame.HwpObject"
```

`False`면 "한컴오피스 한글이 설치되어 있어야 한다"고 알리고 멈춘다(설치는 사용자가 한다).

## 2. 내려받기

설치 위치는 `%USERPROFILE%\hwp-problem-picker` 로 고정한다. 이미 그 폴더가 있으면 아래 "업데이트"로 간다.

`git`이 있으면:

```powershell
git clone https://github.com/acb0808/hwp-problem-picker "$env:USERPROFILE\hwp-problem-picker"
```

`git`이 없으면:

```powershell
$zip = "$env:TEMP\hwp-problem-picker.zip"
Invoke-WebRequest "https://github.com/acb0808/hwp-problem-picker/archive/refs/heads/main.zip" -OutFile $zip
Expand-Archive $zip -DestinationPath "$env:TEMP\hwp-problem-picker-unzip" -Force
Move-Item "$env:TEMP\hwp-problem-picker-unzip\hwp-problem-picker-main" "$env:USERPROFILE\hwp-problem-picker"
```

## 3. 설치

필요한 프로그램을 내려받느라 5~10분 걸린다. **명령 제한 시간을 15분(900000ms)으로 준다.** `HWPICK_NO_PAUSE`를 주지 않으면 끝에서 키 입력을 기다리며 멈춘다. 스크립트는 아래처럼 **전체 경로로** 부른다(폴더로 들어가서 이름만 치면 못 찾는 PC가 있다).

```powershell
$env:HWPICK_NO_PAUSE = "1"
& "$env:USERPROFILE\hwp-problem-picker\setup.bat"
```

이 명령은 설치 도구(uv)를 받고, 설치 폴더 안에 파이썬 환경(`.venv`)을 만들고, 글자 인식 모델을 내려받고, 한글 자동화 보안 모듈을 현재 사용자에게 등록한다(이미 등록되어 있으면 그대로 둔다).

## 4. 안티그래비티에 연결

```powershell
$env:HWPICK_NO_PAUSE = "1"
& "$env:USERPROFILE\hwp-problem-picker\install_skill.bat" antigravity
```

이 명령은 두 가지를 한다.

- `%USERPROFILE%\.gemini\config\skills\hwpick` 에 설치 폴더로 가는 연결(바로가기 폴더)을 만든다. 파일을 복사하지 않는다.
- `%USERPROFILE%\.gemini\config\agents\` 에 `hwpick-reader.md`, `hwpick-dispatch.md` 두 파일을 넣는다. 문제를 읽는 가벼운 서브에이전트의 정의다(명령을 실행하지 못하고 Flash Lite 모델로 돈다).

안티그래비티가 다른 스킬 폴더를 쓰는 것이 확인되면, `HWPICK_SKILLS_DIR` 환경 변수에 그 폴더를 주고 같은 명령을 실행한다. 확인되지 않으면 위의 기본 위치를 그대로 쓴다.

## 5. 점검

```powershell
& "$env:USERPROFILE\hwp-problem-picker\.venv\Scripts\python.exe" -m hwpick doctor
```

모든 줄이 `[준비됨]`이면 끝이다. `[필요함]`이 있으면 그 줄과 안내(`→`)를 사용자에게 그대로 전한다.

- `한글 자동화 보안 모듈`이 `[필요함]`이면 `doctor --fix`를 한 번 실행한다. 그래도 안 되면 안내 문구를 그대로 전한다.

## 6. 사용자에게 알릴 것

- 설치 위치(`%USERPROFILE%\hwp-problem-picker`)와 점검 결과
- **안티그래비티를 껐다 다시 켜야** 스킬이 보인다는 것
- 쓰는 법: 교재 PDF가 든 폴더를 안티그래비티에서 열고, 요청에 "HWPick"이라는 말을 넣는다. 예: `HWPick 스킬로 (교재 이름)에서 1단원 B단계 문제만 모아서 한글 파일로 정리해 줘`
- 실제로 한글 파일을 만들어 보지 않았다면 "만드는 것까지 확인했다"고 말하지 않는다.

## 업데이트

설치 폴더가 이미 있을 때다. `.venv`와 사용자의 작업 폴더는 그대로 둔다.

```powershell
git -C "$env:USERPROFILE\hwp-problem-picker" pull
```

`git`으로 받은 폴더가 아니면(`.git` 폴더가 없으면) 2번의 압축 파일 방식으로 새로 받아, `.venv` 폴더만 남기고 나머지 파일을 덮어쓴다.

그다음 3번(설치)과 4번(연결)을 한 번 더 실행한다. 서브에이전트 정의 두 파일은 복사본이라 다시 넣어야 새 내용이 적용된다. 끝나면 5번(점검)을 하고, 안티그래비티를 다시 켜라고 알린다.

## 제거

사용자가 지워 달라고 할 때만 한다. 지우기 전에 무엇을 지우는지 알리고 확인을 받는다.

- `%USERPROFILE%\.gemini\config\skills\hwpick` (연결만 지운다. 연결이 가리키는 폴더의 내용을 지우지 않게 주의한다)
- `%USERPROFILE%\.gemini\config\agents\hwpick-reader.md`, `hwpick-dispatch.md`
- `%USERPROFILE%\hwp-problem-picker` 폴더
- 교재를 읽어 둔 캐시 `%LOCALAPPDATA%\HWPick`

사용자의 작업 폴더에 있는 `hwpick_data` 폴더와 만든 한글 파일은 사용자가 정한다.
