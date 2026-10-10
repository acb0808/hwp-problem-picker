"""설치 상태 점검. 한글 파일을 만들려면 한글 프로그램과 '자동화 보안 모듈' 등록이 필요하다.

    python -m hwpick doctor          무엇이 준비됐고 무엇이 빠졌는지 보여 준다
    python -m hwpick doctor --fix    보안 모듈이 등록되어 있지 않으면 등록한다(이미 등록된 것은 건드리지 않는다)

보안 모듈은 한컴이 자동화용으로 배포하는 DLL로, 등록해 두면 한글이 파일을 열고 저장할 때마다 묻는
확인 창이 뜨지 않는다. 등록 위치는 현재 사용자 레지스트리(HKCU)다. 이 DLL은 한컴의 것이라 이 스킬에 넣어
배포하지 않는다. 설치할 때 받아 두는 파이썬 꾸러미 pyhwpx에 들어 있는 것을 찾아 등록한다.
"""
from __future__ import annotations

import sys
from pathlib import Path

MODULE_NAME = "FilePathCheckerModule"
MODULE_KEY = r"Software\HNC\HwpAutomation\Modules"


def _registered_module() -> str | None:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, MODULE_KEY) as key:
            return winreg.QueryValueEx(key, MODULE_NAME)[0]
    except OSError:
        return None


def _register_module(dll: Path) -> None:
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, MODULE_KEY) as key:
        winreg.SetValueEx(key, MODULE_NAME, 0, winreg.REG_SZ, str(dll))


def _module_file() -> Path | None:
    """등록할 보안 모듈 파일을 찾는다: 스킬 폴더에 직접 넣어 둔 것, 없으면 pyhwpx 꾸러미에 든 것."""
    import importlib.util
    here = Path(__file__).parent / "assets" / f"{MODULE_NAME}.dll"
    if here.is_file():
        return here
    try:
        spec = importlib.util.find_spec("pyhwpx")
    except (ImportError, ValueError):
        spec = None
    for folder in (spec.submodule_search_locations or []) if spec else []:
        if (Path(folder) / f"{MODULE_NAME}.dll").is_file():
            return Path(folder) / f"{MODULE_NAME}.dll"
    return None


def _hwp_installed() -> bool:
    import winreg
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"HWPFrame.HwpObject\CLSID"))
        return True
    except OSError:
        return False


def run(fix: bool = False) -> int:
    """점검 결과를 출력하고, 한글 파일 작성까지 가능한 상태면 0을 돌려준다."""
    ok = True

    def report(good: bool, text: str, hint: str = "") -> None:
        nonlocal ok
        ok = ok and good
        print(("  [준비됨] " if good else "  [필요함] ") + text)
        if not good and hint:
            print("           → " + hint)

    print("HWPick 설치 점검")
    try:
        import cv2  # noqa: F401
        import pymupdf  # noqa: F401
        import rapidocr  # noqa: F401
        report(True, "문제 찾기에 쓰는 프로그램 모듈")
    except ImportError as e:
        report(False, f"문제 찾기에 쓰는 프로그램 모듈({e.name})", "setup.bat을 다시 실행하세요.")
    try:
        import latex_to_hwpeqn  # noqa: F401
        import lxml  # noqa: F401
        report(True, "한글 문서 작성에 쓰는 프로그램 모듈")
    except ImportError as e:
        report(False, f"한글 문서 작성에 쓰는 프로그램 모듈({e.name})", "setup.bat을 다시 실행하세요.")
    if sys.platform != "win32":
        report(False, "윈도우", "한글 파일 작성은 윈도우에서만 됩니다(문제 고르기는 다른 운영체제에서도 됩니다).")
        return 1
    try:
        import psutil  # noqa: F401
        import win32com.client  # noqa: F401
        report(True, "한글 프로그램을 움직이는 모듈")
    except ImportError as e:
        report(False, f"한글 프로그램을 움직이는 모듈({e.name})", "setup.bat을 다시 실행하세요.")
    report(_hwp_installed(), "한글 프로그램", "한컴오피스 한글이 설치되어 있어야 한글 파일을 만들 수 있습니다.")
    current = _registered_module()
    bundled = _module_file()
    if current and Path(current).is_file():
        report(True, "한글 자동화 보안 모듈")
    elif fix and bundled is not None:
        _register_module(bundled)
        report(True, f"한글 자동화 보안 모듈(방금 등록함: {bundled})")
    elif bundled is not None:
        why = "등록된 파일이 없어졌습니다" if current else "등록되어 있지 않습니다"
        report(False, f"한글 자동화 보안 모듈({why})", "python -m hwpick doctor --fix 로 등록하세요.")
    else:
        report(False, "한글 자동화 보안 모듈(파일을 찾지 못했습니다)",
               "setup.bat을 다시 실행하세요(pyhwpx 꾸러미에 든 FilePathCheckerModule.dll을 씁니다). 그래도 없으면 한컴이 배포하는 "
               "'한글 오토메이션 보안 모듈'을 받아 hwpick" + chr(92) + "assets 폴더에 FilePathCheckerModule.dll로 넣은 뒤 다시 실행하세요.")
    assets = Path(__file__).parent / "assets"
    report((assets / "양식.hwp").is_file() or (assets / "양식.hwpx").is_file(), "기본 양식 파일",
           "작업 폴더에 양식.hwp를 두거나 build --template 으로 지정하세요.")
    print("모두 준비됐습니다." if ok else "위의 [필요함] 항목을 해결해 주세요.")
    return 0 if ok else 1
