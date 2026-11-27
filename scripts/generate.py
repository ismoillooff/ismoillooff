#!/usr/bin/env python3
"""Generate the animated SVG cards used by the profile README.

Reads every repository the token owner owns (public and private), aggregates
languages, topics, commits and the contribution calendar, and renders a set of
theme-aware SVG cards into ``assets/``. Private repositories only ever appear as
aggregate numbers, or through the curated entries in ``profile.config.json``.

Usage:
    METRICS_TOKEN=<token> python scripts/generate.py

The token needs read access to the owner's repositories (classic PAT with
``repo`` scope, or a fine-grained token with Metadata + Contents read on all
repositories). Standard library only.

Set PROFILE_CACHE=/some/path.json to cache the API response while iterating on
the design. Keep that file outside the repository: it contains private data.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import sys
import urllib.request
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
CONFIG = json.loads((ROOT / "scripts" / "profile.config.json").read_text(encoding="utf-8"))

SANS = "-apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans',Helvetica,Arial,sans-serif"
MONO = "ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,'Liberation Mono',monospace"

THEMES = {
    "dark": {
        "card": "#0d1117", "panel": "#151b23", "chip": "#161b22", "border": "#30363d",
        "grid": "#21262d", "dot": "#262c36", "text": "#f0f6fc", "muted": "#9198a1",
        "faint": "#6e7681", "accent": "#2dd4bf", "amber": "#f5b544", "green": "#3fb950",
        "glow": 0.16,
    },
    "light": {
        "card": "#ffffff", "panel": "#f6f8fa", "chip": "#f6f8fa", "border": "#d1d9e0",
        "grid": "#eaeef2", "dot": "#dde3ea", "text": "#1f2328", "muted": "#59636e",
        "faint": "#818b98", "accent": "#0f766e", "amber": "#b45309", "green": "#1a7f37",
        "glow": 0.09,
    },
}

ICON_REPO = ("M2 2.5A2.5 2.5 0 0 1 4.5 0h8.75a.75.75 0 0 1 .75.75v12.5a.75.75 0 0 1-.75.75h-2.5a.75.75 0 0 1 0-1.5h1.75v-2h-8"
             "a1 1 0 0 0-.714 1.7.75.75 0 1 1-1.072 1.05A2.495 2.495 0 0 1 2 11.5Zm10.5-1h-8a1 1 0 0 0-1 1v6.708A2.486 2.486 0 0 1 "
             "4.5 9h8ZM5 12.25a.25.25 0 0 1 .25-.25h3.5a.25.25 0 0 1 .25.25v3.25a.25.25 0 0 1-.4.2l-1.45-1.087a.249.249 0 0 0-.3 0"
             "L5.4 15.7a.25.25 0 0 1-.4-.2Z")
ICON_LOCK = ("M4 4a4 4 0 0 1 8 0v2h.25c.966 0 1.75.784 1.75 1.75v5.5A1.75 1.75 0 0 1 12.25 15h-8.5A1.75 1.75 0 0 1 2 13.25v-5.5"
             "C2 6.784 2.784 6 3.75 6H4Zm8.25 3.5h-8.5a.25.25 0 0 0-.25.25v5.5c0 .138.112.25.25.25h8.5a.25.25 0 0 0 .25-.25v-5.5"
             "a.25.25 0 0 0-.25-.25ZM10.5 6V4a2.5 2.5 0 1 0-5 0v2Z")


# --------------------------------------------------------------------------- GitHub API

REPOS_QUERY = """
query($cursor: String) {
  viewer {
    login
    createdAt
    repositories(first: 50, after: $cursor, ownerAffiliations: OWNER,
                 orderBy: {field: PUSHED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        name isPrivate isFork description pushedAt createdAt stargazerCount homepageUrl
        primaryLanguage { name color }
        languages(first: 25, orderBy: {field: SIZE, direction: DESC}) { edges { size node { name color } } }
        repositoryTopics(first: 40) { nodes { topic { name } } }
        defaultBranchRef { target { ... on Commit { history { totalCount } } } }
      }
    }
  }
}"""

CONTRIB_QUERY = """
query {
  viewer {
    contributionsCollection {
      contributionCalendar { totalContributions weeks { contributionDays { date contributionCount } } }
    }
  }
}"""


def graphql(token: str, query: str, variables: dict | None = None) -> dict:
    request = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        headers={"Authorization": f"bearer {token}", "Content-Type": "application/json",
                 "User-Agent": "profile-readme-generator"},
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        payload = json.load(response)
    if payload.get("errors"):
        raise RuntimeError(payload["errors"])
    return payload["data"]


def fetch() -> dict:
    cache = os.environ.get("PROFILE_CACHE")
    if cache and Path(cache).exists():
        return json.loads(Path(cache).read_text(encoding="utf-8"))
    token = os.environ.get("METRICS_TOKEN") or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("Set METRICS_TOKEN (or GH_TOKEN) to a token that can read your repositories.")
    repos, cursor, viewer = [], None, None
    while True:
        viewer = graphql(token, REPOS_QUERY, {"cursor": cursor})["viewer"]
        page = viewer["repositories"]
        repos.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    calendar = graphql(token, CONTRIB_QUERY)["viewer"]["contributionsCollection"]["contributionCalendar"]
    data = {"login": viewer["login"], "createdAt": viewer["createdAt"], "repos": repos, "calendar": calendar}
    if cache:
        Path(cache).write_text(json.dumps(data), encoding="utf-8")
    return data


# --------------------------------------------------------------------------- analysis

def topics(repo: dict) -> list[str]:
    return [n["topic"]["name"] for n in (repo.get("repositoryTopics") or {}).get("nodes", [])]


def commits(repo: dict) -> int:
    ref = repo.get("defaultBranchRef") or {}
    return ((ref.get("target") or {}).get("history") or {}).get("totalCount", 0)


def code_bytes(repo: dict) -> int:
    return sum(e["size"] for e in repo["languages"]["edges"])


def parse_date(value: str) -> dt.date:
    return dt.date.fromisoformat(value[:10])


def fmt_date(day: dt.date, year: bool = True) -> str:
    return f"{day:%b} {day.day}, {day.year}" if year else f"{day:%b} {day.day}"


def primary_language(repos: list[dict]) -> tuple[str, str]:
    totals: dict[str, list] = {}
    for repo in repos:
        for edge in repo["languages"]["edges"]:
            entry = totals.setdefault(edge["node"]["name"], [0, edge["node"]["color"]])
            entry[0] += edge["size"]
    if not totals:
        return "Markdown", "#083fa1"
    name, (_, color) = max(totals.items(), key=lambda kv: kv[1][0])
    return name, color or "#8b949e"


def analyse(data: dict) -> dict:
    owned = [r for r in data["repos"] if not r["isFork"]]
    rules = CONFIG["exclude"]
    desc_rx = re.compile(rules["description_regex"])

    def excluded(repo: dict) -> bool:
        return (repo["name"] in rules["names"]
                or any(t in rules["topics"] for t in topics(repo))
                or bool(repo["description"] and desc_rx.search(repo["description"])))

    kept = [r for r in owned if not excluded(r)]

    families: dict[str, list[dict]] = {}
    for repo in kept:
        key = next((p for p in CONFIG["families"] if re.search(p, repo["name"])), repo["name"])
        families.setdefault(key, []).append(repo)
    products = list(families.values())

    # Languages: one representative repository per product so mirrors do not double count.
    lang_bytes: dict[str, int] = {}
    lang_color: dict[str, str] = {}
    for family in products:
        rep = max(family, key=code_bytes)
        for edge in rep["languages"]["edges"]:
            name = edge["node"]["name"]
            lang_bytes[name] = lang_bytes.get(name, 0) + edge["size"]
            lang_color[name] = edge["node"]["color"] or "#8b949e"
    total_bytes = sum(lang_bytes.values()) or 1
    ranked = sorted(lang_bytes.items(), key=lambda kv: -kv[1])
    languages = [{"name": n, "pct": 100 * b / total_bytes, "color": lang_color[n]} for n, b in ranked[:9]]
    rest = sum(b for _, b in ranked[9:])
    if rest:
        languages.append({"name": "Other", "pct": 100 * rest / total_bytes, "color": "#8b949e"})

    # Focus areas: topics of each product (union over its repositories).
    product_topics = [set(t for repo in family for t in topics(repo)) for family in products]
    focus = []
    for area in CONFIG["focus"]:
        wanted = set(area["topics"])
        focus.append({"label": area["label"], "count": sum(1 for ts in product_topics if ts & wanted)})
    focus.sort(key=lambda f: -f["count"])

    # Contribution calendar.
    days = [(parse_date(d["date"]), d["contributionCount"])
            for w in data["calendar"]["weeks"] for d in w["contributionDays"]]
    days.sort()
    today = days[-1][0]
    counts = dict(days)
    active = [d for d, c in days if c > 0]
    longest = run = 0
    for _, c in days:
        run = run + 1 if c > 0 else 0
        longest = max(longest, run)
    current, cursor = 0, today if counts.get(today, 0) > 0 else today - dt.timedelta(days=1)
    while counts.get(cursor, 0) > 0:
        current += 1
        cursor -= dt.timedelta(days=1)
    joined = parse_date(data["createdAt"])
    window_start = max(today - dt.timedelta(days=89), joined)
    window = [(d, c) for d, c in days if d >= window_start]
    peak_day, peak = max(window, key=lambda dc: dc[1])

    def card_meta(repos: list[dict], override: str | None = None) -> dict:
        lang, color = primary_language(repos)
        if override:  # e.g. a repo whose build output makes HTML look like the main language
            color = next((e["node"]["color"] for r in repos for e in r["languages"]["edges"]
                          if e["node"]["name"] == override), color)
            lang = override
        return {"commits": sum(commits(r) for r in repos), "repos": len(repos), "language": lang, "color": color,
                "pushed": max(parse_date(r["pushedAt"]) for r in repos)}

    by_name = {r["name"]: r for r in owned}
    featured = []
    for item in CONFIG["featured"]:
        repo = by_name.get(item["repo"])
        if repo and not repo["isPrivate"]:
            featured.append({**item, **card_meta([repo], item.get("language")), "url": f"https://github.com/{data['login']}/{repo['name']}"})

    private = []
    for item in CONFIG["private"]:
        matched = [r for r in kept if r["isPrivate"] and re.search(item["match"], r["name"])]
        if matched:
            private.append({**item, **card_meta(matched)})

    public = sum(1 for r in owned if not r["isPrivate"])
    return {
        "login": data["login"],
        "today": today,
        "repos": len(owned),
        "public": public,
        "private": len(owned) - public,
        "commits": sum(commits(r) for r in owned),
        "products": len(products),
        "live": len(CONFIG["live"]),
        "language_count": len(lang_bytes),
        "languages": languages,
        "focus": focus,
        "contributions": data["calendar"]["totalContributions"],
        "active_days": len(active),
        "first_active": active[0] if active else today,
        "longest": longest,
        "current": current,
        "window": window,
        "window_total": sum(c for _, c in window),
        "peak": peak,
        "peak_day": peak_day,
        "featured": featured,
        "private_cards": private,
    }


# --------------------------------------------------------------------------- SVG helpers

def esc(value) -> str:
    return escape(str(value), {'"': "&quot;"})


def text_width(text: str, size: float, mono: bool = False, bold: bool = False) -> float:
    if mono:
        return len(text) * size * 0.6
    width = 0.0
    for ch in text:
        if ch in "il.,:;'|!·":
            width += 0.27
        elif ch in "fjrt()[]/ -":
            width += 0.34
        elif ch in "mwMW@":
            width += 0.86
        elif ch.isupper():
            width += 0.66
        elif ch.isdigit():
            width += 0.56
        else:
            width += 0.54
    return width * size * (1.07 if bold else 1.0)


def wrap(text: str, size: float, max_width: float, max_lines: int) -> list[str]:
    lines, current = [], ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if text_width(trial, size) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while text_width(last + "…", size) > max_width and " " in last:
            last = last.rsplit(" ", 1)[0]
        lines[-1] = last.rstrip(",;:—- ") + "…"
    return lines


def css(extra: str = "") -> str:
    return (
        ".sans{font-family:" + SANS + "}.mono{font-family:" + MONO + "}"
        ".fade{animation:fadeUp .8s cubic-bezier(.2,.7,.2,1) both}"
        ".grow{transform-box:fill-box;transform-origin:left center;animation:grow 1.2s cubic-bezier(.2,.7,.2,1) both}"
        ".rise{transform-box:fill-box;transform-origin:center bottom;animation:rise .9s cubic-bezier(.2,.7,.2,1) both}"
        ".blink{animation:blink 1.1s steps(1) infinite}"
        ".pulse{transform-box:fill-box;transform-origin:center;animation:pulse 2.4s ease-out infinite}"
        "@keyframes fadeUp{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}"
        "@keyframes grow{from{transform:scaleX(0)}to{transform:scaleX(1)}}"
        "@keyframes rise{from{transform:scaleY(0)}to{transform:scaleY(1)}}"
        "@keyframes blink{50%{opacity:0}}"
        "@keyframes pulse{0%{opacity:.6;transform:scale(1)}75%,100%{opacity:0;transform:scale(2.8)}}"
        + extra +
        "@media (prefers-reduced-motion:reduce){*{animation:none!important}}"
    )


def document(width: int, height: int, title: str, desc: str, body: str, extra_css: str = "") -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'role="img" aria-labelledby="title desc">\n'
        f'<title id="title">{esc(title)}</title>\n<desc id="desc">{esc(desc)}</desc>\n'
        f'<style>{css(extra_css)}</style>\n{body}\n</svg>\n'
    )


def frame(t: dict, width: int, height: int, radius: int = 14) -> str:
    return (f'<rect x=".5" y=".5" width="{width - 1}" height="{height - 1}" rx="{radius}" '
            f'fill="{t["card"]}" stroke="{t["border"]}"/>')


def eyebrow(t: dict, x: float, y: float, label: str, sub: str | None = None,
            right: str | None = None, width: int | None = None) -> str:
    out = (f'<text x="{x}" y="{y}" class="mono" font-size="12" font-weight="600" letter-spacing="2.2" '
           f'fill="{t["accent"]}">{esc(label)}</text>')
    if sub:
        out += f'<text x="{x}" y="{y + 23}" class="sans" font-size="13.5" fill="{t["muted"]}">{esc(sub)}</text>'
    if right and width:
        out += (f'<text x="{width - x}" y="{y}" text-anchor="end" class="mono" font-size="11.5" '
                f'fill="{t["faint"]}">{esc(right)}</text>')
    return out


def chips(t: dict, x: float, y: float, items: list[str], max_width: float, size: float = 11.5) -> str:
    out, cx = [], x
    for item in items:
        w = text_width(item, size, mono=True) + 20
        if cx + w > x + max_width:
            break
        out.append(f'<rect x="{cx:.1f}" y="{y}" width="{w:.1f}" height="22" rx="11" fill="{t["chip"]}" '
                   f'stroke="{t["border"]}"/><text x="{cx + w / 2:.1f}" y="{y + 15}" text-anchor="middle" '
                   f'class="mono" font-size="{size}" fill="{t["muted"]}">{esc(item)}</text>')
        cx += w + 8
    return "".join(out)


def delay(seconds: float) -> str:
    return f' style="animation-delay:{seconds:.2f}s"'


# --------------------------------------------------------------------------- cards
# Canvas sizes match how GitHub displays them: wide cards ~830 px, half cards ~410 px,
# so the SVGs render close to 1:1 and the text stays readable.

def hero(t: dict, a: dict) -> str:
    W, H = 880, 330
    phrases = CONFIG["phrases"]
    cw, x0, ty = 10.8, 62, 186
    slot = 5.0
    period = slot * len(phrases)

    clips, texts, cursor_events = [], [], []
    for i, phrase in enumerate(phrases):
        n, start = len(phrase), i * slot
        events = [(0.0, 0.0)] if i else []
        events += [(start + k * 0.055, k * cw) for k in range(n + 1)]
        hold_end = start + slot - 0.9
        events += [(hold_end + k * 0.022, (n - k) * cw) for k in range(1, n + 1)]
        cursor_events += events[1:] if i else events
        values = ";".join(f"{w:.1f}" for _, w in events)
        key_times = ";".join(f"{tm / period:.4f}" for tm, _ in events)
        initial = n * cw if i == 0 else 0
        clips.append(
            f'<clipPath id="ph{i}"><rect x="{x0}" y="{ty - 19}" height="26" width="{initial:.1f}">'
            f'<animate attributeName="width" dur="{period:g}s" repeatCount="indefinite" calcMode="discrete" '
            f'values="{values}" keyTimes="{key_times}"/></rect></clipPath>')
        texts.append(
            f'<text x="{x0}" y="{ty}" class="mono" font-size="18" fill="{t["text"]}" clip-path="url(#ph{i})" '
            f'textLength="{n * cw:.1f}" lengthAdjust="spacing">{esc(phrase)}</text>')
    cursor_events.sort()
    cursor_values = ";".join(f"{x0 + w + 3:.1f}" for _, w in cursor_events)
    cursor_times = ";".join(f"{tm / period:.4f}" for tm, _ in cursor_events)
    first_width = len(phrases[0]) * cw

    left, col_w = 40, 476
    tagline = wrap(CONFIG["tagline"], 14.5, col_w, 2)
    tag_svg = "".join(
        f'<text x="{left}" y="{226 + k * 21}" class="sans" font-size="14.5" fill="{t["muted"]}">{esc(line)}</text>'
        for k, line in enumerate(tagline))
    chip_y = 226 + (len(tagline) - 1) * 21 + 22
    role_svg, cx = [], float(left)
    for role in CONFIG["roles"]:
        w = text_width(role, 11) + 20
        if cx + w > left + 506:  # stop short of the terminal window
            cx, chip_y = float(left), chip_y + 32
        role_svg.append(
            f'<rect x="{cx:.1f}" y="{chip_y}" width="{w:.1f}" height="24" rx="12" fill="{t["chip"]}" stroke="{t["border"]}"/>'
            f'<text x="{cx + w / 2:.1f}" y="{chip_y + 16}" text-anchor="middle" class="sans" font-size="11" '
            f'fill="{t["muted"]}">{esc(role)}</text>')
        cx += w + 6

    tx, ty0, tw, th = 556, 40, 292, 250
    lines = [
        ("cmd", "whoami"),
        ("out", "hayotbek · full-stack engineer"),
        ("cmd", "gh repo list --summary"),
        ("out", f"{a['repos']} repos · {a['commits']:,} commits"),
        ("out", f"{a['products']} products · {a['live']} live"),
        ("cmd", "cat stack.txt"),
        ("out", CONFIG["terminal_stack"]),
        ("cmd", ""),
    ]
    term = []
    for i, (kind, value) in enumerate(lines):
        y = ty0 + 60 + i * 25
        if kind == "cmd":
            content = f'<tspan fill="{t["accent"]}">$</tspan><tspan fill="{t["text"]}" dx="7">{esc(value)}</tspan>'
        else:
            content = f'<tspan fill="{t["muted"]}">{esc(value)}</tspan>'
        term.append(f'<g class="fade"{delay(0.7 + i * 0.3)}><text x="{tx + 18}" y="{y}" class="mono" '
                    f'font-size="12.5">{content}</text></g>')
    last_y = ty0 + 60 + (len(lines) - 1) * 25
    term.append(f'<g class="fade"{delay(0.7 + (len(lines) - 1) * 0.3)}><rect class="blink" x="{tx + 34}" '
                f'y="{last_y - 12}" width="8" height="15" fill="{t["accent"]}"/></g>')

    body = f'''<defs>
<pattern id="dots" width="22" height="22" patternUnits="userSpaceOnUse"><circle cx="2" cy="2" r="1" fill="{t["dot"]}"/></pattern>
<linearGradient id="dotfade" x1="0" x2="1"><stop offset="0" stop-color="#fff" stop-opacity=".08"/><stop offset=".6" stop-color="#fff" stop-opacity=".8"/><stop offset="1" stop-color="#fff"/></linearGradient>
<mask id="dotmask"><rect width="{W}" height="{H}" fill="url(#dotfade)"/></mask>
<radialGradient id="glow"><stop offset="0" stop-color="{t["accent"]}" stop-opacity="{t["glow"]}"/><stop offset="1" stop-color="{t["accent"]}" stop-opacity="0"/></radialGradient>
<linearGradient id="beam" x1="0" x2="1"><stop offset="0" stop-color="{t["accent"]}" stop-opacity="0"/><stop offset=".5" stop-color="{t["accent"]}"/><stop offset="1" stop-color="{t["accent"]}" stop-opacity="0"/></linearGradient>
<clipPath id="frame"><rect width="{W}" height="{H}" rx="16"/></clipPath>
{"".join(clips)}
</defs>
<g clip-path="url(#frame)">
<rect width="{W}" height="{H}" fill="{t["card"]}"/>
<rect width="{W}" height="{H}" fill="url(#dots)" mask="url(#dotmask)"/>
<circle cx="{tx + tw / 2}" cy="{ty0 + th / 2}" r="280" fill="url(#glow)"/>
<rect class="beam" x="-260" y="0" width="260" height="2" fill="url(#beam)"/>
</g>
<rect x=".5" y=".5" width="{W - 1}" height="{H - 1}" rx="16" fill="none" stroke="{t["border"]}"/>
<g class="fade"><text x="{left}" y="70" class="mono" font-size="11.5" font-weight="600" letter-spacing="2" fill="{t["accent"]}">{esc(CONFIG["eyebrow"])}</text></g>
<g class="fade"{delay(0.12)}><text x="{left - 2}" y="126" class="sans" font-size="46" font-weight="700" letter-spacing="-.8" fill="{t["text"]}">{esc(CONFIG["name"])}</text></g>
<rect class="grow" x="{left + 1}" y="143" width="56" height="3" rx="1.5" fill="{t["accent"]}" style="animation-delay:.5s"/>
<g class="fade"{delay(0.3)}>
<text x="{left}" y="{ty}" class="mono" font-size="18" fill="{t["amber"]}">&gt;</text>
{"".join(texts)}
<rect class="blink" x="{x0 + first_width + 3:.1f}" y="{ty - 15}" width="9.5" height="18" fill="{t["accent"]}" opacity=".9"><animate attributeName="x" dur="{period:g}s" repeatCount="indefinite" calcMode="discrete" values="{cursor_values}" keyTimes="{cursor_times}"/></rect>
</g>
<g class="fade"{delay(0.45)}>{tag_svg}</g>
<g class="fade"{delay(0.6)}>{"".join(role_svg)}</g>
<g class="fade"{delay(0.4)}>
<rect x="{tx}" y="{ty0}" width="{tw}" height="{th}" rx="11" fill="{t["panel"]}" stroke="{t["border"]}"/>
<path d="M{tx} {ty0 + 32}H{tx + tw}" stroke="{t["border"]}"/>
<circle cx="{tx + 18}" cy="{ty0 + 16}" r="5" fill="#ff5f57"/><circle cx="{tx + 34}" cy="{ty0 + 16}" r="5" fill="#febc2e"/><circle cx="{tx + 50}" cy="{ty0 + 16}" r="5" fill="#28c840"/>
<text x="{tx + tw / 2 + 20}" y="{ty0 + 20}" text-anchor="middle" class="mono" font-size="11" fill="{t["faint"]}">{esc(a["login"])}@github: ~</text>
</g>
{"".join(term)}'''
    extra = ".beam{animation:sweep 7s linear infinite}@keyframes sweep{from{transform:translateX(0)}to{transform:translateX(1140px)}}"
    return document(W, H, f'{CONFIG["name"]} — Full-Stack Software Engineer',
                    "Animated profile banner: " + "; ".join(phrases), body, extra)


def stats_card(t: dict, a: dict) -> str:
    W, H = 880, 290
    tiles = [
        ("REPOSITORIES", f"{a['repos']}", f"{a['public']} public · {a['private']} private", "accent"),
        ("COMMITS", f"{a['commits']:,}", "across all repositories", "accent"),
        ("CONTRIBUTIONS", f"{a['contributions']:,}", "in the last 12 months", "accent"),
        ("PRODUCTS", f"{a['products']}", "distinct systems shipped", "accent"),
        ("LIVE IN PRODUCTION", f"{a['live']}", "sites & bots online", "green"),
        ("LANGUAGES", f"{a['language_count']}", f"top: {a['languages'][0]['name']}", "accent"),
        ("ACTIVE DAYS", f"{a['active_days']}", f"since {fmt_date(a['first_active'])}", "accent"),
        ("LONGEST STREAK", f"{a['longest']} days", f"current: {a['current']} days", "amber"),
    ]
    left, gap, th = 28, 12, 94
    tw = (W - 2 * left - 3 * gap) / 4
    out = [frame(t, W, H),
           eyebrow(t, left, 42, "GITHUB AT A GLANCE",
                   right=f"public + private · updated {fmt_date(a['today'])}", width=W)]
    for i, (label, value, sub, tone) in enumerate(tiles):
        x = left + (i % 4) * (tw + gap)
        y = 64 + (i // 4) * (th + gap)
        color = t[tone]
        dot = f'<circle cx="{x + 20:.1f}" cy="{y + 22}" r="3.5" fill="{color}"/>'
        if tone == "green":
            dot += f'<circle class="pulse" cx="{x + 20:.1f}" cy="{y + 22}" r="3.5" fill="{color}"/>'
        out.append(
            f'<g class="fade"{delay(0.08 * i)}>'
            f'<rect x="{x:.1f}" y="{y}" width="{tw:.1f}" height="{th}" rx="11" fill="{t["panel"]}" stroke="{t["border"]}"/>'
            f'{dot}<text x="{x + 31:.1f}" y="{y + 26}" class="mono" font-size="10.5" letter-spacing="1.4" fill="{t["faint"]}">{esc(label)}</text>'
            f'<text x="{x + 19:.1f}" y="{y + 62}" class="sans" font-size="28" font-weight="700" fill="{t["text"]}">{esc(value)}</text>'
            f'<text x="{x + 20:.1f}" y="{y + 82}" class="sans" font-size="12" fill="{t["muted"]}">{esc(sub)}</text>'
            f'</g>')
    return document(W, H, "GitHub at a glance",
                    f"{a['repos']} repositories, {a['commits']} commits, {a['contributions']} contributions, "
                    f"{a['products']} products, {a['live']} live", "".join(out))


def languages_card(t: dict, a: dict) -> str:
    W, H = 440, 296
    langs = a["languages"]
    out = [frame(t, W, H), eyebrow(t, 24, 40, "LANGUAGES",
                                   f"share of code across {a['products']} products · public + private")]
    bar_x, bar_w = 24, W - 48
    segs, cx = [], float(bar_x)
    for lang in langs:
        w = bar_w * lang["pct"] / 100
        segs.append(f'<rect x="{cx:.2f}" y="80" width="{max(w, 0.5):.2f}" height="10" fill="{lang["color"]}"/>')
        cx += w
    out.append(f'<defs><clipPath id="bar"><rect x="{bar_x}" y="80" width="{bar_w}" height="10" rx="5"/></clipPath></defs>'
               f'<g clip-path="url(#bar)"><g class="grow">{"".join(segs)}</g></g>')
    col_w = (W - 48 - 24) / 2
    top = max(l["pct"] for l in langs)
    for i, lang in enumerate(langs):
        col, row = i // 5, i % 5
        x = 24 + col * (col_w + 24)
        y = 124 + row * 35
        fill_w = max(col_w * lang["pct"] / top, 2)
        out.append(
            f'<g class="fade"{delay(0.25 + 0.06 * i)}>'
            f'<circle cx="{x + 5}" cy="{y - 4.5}" r="4.5" fill="{lang["color"]}"/>'
            f'<text x="{x + 16}" y="{y}" class="sans" font-size="13" fill="{t["text"]}">{esc(lang["name"])}</text>'
            f'<text x="{x + col_w:.1f}" y="{y}" text-anchor="end" class="mono" font-size="11.5" fill="{t["muted"]}">{lang["pct"]:.1f}%</text>'
            f'<rect x="{x}" y="{y + 8}" width="{col_w:.1f}" height="4" rx="2" fill="{t["grid"]}"/>'
            f'</g><rect class="grow" x="{x}" y="{y + 8}" width="{fill_w:.1f}" height="4" rx="2" fill="{lang["color"]}"'
            f'{delay(0.35 + 0.06 * i)}/>')
    return document(W, H, "Languages",
                    ", ".join(f"{l['name']} {l['pct']:.1f}%" for l in langs), "".join(out))


def focus_card(t: dict, a: dict) -> str:
    W, H = 440, 296
    focus = a["focus"]
    out = [frame(t, W, H), eyebrow(t, 24, 40, "FOCUS AREAS",
                                   f"what the {a['products']} products are about · topic analysis")]
    top = max(f["count"] for f in focus) or 1
    track_x, track_w = 204, 176
    for i, area in enumerate(focus):
        y = 100 + i * 25
        w = max(track_w * area["count"] / top, 3)
        opacity = 1 - 0.07 * i
        out.append(
            f'<g class="fade"{delay(0.1 + 0.06 * i)}>'
            f'<text x="24" y="{y}" class="sans" font-size="12.5" fill="{t["text"]}">{esc(area["label"])}</text>'
            f'<rect x="{track_x}" y="{y - 8}" width="{track_w}" height="7" rx="3.5" fill="{t["grid"]}"/>'
            f'<text x="{W - 24}" y="{y}" text-anchor="end" class="mono" font-size="12" fill="{t["muted"]}">{area["count"]}</text>'
            f'</g><rect class="grow" x="{track_x}" y="{y - 8}" width="{w:.1f}" height="7" rx="3.5" fill="{t["accent"]}" '
            f'fill-opacity="{opacity:.2f}"{delay(0.2 + 0.06 * i)}/>')
    return document(W, H, "Focus areas", ", ".join(f"{f['label']}: {f['count']}" for f in focus), "".join(out))


def activity_card(t: dict, a: dict) -> str:
    W, H = 880, 268
    window = a["window"]
    n = len(window)
    left, right, top, bottom = 56, W - 28, 94, 226
    peak = max(a["peak"], 1)
    step = 10 if peak <= 40 else 25 if peak <= 150 else 50
    y_max = math.ceil(peak / step) * step
    out = [frame(t, W, H), eyebrow(
        t, 28, 42, "CONTRIBUTION ACTIVITY",
        f"{a['window_total']:,} contributions in the last {n} days · peak {a['peak']} on {fmt_date(a['peak_day'], False)}",
        right=f"active {a['active_days']} days · longest streak {a['longest']} · current {a['current']}", width=W)]
    for k in range(0, y_max + 1, step):
        y = bottom - (bottom - top) * k / y_max
        dash = "" if k == 0 else ' stroke-dasharray="3 4"'
        out.append(f'<path d="M{left} {y:.1f}H{right}" stroke="{t["grid"]}"{dash}/>'
                   f'<text x="{left - 10}" y="{y + 4:.1f}" text-anchor="end" class="mono" font-size="10.5" fill="{t["faint"]}">{k}</text>')
    slot = (right - left) / n
    bar_w = max(2.0, slot * 0.62)
    for i, (day, count) in enumerate(window):
        x = left + i * slot + (slot - bar_w) / 2
        if count:
            h = max(3.0, (bottom - top) * count / y_max)
            alpha = 0.45 + 0.55 * math.sqrt(count / peak)
            out.append(f'<rect class="rise" x="{x:.1f}" y="{bottom - h:.1f}" width="{bar_w:.1f}" height="{h:.1f}" rx="2" '
                       f'fill="{t["accent"]}" fill-opacity="{alpha:.2f}"{delay(0.2 + i * 0.012)}/>')
        else:
            out.append(f'<rect x="{x:.1f}" y="{bottom - 2}" width="{bar_w:.1f}" height="2" rx="1" fill="{t["grid"]}"/>')
        if day.weekday() == 0 or i == 0:
            out.append(f'<text x="{x + bar_w / 2:.1f}" y="{bottom + 21}" text-anchor="middle" class="mono" font-size="10.5" '
                       f'fill="{t["faint"]}">{esc(fmt_date(day, False))}</text>')
        if day == a["peak_day"]:
            h = (bottom - top) * count / y_max
            cx = x + bar_w / 2
            out.append(f'<g class="fade"{delay(1.2)}><circle cx="{cx:.1f}" cy="{bottom - h - 9:.1f}" r="3.2" fill="{t["amber"]}"/>'
                       f'<text x="{cx + 7:.1f}" y="{bottom - h - 5:.1f}" class="mono" font-size="11" font-weight="600" '
                       f'fill="{t["amber"]}">{count}</text></g>')
    return document(W, H, "Contribution activity",
                    f"{a['window_total']} contributions in the last {n} days, peak {a['peak']}", "".join(out))


def project_card(t: dict, item: dict, private: bool) -> str:
    W, H = 440, 192
    out = [frame(t, W, H, radius=12)]
    icon = ICON_LOCK if private else ICON_REPO
    out.append(f'<path transform="translate(20 22)" d="{icon}" fill="{t["muted"]}"/>')
    out.append(f'<text x="44" y="35.5" class="sans" font-size="16" font-weight="700" fill="{t["accent"]}">{esc(item["title"])}</text>')

    if item.get("live"):
        label, tone = "LIVE", "green"
    elif private:
        label, tone = "PRIVATE", "amber"
    else:
        label, tone = item.get("badge", "OPEN SOURCE"), "muted"
    pill_w = len(label) * (10 * 0.6 + 1.2) + (32 if tone == "green" else 20)
    px = W - 20 - pill_w
    pill = f'<rect x="{px:.1f}" y="18" width="{pill_w:.1f}" height="22" rx="11" fill="{t["chip"]}" stroke="{t["border"]}"/>'
    text_x = px + 10
    if tone == "green":
        pill += (f'<circle cx="{px + 13:.1f}" cy="29" r="3.2" fill="{t["green"]}"/>'
                 f'<circle class="pulse" cx="{px + 13:.1f}" cy="29" r="3.2" fill="{t["green"]}"/>')
        text_x = px + 22
    pill += (f'<text x="{text_x:.1f}" y="32.5" class="mono" font-size="10" font-weight="600" letter-spacing="1.2" '
             f'fill="{t[tone]}">{esc(label)}</text>')
    out.append(pill)

    for k, line in enumerate(wrap(item["blurb"], 12.5, W - 40, 3)):
        out.append(f'<text x="20" y="{66 + k * 19}" class="sans" font-size="12.5" fill="{t["muted"]}">{esc(line)}</text>')
    out.append(chips(t, 20, 116, item["tags"], W - 40, size=10.5))
    out.append(f'<path d="M20 152H{W - 20}" stroke="{t["grid"]}"/>')

    tail = f'{item["repos"]} repos' if private and item["repos"] > 1 else fmt_date(item["pushed"], False)
    out.append(f'<circle cx="25" cy="170" r="5" fill="{item["color"]}"/>'
               f'<text x="36" y="174.5" class="sans" font-size="12" fill="{t["muted"]}">{esc(item["language"])}'
               f'<tspan fill="{t["faint"]}" dx="8">·</tspan><tspan dx="8">{item["commits"]:,} '
               f'{"commit" if item["commits"] == 1 else "commits"}</tspan>'
               f'<tspan fill="{t["faint"]}" dx="8">·</tspan><tspan dx="8">{esc(tail)}</tspan></text>')
    if item.get("live"):
        cta, tone = f'↗ {item["live"]}', "accent"
    elif item.get("link"):
        cta, tone = "↓ download (Windows)", "accent"
    elif private:
        cta, tone = "source on request", "faint"
    else:
        cta, tone = "view source →", "accent"
    out.append(f'<text x="{W - 20}" y="174.5" text-anchor="end" class="mono" font-size="11.5" fill="{t[tone]}">{esc(cta)}</text>')
    return document(W, H, item["title"], item["blurb"], f'<g class="fade">{"".join(out)}</g>')


def footer(t: dict, a: dict) -> str:
    W, H = 880, 108

    def wave(amplitude: float, length: float, y: float) -> str:
        parts, x = [f"M{-length} {y}"], -length
        while x < W + length:
            parts.append(f"q{length / 4} {-amplitude} {length / 2} 0t{length / 2} 0")
            x += length
        return "".join(parts)

    body = (f'<g class="wave1"><path d="{wave(6, 220, 88)}" fill="none" stroke="{t["accent"]}" stroke-opacity=".55" stroke-width="1.5"/></g>'
            f'<g class="wave2"><path d="{wave(4, 160, 94)}" fill="none" stroke="{t["amber"]}" stroke-opacity=".35" stroke-width="1.2"/></g>'
            f'<text x="{W / 2}" y="36" text-anchor="middle" class="sans" font-size="15.5" fill="{t["text"]}">'
            f'Thanks for stopping by — let’s build something that works.</text>'
            f'<text x="{W / 2}" y="58" text-anchor="middle" class="mono" font-size="11" fill="{t["faint"]}">'
            f'analytics regenerate daily from the GitHub API · updated {esc(fmt_date(a["today"]))}</text>')
    extra = (".wave1{animation:w1 7s linear infinite}.wave2{animation:w2 5s linear infinite}"
             "@keyframes w1{to{transform:translateX(220px)}}@keyframes w2{to{transform:translateX(160px)}}")
    return document(W, H, "Thanks for visiting", "Animated footer", body, extra)


# --------------------------------------------------------------------------- main

def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def main() -> None:
    a = analyse(fetch())
    for theme, t in THEMES.items():
        write(ASSETS / f"hero-{theme}.svg", hero(t, a))
        write(ASSETS / f"stats-{theme}.svg", stats_card(t, a))
        write(ASSETS / f"languages-{theme}.svg", languages_card(t, a))
        write(ASSETS / f"focus-{theme}.svg", focus_card(t, a))
        write(ASSETS / f"activity-{theme}.svg", activity_card(t, a))
        write(ASSETS / f"footer-{theme}.svg", footer(t, a))
        for item in a["featured"]:
            write(ASSETS / "cards" / f"{slugify(item['repo'])}-{theme}.svg", project_card(t, item, private=False))
        for item in a["private_cards"]:
            write(ASSETS / "cards" / f"private-{slugify(item['title'])}-{theme}.svg", project_card(t, item, private=True))

    summary = {key: a[key] for key in ("repos", "public", "private", "commits", "products", "live", "language_count",
                                       "contributions", "active_days", "longest", "current", "window_total", "peak")}
    summary.update(updated=a["today"].isoformat(), languages=a["languages"], focus=a["focus"])
    write(ASSETS / "stats.json", json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
