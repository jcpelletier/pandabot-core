"""
pandabot_core.active_qa.journal
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The session journal: a running narrative of what the harness did, with frames.

This is **not** the findings report. The report is curated -- deduped, confirmed,
"here is what is wrong". The journal is chronological and uncurated -- "here is
what happened, in order, and here is what it looked like". They answer different
questions and have different lifetimes: you read the report to fix bugs, and you
read the journal to understand or distrust the run that produced it.

Three things shape the design.

**It is written as it goes, not at the end.** Events append to a JSONL file the
moment they happen, and the HTML is rendered from that file on demand. So the
journal can be watched mid-run, survives a crash, and survives the pause that
happens when the model is unloaded out from under a session (ACTIVE_QA.md 8.2).

**Frames are content-addressed.** A frame is stored at ``frames/<sha1>.webp``,
so a screen that does not change between forty actions is stored once and
referenced forty times. On a run that clicks its way through a screen full of
dead controls, that is the difference between forty images and one.

**"Thought process" is shown honestly.** Most steps in this loop have no model
reasoning behind them, because the loop is policy-driven precisely so it can
afford to run for an hour (ACTIVE_QA.md 5). The journal therefore marks which
turns were the model talking and which were the priority queue, rather than
narrating a queue's arithmetic as if it were thought. A journal that implied the
model was reasoning about every click would be a more comfortable read and a
false one.
"""

from __future__ import annotations

import hashlib
import html
import json
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

__all__ = [
    "EventKind", "JournalEvent", "FrameStore", "Journal", "render_html",
    "compress_frame",
]


class EventKind:
    """What a journal line can be. ``MODEL_*`` are the only ones the model said."""

    SESSION_START = "session_start"
    SESSION_END = "session_end"
    STATE_NEW = "state_new"
    ACTION = "action"
    MODEL_ORIENT = "model_orient"
    MODEL_JUDGE = "model_judge"
    CARD = "card_played"
    FINDING = "finding"
    CRASH = "crash"
    RELAUNCH = "relaunch"
    NOTE = "note"


#: Which kinds came from the model rather than the policy. Drives the visual
#: distinction in the rendered page.
MODEL_KINDS = frozenset({EventKind.MODEL_ORIENT, EventKind.MODEL_JUDGE})

_SEVERITY_CLASS = {
    "blocker": "sev-blocker", "major": "sev-major",
    "minor": "sev-minor", "polish": "sev-polish",
}


@dataclass
class JournalEvent:
    """One line of the narrative."""

    kind: str
    text: str
    at: float = field(default_factory=time.time)
    step: int = -1
    screen: str = ""
    rule: str = ""
    severity: str = ""
    #: Relative path of the frame, if one was captured for this event.
    frame: str | None = None
    #: True when this frame is byte-identical to one already stored: the screen
    #: did not change. Rendered as a marker rather than a repeated image.
    frame_repeat: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def from_model(self) -> bool:
        return self.kind in MODEL_KINDS

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "JournalEvent":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


# --- Frames -----------------------------------------------------------------


def compress_frame(src: Path, dst: Path, *, max_width: int = 800,
                   quality: int = 60) -> bool:
    """Shrink one screenshot into ``dst``. Returns True if it was really compressed.

    Pillow is not a dependency of this package and must not become one, so the
    import is lazy and failure is graceful: with no Pillow the original bytes
    are copied and the journal records that it is uncompressed. A big journal is
    a much smaller problem than a core package that will not import.
    """
    try:
        from PIL import Image  # noqa: PLC0415  (deliberately lazy)
    except Exception:
        shutil.copyfile(src, dst)
        return False
    try:
        with Image.open(src) as im:
            im = im.convert("RGB")
            if im.width > max_width:
                height = round(im.height * max_width / im.width)
                im = im.resize((max_width, height), Image.LANCZOS)
            im.save(dst, "WEBP", quality=quality, method=4)
        return True
    except Exception:
        shutil.copyfile(src, dst)
        return False


