from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.ja_linebreak import break_ja


ROOT = Path(__file__).resolve().parents[1]


def _node_bin() -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    proc = subprocess.run(
        [node, "-p", "process.versions.node"],
        capture_output=True,
        text=True,
        check=False,
    )
    raw = (proc.stdout or "").strip()
    try:
        major = int(raw.split(".", 1)[0])
    except ValueError:
        pytest.skip(f"could not parse node version {raw!r}")
    if major < 22:
        pytest.skip(f"node {raw} is older than 22")
    return node


def _render_fake_caption() -> dict:
    renderer = (ROOT / "app" / "static" / "room_target.js").as_uri()
    script = f"""
import {{ createRubyMemory, paintTarget }} from {json.dumps(renderer)};

class Node {{
  constructor(tag, text = null) {{
    this.tag = tag;
    this.text = text;
    this.children = [];
  }}
  appendChild(child) {{ this.children.push(child); return child; }}
  removeChild(child) {{ this.children.splice(this.children.indexOf(child), 1); }}
  get firstChild() {{ return this.children[0] || null; }}
  get textContent() {{
    return this.text === null
      ? this.children.map((child) => child.textContent).join("")
      : this.text;
  }}
}}

const doc = {{
  createTextNode: (text) => new Node("#text", text),
  createElement: (tag) => new Node(tag),
}};
const container = new Node("div");
const item = {{
  id: "room:session:1",
  session_id: "session",
  tgt_lang: "ja",
  en: 'prefix <script>alert("quoted") & safe</script> 禅',
  segments: ['prefix <script>alert("quoted") & safe</script> ', "禅"],
  ruby: [{{ text: "禅", reading: '<script title="read&">not code</script>' }}],
}};
paintTarget(doc, container, item, {{ memory: createRubyMemory() }});

function escapeText(text) {{
  return text.replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#x27;");
}}
function serialize(node) {{
  if (node.tag === "#text") return escapeText(node.text);
  const children = node.children.map(serialize).join("");
  return node.tag === "wbr" ? "<wbr>" : `<${{node.tag}}>${{children}}</${{node.tag}}>`;
}}
function tagNames(node) {{
  return [node.tag, ...node.children.flatMap(tagNames)];
}}
console.log(JSON.stringify({{
  markup: container.children.map(serialize).join(""),
  tags: container.children.flatMap(tagNames),
}}));
"""
    proc = subprocess.run(
        [_node_bin(), "--input-type=module", "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
    return json.loads(proc.stdout)


def test_fake_japanese_caption_escapes_text_and_emits_only_ruby_elements() -> None:
    rendered = _render_fake_caption()

    assert (
        '&lt;script&gt;alert(&quot;quoted&quot;) &amp; safe&lt;/script&gt;'
        in rendered["markup"]
    )
    assert (
        '<ruby>禅<rt>&lt;script title=&quot;read&amp;&quot;&gt;not code'
        '&lt;/script&gt;</rt></ruby>'
    ) in rendered["markup"]
    assert set(rendered["tags"]) <= {"#text", "wbr", "ruby", "rt"}
    assert rendered["tags"].count("ruby") == 1
    assert rendered["tags"].count("rt") == 1


@pytest.mark.xfail(
    strict=True,
    reason="ja_linebreak counts the full {base|reading} annotation instead of only its base text",
)
def test_ja_linebreak_does_not_count_ruby_reading_toward_line_width() -> None:
    base = "禅" * 15
    annotated = f"{{{base}|{'ぜん' * 10}}}"

    assert break_ja(f"{annotated}日", max_chars=16, max_lines=10) == [
        f"{annotated}日"
    ]


def test_ja_linebreak_never_splits_a_ruby_group_across_lines() -> None:
    ruby = "{禅禅|ぜんぜん}"
    text = "前" * 14 + ruby + "後" * 16

    lines = break_ja(text, max_chars=16, max_lines=10)

    assert "".join(lines) == text
    assert sum(ruby in line for line in lines) == 1
