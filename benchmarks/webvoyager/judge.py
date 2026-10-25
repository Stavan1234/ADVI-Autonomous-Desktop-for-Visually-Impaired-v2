"""Judge a WebVoyager run: an AI pre-screen, then the human verdict.

    .venv\\Scripts\\python.exe -m benchmarks.webvoyager.judge benchmarks\\results\\webvoyager\\dev-r1

1. Pre-screen: Gemini (the lite vision model, its own quota) reads the task,
   its annotation questions, ADVI's answer and the last 3 screenshots, and
   suggests pass/fail. It is a suggestion: Emergence WebVoyager found automatic
   judges disagree with humans 20-40% of the time.
2. review.html: one page per run with every task's answer and screenshots,
   the suggestion, and pass/fail plus a failure category to choose. "Save"
   downloads annotations.json; put it in the run folder. That file is the score.
"""

from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from benchmarks.common.runner import load_records  # noqa: E402

# WebVoyager's failure reasons, plus ADVI's own.
CATEGORIES = [
    "navigation stuck", "visual grounding", "hallucination", "prompt misalignment",
    "blocked (captcha / login)", "needs user (confirmation)", "missing tool", "site changed / task impossible", "other",
]

_PROMPT = """You are checking whether a web assistant completed a task on a real website.
Task: {task}
Questions the human judge will answer: {questions}
{golden}The assistant's final answer: {answer}
The images are the last screenshots of the browser, in order.
Judge only from the answer and the screenshots. Reply with JSON only:
{{"pass": true or false, "reason": "one sentence"}}"""


def prescreen(record: dict[str, Any], folder: Path, *, ask: Any) -> dict[str, Any]:
    shots = [folder / name for name in record.get("screenshots", [])][-3:]
    golden = f"Reference answer: {record['golden_answer']}\n" if record.get("golden_answer") else ""
    prompt = _PROMPT.format(task=record["instruction"], questions="; ".join(record.get("questions") or []),
                            golden=golden, answer=record.get("answer") or "(none)")
    text = ask(prompt, [p.read_bytes() for p in shots if p.exists()])
    match = re.search(r"\{.*\}", str(text), re.S)
    try:
        verdict = json.loads(match.group(0)) if match else {}
    except ValueError:
        verdict = {}
    return {"pass": bool(verdict.get("pass")), "reason": str(verdict.get("reason") or text)[:300]}


def gemini_ask():
    from google import genai
    from google.genai import types

    from advi.capabilities.desktop.screen_describer import DEFAULT_VISION_MODEL
    from advi.core.config import load_settings

    keys = list(load_settings().gemini_api_keys)
    if not keys:
        raise RuntimeError("No Gemini key configured for the pre-screen.")
    client = genai.Client(api_key=keys[0], http_options=types.HttpOptions(timeout=60000))

    def ask(prompt: str, images: list[bytes]) -> str:
        parts = [prompt, *[types.Part.from_bytes(data=b, mime_type="image/jpeg") for b in images]]
        return client.models.generate_content(model=DEFAULT_VISION_MODEL, contents=parts).text or ""

    return ask


def review_page(run_dir: Path, records: dict[str, dict[str, Any]], prescreens: dict[str, dict[str, Any]]) -> str:
    options = "".join(f"<option>{html.escape(c)}</option>" for c in CATEGORIES)
    cards = []
    for tid, rec in records.items():
        pre = prescreens.get(tid, {})
        shots = "".join(f'<a href="{tid}/{s}" target="_blank"><img src="{tid}/{s}" loading="lazy"></a>'
                        for s in rec.get("screenshots", []))
        steps = "".join(f"<li>{html.escape(str(t.get('name')))}: {html.escape(str(t.get('summary'))[:160])}</li>"
                        for t in rec.get("tools", []))
        questions = "".join(f"<li>{html.escape(q)}</li>" for q in rec.get("questions") or [])
        golden = f"<p><b>Reference answer:</b> {html.escape(str(rec['golden_answer']))}</p>" if rec.get("golden_answer") else ""
        suggestion = f"AI pre-screen: {'pass' if pre.get('pass') else 'fail'} ({html.escape(pre.get('reason', ''))})" if pre else "No pre-screen."
        cards.append(f"""
<section data-id="{tid}">
  <h2>{tid} <small>{html.escape(str(rec.get('status')))} · {rec.get('seconds')} s · {rec.get('llm_calls')} AI calls · {rec.get('tool_calls')} tool calls</small></h2>
  <p><b>Task:</b> {html.escape(rec['instruction'])}</p>
  <p><b>Judge these:</b></p><ul>{questions}</ul>{golden}
  <p><b>ADVI's answer:</b> {html.escape(rec.get('answer') or '')}</p>
  <details><summary>Steps ({len(rec.get('tools', []))})</summary><ol>{steps}</ol></details>
  <div class="shots">{shots}</div>
  <p class="pre">{suggestion}</p>
  <label><input type="radio" name="{tid}" value="pass"> Pass</label>
  <label><input type="radio" name="{tid}" value="fail"> Fail</label>
  <select name="{tid}-category"><option value="">failure category…</option>{options}</select>
  <input name="{tid}-note" placeholder="note">
</section>""")
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>WebVoyager review: {html.escape(run_dir.name)}</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:1100px;margin:0 auto;padding:16px;background:#fafafa;color:#222}}
section{{background:#fff;border:1px solid #ddd;border-radius:8px;padding:12px 16px;margin:16px 0}}
h2{{font-size:1.05rem}} small{{color:#666;font-weight:normal}} .pre{{color:#555;font-style:italic}}
.shots{{display:flex;gap:6px;overflow-x:auto}} .shots img{{height:150px;border:1px solid #ccc}}
button{{position:sticky;top:8px;padding:8px 16px;font-size:1rem}}
</style></head><body>
<h1>WebVoyager review: {html.escape(run_dir.name)}</h1>
<p>Mark every task. The AI pre-screen is only a suggestion. Save downloads <code>annotations.json</code>; put it in <code>{html.escape(str(run_dir))}</code>.</p>
<button onclick="save()">Save annotations.json</button>
{''.join(cards)}
<script>
function save(){{
  const out={{}};
  document.querySelectorAll('section').forEach(s=>{{
    const id=s.dataset.id, v=s.querySelector('input[type=radio]:checked');
    if(!v) return;
    out[id]={{pass:v.value==='pass', category:s.querySelector('select').value, note:s.querySelector('input[name$=-note]').value}};
  }});
  const a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([JSON.stringify(out,null,2)],{{type:'application/json'}}));
  a.download='annotations.json'; a.click();
}}
</script></body></html>"""


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    run_dir = Path(args[0])
    skip_ai = "--no-prescreen" in args
    records = load_records(run_dir)
    pre_path = run_dir / "prescreen.json"
    prescreens = json.loads(pre_path.read_text(encoding="utf-8")) if pre_path.exists() else {}
    if not skip_ai:
        ask = gemini_ask()
        for tid, rec in records.items():
            if tid in prescreens or rec.get("status") == "quota":
                continue
            try:
                prescreens[tid] = prescreen(rec, run_dir / tid, ask=ask)
            except Exception as exc:  # a pre-screen is optional; the human verdict is not
                print(f"pre-screen failed for {tid}: {exc}")
            pre_path.write_text(json.dumps(prescreens, indent=2), encoding="utf-8")
    page = run_dir / "review.html"
    page.write_text(review_page(run_dir, records, prescreens), encoding="utf-8")
    print(f"Open {page} in a browser, mark each task, and save annotations.json into {run_dir}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