class FrameStore:
    """Content-addressed storage for compressed frames.

    Storing by hash of the *compressed* bytes means an unchanged screen costs
    nothing after the first capture, with no bookkeeping: the second write
    simply lands on the same path and is skipped.
    """

    def __init__(self, root: Path, *, max_width: int = 800, quality: int = 60,
                 compressor: Callable[..., bool] = compress_frame) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_width = max_width
        self.quality = quality
        self.compressor = compressor
        self._hashes: set[str] = set()
        self.compressed_all = True
        self.bytes_stored = 0
        self.frames_offered = 0
        self.frames_stored = 0

    def add(self, screenshot: str | Path | None) -> tuple[str | None, bool]:
        """Store one screenshot. Returns ``(relative path, was_a_repeat)``."""
        if not screenshot:
            return None, False
        src = Path(screenshot)
        if not src.exists():
            return None, False
        self.frames_offered += 1

        tmp = self.root / f".tmp{src.suffix or '.png'}"
        if not self.compressor(src, tmp, max_width=self.max_width,
                               quality=self.quality):
            self.compressed_all = False
        data = tmp.read_bytes()
        digest = hashlib.sha1(data).hexdigest()
        rel = f"{digest}.webp"
        target = self.root / rel

        if digest in self._hashes or target.exists():
            tmp.unlink(missing_ok=True)
            self._hashes.add(digest)
            return rel, True

        tmp.replace(target)
        self._hashes.add(digest)
        self.frames_stored += 1
        self.bytes_stored += len(data)
        return rel, False

    def stats(self) -> dict[str, Any]:
        saved = self.frames_offered - self.frames_stored
        return {
            "frames_offered": self.frames_offered,
            "frames_stored": self.frames_stored,
            "duplicates_collapsed": saved,
            "bytes_stored": self.bytes_stored,
            "megabytes_stored": round(self.bytes_stored / 1_048_576, 2),
            "compressed": self.compressed_all,
        }


# --- The journal ------------------------------------------------------------


class Journal:
    """Append-only session narrative on disk.

    Layout::

        <root>/
          events.jsonl      one JournalEvent per line, written as it happens
          frames/<sha1>.webp
          journal.html      rendered on demand, regenerable at any time
    """

    def __init__(self, root: str | Path, *, session_id: str = "",
                 title: str = "Active QA session",
                 max_width: int = 800, quality: int = 60,
                 compressor: Callable[..., bool] = compress_frame) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id
        self.title = title
        self.events_path = self.root / "events.jsonl"
        self.frames = FrameStore(self.root / "frames", max_width=max_width,
                                 quality=quality, compressor=compressor)
        self._count = 0

    # -- writing -----------------------------------------------------------
    def record(self, kind: str, text: str, *, screenshot: str | Path | None = None,
               **fields: Any) -> JournalEvent:
        frame, repeat = self.frames.add(screenshot)
        event = JournalEvent(kind=kind, text=text, frame=frame,
                             frame_repeat=repeat, **fields)
        with self.events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event.to_dict(), sort_keys=True) + "\n")
        self._count += 1
        return event

    def note(self, text: str, **fields: Any) -> JournalEvent:
        return self.record(EventKind.NOTE, text, **fields)

    def __len__(self) -> int:
        return self._count

    # -- reading -----------------------------------------------------------
    def events(self) -> Iterator[JournalEvent]:
        if not self.events_path.exists():
            return iter(())
        def _iter() -> Iterator[JournalEvent]:
            with self.events_path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        yield JournalEvent.from_dict(json.loads(line))
        return _iter()

    def stats(self) -> dict[str, Any]:
        return {"events": self._count, **self.frames.stats()}

    def render(self, path: str | Path | None = None) -> Path:
        """Render (or re-render) the HTML page. Safe to call mid-run."""
        out = Path(path) if path else self.root / "journal.html"
        out.write_text(
            render_html(list(self.events()), title=self.title,
                        session_id=self.session_id, stats=self.stats()),
            encoding="utf-8")
        return out


# --- Rendering --------------------------------------------------------------

