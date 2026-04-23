#!/usr/bin/env python3
"""Generate a text-based GitHub stats block for the profile README.

Writes a block of markdown between the markers:
    <!-- GH_STATS:START -->  ...generated content...  <!-- GH_STATS:END -->

Text rendering was chosen over SVG so the block:
  - auto-themes with the viewer's GitHub theme (no <picture> hacks)
  - is copy-pasteable and information-dense
  - doesn't depend on raw.githubusercontent.com caching

All stats include private-repo activity via GraphQL's
`restrictedContributionsCount` and per-repo language proportions.
"""
import os
import re
import sys
from datetime import datetime, timezone

import requests

TOKEN = os.environ['ACCESS_TOKEN']
HEADERS = {
    'Authorization': f'Bearer {TOKEN}',
    'Accept': 'application/vnd.github.v3+json',
}
GQL_URL = 'https://api.github.com/graphql'

README_PATH = 'README.md'
MARKER_START = '<!-- GH_STATS:START -->'
MARKER_END = '<!-- GH_STATS:END -->'

BAR_WIDTH = 25


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
    Makefile build directory, a C dependency, a Pawn game-server binary)
    can dwarf hundreds of small repos and bury the languages actually used
    most often.
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
    """All-time totals + this-year activity metrics via GraphQL."""
    user_q = """
    query {
      viewer {
        login
        createdAt
        followers { totalCount }
        following { totalCount }
      }
    }
    """
    viewer = gql(user_q)['viewer']
    login = viewer['login']
    created = datetime.fromisoformat(viewer['createdAt'].replace('Z', '+00:00'))
    now = datetime.now(timezone.utc)

    totals = {
        'login':          login,
        'created_at':     created,
        'followers':      viewer['followers']['totalCount'],
        'following':      viewer['following']['totalCount'],
        'commits':        0,
        'prs':            0,
        'issues':         0,
        'reviews':        0,
        'contributed_to': 0,
    }

    # contributionsCollection windows max 1 year — iterate per calendar year.
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
        totals['commits'] += cc['totalCommitContributions'] + cc['restrictedContributionsCount']
        totals['prs']     += cc['totalPullRequestContributions']
        totals['issues']  += cc['totalIssueContributions']
        totals['reviews'] += cc['totalPullRequestReviewContributions']
        totals['contributed_to'] = max(totals['contributed_to'], cc['totalRepositoriesWithContributedCommits'])
        year += 1

    # Day-level contribution calendar for this year → active days + streaks.
    start_of_year = datetime(now.year, 1, 1, tzinfo=timezone.utc)
    cal_q = """
    query($from: DateTime!, $to: DateTime!) {
      viewer {
        contributionsCollection(from: $from, to: $to) {
          contributionCalendar {
            totalContributions
            weeks { contributionDays { date contributionCount } }
          }
        }
      }
    }
    """
    cal = gql(cal_q, {'from': start_of_year.isoformat(), 'to': now.isoformat()})['viewer']['contributionsCollection']['contributionCalendar']
    days = [(d['date'], d['contributionCount']) for w in cal['weeks'] for d in w['contributionDays']]

    totals['contributions_this_year'] = cal['totalContributions']
    totals['active_days_this_year']   = sum(1 for _, c in days if c > 0)

    longest = run = 0
    for _, c in days:
        if c > 0:
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    totals['longest_streak'] = longest

    # Current streak — trailing consecutive days with contributions.
    # Don't penalize for today being empty (user may not have coded yet).
    today_str = now.strftime('%Y-%m-%d')
    current = 0
    for date, c in reversed(days):
        if date == today_str and c == 0:
            continue
        if c > 0:
            current += 1
        else:
            break
    totals['current_streak'] = current

    return totals


def total_stars(repos):
    return sum(r.get('stargazers_count', 0) for r in repos if not r.get('fork'))


def non_fork_count(repos):
    return sum(1 for r in repos if not r.get('fork'))


# ---------- Rendering helpers ----------

def fmt_int(n):
    return f'{n:,}'


def bar(pct, width=BAR_WIDTH):
    """Render a percentage as a text bar using █ ▓ ▒ ░ block chars."""
    total = pct / 100 * width
    full = int(total)
    remainder = total - full
    if remainder >= 0.66:
        mid = '▓'
    elif remainder >= 0.33:
        mid = '▒'
    else:
        mid = ''
    empty = max(0, width - full - (1 if mid else 0))
    s = '█' * full + mid + '░' * empty
    # Pad/truncate to exactly `width` chars (unicode blocks are all 1-wide).
    if len(s) < width:
        s += '░' * (width - len(s))
    return s[:width]


def humanize_duration(start, end):
    """Approximate 'X years, Y months' from two datetimes."""
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day < start.day:
        months -= 1
    years, months = divmod(max(0, months), 12)
    parts = []
    if years:
        parts.append(f'{years} yr' + ('s' if years != 1 else ''))
    if months:
        parts.append(f'{months} mo')
    return ', '.join(parts) or '< 1 mo'


