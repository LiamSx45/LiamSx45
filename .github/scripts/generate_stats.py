#!/usr/bin/env python3
"""Generate custom GitHub stats SVGs (languages + overview) including private repos.

Outputs:
  generated/languages.svg  - top languages across all non-fork owned repos
                              (averaged per-repo proportions, not raw bytes)
  generated/stats.svg      - total stars, commits (all-time), PRs, issues, contributed-to
"""
import os
import sys
from datetime import datetime, timezone

import requests

TOKEN = os.environ['ACCESS_TOKEN']
HEADERS = {
    'Authorization': f'Bearer {TOKEN}',
    'Accept': 'application/vnd.github.v3+json',
}
GQL_URL = 'https://api.github.com/graphql'

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
    'Makefile':     '#427819',
    'Pawn':         '#dbb284',
}

FF = "'Segoe UI',Ubuntu,sans-serif"

# Two themes — keys match GitHub's dark/light readme rendering.
THEMES = {
    'dark': {
        'bg':     '#0d1117',
        'border': '#21262d',
        'title':  '#e6edf3',
        'text':   '#c9d1d9',
        'muted':  '#8b949e',
        'accent': '#58a6ff',
        'track':  '#21262d',
    },
    'light': {
        'bg':     '#ffffff',
        'border': '#d0d7de',
        'title':  '#1f2328',
        'text':   '#1f2328',
        'muted':  '#656d76',
        'accent': '#0969da',
        'track':  '#eaeef2',
    },
}


# ---------- API helpers ----------

def gql(query, variables=None):
    r = requests.post(GQL_URL, headers=HEADERS, json={'query': query, 'variables': variables or {}})
    if r.status_code != 200:
        print(f'GraphQL HTTP {r.status_code}: {r.text}', file=sys.stderr)
        sys.exit(1)
    data = r.json()
    if 'errors' in data:
        print(f'GraphQL errors: {data["errors"]}', file=sys.stderr)
        sys.exit(1)
    return data['data']


def fetch_owned_repos():
    """Return list of (full_name, languages_url, stargazers_count, fork) for owned repos (incl. private)."""
    repos = []
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
        batch = r.json()
        if not batch:
            break
        repos.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return repos


def fetch_language_proportions(repos):
    """Average per-repo language percentage across all non-fork repos.

    Each repo contributes equally regardless of size. Raw byte totals are
    misleading because one vendored library or generated artifact (e.g. a
    Makefile build directory, a C dependency, a pawn game-server binary)
    can dwarf hundreds of small repos and bury the languages actually used
    most often. Averaging proportions matches how liamsawyer.com reports
    language usage and reflects real usage patterns more honestly.
    """
    aggregate = {}
    repo_count = 0
    for repo in repos:
        if repo.get('fork'):
            continue
        lr = requests.get(repo['languages_url'], headers=HEADERS)
        if lr.status_code != 200:
            continue
        rep_langs = lr.json()
        total = sum(rep_langs.values())
        if total == 0:
            continue
        repo_count += 1
        for lang, b in rep_langs.items():
            aggregate[lang] = aggregate.get(lang, 0) + (b / total * 100)
    if repo_count == 0:
        return {}
    return {lang: pct / repo_count for lang, pct in aggregate.items()}


def fetch_user_stats():
    """All-time totals via GraphQL: commits, PRs, issues, repos contributed to, followers."""
    user_q = """
    query {
      viewer {
        login
        createdAt
        followers { totalCount }
      }
    }
    """
    viewer = gql(user_q)['viewer']
    login = viewer['login']
    created = datetime.fromisoformat(viewer['createdAt'].replace('Z', '+00:00'))
    now = datetime.now(timezone.utc)

    totals = {
        'commits': 0,
        'prs': 0,
        'issues': 0,
        'reviews': 0,
        'contributed_to': 0,
        'followers': viewer['followers']['totalCount'],
    }

    # contributionsCollection windows max 1 year — iterate per calendar year from createdAt.
    year = created.year
    while year <= now.year:
        start = datetime(year, 1, 1, tzinfo=timezone.utc)
        end = datetime(year, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
        if start < created:
            start = created
        if end > now:
            end = now

        q = """
        query($from: DateTime!, $to: DateTime!) {
          viewer {
            contributionsCollection(from: $from, to: $to) {
              totalCommitContributions
              restrictedContributionsCount
              totalPullRequestContributions
              totalIssueContributions
              totalPullRequestReviewContributions
              totalRepositoriesWithContributedCommits
            }
          }
        }
        """
        cc = gql(q, {'from': start.isoformat(), 'to': end.isoformat()})['viewer']['contributionsCollection']
        # restrictedContributionsCount covers contributions to private repos the viewer can't expose
        totals['commits'] += cc['totalCommitContributions'] + cc['restrictedContributionsCount']
        totals['prs'] += cc['totalPullRequestContributions']
        totals['issues'] += cc['totalIssueContributions']
        totals['reviews'] += cc['totalPullRequestReviewContributions']
        # contributed_to: take the max across years (avoids double-counting)
        totals['contributed_to'] = max(totals['contributed_to'], cc['totalRepositoriesWithContributedCommits'])
        year += 1

    totals['login'] = login
    return totals


def total_stars(repos):
    return sum(r.get('stargazers_count', 0) for r in repos if not r.get('fork'))


# ---------- SVG generators ----------

def svg_languages(langs, theme, limit=8):
    total = sum(langs.values())
    if total == 0:
        return None
    top = sorted(langs.items(), key=lambda x: x[1], reverse=True)[:limit]
    t = THEMES[theme]

    W = 360
    PAD = 20
    TITLE_H = 42
    ROW_H = 28
    BAR_H = 8
    BAR_MAX_W = W - PAD * 2 - 60
    height = PAD + TITLE_H + len(top) * ROW_H + PAD
    top_bytes = top[0][1]

    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{height}" viewBox="0 0 {W} {height}" role="img" aria-label="Most used languages">',
        f'  <rect width="{W}" height="{height}" rx="10" fill="{t["bg"]}" stroke="{t["border"]}" stroke-width="1"/>',
        f'  <text x="{PAD}" y="{PAD + 20}" fill="{t["title"]}" font-size="15" font-weight="600" font-family="{FF}">Most Used Languages</text>',
        f'  <line x1="{PAD}" y1="{PAD + 28}" x2="{W - PAD}" y2="{PAD + 28}" stroke="{t["border"]}" stroke-width="1"/>',
    ]

    for i, (lang, count) in enumerate(top):
        pct = count / total * 100
        bw = max(4.0, (count / top_bytes) * BAR_MAX_W)
        clr = COLORS.get(lang, t['muted'])
        y = PAD + TITLE_H + i * ROW_H
        lines += [
            f'  <rect x="{PAD}" y="{y}" width="{BAR_MAX_W:.1f}" height="{BAR_H}" rx="3" fill="{t["track"]}"/>',
            f'  <rect x="{PAD}" y="{y}" width="{bw:.1f}" height="{BAR_H}" rx="3" fill="{clr}"/>',
            f'  <text x="{PAD}" y="{y + BAR_H + 13}" fill="{t["text"]}" font-size="12" font-family="{FF}">{lang}</text>',
            f'  <text x="{W - PAD}" y="{y + BAR_H + 13}" fill="{t["muted"]}" font-size="12" font-family="{FF}" text-anchor="end">{pct:.1f}%</text>',
        ]

    lines.append('</svg>')
    return '\n'.join(lines)