_CSS = """
:root{--bg:#fbfbfa;--fg:#1a1a19;--muted:#6b6b66;--line:#e3e3df;--card:#fff;
--model:#4c3fb5;--model-bg:#f0eefc;--blocker:#a3121b;--major:#b3541e;
--minor:#7a6a20;--polish:#4a6b58;}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){
--bg:#151514;--fg:#eceae4;--muted:#9a978e;--line:#2e2e2b;--card:#1e1e1c;
--model:#b3a8ff;--model-bg:#241f45;--blocker:#ff8a80;--major:#ffb27a;
--minor:#e0d08a;--polish:#9fd7b8;}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.55 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:24px 20px 96px}
h1{font-size:20px;margin:0 0 2px}
.sub{color:var(--muted);font-size:13px;margin-bottom:20px}
.bar{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);
padding:10px 0;margin-bottom:18px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.bar button{font:inherit;font-size:12px;padding:4px 10px;border:1px solid var(--line);
background:var(--card);color:var(--fg);border-radius:999px;cursor:pointer}
.bar button[aria-pressed=true]{background:var(--fg);color:var(--bg);border-color:var(--fg)}
.stats{color:var(--muted);font-size:12px;margin-left:auto}
.ev{display:grid;grid-template-columns:64px 1fr;gap:14px;padding:10px 0;
border-bottom:1px solid var(--line);align-items:start}
.t{color:var(--muted);font-size:11px;font-variant-numeric:tabular-nums;padding-top:2px}
.body{min-width:0}
.kind{display:inline-block;font-size:10px;letter-spacing:.06em;text-transform:uppercase;
color:var(--muted);border:1px solid var(--line);border-radius:4px;padding:1px 6px;margin-right:8px}
.model{background:var(--model-bg);border-left:3px solid var(--model);
padding:10px 12px;border-radius:0 6px 6px 0;margin-top:4px}
.model .who{color:var(--model);font-weight:600;font-size:11px;text-transform:uppercase;
letter-spacing:.06em;display:block;margin-bottom:4px}
.txt{white-space:pre-wrap;overflow-wrap:anywhere}
.meta{color:var(--muted);font-size:12px;margin-top:3px}
.f{margin-top:8px}
.f img{max-width:100%;width:420px;border:1px solid var(--line);border-radius:6px;display:block}
.rep{color:var(--muted);font-size:11px;font-style:italic}
.find{border-left:3px solid var(--line);padding:8px 12px;border-radius:0 6px 6px 0;margin-top:4px}
.sev-blocker{border-color:var(--blocker)} .sev-blocker .rule{color:var(--blocker)}
.sev-major{border-color:var(--major)} .sev-major .rule{color:var(--major)}
.sev-minor{border-color:var(--minor)} .sev-minor .rule{color:var(--minor)}
.sev-polish{border-color:var(--polish)} .sev-polish .rule{color:var(--polish)}
.rule{font-weight:600}
.note{color:var(--muted);font-size:12px;padding:6px 10px;background:var(--card);
border:1px solid var(--line);border-radius:6px;margin-top:16px}
.hid{display:none}
"""

_JS = """
(function(){
 var b=document.querySelectorAll('.bar button[data-filter]');
 function apply(f){
  document.querySelectorAll('.ev').forEach(function(e){
   e.classList.toggle('hid', f!=='all' && !e.matches('[data-group~="'+f+'"]'));
  });
  b.forEach(function(x){x.setAttribute('aria-pressed', String(x.dataset.filter===f));});
 }
 b.forEach(function(x){x.addEventListener('click',function(){apply(x.dataset.filter);});});
 apply('all');
})();
"""


def _esc(s: Any) -> str:
    return html.escape(str(s), quote=True)


def _groups(ev: JournalEvent) -> str:
    g = ["all", ev.kind]
    if ev.from_model:
        g.append("model")
    if ev.kind in (EventKind.FINDING, EventKind.CRASH):
        g.append("finding")
    if ev.frame and not ev.frame_repeat:
        g.append("frame")
    return " ".join(g)


