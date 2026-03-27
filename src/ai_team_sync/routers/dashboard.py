"""Dashboard — who has what files open, right now."""

from __future__ import annotations

import html

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from ai_team_sync.presence import store

router = APIRouter(tags=["dashboard"])

COLORS = ["#f0883e", "#58a6ff", "#3fb950", "#bc8cff", "#f85149", "#d29922"]


def _esc(val: str | None) -> str:
    return html.escape(val or "")


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    presence = store.get_all()
    return _render(presence)


def _render(presence: list[dict]) -> str:
    dev_colors: dict[str, str] = {}
    ci = 0

    def color_for(dev: str) -> str:
        nonlocal ci
        if dev not in dev_colors:
            dev_colors[dev] = COLORS[ci % len(COLORS)]
            ci += 1
        return dev_colors[dev]

    cards = ""
    for p in presence:
        c = color_for(p["developer"])
        initials = "".join(w[0].upper() for w in p["developer"].split()[:2])
        files = "".join(
            f'<div class="file"><span class="badge" style="background:{c}">{initials}</span> {_esc(f)}</div>'
            for f in p["files"]
        )
        cards += f"""<div class="card" style="border-left-color:{c}">
          <div class="dev" style="color:{c}">{_esc(p["developer"])}</div>
          <div class="agent">{_esc(p["agent"])}</div>
          {files}
        </div>"""

    if not presence:
        cards = '<div class="empty">Nobody has files open right now.</div>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="5">
<title>team sync</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #0d1117; color: #c9d1d9; padding: 24px 20px; max-width: 640px; margin: 0 auto; }}
  h1 {{ font-size: 1.1rem; color: #8b949e; font-weight: 400; margin-bottom: 20px; }}
  h1 strong {{ color: #f0f6fc; }}
  .card {{ border-left: 3px solid #444; padding: 12px 14px; margin-bottom: 12px;
           background: #161b22; border-radius: 4px; }}
  .dev {{ font-weight: 600; font-size: 0.95rem; }}
  .agent {{ color: #8b949e; font-size: 0.78rem; margin-bottom: 8px; }}
  .file {{ padding: 3px 0; font-family: 'SF Mono', 'Fira Code', monospace; font-size: 0.82rem; }}
  .badge {{ display: inline-block; width: 22px; height: 16px; border-radius: 3px; text-align: center;
            font-size: 0.65rem; font-weight: 700; color: #fff; line-height: 16px; margin-right: 6px;
            font-family: -apple-system, sans-serif; }}
  .empty {{ color: #484f58; text-align: center; padding: 32px; font-style: italic; }}
  .install {{ background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 16px;
              margin-top: 24px; font-size: 0.82rem; color: #8b949e; }}
  .install code {{ background: #0d1117; padding: 2px 6px; border-radius: 3px; color: #79c0ff; }}
  .install strong {{ color: #f0f6fc; }}
</style>
</head>
<body>
<h1><strong>team sync</strong> &middot; {len(presence)} active</h1>
{cards}
<div class="install">
  <strong>VS Code extension</strong> &mdash; see colored files + badges right in your editor<br><br>
  <code>curl -o /tmp/ats.vsix http://YOUR_SERVER:8400/ext/ai-team-sync-0.2.0.vsix && code --install-extension /tmp/ats.vsix</code>
  <br><br>Auto-connects. No config needed.
</div>
</body>
</html>"""
