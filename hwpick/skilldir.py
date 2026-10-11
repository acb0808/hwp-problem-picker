"""에이전트의 스킬 폴더에 이 스킬을 넣는다.

    python -m hwpick.skilldir "<보여 줄 이름>" "<스킬 폴더>"

스킬 폴더에 SKILL.md 한 장만 든 진짜 폴더를 만든다. 예전에는 설치 폴더로 가는 연결(junction)을 만들었는데,
안티그래비티는 연결된 폴더를 스킬로 읽지 않는다(진짜 폴더와 연결 폴더를 나란히 두고 확인했다. 연결 쪽만 목록에
없었다). 그래서 SKILL.md를 복사해 두고, 그 안에 설치 폴더의 위치를 적어 넣는다. 설치 폴더를 고친 뒤에는 이
명령을 다시 실행해야 스킬 폴더의 SKILL.md가 따라온다.

끝난 값: 0 넣음(또는 이미 그 자리가 설치 폴더), 1 다른 것이 있어 넣지 않음, 2 실패.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

NAME = "hwp-problem-picker"
OLD_NAMES = ("hwpick",)                      # 예전 판이 만든 연결의 이름
HOME = Path(__file__).resolve().parent.parent
# SKILL.md에서 스킬 폴더를 정하는 대목. 복사본에서는 이 대목을 설치 폴더의 위치로 바꾼다
ANCHOR = "이 스킬 폴더(이 파일이 있는 폴더)를 `SKILL`이라 하고"
STAMP = "<!-- hwp-problem-picker: install_skill.bat이 만든 복사본. 고칠 때는 설치 폴더의 SKILL.md를 고치고 install_skill.bat을 다시 실행한다 -->"


def _same(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def skill_text(home: Path = HOME) -> str:
    """스킬 폴더에 둘 SKILL.md의 내용: 설치 폴더의 SKILL.md에 설치 폴더의 위치를 적어 넣은 것."""
    text = (home / "SKILL.md").read_text(encoding="utf-8")
    if text.count(ANCHOR) != 1:
        raise ValueError("SKILL.md에서 스킬 폴더를 정하는 대목을 찾지 못했습니다")
    text = text.replace(ANCHOR, f"이 스킬이 설치된 폴더는 `{home}`이다(이 파일은 그 폴더의 SKILL.md를 복사한 것이다). 그 폴더를 `SKILL`이라 하고")
    head, sep, body = text.partition("\n---\n")          # 머리말(--- … ---) 바로 아래에 표시를 남긴다
    return head + sep + "\n" + STAMP + "\n" + body if sep else text + "\n" + STAMP + "\n"


def install(skills: Path, home: Path = HOME) -> tuple[int, str]:
    """skills(에이전트의 스킬 폴더)에 이 스킬을 넣는다. (끝난 값, 알릴 말)을 돌려준다."""
    target = skills / NAME
    notes = []
    for name in (*OLD_NAMES, NAME):                      # 이 설치 폴더를 가리키는 연결은 지운다(연결만. 다른 곳을 가리키면 둔다)
        old = skills / name
        if os.path.isjunction(old) or old.is_symlink():
            if _same(old, home):
                os.rmdir(old)
                notes.append(f"예전 방식의 연결({name})을 지웠습니다")
            elif name == NAME:
                return 1, f"{target} 에 다른 곳으로 가는 연결이 있어 넣지 않았습니다. 그 폴더를 확인해 주세요."
    if target.exists():
        if _same(target, home):
            return 0, "; ".join(notes + ["설치 폴더가 이미 스킬 폴더 안에 있습니다."])
        ours = target / "SKILL.md"
        inside = [p.name for p in target.iterdir()]
        if inside != ["SKILL.md"] or STAMP not in ours.read_text(encoding="utf-8", errors="replace"):
            return 1, f"{target} 에 다른 폴더가 있어 넣지 않았습니다. 그 폴더를 확인해 주세요."
    target.mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_text(skill_text(home), encoding="utf-8", newline="\n")
    return 0, "; ".join(notes + ["스킬을 넣었습니다."])


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    if len(argv) != 2:
        print('쓰는 법: python -m hwpick.skilldir "<보여 줄 이름>" "<스킬 폴더>"')
        return 2
    label, skills = argv[0], Path(argv[1])
    try:
        code, said = install(skills)
    except (OSError, ValueError) as exc:
        code, said = 2, f"스킬을 넣지 못했습니다: {exc}"
    print(f" {label}: {said}")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