def _render_event(ev: JournalEvent, t0: float) -> str:
    rel = max(ev.at - t0, 0.0)
    clock = f"{int(rel // 60):02d}:{int(rel % 60):02d}"
    parts = [f'<div class="ev" data-group="{_esc(_groups(ev))}">',
             f'<div class="t">{clock}</div><div class="body">']

    label = ev.kind.replace("_", " ")
    if ev.from_model:
        parts.append(f'<div class="model"><span class="who">model &middot; {_esc(label)}</span>'
                     f'<div class="txt">{_esc(ev.text)}</div></div>')
    elif ev.kind in (EventKind.FINDING, EventKind.CRASH):
        cls = _SEVERITY_CLASS.get(ev.severity, "sev-minor")
        rule = f'<span class="rule">{_esc(ev.rule or "finding")}</span> '
        parts.append(f'<div class="find {cls}">{rule}<span class="txt">{_esc(ev.text)}</span></div>')
    else:
        parts.append(f'<span class="kind">{_esc(label)}</span>'
                     f'<span class="txt">{_esc(ev.text)}</span>')

    bits = []
    if ev.screen:
        bits.append(_esc(ev.screen))
    if ev.step >= 0:
        bits.append(f"step {ev.step}")
    for k, v in sorted(ev.detail.items()):
        bits.append(f"{_esc(k)} {_esc(v)}")
    if bits:
        parts.append(f'<div class="meta">{" &middot; ".join(bits)}</div>')

    if ev.frame:
        if ev.frame_repeat:
            parts.append('<div class="meta rep">screen unchanged</div>')
        else:
            parts.append(
                f'<div class="f"><a href="{_esc(ev.frame)}" target="_blank">'
                f'<img src="{_esc(ev.frame)}" loading="lazy" decoding="async" '
                f'alt="{_esc(ev.text[:80])}"></a></div>')

    parts.append("</div></div>")
    return "".join(parts)


def render_html(events: Sequence[JournalEvent], *, title: str = "Active QA session",
                session_id: str = "", stats: dict[str, Any] | None = None) -> str:
    """Render the journal to a standalone page.

    Images are referenced, never inlined. A run of a few thousand frames as
    base64 would be a hundred-megabyte HTML file that no browser will open;
    ``loading="lazy"`` on referenced files renders instantly and fetches only
    what is scrolled to.
    """
    stats = stats or {}
    t0 = events[0].at if events else time.time()
    counts: dict[str, int] = {}
    for ev in events:
        counts[ev.kind] = counts.get(ev.kind, 0) + 1

    summary = [f"{len(events)} events"]
    if stats.get("frames_stored") is not None:
        summary.append(f"{stats['frames_stored']} frames "
                       f"({stats.get('megabytes_stored', 0)} MB)")
    if stats.get("duplicates_collapsed"):
        summary.append(f"{stats['duplicates_collapsed']} unchanged screens collapsed")
    if stats.get("compressed") is False:
        summary.append("uncompressed (Pillow unavailable)")

    filters = [("all", "Everything"), ("model", "Model only"),
               ("finding", "Findings"), ("state_new", "New screens"),
               ("action", "Actions"), ("frame", "With a frame")]
    buttons = "".join(
        f'<button data-filter="{k}" aria-pressed="false">{_esc(v)}'
        + (f' <span class="stats">{counts[k]}</span>' if k in counts else "")
        + "</button>"
        for k, v in filters)

    body = "".join(_render_event(ev, t0) for ev in events)

    note = ('<div class="note">Turns marked <strong>model</strong> are the '
            'language model speaking. Everything else was chosen by the '
            'exploration policy without a model call &mdash; that ratio is '
            'deliberate, and it is what lets a run last an hour.</div>')

    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{_esc(title)}</title><style>{_CSS}</style></head><body><div class=\"wrap\">"
        f"<h1>{_esc(title)}</h1>"
        f"<div class=\"sub\">{_esc(session_id)}{' &middot; ' if session_id else ''}"
        f"{_esc(' · '.join(summary))}</div>"
        f"<div class=\"bar\">{buttons}</div>"
        f"{body}{note}</div>"
        f"<script>{_JS}</script></body></html>"
    )
