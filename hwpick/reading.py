"""옮겨 적은 내용을 저장하는 형식. hwp-exam-restoration 스킬의 `restoration-reading/1`과 같은 꼴이다.

    {"schema": "restoration-reading/1", "page": 159, "worker_id": "…", "source_sha256": "…(64자)",
     "complete": true, "issues": [],
     "questions": [{"id": "q1114", "column": "left", "uncertain": false, "content": [블록 …]}]}

    블록   paragraph {runs[, align: "CENTER"][, figure_ref: "A"]}   그림 자리는 runs가 빈 문단
           box       {title, content: [paragraph …]}
           choices   {columns, rows: [[조각들, …], …]}                 선택지의 조각들은 '① '로 시작한다
    조각   text {text} / equation {latex} / boxed_text {text} / break

두 스킬이 같은 파일을 주고받을 수 있게 하려는 것이라, 이 꼴에 없는 키는 넣지 않는다. HWPick에만 있는 것은 따로 둔다.
- 선택지를 한 줄에 몇 개 놓을지의 힌트(cols, 원본 표의 폭 w)는 자리가 없어 extras로 돌려준다(저장할 때 옆에 둔다).
- 수식 안의 상자(`\\frac{n!}{\\boxed{(가)}}`)와 한글 문서에서 꺼낸 수식(`\\hwpeq …`)은 equation의 latex에 그대로
  둔다. 형식에는 맞지만 복원 스킬은 그리지 못한다. 혼자 선 `$\\boxed{(가)}$`는 그쪽에도 있는 boxed_text로 적는다.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from . import transcript
from .transcript import CIRCLED, Boxed, Choices, Figure, Figures, Para, Run

SCHEMA = "restoration-reading/1"
BOXED_TEXT_RE = re.compile(r"\\boxed\s*\{\s*([^\[\]\\${}\r\n]{1,20}?)\s*\}")
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
RUN_FIELDS = {"text": {"kind", "text"}, "boxed_text": {"kind", "text"}, "equation": {"kind", "latex"}, "break": {"kind"}}
COMMON = {"before_mm", "after_mm", "left_mm", "right_mm", "font_pt", "font_family", "line_spacing_pct", "align"}


def _runs_out(runs: list[Run]) -> list[dict]:
    out = []
    for r in runs:
        if not r.eq:
            out.append({"kind": "text", "text": r.text})
            continue
        latex = " ".join(r.latex.split("\n")).strip()
        boxed = BOXED_TEXT_RE.fullmatch(latex)
        out.append({"kind": "boxed_text", "text": boxed.group(1)} if boxed else {"kind": "equation", "latex": latex})
    return out


def content_of(blocks: list) -> tuple[list[dict], dict]:
    """HWPick의 블록들을 저장 형식의 content로 바꾼다. (content, extras)를 돌려준다."""
    hints: list[dict] = []

    def walk(items: list, in_box: bool) -> list[dict]:
        out = []
        for b in items:
            if isinstance(b, Para):
                if b.runs:
                    out.append({"kind": "paragraph", "runs": _runs_out(b.runs), **({"align": "CENTER"} if b.center else {})})
            elif isinstance(b, Figure):
                out.append({"kind": "paragraph", "runs": [], "figure_ref": b.name})
            elif isinstance(b, Figures):
                # 나란히 놓인 모습은 저장 형식에 자리가 없다. 그림만 차례대로 적는다(되살릴 때는 글에서 만든 블록을 쓴다)
                out += [{"kind": "paragraph", "runs": [], "figure_ref": n} for row in b.rows for n in row]
            elif isinstance(b, Choices):
                cells = [[{"kind": "text", "text": CIRCLED[k % len(CIRCLED)] + " "}] + _runs_out(o) for k, o in enumerate(b.options)]
                if in_box:
                    # 상자 안에는 문단만 들어간다. 선택지를 한 문단으로 풀어 적는다(상자 안 선택지는 드물다)
                    runs = [r for k, cell in enumerate(cells) for r in ([{"kind": "text", "text": "  "}] if k else []) + cell]
                    out.append({"kind": "paragraph", "runs": runs})
                    continue
                columns = b.cols if 0 < b.cols <= 5 else max(1, min(5, len(cells)))
                out.append({"kind": "choices", "columns": columns,
                            "rows": [cells[k:k + columns] for k in range(0, len(cells), columns)]})
                hints.append({"cols": b.cols, "w": b.cols_width})
            elif isinstance(b, Boxed):
                inner = walk(b.blocks, True)
                if in_box:
                    out += inner                      # 상자 속의 상자는 없다. 속의 것을 꺼내 잇는다
                elif inner:
                    out.append({"kind": "box", "title": b.label, "content": inner})
        return out

    content = walk(blocks, False)
    return content, ({"choices": hints} if any(h["cols"] or h["w"] for h in hints) else {})


def _runs_in(runs: list[dict], errors: list[str]) -> list[list[Run]]:
    """조각들을 문단별 Run 목록으로(줄 나눔 break는 문단을 가른다)."""
    lines: list[list[Run]] = [[]]
    for r in runs:
        kind = r.get("kind")
        if kind == "break":
            lines.append([])
        elif kind == "text":
            if r["text"]:
                lines[-1].append(Run(text=r["text"]))
        elif kind in ("equation", "boxed_text"):
            latex = r["latex"] if kind == "equation" else "\\boxed{" + r["text"] + "}"
            if latex.lstrip().startswith(transcript.RAW_EQUATION):
                script = latex.lstrip()[len(transcript.RAW_EQUATION):].strip()
                lines[-1] += [Run(latex=latex.strip(), eq=script)] if script else []
                continue
            eq, err = transcript.to_hwp_equation(latex)
            if eq:
                lines[-1].append(Run(latex=latex, eq=eq))
            else:
                errors.append(f"수식을 바꾸지 못했습니다: ${latex.strip()}$ ({err})")
                lines[-1].append(Run(text=latex))
    return [line for line in lines if line]


def blocks_of(content: list[dict], extras: dict | None = None) -> tuple[list, list[str]]:
    """저장 형식의 content를 HWPick의 블록들로 바꾼다. (블록들, 오류들)을 돌려준다."""
    errors: list[str] = []
    hints = list((extras or {}).get("choices", []))

    def walk(items: list[dict]) -> list:
        out: list = []
        for b in items:
            kind = b.get("kind")
            if kind == "paragraph":
                if b.get("figure_ref"):
                    out.append(Figure(b["figure_ref"]))
                out += [Para(line, center=b.get("align") == "CENTER") for line in _runs_in(b.get("runs", []), errors)]
            elif kind == "box":
                out.append(Boxed(label=b.get("title", ""), blocks=walk(b.get("content", []))))
            elif kind == "choices":
                options = []
                for cell in (c for row in b.get("rows", []) for c in row):
                    runs = [r for line in _runs_in(cell, errors) for r in line]
                    if runs and not runs[0].eq and runs[0].text.lstrip()[:1] in tuple(CIRCLED):
                        rest = runs[0].text.lstrip()[1:].lstrip()
                        runs = ([Run(text=rest)] if rest else []) + runs[1:]
                    options.append(runs)
                hint = hints.pop(0) if hints else {"cols": b.get("columns", 0) if len(b.get("rows", [])) > 1 else 0, "w": 0}
                out.append(Choices(options, cols=hint.get("cols", 0), cols_width=hint.get("w", 0)))
            else:
                errors.append(f"알 수 없는 블록입니다: {kind}")
        return out

    return walk(content), errors


def flat(blocks: list) -> list:
    """블록들을 견줄 수 있는 꼴로(글과 한글 수식만 본다)."""
    out = []
    for b in blocks:
        if isinstance(b, Para):
            out.append(("P", b.center, [(r.text, r.eq) for r in b.runs]))
        elif isinstance(b, Choices):
            out.append(("C", b.cols, b.cols_width, [[(r.text, r.eq) for r in o] for o in b.options]))
        elif isinstance(b, Boxed):
            out.append(("B", b.label, flat(b.blocks)))
        elif isinstance(b, Figure):
            out.append(("F", b.name))
        elif isinstance(b, Figures):
            out.append(("G", b.rows))
    return out


def source_sha256(path: str | Path, cache_dir: str | Path | None = None) -> str:
    """원본 파일의 sha256(64자). 큰 교재는 몇 초 걸리므로 교재의 캐시 폴더에 적어 두고 다시 쓴다.
    원본이 그 자리에 없으면 캐시에 적어 둔 값을, 그것도 없으면 캐시에 있는 변환본의 값을 쓴다."""
    note = Path(cache_dir) / "sha256.txt" if cache_dir else None
    if note is not None and note.is_file():
        kept = note.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[0-9a-f]{64}", kept):
            return kept
    target = Path(path)
    if not target.is_file() and cache_dir:
        target = next((f for f in (Path(cache_dir) / "doc.hwpx", Path(cache_dir) / "doc.pdf") if f.is_file()), target)
    h = hashlib.sha256()
    with target.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    if note is not None and Path(path).is_file():
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(h.hexdigest(), encoding="utf-8")
    return h.hexdigest()


def question_id(item: dict) -> str:
    """문제의 식별자: 'q' + 교재의 문제 번호(글자·숫자만). 번호가 없으면 목록의 문항 ID."""
    label = re.sub(r"[^A-Za-z0-9_-]", "", str(item.get("label", "")))
    return "q" + label if label else re.sub(r"[^A-Za-z0-9_-]", "-", str(item["pid"]))


def document(item: dict, blocks: list, *, sha256: str, worker: str, page_width: float, issues: list[str] | None = None,
             uncertain: bool = False, complete: bool = True) -> tuple[dict, dict]:
    """문제 하나를 담은 reading 문서와 extras. item은 목록의 문항(page, bbox, label, pid)이다."""
    content, extras = content_of(blocks)
    x0, _, x1, _ = item["bbox"]
    column = "left" if (x0 + x1) / 2 < page_width / 2 else "right"
    doc = {"schema": SCHEMA, "page": int(item["page"]), "worker_id": worker or "unknown", "source_sha256": sha256,
           "complete": bool(complete and content), "issues": [str(v) for v in issues or [] if str(v).strip()],
           "questions": [{"id": question_id(item), "column": column, "uncertain": bool(uncertain), "content": content}]
           if content else []}
    return doc, extras


def validate(doc: dict) -> list[str]:
    """reading 문서가 형식에 맞는지 본다(복원 스킬의 검사와 같은 규칙). 어긋난 점들을 돌려준다."""
    bad: list[str] = []
    need = {"schema", "page", "worker_id", "source_sha256", "complete", "issues", "questions"}
    if not isinstance(doc, dict) or set(doc) != need:
        return ["reading: 키가 모자라거나 남습니다"]
    if doc["schema"] != SCHEMA or type(doc["page"]) is not int or doc["page"] < 1:
        bad.append("reading: schema 또는 page")
    if not isinstance(doc["worker_id"], str) or not doc["worker_id"].strip():
        bad.append("reading.worker_id")
    if not isinstance(doc["source_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", doc["source_sha256"]):
        bad.append("reading.source_sha256")
    if type(doc["complete"]) is not bool or not isinstance(doc["issues"], list) or not isinstance(doc["questions"], list):
        return bad + ["reading: complete/issues/questions"]
    if doc["complete"] and not doc["questions"]:
        bad.append("reading: complete인데 문제가 없습니다")
    bad += [f"reading.issues[{k}]" for k, v in enumerate(doc["issues"]) if not isinstance(v, str) or not v.strip()]

    def runs(values, where: str, allow_empty: bool = False) -> None:
        if not isinstance(values, list) or (not values and not allow_empty):
            bad.append(where + ": 조각이 비었습니다")
            return
        for k, r in enumerate(values):
            kind = r.get("kind") if isinstance(r, dict) else None
            if kind not in RUN_FIELDS or set(r) != RUN_FIELDS[kind]:
                bad.append(f"{where}[{k}]: 조각의 키")
            elif kind == "equation" and (not r["latex"].strip() or any(c in r["latex"] for c in "$\r\n")):
                bad.append(f"{where}[{k}]: 수식은 한 줄, $ 없이")
            elif kind == "boxed_text" and (not 1 <= len(r["text"]) <= 20 or any(c in r["text"] for c in "[]\\")):
                bad.append(f"{where}[{k}]: 상자 글은 1~20자")
            elif kind == "text" and not isinstance(r["text"], str):
                bad.append(f"{where}[{k}]: 글")

    def content(values, where: str, refs: list, in_box: bool = False) -> None:
        if not isinstance(values, list) or not values:
            bad.append(where + ": 내용이 비었습니다")
            return
        for k, b in enumerate(values):
            at = f"{where}[{k}]"
            kind = b.get("kind") if isinstance(b, dict) else None
            required = {"paragraph": {"kind", "runs"}, "box": {"kind", "title", "content"},
                        "choices": {"kind", "columns", "rows"}}.get(kind)
            if required is None or (in_box and kind != "paragraph"):
                bad.append(at + ": 쓸 수 없는 블록")
                continue
            optional = COMMON | ({"figure_ref"} if kind == "paragraph" else {"stroke_mm", "padding_mm"} if kind == "box"
                                 else {"tab_stops_mm"})
            if not required <= set(b) or not set(b) <= required | optional:
                bad.append(at + ": 블록의 키")
                continue
            if b.get("align", "LEFT") not in ("LEFT", "CENTER", "RIGHT", "JUSTIFY"):
                bad.append(at + ": align")
            if kind == "paragraph":
                if "figure_ref" in b:
                    if not isinstance(b["figure_ref"], str) or not ID_RE.fullmatch(b["figure_ref"]) or b["figure_ref"] in refs \
                            or b["runs"] != []:
                        bad.append(at + ": figure_ref")
                    refs.append(b["figure_ref"])
                runs(b["runs"], at + ".runs", "figure_ref" in b)
            elif kind == "choices":
                if type(b["columns"]) is not int or not 1 <= b["columns"] <= 5 or not isinstance(b["rows"], list) or not b["rows"]:
                    bad.append(at + ": columns/rows")
                    continue
                for row in b["rows"]:
                    if not isinstance(row, list) or not 1 <= len(row) <= b["columns"]:
                        bad.append(at + ": 선택지 줄")
                        continue
                    for cell in row:
                        runs(cell, at + ".rows")
            else:
                if not isinstance(b["title"], str):
                    bad.append(at + ".title")
                content(b["content"], at + ".content", refs, True)

    seen, refs = set(), []
    for k, q in enumerate(doc["questions"]):
        at = f"reading.questions[{k}]"
        if not isinstance(q, dict) or set(q) != {"id", "column", "uncertain", "content"}:
            bad.append(at + ": 키")
            continue
        if not isinstance(q["id"], str) or not q["id"].strip() or q["id"] in seen:
            bad.append(at + ".id")
        seen.add(q["id"])
        if q["column"] not in ("left", "right") or type(q["uncertain"]) is not bool:
            bad.append(at + ": column/uncertain")
        content(q["content"], at + ".content", refs)
    return bad