def render_block(stats, langs, stars, repo_count):
    now = datetime.now(timezone.utc)
    age = humanize_duration(stats['created_at'], now)
    joined = stats['created_at'].strftime('%b %Y')

    # --- Profile & activity stats ---
    rows = [
        ('Profile', [
            ('⭐ Total Stars Earned',          fmt_int(stars)),
            ('👥 Followers',                  fmt_int(stats['followers'])),
            ('🧭 Following',                  fmt_int(stats['following'])),
            ('📁 Public Repos (owned)',       fmt_int(repo_count)),
            ('🎂 GitHub Age',                 f"{joined} ({age})"),
        ]),
        ('Contributions (All-Time, incl. private)', [
            ('🔥 Total Commits',              fmt_int(stats['commits'])),
            ('🔀 Total PRs',                  fmt_int(stats['prs'])),
            ('✅ Total PR Reviews',           fmt_int(stats['reviews'])),
            ('💬 Total Issues',               fmt_int(stats['issues'])),
            ('📦 Repos Contributed To',       fmt_int(stats['contributed_to'])),
        ]),
        (f'Activity ({now.year})', [
            ('📈 Total Contributions',        fmt_int(stats['contributions_this_year'])),
            ('🗓️  Active Days',               f"{stats['active_days_this_year']} / {(now.timetuple().tm_yday)}"),
            ('🔥 Current Streak',             f"{stats['current_streak']} days"),
            ('⚡ Longest Streak',              f"{stats['longest_streak']} days"),
        ]),
    ]

    # Compute label column width. Emojis render ~2 columns wide in most
    # monospace fonts (including the one GitHub uses); variation selectors
    # (U+FE0F) are zero-width.
    def visual_len(s):
        width = 0
        for ch in s:
            o = ord(ch)
            if o == 0xFE0F:
                continue
            if o >= 0x2600:
                width += 2
            else:
                width += 1
        return width

    max_label = 0
    for _, section in rows:
        for label, _ in section:
            max_label = max(max_label, visual_len(label))
    max_label += 2  # little breathing room

    stats_lines = []
    for title, items in rows:
        stats_lines.append(f'{title}')
        for label, value in items:
            pad = ' ' * (max_label - visual_len(label))
            stats_lines.append(f'   {label}{pad}{value}')
        stats_lines.append('')

    # --- Language breakdown (top 8) ---
    top = sorted(langs.items(), key=lambda x: x[1], reverse=True)[:8]
    tail_pct = sum(pct for lang, pct in sorted(langs.items(), key=lambda x: x[1], reverse=True)[8:])

    max_lang = max((len(l) for l, _ in top), default=0)
    lang_lines = []
    for lang, pct in top:
        lang_lines.append(f'   {lang:<{max_lang}}  {pct:5.2f} %  {bar(pct)}')
    if tail_pct > 0:
        lang_lines.append(f'   {"Other":<{max_lang}}  {tail_pct:5.2f} %  {bar(tail_pct)}')

    # --- Assemble the block ---
    updated = now.strftime('%b %d, %Y · %H:%M UTC')
    block = []
    block.append('**⚡ Profile Metrics**')
    block.append('')
    block.append('```text')
    block.extend(stats_lines)
    block.append('```')
    block.append('')
    block.append('**💻 Most Used Languages** (per-repo average across all non-fork repos)')
    block.append('')
    block.append('```text')
    block.extend(lang_lines)
    block.append('```')
    block.append('')
    block.append(f'<sub>Last updated: {updated} · Generated from private + public repos.</sub>')
    return '\n'.join(block)


def update_readme(block):
    with open(README_PATH) as f:
        content = f.read()
    pattern = re.compile(
        re.escape(MARKER_START) + r'.*?' + re.escape(MARKER_END),
        re.DOTALL,
    )
    if not pattern.search(content):
        print(
            f'ERROR: markers {MARKER_START} / {MARKER_END} not found in {README_PATH}',
            file=sys.stderr,
        )
        sys.exit(1)
    new_content = pattern.sub(
        f'{MARKER_START}\n\n{block}\n\n{MARKER_END}',
        content,
    )
    if new_content == content:
        print('README.md unchanged')
        return False
    with open(README_PATH, 'w') as f:
        f.write(new_content)
    print(f'Updated {README_PATH}')
    return True


# ---------- main ----------

def main():
    print('Fetching owned repos…')
    repos = fetch_owned_repos()
    non_fork = non_fork_count(repos)
    print(f'  {len(repos)} total ({non_fork} non-fork)')

    print('Fetching per-repo language proportions…')
    langs = fetch_language_proportions(repos)
    print(f'  {len(langs)} languages')

    print('Fetching user stats via GraphQL…')
    stats = fetch_user_stats()
    stars = total_stars(repos)
    print(
        f'  stars={stars} commits={stats["commits"]} prs={stats["prs"]} '
        f'reviews={stats["reviews"]} issues={stats["issues"]} '
        f'contributed_to={stats["contributed_to"]} '
        f'streak={stats["current_streak"]}/{stats["longest_streak"]} '
        f'active_days={stats["active_days_this_year"]}'
    )

    block = render_block(stats, langs, stars, non_fork)
    update_readme(block)


if __name__ == '__main__':
    main()
