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
from datetime import datetime, timedelta, timezone

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

    # Day-level calendar for this year — powers active-days count and both streaks.
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
    this_year_cal = gql(cal_q, {'from': start_of_year.isoformat(), 'to': now.isoformat()})['viewer']['contributionsCollection']['contributionCalendar']
    days = [(d['date'], d['contributionCount']) for w in this_year_cal['weeks'] for d in w['contributionDays']]

    totals['contributions_this_year'] = this_year_cal['totalContributions']
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


def fetch_recent_repos(limit, exclude_name_with_owner):
    """Most-recently-pushed repos the viewer owns, public + private.

    Excludes forks, archived repos, and the viewer's profile README repo
    (`login/login`) so the generator's own automated commits never push
    this profile to the top of its own list.
    """
    query = """
    query {
      viewer {
        repositories(
          first: 30,
          ownerAffiliations: OWNER,
          orderBy: {field: PUSHED_AT, direction: DESC}
        ) {
          nodes {
            name
            nameWithOwner
            description
            isPrivate
            isFork
            isArchived
            pushedAt
            primaryLanguage { name }
          }
        }
      }
    }
    """
    nodes = gql(query)['viewer']['repositories']['nodes']
    out = []
    for n in nodes:
        if n['isFork'] or n['isArchived']:
            continue
        if n['nameWithOwner'] == exclude_name_with_owner:
            continue
        out.append(n)
        if len(out) >= limit:
            break
    return out


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


def ago(dt, now):
    """Compact relative-time string (e.g. '3h ago', '2w ago')."""
    secs = int((now - dt).total_seconds())
    if secs < 60:        return f'{secs}s ago'
    mins = secs // 60
    if mins < 60:        return f'{mins}m ago'
    hrs = mins // 60
    if hrs < 24:         return f'{hrs}h ago'
    days = hrs // 24
    if days < 7:         return f'{days}d ago'
    if days < 30:        return f'{days // 7}w ago'
    if days < 365:       return f'{days // 30}mo ago'
    return f'{days // 365}y ago'


def recency_block(dt, now):
    """Map how fresh a push is onto a block char (matches the heatmap language)."""
    days = (now - dt).total_seconds() / 86400
    if days < 1:   return '█'  # pushed within 24h
    if days < 7:   return '▓'  # this week
    if days < 30:  return '▒'  # this month
    return '░'                  # older


def render_recent_repos(repos, now):
    """Render the 'currently building' block with a recency indicator per repo."""
    if not repos:
        return ''

    parsed = []
    for r in repos:
        pushed = datetime.fromisoformat(r['pushedAt'].replace('Z', '+00:00'))
        parsed.append({
            'name':    r['name'],
            'lang':    (r.get('primaryLanguage') or {}).get('name') or '—',
            'desc':    (r.get('description') or '').strip(),
            'private': r['isPrivate'],
            'pushed':  pushed,
        })

    name_w = max(len(p['name']) for p in parsed)
    lang_w = max(len(p['lang']) for p in parsed)
    ago_w  = max(len(ago(p['pushed'], now)) for p in parsed)

    lines = []
    for p in parsed:
        block = recency_block(p['pushed'], now)
        name  = p['name'].ljust(name_w)
        lang  = p['lang'].ljust(lang_w)
        rel   = ago(p['pushed'], now).ljust(ago_w)
        tag   = '  🔒 private' if p['private'] else ''
        lines.append(f'   {block}  {name}   {lang}   · {rel}{tag}')
        if p['desc']:
            desc = p['desc']
            if len(desc) > 80:
                desc = desc[:77].rstrip() + '…'
            lines.append(f'         └ {desc}')
    lines.append('')
    lines.append('      █  last 24h     ▓  this week     ▒  this month     ░  older')
    return '\n'.join(lines)


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


