# 출처 안내

이 저장소의 코드는 [MIT 라이선스](LICENSE)입니다. 아래는 함께 쓰는 프로그램입니다.

## 저장소에 들어 있는 것

- **latex-to-hwpeqn 0.3.0** (`vendor/latex_to_hwpeqn-0.3.0-py3-none-any.whl`): LaTeX 수식을 한글 수식으로 바꾸는 변환기. 같은 제작자가 만든 것으로, MIT 라이선스인 [hwp-exam-restoration](https://github.com/acb0808/hwp-exam-restoration)에도 들어 있습니다.
- **기본 양식** (`hwpick/assets/양식.hwpx`): 쪽마다 2×2칸인 빈 문서 틀. 이 프로젝트의 일부입니다.

## 설치할 때 내려받는 것 (저장소에는 없습니다)

`setup.bat`이 각 배포처에서 받아 설치 폴더의 `.venv`에 넣습니다. 이용 조건은 각 프로젝트를 따릅니다.

- uv (설치 도구), Python 3.12
- PyMuPDF, NumPy, OpenCV, Pillow, lxml, pywin32, psutil
- RapidOCR, ONNX Runtime, 그리고 RapidOCR이 처음 실행될 때 받는 글자 인식 모델
- pyhwpx: 이 스킬은 pyhwpx의 기능을 쓰지 않습니다. 그 꾸러미에 들어 있는 **한글 자동화 보안 모듈(`FilePathCheckerModule.dll`, 한글과컴퓨터 배포)** 파일의 위치만 찾아 현재 사용자에게 등록합니다. 이 DLL은 한글과컴퓨터의 것이며 이 저장소에는 넣지 않았습니다.

## 필요하지만 포함되지 않는 것

- 한컴오피스 한글 (한글과컴퓨터)
- 구글 안티그래비티와 그 이용권
- 교재 PDF: 이 저장소에는 교재나 그 내용이 들어 있지 않습니다.
