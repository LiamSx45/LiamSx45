#!/usr/bin/env python3
"""Fetch language stats from all owned repos (incl. private) and write an SVG."""
import requests
import os
import sys

TOKEN = os.environ['ACCESS_TOKEN']
HEADERS = {
    'Authorization': f'Bearer {TOKEN}',
    'Accept': 'application/vnd.github.v3+json',
}

COLORS = {
    'Python':       '#3572A5',
    'JavaScript':   '#f1e05a',
    'TypeScript':   '#3178c6',
    'Swift':        '#F05138',
    'Java':         '#b07219',
    'Kotlin':       '#A97BFF',
    'Ruby':         '#701516',
    'Go':           '#00ADD8',
    'Rust':         '#dea584',
    'C++':          '#f34b7d',
    'C':            '#555555',
    'C#':           '#178600',
    'HTML':         '#e34c26',
    'CSS':          '#563d7c',
    'Shell':        '#89e051',
    'Dockerfile':   '#384d54',
    'PHP':          '#4F5D95',
    'Dart':         '#00B4AB',
    'Objective-C':  '#438eff',
    'SCSS':         '#c6538c',
    'Vue':          '#41b883',
}

def fetch_languages():
    langs = {}
    page = 1
    while True:
        r = requests.get(
            'https://api.github.com/user/repos',
            headers=HEADERS,
            params={'per_page': 100, 'page': page, 'type': 'owner'},
        )
        if r.status_code != 200:
            print(f'Error {r.status_code}: {r.text}', file=sys.stderr)
            sys.exit(1)
        repos = r.json()
        if not repos:
            break
        for repo in repos:
            if repo.get('fork'):
                continue
            lr = requests.get(repo['languages_url'], headers=HEADERS)
            if lr.status_code == 200:
                for lang, b in lr.json().items():
                    langs[lang] = langs.get(lang, 0) + b
        if len(repos) < 100:
            break
        page += 1
    return langs

def generate_svg(langs, limit=8):
    total = sum(langs.values())
    if total == 0:
        return None
    top = sorted(langs.items(), key=lambda x: x[1], reverse=True)[:limit]

    W = 320
    PAD = 20
    TITLE_H = 42
    ROW_H = 26
    BAR_H = 8
    BAR_MAX_W = W - PAD * 2 - 52
    height = PAD + TITLE_H + len(top) * ROW_H + PAD
    FF = "'Segoe UI',Ubuntu,sans-serif"
    top_bytes = top[0][1]

    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{height}">',
        f'  <rect width="{W}" height="{height}" rx="10" fill="#0d1117" stroke="#21262d" stroke-width="1"/>',
        f'  <text x="{PAD}" y="{PAD + 20}" fill="#e6edf3" font-size="14" font-weight="600" font-family="{FF}">Most Used Languages</text>',
        f'  <line x1="{PAD}" y1="{PAD + 28}" x2="{W - PAD}" y2="{PAD + 28}" stroke="#21262d" stroke-width="1"/>',
    ]

    for i, (lang, count) in enumerate(top):
        pct = count / total * 100
        bw = max(4.0, (count / top_bytes) * BAR_MAX_W)
        clr = COLORS.get(lang, '#8b949e')
        y = PAD + TITLE_H + i * ROW_H
        lines += [
            f'  <rect x="{PAD}" y="{y}" width="{bw:.1f}" height="{BAR_H}" rx="3" fill="{clr}" opacity="0.9"/>',
            f'  <text x="{PAD}" y="{y + BAR_H + 12}" fill="#c9d1d9" font-size="11" font-family="{FF}">{lang}</text>',
            f'  <text x="{W - PAD}" y="{y + BAR_H + 12}" fill="#8b949e" font-size="11" font-family="{FF}" text-anchor="end">{pct:.1f}%</text>',
        ]

    lines.append('</svg>')
    return '\n'.join(lines)

langs = fetch_languages()
print(f'Found {len(langs)} languages across owned repos')
for lang, b in sorted(langs.items(), key=lambda x: x[1], reverse=True)[:10]:
    print(f'  {lang}: {b:,} bytes')

svg = generate_svg(langs)
if not svg:
    print('No language data — nothing to commit', file=sys.stderr)
    sys.exit(1)

os.makedirs('generated', exist_ok=True)
with open('generated/languages.svg', 'w') as f:
    f.write(svg)
print('Wrote generated/languages.svg')