def render_block(stats, langs, stars, repo_count, recent_repos):
    now = datetime.now(timezone.utc)
    age = humanize_duration(stats['created_at'], now)
    joined = stats['created_at'].strftime('%b %Y')

    # --- Stat sections: Profile / Contributions (all-time) / Activity (ytd) ---
    sections = [
        ('👤 Profile', '', [
            ('⭐ Total Stars Earned',          fmt_int(stars)),
            ('👥 Followers',                  fmt_int(stats['followers'])),
            ('🧭 Following',                  fmt_int(stats['following'])),
            ('📁 Public Repos (owned)',       fmt_int(repo_count)),
            ('🎂 GitHub Age',                 f"{joined} ({age})"),
        ]),
        ('📊 Contributions', '(all-time, includes private repos)', [
            ('🔥 Total Commits',              fmt_int(stats['commits'])),
            ('🔀 Total PRs',                  fmt_int(stats['prs'])),
            ('✅ Total PR Reviews',           fmt_int(stats['reviews'])),
            ('💬 Total Issues',               fmt_int(stats['issues'])),
            ('📦 Repos Contributed To',       fmt_int(stats['contributed_to'])),
        ]),
        (f'📈 Activity ({now.year})', '', [
            ('📈 Total Contributions',        fmt_int(stats['contributions_this_year'])),
            ('🗓️  Active Days',               f"{stats['active_days_this_year']} / {now.timetuple().tm_yday}"),
            ('🔥 Current Streak',             f"{stats['current_streak']} days"),
            ('⚡ Longest Streak',              f"{stats['longest_streak']} days"),
        ]),
    ]

    # Label column width — computed across all three sections so the
    # value columns line up consistently even though each section gets
    # its own code fence.
    def visual_len(s):
        width = 0
        for ch in s:
            o = ord(ch)
            if o == 0xFE0F:
                continue  # variation selector renders zero-width
            if o >= 0x2600:
                width += 2  # most emojis render ~2 columns wide
            else:
                width += 1
        return width

    max_label = 0
    for _, _, items in sections:
        for label, _ in items:
            max_label = max(max_label, visual_len(label))
    max_label += 2

    def render_section_rows(items):
        return [
            f'   {label}{" " * (max_label - visual_len(label))}{value}'
            for label, value in items
        ]

    # --- Language breakdown (top 8) ---
    sorted_langs = sorted(langs.items(), key=lambda x: x[1], reverse=True)
    top = sorted_langs[:8]
    tail_pct = sum(pct for _, pct in sorted_langs[8:])

    max_lang = max((len(l) for l, _ in top), default=0)
    lang_lines = [
        f'   {lang:<{max_lang}}  {pct:5.2f} %  {bar(pct)}'
        for lang, pct in top
    ]
    if tail_pct > 0:
        lang_lines.append(f'   {"Other":<{max_lang}}  {tail_pct:5.2f} %  {bar(tail_pct)}')

    # --- Currently building (recent repos) ---
    recent = render_recent_repos(recent_repos, now)

    # --- Assemble the block ---
    updated = now.strftime('%b %d, %Y · %H:%M UTC')
    block = []
    for heading, subtitle, items in sections:
        suffix = f' <sub>{subtitle}</sub>' if subtitle else ''
        block.append(f'**{heading}**{suffix}')
        block.append('')
        block.append('```text')
        block.extend(render_section_rows(items))
        block.append('```')
        block.append('')

    block.append('**💻 Most Used Languages** <sub>per-repo average across all non-fork repos</sub>')
    block.append('')
    block.append('```text')
    block.extend(lang_lines)
    block.append('```')
    block.append('')

    if recent:
        block.append('**🛠️ Currently Building** <sub>5 most recent pushes, private included</sub>')
        block.append('')
        block.append('```text')
        block.append(recent)
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

    print('Fetching recent repos…')
    # Exclude the profile README repo so our own stats-bot push doesn't
    # dominate the "currently building" list every run.
    profile_repo = f'{stats["login"]}/{stats["login"]}'
    recent = fetch_recent_repos(limit=5, exclude_name_with_owner=profile_repo)
    print(f'  {len(recent)} recent repos (excluding {profile_repo})')

    block = render_block(stats, langs, stars, non_fork, recent)
    update_readme(block)


if __name__ == '__main__':
    main()