def _fmt(n):
    if n >= 1000:
        return f'{n/1000:.1f}k'.replace('.0k', 'k')
    return str(n)


def svg_stats(stats, stars, theme):
    """A clean overview card: header + rows of metrics."""
    t = THEMES[theme]
    rows = [
        ('Total Stars Earned',          _fmt(stars),                  '★'),
        ('Total Commits (all-time)',    _fmt(stats['commits']),       '◆'),
        ('Total PRs',                   _fmt(stats['prs']),           '⤴'),
        ('Total PR Reviews',            _fmt(stats['reviews']),       '✓'),
        ('Total Issues',                _fmt(stats['issues']),        '◉'),
        ('Contributed to (last yr)',    _fmt(stats['contributed_to']),'❖'),
    ]

    W = 360
    PAD = 20
    TITLE_H = 42
    ROW_H = 28
    height = PAD + TITLE_H + len(rows) * ROW_H + PAD

    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{height}" viewBox="0 0 {W} {height}" role="img" aria-label="GitHub stats">',
        f'  <rect width="{W}" height="{height}" rx="10" fill="{t["bg"]}" stroke="{t["border"]}" stroke-width="1"/>',
        f'  <text x="{PAD}" y="{PAD + 20}" fill="{t["title"]}" font-size="15" font-weight="600" font-family="{FF}">{stats["login"]}\'s GitHub Stats</text>',
        f'  <line x1="{PAD}" y1="{PAD + 28}" x2="{W - PAD}" y2="{PAD + 28}" stroke="{t["border"]}" stroke-width="1"/>',
    ]

    for i, (label, value, icon) in enumerate(rows):
        y = PAD + TITLE_H + i * ROW_H + 14
        lines += [
            f'  <text x="{PAD}" y="{y}" fill="{t["accent"]}" font-size="13" font-family="{FF}">{icon}</text>',
            f'  <text x="{PAD + 22}" y="{y}" fill="{t["text"]}" font-size="12" font-family="{FF}">{label}</text>',
            f'  <text x="{W - PAD}" y="{y}" fill="{t["title"]}" font-size="13" font-weight="600" font-family="{FF}" text-anchor="end">{value}</text>',
        ]

    lines.append('</svg>')
    return '\n'.join(lines)


# ---------- main ----------

def main():
    print('Fetching owned repos…')
    repos = fetch_owned_repos()
    print(f'  {len(repos)} repos')

    print('Fetching per-repo language proportions…')
    langs = fetch_language_proportions(repos)
    print(f'  {len(langs)} languages across {sum(1 for r in repos if not r.get("fork"))} non-fork repos')

    print('Fetching user stats via GraphQL…')
    stats = fetch_user_stats()
    stars = total_stars(repos)
    print(f'  stars={stars} commits={stats["commits"]} prs={stats["prs"]} issues={stats["issues"]} reviews={stats["reviews"]} contributed_to={stats["contributed_to"]}')

    os.makedirs('generated', exist_ok=True)

    # Default filenames are the LIGHT theme (matches the rest of the site);
    # *-dark.svg variants are picked up by <picture> in the README for dark mode.
    artifacts = {
        'generated/languages.svg':       svg_languages(langs, 'light'),
        'generated/languages-dark.svg':  svg_languages(langs, 'dark'),
        'generated/stats.svg':           svg_stats(stats, stars, 'light'),
        'generated/stats-dark.svg':      svg_stats(stats, stars, 'dark'),
    }

    for path, body in artifacts.items():
        if body is None:
            print(f'Skipping {path} — no data', file=sys.stderr)
            continue
        with open(path, 'w') as f:
            f.write(body)
        print(f'Wrote {path}')


if __name__ == '__main__':
    main()
