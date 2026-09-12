#!/usr/bin/env python3
"""
GitHub Repo Analyzer
Fetches discussions and issues from one or more GitHub repos via the GitHub CLI
and generates a self-contained interactive HTML report.

Usage:
  python analyze_discussions.py <owner/repo>
  python analyze_discussions.py <owner/repo1> <owner/repo2> ...
  python analyze_discussions.py <owner/repo> --refresh
"""

import json
import os
import subprocess
import sys
import argparse
import time
from datetime import datetime, timezone
from collections import defaultdict
from pathlib import Path


# ---------------------------------------------------------------------------
# GraphQL Queries
# ---------------------------------------------------------------------------

DISCUSSIONS_QUERY = """
query($owner: String!, $repo: String!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    discussions(first: 100, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title createdAt closedAt
        author { login }
        comments(first: 100) {
          totalCount
          pageInfo { hasNextPage endCursor }
          nodes { author { login } }
        }
      }
    }
  }
}
"""

DISCUSSION_COMMENTS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    discussion(number: $number) {
      comments(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { author { login } }
      }
    }
  }
}
"""

ISSUES_QUERY = """
query($owner: String!, $repo: String!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    issues(first: 100, after: $cursor, states: [OPEN, CLOSED]) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title createdAt closedAt stateReason
        author { login }
        labels(first: 10) { nodes { name color } }
        comments(first: 100) {
          totalCount
          pageInfo { hasNextPage endCursor }
          nodes { author { login } }
        }
      }
    }
  }
}
"""

ISSUE_COMMENTS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    issue(number: $number) {
      comments(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { author { login } }
      }
    }
  }
}
"""

RELEASES_QUERY = """
query($owner: String!, $repo: String!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    releases(first: 100, after: $cursor, orderBy: {field: CREATED_AT, direction: ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id name tagName createdAt publishedAt isDraft isPrerelease url
        author { login }
      }
    }
  }
}
"""

# Query to fetch multiple user profiles at once using node IDs
# We need to first get node IDs from logins, then fetch profiles
USER_BY_LOGIN_QUERY = """
query($login: String!) {
  user(login: $login) {
    login
    name
    company
    bio
    location
  }
}
"""

# Batch query for user profiles - more efficient
# Takes an array of logins and returns their profiles
USERS_BATCH_QUERY = """
query($logins: [String!]!) {
  nodes(ids: $logins) {
    ... on User {
      login
      name
      company
      bio
      location
    }
  }
}
"""


# ---------------------------------------------------------------------------
# Data Fetching
# ---------------------------------------------------------------------------

def graphql(query: str, variables: dict, exit_on_error=True) -> dict:
    payload = json.dumps({"query": query, "variables": variables})
    r = subprocess.run(
        ["gh", "api", "graphql", "--input", "-"],
        input=payload, capture_output=True, encoding="utf-8",
    )
    if r.returncode != 0:
        if exit_on_error:
            print(f"\nGitHub CLI error:\n{r.stderr}", file=sys.stderr)
            sys.exit(1)
        else:
            raise Exception(f"GitHub CLI error: {r.stderr}")
    data = json.loads(r.stdout)
    if "errors" in data:
        if exit_on_error:
            print(f"\nGraphQL error: {json.dumps(data['errors'], indent=2)}", file=sys.stderr)
            sys.exit(1)
        else:
            raise Exception(f"GraphQL error: {json.dumps(data['errors'])}")
    return data["data"]


def _fetch_paged(owner, repo, list_query, comments_query, list_key, item_key, label):
    all_items = []
    cursor = None
    page = 0
    while True:
        page += 1
        print(f"\r  Page {page}: {len(all_items)} {label} fetched...", end="", flush=True)
        data = graphql(list_query, {"owner": owner, "repo": repo, "cursor": cursor})
        page_data = data["repository"][list_key]
        for item in page_data["nodes"]:
            ci = item["comments"]
            logins = [c["author"]["login"] if c["author"] else "[deleted]" for c in ci["nodes"]]
            has_next, c_cursor = ci["pageInfo"]["hasNextPage"], ci["pageInfo"]["endCursor"]
            while has_next:
                more = graphql(comments_query, {
                    "owner": owner, "repo": repo, "number": item["number"], "cursor": c_cursor,
                })["repository"][item_key]["comments"]
                logins.extend(c["author"]["login"] if c["author"] else "[deleted]" for c in more["nodes"])
                has_next, c_cursor = more["pageInfo"]["hasNextPage"], more["pageInfo"]["endCursor"]
            record = {
                "number": item["number"],
                "title": item["title"],
                "createdAt": item["createdAt"],
                "closedAt": item["closedAt"],
                "author": item["author"]["login"] if item["author"] else "[deleted]",
                "comments": logins,
                "commentCount": ci["totalCount"],
            }
            if "labels" in item:
                record["labels"] = [{"name": l["name"], "color": l["color"]} for l in item["labels"]["nodes"]]
            if "stateReason" in item:
                record["stateReason"] = item["stateReason"]
            all_items.append(record)
        if not page_data["pageInfo"]["hasNextPage"]:
            break
        cursor = page_data["pageInfo"]["endCursor"]
    print(f"\r  Done: {len(all_items)} {label} fetched.          ")
    return all_items


def fetch_discussions(owner, repo):
    return _fetch_paged(owner, repo, DISCUSSIONS_QUERY, DISCUSSION_COMMENTS_QUERY,
                        "discussions", "discussion", "discussions")


def fetch_issues(owner, repo):
    return _fetch_paged(owner, repo, ISSUES_QUERY, ISSUE_COMMENTS_QUERY,
                        "issues", "issue", "issues")


def fetch_releases(owner, repo):
    all_items = []
    cursor = None
    page = 0
    while True:
        page += 1
        print(f"\r  Page {page}: {len(all_items)} releases fetched...", end="", flush=True)
        data = graphql(RELEASES_QUERY, {"owner": owner, "repo": repo, "cursor": cursor})
        page_data = data["repository"]["releases"]
        for item in page_data["nodes"]:
            all_items.append({
                "id": item["id"],
                "name": item["name"],
                "tagName": item["tagName"],
                "createdAt": item["createdAt"],
                "publishedAt": item["publishedAt"],
                "isDraft": item["isDraft"],
                "isPrerelease": item["isPrerelease"],
                "url": item["url"],
                "author": item["author"]["login"] if item["author"] else "[deleted]",
            })
        if not page_data["pageInfo"]["hasNextPage"]:
            break
        cursor = page_data["pageInfo"]["endCursor"]
    print(f"\r  Done: {len(all_items)} releases fetched.          ")
    return all_items


def load_user_profiles_cache(data_dir):
    """Load user profiles from cache file if it exists."""
    cache_file = data_dir / "user_profiles.json"
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            print(f"  Loaded {len(cached)} cached user profiles.")
            return cached
        except Exception as e:
            print(f"  Warning: Could not load user profile cache: {e}")
    return {}


def save_user_profiles_cache(data_dir, profiles):
    """Save user profiles to cache file."""
    cache_file = data_dir / "user_profiles.json"
    try:
        cache_file.write_text(json.dumps(profiles, indent=2), encoding="utf-8")
        print(f"  Saved {len(profiles)} user profiles to cache.")
    except Exception as e:
        print(f"  Warning: Could not save user profile cache: {e}")


def fetch_user_profiles(logins, batch_size=50, data_dir=None):
    """
    Fetch GitHub user profiles for a list of logins.
    Uses individual queries since GitHub's nodes() query doesn't support
    login-based lookups directly. Batches requests to avoid rate limits.
    Returns a dict mapping login -> profile dict.
    
    If data_dir is provided, will merge with and save to cached profiles.
    """
    profiles = {}
    total = len(logins)
    
    # Remove deleted user placeholder
    clean_logins = [l for l in logins if l != "[deleted]"]
    
    # If data_dir provided, try to load existing cache first
    if data_dir:
        profiles = load_user_profiles_cache(data_dir)
    
    for i, login in enumerate(clean_logins):
        # Skip if already in cache
        if login in profiles:
            continue
            
        # Print progress every 10 users
        if (i + 1) % 10 == 0 or i == 0:
            print(f"\r  Fetching profiles: {i+1}/{len(clean_logins)}...", end="", flush=True)
        
        # Rate limiting: add small delay between requests to avoid hitting GitHub's rate limit
        # GitHub allows ~5000 points/hour, and each user query costs ~1 point
        # With 0.1s delay, we can do ~360 requests/minute = ~21600/hour (well within limits)
        if i > 0:
            time.sleep(0.1)
        
        try:
            data = graphql(USER_BY_LOGIN_QUERY, {"login": login}, exit_on_error=False)
            user_data = data.get("user")
            if user_data:
                profiles[login] = {
                    "login": user_data.get("login"),
                    "name": user_data.get("name"),
                    "company": user_data.get("company"),
                    "bio": user_data.get("bio"),
                    "location": user_data.get("location"),
                }
            else:
                # User might be deleted, private, or an organization/bot
                profiles[login] = {"login": login, "name": None, "company": None, "bio": None, "location": None}
        except Exception as e:
            # Rate limit or other error - store minimal info and continue
            print(f"\n  Warning: Failed to fetch profile for {login}: {str(e)[:100]}")
            profiles[login] = {"login": login, "name": None, "company": None, "bio": None, "location": None}
            # If we hit a rate limit, wait a bit longer
            if "rate limit" in str(e).lower() or "abuse" in str(e).lower():
                print("  Rate limit detected, waiting 60 seconds...")
                time.sleep(60)
    
    if clean_logins:
        print(f"\r  Done: Fetched {len(profiles)} user profiles.         ")
    
    # Add placeholder for deleted users
    if "[deleted]" in logins:
        profiles["[deleted]"] = {"login": "[deleted]", "name": None, "company": None, "bio": None, "location": None}
    
    # Save to cache if data_dir provided
    if data_dir:
        save_user_profiles_cache(data_dir, profiles)
    
    return profiles


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def normalize_company(company_str):
    """
    Normalize a company string for grouping.
    - Splits on common separators (commas, @, pipes, slashes)
    - Strips whitespace and @ symbols
    - Lowercases for comparison
    - Returns the normalized key and the pretty display name
    """
    if not company_str:
        return None, None
    
    # Split on common separators
    parts = []
    for part in company_str.replace("@", ",").replace("|", ",").replace("/", ",").split(","):
        part = part.strip().strip("@")
        if part:
            parts.append(part)
    
    if not parts:
        return None, None
    
    # Return first part as the normalized key (lowercase), original as display
    # This groups "@Microsoft", "Microsoft", "@microsoft" together
    normalized_key = parts[0].lower()
    display_name = parts[0]  # Keep original capitalization for display
    
    return normalized_key, display_name


def _agg(by_day, freq):
    out = defaultdict(int)
    for date_str, count in by_day.items():
        d = datetime.strptime(date_str, "%Y-%m-%d")
        key = d.strftime("%G-W%V") if freq == "weekly" else d.strftime("%Y-%m") if freq == "monthly" else date_str
        out[key] += count
    return out


def _agg_nested(by_day, freq):
    out = defaultdict(lambda: defaultdict(int))
    for date_str, label_counts in by_day.items():
        d = datetime.strptime(date_str, "%Y-%m-%d")
        key = d.strftime("%G-W%V") if freq == "weekly" else d.strftime("%Y-%m") if freq == "monthly" else date_str
        for label, count in label_counts.items():
            out[key][label] += count
    return out


def _closed_by_label_series(closed_label_day, top_n=8):
    totals = defaultdict(int)
    for label_counts in closed_label_day.values():
        for label, count in label_counts.items():
            totals[label] += count
    top_labels = [l for l, _ in sorted(totals.items(), key=lambda x: -x[1])[:top_n]]
    has_other = len(totals) > len(top_labels)
    label_names = top_labels + (["Other"] if has_other else [])

    def series(freq):
        agg = _agg_nested(closed_label_day, freq)
        keys = sorted(agg.keys())
        series_data = {label: [] for label in label_names}
        for key in keys:
            label_counts = agg[key]
            for label in top_labels:
                series_data[label].append(label_counts.get(label, 0))
            if has_other:
                other_sum = sum(c for l, c in label_counts.items() if l not in top_labels)
                series_data["Other"].append(other_sum)
        return {"labels": keys, "labelNames": label_names, "series": series_data}

    return {"daily": series("daily"), "weekly": series("weekly"), "monthly": series("monthly")}


def analyze(items):
    starters, commenters, created_day, closed_day, labels = (defaultdict(int) for _ in range(5))
    closed_label_day = defaultdict(lambda: defaultdict(int))
    for item in items:
        starters[item["author"]] += 1
        created_day[item["createdAt"][:10]] += 1
        if item["closedAt"]:
            closed_day[item["closedAt"][:10]] += 1
            item_labels = item.get("labels", [])
            day = item["closedAt"][:10]
            if item_labels:
                for lbl in item_labels:
                    closed_label_day[day][lbl["name"]] += 1
            elif "labels" in item:
                closed_label_day[day]["(no label)"] += 1
        for c in item["comments"]:
            commenters[c] += 1
        for lbl in item.get("labels", []):
            labels[lbl["name"]] += 1

    def series(freq):
        ca, cla = _agg(created_day, freq), _agg(closed_day, freq)
        keys = sorted(set(ca) | set(cla))
        return {"labels": keys, "created": [ca.get(k, 0) for k in keys], "closed": [cla.get(k, 0) for k in keys]}

    recent = sorted(
        [{"number": i["number"], "title": i["title"], "author": i["author"],
          "createdAt": i["createdAt"], "closedAt": i["closedAt"],
          "commentCount": i["commentCount"], "labels": i.get("labels", []),
          "repo": i.get("_repo", "")} for i in items],
        key=lambda x: x["createdAt"], reverse=True,
    )
    return {
        "summary": {
            "total": len(items),
            "open": sum(1 for i in items if not i["closedAt"]),
            "closed": sum(1 for i in items if i["closedAt"]),
            "totalComments": sum(i["commentCount"] for i in items),
        },
        "timeSeries": {"daily": series("daily"), "weekly": series("weekly"), "monthly": series("monthly")},
        "closedByLabel": _closed_by_label_series(closed_label_day),
        "topStarters":   [{"user": u, "count": c} for u, c in sorted(starters.items(),   key=lambda x: -x[1])[:25]],
        "topCommenters": [{"user": u, "count": c} for u, c in sorted(commenters.items(), key=lambda x: -x[1])[:25]],
        "topLabels":     [{"label": l, "count": c} for l, c in sorted(labels.items(),    key=lambda x: -x[1])[:20]],
        "recent": recent,
    }


# ---------------------------------------------------------------------------
# Contributor Leaderboard
# ---------------------------------------------------------------------------

def compute_contributors(discussions, issues, user_profiles=None):
    """
    Compute contributor statistics from discussions and issues.
    Optionally enrich with user profile data (company, name, etc.).
    """
    users = defaultdict(lambda: {"discussionsOpened": 0, "issuesOpened": 0,
                                 "discussionComments": 0, "issueComments": 0})
    for d in discussions:
        users[d["author"]]["discussionsOpened"] += 1
        for c in d["comments"]:
            users[c]["discussionComments"] += 1
    for i in issues:
        users[i["author"]]["issuesOpened"] += 1
        for c in i["comments"]:
            users[c]["issueComments"] += 1

    first_seen_day = {}
    for item in discussions + issues:
        author, day = item["author"], item["createdAt"][:10]
        if author not in first_seen_day or day < first_seen_day[author]:
            first_seen_day[author] = day
    new_by_day = defaultdict(int)
    for day in first_seen_day.values():
        new_by_day[day] += 1

    def growth_series(freq):
        agg = _agg(new_by_day, freq)
        keys = sorted(agg.keys())
        new_counts, cumulative, running = [], [], 0
        for k in keys:
            running += agg[k]
            new_counts.append(agg[k])
            cumulative.append(running)
        return {"labels": keys, "newContributors": new_counts, "cumulativeContributors": cumulative}

    # Build contributor list with optional profile data
    contributors = []
    for u, v in users.items():
        contrib = {
            "user": u,
            "discussionsOpened": v["discussionsOpened"],
            "issuesOpened": v["issuesOpened"],
            "discussionComments": v["discussionComments"],
            "issueComments": v["issueComments"],
            "total": v["discussionsOpened"] + v["issuesOpened"] + v["discussionComments"] + v["issueComments"],
        }
        # Add profile data if available
        if user_profiles and u in user_profiles:
            profile = user_profiles[u]
            contrib["name"] = profile.get("name")
            contrib["company"] = profile.get("company")
            contrib["bio"] = profile.get("bio")
            contrib["location"] = profile.get("location")
        else:
            contrib["name"] = None
            contrib["company"] = None
            contrib["bio"] = None
            contrib["location"] = None
        contributors.append(contrib)

    # Calculate company distribution
    company_counts = defaultdict(int)
    company_display_names = {}
    for contrib in contributors:
        company = contrib.get("company")
        if company:
            norm_key, display_name = normalize_company(company)
            if norm_key:
                company_counts[norm_key] += contrib["total"]
                # Store the best display name (first one we see, or longest)
                if norm_key not in company_display_names or len(display_name) > len(company_display_names[norm_key]):
                    company_display_names[norm_key] = display_name
    
    # Sort by total contributions, take top N
    sorted_companies = sorted(company_counts.items(), key=lambda x: -x[1])
    top_companies = [{"company": company_display_names.get(k, k), "contributions": v} 
                    for k, v in sorted_companies[:20]]  # Top 20 companies

    return {"contributors": sorted(contributors, key=lambda x: -x["total"]), "growth": {
        "daily": growth_series("daily"),
        "weekly": growth_series("weekly"),
        "monthly": growth_series("monthly"),
    }, "byCompany": top_companies}


# ---------------------------------------------------------------------------
# Updates (recently closed items)
# ---------------------------------------------------------------------------

def compute_closed_items(discussions, issues):
    items = []
    for d in discussions:
        if d.get("closedAt"):
            items.append({
                "type": "discussion", "number": d["number"], "title": d["title"],
                "author": d["author"], "createdAt": d["createdAt"], "closedAt": d["closedAt"],
                "commentCount": d["commentCount"], "labels": d.get("labels", []),
                "closeReason": None,
                "repo": d.get("_repo", ""),
            })
    for i in issues:
        if i.get("closedAt"):
            items.append({
                "type": "issue", "number": i["number"], "title": i["title"],
                "author": i["author"], "createdAt": i["createdAt"], "closedAt": i["closedAt"],
                "commentCount": i["commentCount"], "labels": i.get("labels", []),
                "closeReason": i.get("stateReason"),
                "repo": i.get("_repo", ""),
            })
    items.sort(key=lambda x: x["closedAt"], reverse=True)
    return {"closedItems": items}


# ---------------------------------------------------------------------------
# Releases
# ---------------------------------------------------------------------------

def compute_releases(releases):
    # Dedupe by release id (same release could otherwise be counted twice,
    # e.g. if the same repo's data ends up merged more than once).
    by_id = {}
    for r in releases:
        by_id[r["id"]] = r
    deduped = list(by_id.values())

    def item_date(r):
        return r.get("publishedAt") or r.get("createdAt") or ""

    # Group releases that share the same tag name across repos into a single
    # combined entry (e.g. a public repo + its private-preview mirror cutting
    # the same version at the same time).
    groups = defaultdict(list)
    for r in deduped:
        key = r.get("tagName") or f"__id__{r['id']}"
        groups[key].append(r)

    merged = []
    for group in groups.values():
        group_sorted = sorted(group, key=item_date, reverse=True)
        primary = group_sorted[0]
        merged.append({
            "id": primary["id"],
            "name": primary.get("name") or primary.get("tagName") or "(untitled)",
            "tagName": primary.get("tagName"),
            "publishedAt": primary.get("publishedAt"),
            "createdAt": primary.get("createdAt"),
            "isPrerelease": any(r.get("isPrerelease") for r in group),
            "isDraft": any(r.get("isDraft") for r in group),
            "repos": [
                {
                    "repo": r.get("_repo", ""),
                    "author": r.get("author"),
                    "url": r.get("url"),
                    "publishedAt": r.get("publishedAt"),
                    "createdAt": r.get("createdAt"),
                }
                for r in group_sorted
            ],
        })

    merged_sorted = sorted(merged, key=lambda r: item_date(r), reverse=True)

    day_counts = defaultdict(int)
    for r in merged:
        date = item_date(r)
        if date:
            day_counts[date[:10]] += 1

    def series(freq):
        agg = _agg(day_counts, freq)
        keys = sorted(agg.keys())
        return {"labels": keys, "count": [agg[k] for k in keys]}

    return {
        "summary": {
            "total": len(merged),
            "prereleases": sum(1 for r in merged if r["isPrerelease"]),
            "drafts": sum(1 for r in merged if r["isDraft"]),
        },
        "timeSeries": {"daily": series("daily"), "weekly": series("weekly"), "monthly": series("monthly")},
        "releases": merged_sorted,
    }


# ---------------------------------------------------------------------------
# HTML Template
# ---------------------------------------------------------------------------

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Analytics · TMPL_REPO_TITLE</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    :root {
      --bg: #f6f8fa; --card: #fff; --border: #d1d9e0;
      --text: #1f2328; --muted: #656d76;
      --accent: #0969da; --green: #1a7f37; --red: #cf222e;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
           background: var(--bg); color: var(--text); min-height: 100vh; }
    header { background: var(--card); border-bottom: 1px solid var(--border);
             padding: 1rem 1.5rem; display: flex; align-items: center; gap: .75rem; flex-wrap: wrap; }
    header h1 { font-size: 1.1rem; font-weight: 600; }
    header .meta { color: var(--muted); font-size: .8rem; margin-left: auto; white-space: nowrap; }
    /* Repo switcher toolbar */
    .repo-toolbar { background: var(--card); border-bottom: 1px solid var(--border); padding: .6rem 1.5rem; }
    .repo-toolbar .inner { max-width: 1280px; margin: 0 auto; display: flex; align-items: center; gap: .75rem; flex-wrap: wrap; }
    .repo-toolbar-label { font-size: .8rem; color: var(--muted); font-weight: 500; white-space: nowrap; }
    .container { max-width: 1280px; margin: 0 auto; padding: 1.5rem 1rem; }
    /* Tabs */
    .tab-nav { display: flex; gap: 0; margin-bottom: 1.5rem; border-bottom: 2px solid var(--border); flex-wrap: wrap; }
    .tab-btn { padding: .65rem 1.4rem; border: none; background: none; cursor: pointer;
               font-size: .9rem; font-weight: 500; color: var(--muted);
               border-bottom: 3px solid transparent; margin-bottom: -2px; transition: color .15s; white-space: nowrap; }
    .tab-btn.active { color: var(--accent); border-bottom-color: var(--accent); }
    .tab-btn:hover:not(.active) { color: var(--text); }
    .tab-content { display: none; }
    .tab-content.active { display: block; }
    /* Stats */
    .stats { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
             gap: 1rem; margin-bottom: 1.5rem; }
    .stat-card { background: var(--card); border: 1px solid var(--border);
                 border-radius: 8px; padding: 1rem; text-align: center; }
    .stat-card .value { font-size: 2rem; font-weight: 700; line-height: 1; }
    .stat-card .label { font-size: .75rem; color: var(--muted); margin-top: .35rem;
                        text-transform: uppercase; letter-spacing: .04em; }
    .stat-card.open .value   { color: var(--green); }
    .stat-card.closed .value { color: var(--red); }
    .stat-card.accent .value { color: var(--accent); }
    /* Cards */
    .card { background: var(--card); border: 1px solid var(--border);
            border-radius: 8px; padding: 1.25rem; margin-bottom: 1.5rem; }
    .card h2 { font-size: .95rem; font-weight: 600; margin-bottom: 1rem; }
    /* Toggle buttons */
    .toggle-group { display: flex; gap: .4rem; flex-wrap: wrap; }
    .toggle-group.mb { margin-bottom: 1rem; }
    .toggle-btn { padding: .3rem .7rem; border: 1px solid var(--border); background: var(--bg);
                  border-radius: 6px; cursor: pointer; font-size: .8rem; color: var(--text); }
    .toggle-btn.active { background: var(--accent); color: #fff; border-color: var(--accent); }
    .toggle-btn:hover:not(.active) { background: #e6edf3; }
    /* Hero card (Updates tab) */
    .hero-card { background: linear-gradient(135deg, var(--accent), #1a56c4); color: #fff; border: none; }
    .hero-card .hero-stat { font-size: 2.5rem; font-weight: 700; line-height: 1; }
    .hero-card .hero-sub { font-size: .85rem; opacity: .85; margin-top: .25rem; }
    .hero-card .hero-label { font-size: .75rem; opacity: .85; margin-bottom: .5rem; text-transform: uppercase; letter-spacing: .04em; }
    .hero-card .toggle-btn { background: rgba(255,255,255,.15); color: #fff; border-color: rgba(255,255,255,.4); }
    .hero-card .toggle-btn.active { background: #fff; color: var(--accent); border-color: #fff; }
    .hero-card .toggle-btn:hover:not(.active) { background: rgba(255,255,255,.28); }
    .label-chip, .status-chip { font-size: .75rem; padding: .25rem .65rem; }
    /* Charts */
    .chart-outer { overflow-x: auto; }
    .chart-wrap  { position: relative; height: 300px; min-width: 400px; }
    .charts-row { display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; margin-bottom: 1.5rem; }
    .charts-row.three { grid-template-columns: 1fr 1fr 1fr; }
    .charts-row.three .chart-wrap { min-width: 0; }
    @media (max-width: 900px) { .charts-row, .charts-row.three { grid-template-columns: 1fr; } }
    /* Table */
    .table-container { overflow-x: auto; margin-bottom: -1px; }
    table { width: 100%; border-collapse: collapse; font-size: .85rem; }
    th { text-align: left; padding: .5rem .75rem; border-bottom: 2px solid var(--border);
         color: var(--muted); font-weight: 600; white-space: nowrap; }
    th.sortable { cursor: pointer; user-select: none; }
    th.sortable:hover { color: var(--text); background: #f0f3f6; }
    th.sort-active { color: var(--accent); }
    td { padding: .5rem .75rem; border-bottom: 1px solid var(--border); vertical-align: middle; }
    tr:last-child td { border-bottom: none; }
    tr:hover td { background: #f6f8fa; }
    .td-title  { max-width: 300px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .td-labels { max-width: 200px; }
    .td-repo   { font-size: .8rem; white-space: nowrap; }
    .badge { display: inline-block; padding: .15em .55em; border-radius: 20px;
             font-size: .72rem; font-weight: 600; white-space: nowrap; }
    .badge.open   { background: #dafbe1; color: var(--green); }
    .badge.closed { background: #ffebe9; color: var(--red); }
    a { color: var(--accent); text-decoration: none; }
    a:hover { text-decoration: underline; }
    /* Filter & pagination */
    .filter-row { display: flex; align-items: center; gap: .75rem; margin-bottom: .75rem; flex-wrap: wrap; }
    .filter-row input  { flex: 1; min-width: 180px; padding: .35rem .65rem;
                         border: 1px solid var(--border); border-radius: 6px; font-size: .85rem; }
    .filter-row select { padding: .35rem .65rem; border: 1px solid var(--border);
                         border-radius: 6px; font-size: .85rem; background: var(--bg); color: var(--text); }
    .info-text { font-size: .8rem; color: var(--muted); }
    .pager { display: flex; gap: .35rem; margin-top: 1rem; justify-content: center; flex-wrap: wrap; }
    .pager-btn { padding: .3rem .65rem; border: 1px solid var(--border); background: var(--bg);
                 border-radius: 6px; cursor: pointer; font-size: .8rem; color: var(--text); }
    .pager-btn.active { background: var(--accent); color: #fff; border-color: var(--accent); }
    .pager-btn:hover:not(.active) { background: #e6edf3; }
  </style>
</head>
<body>

<header>
  <svg width="20" height="20" viewBox="0 0 16 16" fill="var(--accent)">
    <path d="M8 0c4.42 0 8 3.58 8 8a8.013 8.013 0 0 1-5.45 7.59c-.4.08-.55-.17-.55-.38 0-.27.01-1.13.01-2.2 0-.75-.25-1.23-.54-1.48 1.78-.2 3.65-.88 3.65-3.95 0-.88-.31-1.59-.82-2.15.08-.2.36-1.02-.08-2.12 0 0-.67-.22-2.2.82-.64-.18-1.32-.27-2-.27-.68 0-1.36.09-2 .27-1.53-1.03-2.2-.82-2.2-.82-.44 1.1-.16 1.92-.08 2.12-.51.56-.82 1.28-.82 2.15 0 3.06 1.86 3.75 3.64 3.95-.23.2-.44.55-.51 1.07-.46.21-1.61.55-2.33-.66-.15-.24-.6-.83-1.23-.82-.67.01-.27.38.01.53.34.19.73.9.82 1.13.16.45.68 1.31 2.69.94 0 .67.01 1.3.01 1.49 0 .21-.15.45-.55.38A7.995 7.995 0 0 1 0 8c0-4.42 3.58-8 8-8Z"/>
  </svg>
  <h1>TMPL_REPO_TITLE &mdash; Analytics</h1>
  <span class="meta">Data fetched: TMPL_FETCHED_AT</span>
</header>

<!-- Repo switcher toolbar (hidden for single repo) -->
<div class="repo-toolbar" id="repoToolbar" style="display:none">
  <div class="inner">
    <span class="repo-toolbar-label">Repository:</span>
    <div class="toggle-group" id="repoSwitcher"></div>
  </div>
</div>

<div class="container">

  <div class="tab-nav">
    <button class="tab-btn active" data-tab="contributors">
      👥 Contributors (<span id="contrib-tab-count"></span>)
    </button>
    <button class="tab-btn" data-tab="discussions">
      💬 Discussions (<span id="disc-tab-count"></span>)
    </button>
    <button class="tab-btn" data-tab="issues">
      🐛 Issues (<span id="issue-tab-count"></span>)
    </button>
    <button class="tab-btn" data-tab="updates">
      🆕 Updates (<span id="updates-tab-count"></span>)
    </button>
    <button class="tab-btn" data-tab="releases">
      🚀 Releases (<span id="releases-tab-count"></span>)
    </button>
  </div>

  <!-- ── Contributors tab ── -->
  <div id="tab-contributors" class="tab-content active">
    <div class="stats" id="contrib-stats"></div>
    <div class="card">
      <h2>Issues &amp; Discussions Opened Over Time</h2>
      <p style="font-size:.8rem;color:var(--muted);margin:.5rem 0 1rem">
        Bars show new discussions and issues opened; the line shows the combined total closed per period.
      </p>
      <div class="toggle-group mb" id="contrib-freqBtns">
        <button class="toggle-btn" data-freq="daily">Daily</button>
        <button class="toggle-btn" data-freq="weekly">Weekly</button>
        <button class="toggle-btn active" data-freq="monthly">Monthly</button>
      </div>
      <div class="chart-outer"><div class="chart-wrap"><canvas id="activityOverTimeChart"></canvas></div></div>
    </div>
    <div class="card">
      <h2>Contributor Growth Over Time</h2>
      <p style="font-size:.8rem;color:var(--muted);margin:.5rem 0 1rem">
        New contributors are counted the first time someone opens a discussion or issue.
        Cumulative shows the running total of unique contributors.
      </p>
      <div class="chart-outer"><div class="chart-wrap"><canvas id="contribGrowthChart"></canvas></div></div>
    </div>
    <div class="card">
      <h2>Backlog Growth Over Time</h2>
      <p style="font-size:.8rem;color:var(--muted);margin:.5rem 0 1rem">
        Net change (opened − closed) per period across discussions and issues, and the
        cumulative open backlog size over time.
      </p>
      <div class="chart-outer"><div class="chart-wrap"><canvas id="backlogGrowthChart"></canvas></div></div>
    </div>
    <div class="card">
      <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:.5rem">
        <h2 style="margin-bottom:0">Most Active Contributors — <span id="contrib-chart-scope">Top 25</span></h2>
        <button class="toggle-btn" id="contrib-chart-more-btn" onclick="toggleContribChart()" style="display:none"></button>
      </div>
      <p style="font-size:.8rem;color:var(--muted);margin:.5rem 0 1rem">
        Combined score: discussions opened + discussion comments + issues opened + issue comments.
        Stacked bars show the breakdown per activity type.
      </p>
      <div class="chart-outer">
        <div class="chart-wrap" id="contrib-chart-wrap" style="min-width:400px">
          <canvas id="contribChart"></canvas>
        </div>
      </div>
    </div>
    <div class="card">
      <h2>Contributors by Company</h2>
      <p style="font-size:.8rem;color:var(--muted);margin:.5rem 0 1rem">
        Shows the top companies by total contributions from their employees.
        Company names are normalized to group variations together (e.g., "@Microsoft" and "Microsoft").
      </p>
      <div class="chart-outer">
        <div class="chart-wrap" style="min-width:400px">
          <canvas id="contribByCompanyChart"></canvas>
        </div>
      </div>
    </div>
    <div class="card">
      <h2>All Contributors</h2>
      <div class="filter-row">
        <input type="search" id="contrib-search" placeholder="Filter by username, name, or company…" oninput="applyContribFilter()">
        <button class="toggle-btn" id="contrib-collapse-btn" onclick="collapseContribTable()" style="display:none">Show top 10 only</button>
        <span class="info-text" id="contrib-countInfo"></span>
      </div>
      <div class="table-container">
        <table>
          <thead id="contrib-thead"></thead>
          <tbody id="contrib-tbody"></tbody>
        </table>
      </div>
      <div class="pager" id="contrib-pager"></div>
    </div>
  </div>

  <!-- ── Discussions tab ── -->
  <div id="tab-discussions" class="tab-content">
    <div class="stats" id="disc-stats"></div>
    <div class="card">
      <h2>Discussions Over Time</h2>
      <div class="toggle-group mb" id="disc-freqBtns">
        <button class="toggle-btn active" data-freq="daily">Daily</button>
        <button class="toggle-btn" data-freq="weekly">Weekly</button>
        <button class="toggle-btn" data-freq="monthly">Monthly</button>
      </div>
      <div class="chart-outer"><div class="chart-wrap"><canvas id="discTimeChart"></canvas></div></div>
    </div>
    <div class="charts-row">
      <div class="card">
        <h2>Top Discussion Starters</h2>
        <div class="chart-outer"><div class="chart-wrap"><canvas id="discStartersChart"></canvas></div></div>
      </div>
      <div class="card">
        <h2>Top Discussion Commenters</h2>
        <div class="chart-outer"><div class="chart-wrap"><canvas id="discCommentersChart"></canvas></div></div>
      </div>
    </div>
    <div class="card">
      <h2>All Discussions</h2>
      <div class="filter-row">
        <input type="search" id="disc-search" placeholder="Filter by title or author…" oninput="applyFilter('disc')">
        <select id="disc-status" onchange="applyFilter('disc')">
          <option value="all">All statuses</option>
          <option value="open">Open</option>
          <option value="closed">Closed</option>
        </select>
        <span class="info-text" id="disc-countInfo"></span>
      </div>
      <div class="table-container">
        <table><thead id="disc-thead"></thead><tbody id="disc-tbody"></tbody></table>
      </div>
      <div class="pager" id="disc-pager"></div>
    </div>
  </div>

  <!-- ── Issues tab ── -->
  <div id="tab-issues" class="tab-content">
    <div class="stats" id="issue-stats"></div>
    <div class="card">
      <h2>Issues Over Time</h2>
      <div class="toggle-group mb" id="issue-freqBtns">
        <button class="toggle-btn active" data-freq="daily">Daily</button>
        <button class="toggle-btn" data-freq="weekly">Weekly</button>
        <button class="toggle-btn" data-freq="monthly">Monthly</button>
      </div>
      <div class="chart-outer"><div class="chart-wrap"><canvas id="issueTimeChart"></canvas></div></div>
    </div>
    <div class="card" id="issueClosedByLabelCard" style="display:none">
      <h2>Closed Issues by Label Over Time</h2>
      <div class="chart-outer"><div class="chart-wrap"><canvas id="issueClosedByLabelChart"></canvas></div></div>
    </div>
    <div class="charts-row" id="issue-charts-row">
      <div class="card">
        <h2>Top Issue Openers</h2>
        <div class="chart-outer"><div class="chart-wrap"><canvas id="issueStartersChart"></canvas></div></div>
      </div>
      <div class="card">
        <h2>Top Issue Commenters</h2>
        <div class="chart-outer"><div class="chart-wrap"><canvas id="issueCommentersChart"></canvas></div></div>
      </div>
      <div class="card" id="issueLabelsCard" style="display:none">
        <h2>Top Labels</h2>
        <div class="chart-outer"><div class="chart-wrap"><canvas id="issueLabelsChart"></canvas></div></div>
      </div>
    </div>
    <div class="card">
      <h2>All Issues</h2>
      <div class="filter-row">
        <input type="search" id="issue-search" placeholder="Filter by title, author, or label…" oninput="applyFilter('issue')">
        <select id="issue-status" onchange="applyFilter('issue')">
          <option value="all">All statuses</option>
          <option value="open">Open</option>
          <option value="closed">Closed</option>
        </select>
        <span class="info-text" id="issue-countInfo"></span>
      </div>
      <div class="table-container">
        <table><thead id="issue-thead"></thead><tbody id="issue-tbody"></tbody></table>
      </div>
      <div class="pager" id="issue-pager"></div>
    </div>
  </div>

  <!-- ── Updates tab ── -->
  <div id="tab-updates" class="tab-content">
    <div class="card hero-card">
      <div style="display:flex;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;gap:1rem">
        <div>
          <div class="hero-stat" id="updates-hero-count">0</div>
          <div class="hero-sub">Closed items <span id="updates-hero-range-label">in the last 7 days</span></div>
        </div>
        <div class="toggle-group" id="updates-range-btns">
          <button class="toggle-btn active" data-range="week">Last 7 Days</button>
          <button class="toggle-btn" data-range="month">Last 30 Days</button>
        </div>
      </div>
      <div style="margin-top:1.25rem">
        <div class="hero-label">Filter by close reason</div>
        <div class="toggle-group" id="updates-status-btns"></div>
      </div>
      <div style="margin-top:1.25rem;display:none" id="updates-author-section">
        <div class="hero-label">Author</div>
        <div class="toggle-group" id="updates-author-btns">
          <button class="toggle-btn active" data-author="all">Everyone</button>
          <button class="toggle-btn" data-author="me">My items only</button>
        </div>
      </div>
      <div style="margin-top:1.25rem">
        <div class="hero-label">Filter by label</div>
        <div class="toggle-group" id="updates-label-chips"></div>
      </div>
    </div>
    <div class="card">
      <h2>Closed Issues &amp; Discussions</h2>
      <div class="filter-row">
        <span class="info-text" id="updates-countInfo"></span>
      </div>
      <div class="table-container">
        <table><thead id="updates-thead"></thead><tbody id="updates-tbody"></tbody></table>
      </div>
      <div class="pager" id="updates-pager"></div>
    </div>
  </div>

  <!-- ── Releases tab ── -->
  <div id="tab-releases" class="tab-content">
    <div class="stats" id="releases-stats"></div>
    <div class="card">
      <h2>Releases Over Time</h2>
      <p style="font-size:.8rem;color:var(--muted);margin:.5rem 0 1rem">
        Releases are deduplicated by release id, and releases sharing the same tag name across
        repos (e.g. a repo and its preview mirror) are merged into a single entry.
      </p>
      <div class="toggle-group mb" id="releases-freqBtns">
        <button class="toggle-btn" data-freq="daily">Daily</button>
        <button class="toggle-btn" data-freq="weekly">Weekly</button>
        <button class="toggle-btn active" data-freq="monthly">Monthly</button>
      </div>
      <div class="chart-outer"><div class="chart-wrap"><canvas id="releasesTimeChart"></canvas></div></div>
    </div>
    <div class="card">
      <h2>All Releases</h2>
      <div class="filter-row">
        <input type="search" id="releases-search" placeholder="Filter by name, tag, or author…" oninput="applyReleasesFilter()">
        <span class="info-text" id="releases-countInfo"></span>
      </div>
      <div class="table-container">
        <table><thead id="releases-thead"></thead><tbody id="releases-tbody"></tbody></table>
      </div>
      <div class="pager" id="releases-pager"></div>
    </div>
  </div>

</div>

<script>
const PAYLOAD   = TMPL_PAYLOAD_JSON;
const PAGE_SIZE = 30;
const CONTRIB_TABLE_COLLAPSED_N = 10;
const multiRepo = PAYLOAD.repos.length > 1;

let activeRepo = "all";
const activeFreq = { disc: "daily", issue: "daily", contrib: "monthly", releases: "monthly" };
const charts = {};

// Per-table state
const tbl = {
  disc:     { filtered: [], page: 0 },
  issue:    { filtered: [], page: 0 },
  contrib:  { filtered: [], page: 0, sortCol: "total", sortDir: -1, expanded: false },
  updates:  { filtered: [], page: 0 },
  releases: { filtered: [], page: 0 },
};
const updatesState = { range: "week", labels: new Set(), closeReason: "all", author: "all" };
const CLOSE_REASON_META = {
  COMPLETED:   { text: "✅ Completed" },
  NOT_PLANNED: { text: "🚫 Not planned" },
  DUPLICATE:   { text: "🔁 Duplicate" },
  __none__:    { text: "— No reason" },
};

/* ── Core helpers ── */
function getView() {
  return activeRepo === "all" ? PAYLOAD.all : PAYLOAD.byRepo[activeRepo];
}
function esc(s) {
  return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}
function fmtDate(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString(undefined, { year:"numeric", month:"short", day:"numeric" });
}
function renderLabelBadges(labels) {
  if (!labels || !labels.length) return "—";
  return labels.map(l => {
    const bg = l.color ? `#${l.color}` : "#e1e4e8";
    const r = parseInt((l.color||"888888").substring(0,2), 16);
    const g = parseInt((l.color||"888888").substring(2,4), 16);
    const b = parseInt((l.color||"888888").substring(4,6), 16);
    const fg = (0.299*r + 0.587*g + 0.114*b) > 140 ? "#000" : "#fff";
    return `<span class="badge" style="background:${bg};color:${fg};margin:1px">${esc(l.name)}</span>`;
  }).join(" ");
}
const showRepoCol = () => multiRepo && activeRepo === "all";

/* ── Tab switching ── */
const VALID_TABS = [...document.querySelectorAll(".tab-btn")].map(b => b.dataset.tab);
function switchTab(name, updateHash = true) {
  if (!VALID_TABS.includes(name)) return;
  document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
  document.querySelectorAll(".tab-content").forEach(t => t.classList.remove("active"));
  document.querySelector(`.tab-btn[data-tab="${name}"]`).classList.add("active");
  document.getElementById(`tab-${name}`).classList.add("active");
  if (updateHash && location.hash.slice(1) !== name) location.hash = name;
}
document.querySelectorAll(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
});
window.addEventListener("hashchange", () => switchTab(location.hash.slice(1), false));

/* ── Repo switcher ── */
function initRepoSwitcher() {
  if (!multiRepo) return;
  document.getElementById("repoToolbar").style.display = "";
  const sw = document.getElementById("repoSwitcher");
  [["all", "All repos"], ...PAYLOAD.repos.map(r => [r, r])].forEach(([val, label]) => {
    const btn = document.createElement("button");
    btn.className = "toggle-btn" + (val === "all" ? " active" : "");
    btn.textContent = label;
    btn.addEventListener("click", () => {
      document.querySelectorAll("#repoSwitcher .toggle-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      switchRepo(val);
    });
    sw.appendChild(btn);
  });
}

/* ── Stats cards ── */
function renderStats(containerId, cards) {
  const el = document.getElementById(containerId);
  el.innerHTML = "";
  cards.forEach(({ label, value, cls }) => {
    el.insertAdjacentHTML("beforeend",
      `<div class="stat-card ${cls||""}">
         <div class="value">${value.toLocaleString()}</div>
         <div class="label">${label}</div>
       </div>`);
  });
}
function updateAllStats(v) {
  const ds = v.discussions.summary, is_ = v.issues.summary, rs = v.releases.summary;
  const cc = v.contributors.contributors;
  const discAct  = cc.reduce((s,c)=>s+c.discussionsOpened+c.discussionComments, 0);
  const issueAct = cc.reduce((s,c)=>s+c.issuesOpened+c.issueComments, 0);
  renderStats("contrib-stats", [
    { label: "Unique Contributors",  value: cc.length,             cls: "accent" },
    { label: "Total Activity",       value: discAct + issueAct },
    { label: "💬 Discussion Actions", value: discAct },
    { label: "🐛 Issue Actions",      value: issueAct },
  ]);
  renderStats("disc-stats", [
    { label: "Total Discussions", value: ds.total },
    { label: "Open",              value: ds.open,          cls: "open" },
    { label: "Closed",            value: ds.closed,        cls: "closed" },
    { label: "Total Comments",    value: ds.totalComments },
  ]);
  renderStats("issue-stats", [
    { label: "Total Issues",   value: is_.total },
    { label: "Open",           value: is_.open,         cls: "open" },
    { label: "Closed",         value: is_.closed,       cls: "closed" },
    { label: "Total Comments", value: is_.totalComments },
  ]);
  renderStats("releases-stats", [
    { label: "Total Releases", value: rs.total,       cls: "accent" },
    { label: "Pre-releases",   value: rs.prereleases },
    { label: "Drafts",         value: rs.drafts },
  ]);
  document.getElementById("contrib-tab-count").textContent  = cc.length;
  document.getElementById("disc-tab-count").textContent     = ds.total;
  document.getElementById("issue-tab-count").textContent    = is_.total;
  document.getElementById("releases-tab-count").textContent = rs.total;
}

/* ── Chart: time series ── */
function makeTimeChart(canvasId, ts) {
  return new Chart(document.getElementById(canvasId).getContext("2d"), {
    type: "bar",
    data: {
      labels: ts.daily.labels,
      datasets: [
        { label:"Created", data: ts.daily.created, backgroundColor:"rgba(9,105,218,.55)",  borderColor:"rgba(9,105,218,.9)",  borderWidth:1, borderRadius:2 },
        { label:"Closed",  data: ts.daily.closed,  backgroundColor:"rgba(26,127,55,.55)", borderColor:"rgba(26,127,55,.9)", borderWidth:1, borderRadius:2 },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: "top" } },
      scales: { y: { beginAtZero:true, ticks:{ stepSize:1, precision:0 } } },
    },
  });
}
function updateTimeChart(chart, ts, freq) {
  chart.data.labels = ts[freq].labels;
  chart.data.datasets[0].data = ts[freq].created;
  chart.data.datasets[1].data = ts[freq].closed;
  chart.update();
}

/* ── Chart: horizontal bar ── */
function makeHBar(canvasId, labels, values, color) {
  return new Chart(document.getElementById(canvasId).getContext("2d"), {
    type: "bar",
    data: { labels, datasets: [{ data: values, backgroundColor: color, borderRadius: 3 }] },
    options: {
      indexAxis: "y", responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: { x: { beginAtZero:true, ticks:{ stepSize:1, precision:0 } } },
    },
  });
}
function updateHBar(chart, labels, values) {
  chart.data.labels = labels;
  chart.data.datasets[0].data = values;
  chart.update();
}

/* ── Chart: combined issues+discussions opened over time ── */
function combinedActivitySeries(v, freq) {
  const d = v.discussions.timeSeries[freq], i = v.issues.timeSeries[freq];
  const keys = [...new Set([...d.labels, ...i.labels])].sort();
  const dMap  = Object.fromEntries(d.labels.map((k, idx) => [k, d.created[idx]]));
  const iMap  = Object.fromEntries(i.labels.map((k, idx) => [k, i.created[idx]]));
  const dcMap = Object.fromEntries(d.labels.map((k, idx) => [k, d.closed[idx]]));
  const icMap = Object.fromEntries(i.labels.map((k, idx) => [k, i.closed[idx]]));
  return {
    labels: keys,
    discussions: keys.map(k => dMap[k] || 0),
    issues: keys.map(k => iMap[k] || 0),
    closed: keys.map(k => (dcMap[k] || 0) + (icMap[k] || 0)),
  };
}
function makeActivityChart(canvasId, v, freq) {
  const s = combinedActivitySeries(v, freq);
  return new Chart(document.getElementById(canvasId).getContext("2d"), {
    data: {
      labels: s.labels,
      datasets: [
        { type: "bar",  label: "💬 Discussions Opened", data: s.discussions, backgroundColor: "rgba(9,105,218,.65)",  borderColor: "rgba(9,105,218,.9)",  borderWidth: 1, borderRadius: 2, stack: "opened" },
        { type: "bar",  label: "🐛 Issues Opened",      data: s.issues,      backgroundColor: "rgba(207,34,46,.65)", borderColor: "rgba(207,34,46,.9)", borderWidth: 1, borderRadius: 2, stack: "opened" },
        { type: "line", label: "✅ Closed (combined)",  data: s.closed,      borderColor: "rgba(26,127,55,.9)", backgroundColor: "rgba(26,127,55,.15)", borderWidth: 2, tension: .25, pointRadius: 2 },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: "top" } },
      scales: {
        x: { stacked: true },
        y: { stacked: true, beginAtZero: true, ticks: { stepSize: 1, precision: 0 } },
      },
    },
  });
}
function updateActivityChart(chart, v, freq) {
  const s = combinedActivitySeries(v, freq);
  chart.data.labels = s.labels;
  chart.data.datasets[0].data = s.discussions;
  chart.data.datasets[1].data = s.issues;
  chart.data.datasets[2].data = s.closed;
  chart.update();
}

/* ── Chart: contributor growth over time ── */
function makeGrowthChart(canvasId, growth) {
  const freq = activeFreq.contrib;
  return new Chart(document.getElementById(canvasId).getContext("2d"), {
    data: {
      labels: growth[freq].labels,
      datasets: [
        { type: "bar",  label: "New Contributors",        data: growth[freq].newContributors,
          backgroundColor: "rgba(9,105,218,.55)", borderColor: "rgba(9,105,218,.9)", borderWidth: 1, borderRadius: 2, yAxisID: "y" },
        { type: "line", label: "Cumulative Contributors",  data: growth[freq].cumulativeContributors,
          borderColor: "rgba(130,80,223,.9)", backgroundColor: "rgba(130,80,223,.15)", borderWidth: 2, tension: .25, pointRadius: 2, yAxisID: "y1" },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: "top" } },
      scales: {
        y:  { beginAtZero: true, ticks: { stepSize: 1, precision: 0 }, position: "left",
              title: { display: true, text: "New" } },
        y1: { beginAtZero: true, position: "right", grid: { drawOnChartArea: false },
              title: { display: true, text: "Cumulative" } },
      },
    },
  });
}
function updateGrowthChart(chart, growth, freq) {
  chart.data.labels = growth[freq].labels;
  chart.data.datasets[0].data = growth[freq].newContributors;
  chart.data.datasets[1].data = growth[freq].cumulativeContributors;
  chart.update();
}

/* ── Chart: backlog growth over time ── */
function combinedBacklogSeries(v, freq) {
  const d = v.discussions.timeSeries[freq], i = v.issues.timeSeries[freq];
  const keys = [...new Set([...d.labels, ...i.labels])].sort();
  const dCreated = Object.fromEntries(d.labels.map((k, idx) => [k, d.created[idx]]));
  const iCreated = Object.fromEntries(i.labels.map((k, idx) => [k, i.created[idx]]));
  const dClosed  = Object.fromEntries(d.labels.map((k, idx) => [k, d.closed[idx]]));
  const iClosed  = Object.fromEntries(i.labels.map((k, idx) => [k, i.closed[idx]]));
  const net = [], cumulative = [];
  let running = 0;
  keys.forEach(k => {
    const created = (dCreated[k] || 0) + (iCreated[k] || 0);
    const closed  = (dClosed[k]  || 0) + (iClosed[k]  || 0);
    const n = created - closed;
    running += n;
    net.push(n);
    cumulative.push(running);
  });
  return { labels: keys, net, cumulative };
}
function makeBacklogChart(canvasId, v, freq) {
  const s = combinedBacklogSeries(v, freq);
  return new Chart(document.getElementById(canvasId).getContext("2d"), {
    data: {
      labels: s.labels,
      datasets: [
        { type: "bar",  label: "Net Change (opened − closed)", data: s.net,
          backgroundColor: s.net.map(n => n >= 0 ? "rgba(207,34,46,.55)" : "rgba(26,127,55,.55)"),
          borderColor: s.net.map(n => n >= 0 ? "rgba(207,34,46,.9)" : "rgba(26,127,55,.9)"),
          borderWidth: 1, borderRadius: 2, yAxisID: "y" },
        { type: "line", label: "Open Backlog Size", data: s.cumulative,
          borderColor: "rgba(191,135,0,.9)", backgroundColor: "rgba(191,135,0,.15)", borderWidth: 2, tension: .25, pointRadius: 2, yAxisID: "y1" },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: "top" } },
      scales: {
        y:  { ticks: { stepSize: 1, precision: 0 }, position: "left",
              title: { display: true, text: "Net change" } },
        y1: { beginAtZero: true, position: "right", grid: { drawOnChartArea: false },
              title: { display: true, text: "Backlog size" } },
      },
    },
  });
}
function updateBacklogChart(chart, v, freq) {
  const s = combinedBacklogSeries(v, freq);
  chart.data.labels = s.labels;
  chart.data.datasets[0].data = s.net;
  chart.data.datasets[0].backgroundColor = s.net.map(n => n >= 0 ? "rgba(207,34,46,.55)" : "rgba(26,127,55,.55)");
  chart.data.datasets[0].borderColor = s.net.map(n => n >= 0 ? "rgba(207,34,46,.9)" : "rgba(26,127,55,.9)");
  chart.data.datasets[1].data = s.cumulative;
  chart.update();
}

/* ── Chart: stacked contributor bar ── */
const CONTRIB_CHART_TOP_N = 25;
let contribChartExpanded = false;

function updateContribMoreBtn(contributors) {
  const btn = document.getElementById("contrib-chart-more-btn");
  const rest = contributors.slice(CONTRIB_CHART_TOP_N);
  if (rest.length <= 0) {
    btn.style.display = "none";
    return;
  }
  const restTotal = rest.reduce((s, c) => s + c.total, 0);
  btn.style.display = "";
  btn.textContent = contribChartExpanded
    ? "Show top 25 only"
    : `${rest.length} other contributors (${restTotal.toLocaleString()} contribs)`;
  document.getElementById("contrib-chart-scope").textContent = contribChartExpanded ? "All" : "Top 25";
}
function toggleContribChart() {
  contribChartExpanded = !contribChartExpanded;
  updateContribChart(charts.contrib, getView().contributors.contributors);
}
function makeContribChart(canvasId, contributors) {
  const top  = contribChartExpanded ? contributors : contributors.slice(0, CONTRIB_CHART_TOP_N);
  const wrap = document.getElementById(canvasId).parentElement;
  wrap.style.height = Math.max(300, top.length * 28 + 80) + "px";
  updateContribMoreBtn(contributors);
  return new Chart(document.getElementById(canvasId).getContext("2d"), {
    type: "bar",
    data: {
      labels: top.map(x => x.user),
      datasets: [
        { label:"💬 Discussions Opened",  data: top.map(x=>x.discussionsOpened),  backgroundColor:"rgba(9,105,218,.85)" },
        { label:"💬 Discussion Comments", data: top.map(x=>x.discussionComments), backgroundColor:"rgba(9,105,218,.35)" },
        { label:"🐛 Issues Opened",       data: top.map(x=>x.issuesOpened),       backgroundColor:"rgba(207,34,46,.85)" },
        { label:"🐛 Issue Comments",      data: top.map(x=>x.issueComments),      backgroundColor:"rgba(207,34,46,.35)" },
      ],
    },
    options: {
      indexAxis:"y", responsive:true, maintainAspectRatio:false,
      plugins:{ legend:{ position:"top" } },
      scales:{
        x:{ stacked:true, beginAtZero:true, ticks:{ stepSize:1, precision:0 } },
        y:{ stacked:true },
      },
    },
  });
}
function updateContribChart(chart, contributors) {
  const top  = contribChartExpanded ? contributors : contributors.slice(0, CONTRIB_CHART_TOP_N);
  const wrap = chart.canvas.parentElement;
  wrap.style.height = Math.max(300, top.length * 28 + 80) + "px";
  updateContribMoreBtn(contributors);
  chart.data.labels = top.map(x => x.user);
  [top.map(x=>x.discussionsOpened), top.map(x=>x.discussionComments),
   top.map(x=>x.issuesOpened),      top.map(x=>x.issueComments)]
    .forEach((d, i) => { chart.data.datasets[i].data = d; });
  chart.resize();
  chart.update();
}

/* ── Chart: contributors by company ── */
function makeCompanyChart(canvasId, byCompany) {
  // Sort by contributions descending, take top 15
  const sorted = [...byCompany].sort((a, b) => b.contributions - a.contributions).slice(0, 15);
  const canvas = document.getElementById(canvasId);
  const wrap = canvas.parentElement;
  // Set height based on number of companies (30px per entry + padding)
  wrap.style.height = Math.max(200, sorted.length * 35 + 40) + "px";
  
  return new Chart(canvas.getContext("2d"), {
    type: "bar",
    data: {
      labels: sorted.map(x => esc(x.company)),
      datasets: [{
        label: "Total Contributions",
        data: sorted.map(x => x.contributions),
        backgroundColor: "rgba(9, 105, 218, 0.75)",
        borderColor: "rgba(9, 105, 218, 0.9)",
        borderWidth: 1,
        borderRadius: 4,
      }],
    },
    options: {
      indexAxis: "y",
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: function(context) {
              return context.parsed.x + " contributions";
            }
          }
        }
      },
      scales: {
        x: {
          beginAtZero: true,
          ticks: { stepSize: 1, precision: 0 }
        },
        y: {
          stacked: false,
          // Ensure all labels are visible
          ticks: {
            autoSkip: false,
            maxRotation: 0,
            minRotation: 0
          }
        },
      },
    },
  });
}
function updateCompanyChart(chart, byCompany) {
  const sorted = [...byCompany].sort((a, b) => b.contributions - a.contributions).slice(0, 15);
  const canvas = document.getElementById(chart.canvas.id);
  const wrap = canvas.parentElement;
  wrap.style.height = Math.max(200, sorted.length * 35 + 40) + "px";
  chart.data.labels = sorted.map(x => esc(x.company));
  chart.data.datasets[0].data = sorted.map(x => x.contributions);
  chart.options.scales.y.ticks.autoSkip = false;
  chart.update();
}

/* ── Chart: stacked closed-by-label over time ── */
const LABEL_COLORS = ["#0969da","#1a7f37","#cf222e","#8250df","#bf8700","#bc4c00","#1b7c83","#953800","#6e7781"];
function colorForIndex(i, alpha) {
  const hex = LABEL_COLORS[i % LABEL_COLORS.length];
  const r = parseInt(hex.slice(1,3),16), g = parseInt(hex.slice(3,5),16), b = parseInt(hex.slice(5,7),16);
  return `rgba(${r},${g},${b},${alpha === undefined ? 0.75 : alpha})`;
}
function labelDatasets(freqData) {
  return freqData.labelNames.map((name, i) => ({
    label: name, data: freqData.series[name],
    backgroundColor: colorForIndex(i), borderRadius: 2,
  }));
}
function makeStackedLabelChart(canvasId, ts) {
  return new Chart(document.getElementById(canvasId).getContext("2d"), {
    type: "bar",
    data: { labels: ts.daily.labels, datasets: labelDatasets(ts.daily) },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: "top" } },
      scales: {
        x: { stacked: true },
        y: { stacked: true, beginAtZero: true, ticks: { stepSize: 1, precision: 0 } },
      },
    },
  });
}
function updateStackedLabelChart(chart, ts, freq) {
  chart.data.labels = ts[freq].labels;
  chart.data.datasets = labelDatasets(ts[freq]);
  chart.update();
}
function refreshClosedByLabelChart(v) {
  const cbl = v.issues.closedByLabel;
  const hasData = !!(cbl && cbl.daily.labelNames.length &&
    cbl.daily.labelNames.some(n => cbl.daily.series[n].some(c => c > 0)));
  const card = document.getElementById("issueClosedByLabelCard");
  card.style.display = hasData ? "" : "none";
  if (!hasData) return;
  if (!charts.issueClosedByLabel) {
    charts.issueClosedByLabel = makeStackedLabelChart("issueClosedByLabelChart", cbl);
  }
  updateStackedLabelChart(charts.issueClosedByLabel, cbl, activeFreq.issue);
}

/* ── Freq toggle listeners ── */
function setFreq(prefix, freq) {
  activeFreq[prefix] = freq;
  const v  = getView();
  if (prefix === "contrib") {
    updateActivityChart(charts.activityOverTime, v, freq);
    updateGrowthChart(charts.contribGrowth, v.contributors.growth, freq);
    updateBacklogChart(charts.backlogGrowth, v, freq);
    return;
  }
  if (prefix === "releases") {
    updateReleasesTimeChart(charts.releasesTime, v.releases.timeSeries, freq);
    return;
  }
  const ts = prefix === "disc" ? v.discussions.timeSeries : v.issues.timeSeries;
  updateTimeChart(prefix === "disc" ? charts.discTime : charts.issueTime, ts, freq);
  if (prefix === "issue" && charts.issueClosedByLabel) {
    updateStackedLabelChart(charts.issueClosedByLabel, v.issues.closedByLabel, freq);
  }
}
["disc","issue","contrib","releases"].forEach(p => {
  document.getElementById(`${p}-freqBtns`).addEventListener("click", e => {
    const btn = e.target.closest(".toggle-btn"); if (!btn) return;
    document.querySelectorAll(`#${p}-freqBtns .toggle-btn`).forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    setFreq(p, btn.dataset.freq);
  });
});

/* ── Labels chart ── */
function refreshLabelsChart(v) {
  const tl = v.issues.topLabels || [];
  if (tl.length > 0) {
    document.getElementById("issueLabelsCard").style.display = "";
    document.getElementById("issue-charts-row").classList.add("three");
    if (!charts.issueLabels) {
      charts.issueLabels = makeHBar("issueLabelsChart", tl.map(x=>x.label), tl.map(x=>x.count), "rgba(251,140,0,.75)");
    } else {
      updateHBar(charts.issueLabels, tl.map(x=>x.label), tl.map(x=>x.count));
    }
  } else {
    document.getElementById("issueLabelsCard").style.display = "none";
    document.getElementById("issue-charts-row").classList.remove("three");
  }
}

/* ── Repo switch ── */
function switchRepo(repo) {
  activeRepo = repo;
  const v = getView();
  contribChartExpanded = false;
  updatesState.labels.clear();
  updatesState.closeReason = "all";
  updateAllStats(v);
  // Charts
  updateActivityChart(charts.activityOverTime, v, activeFreq.contrib);
  updateGrowthChart(charts.contribGrowth, v.contributors.growth, activeFreq.contrib);
  updateBacklogChart(charts.backlogGrowth, v, activeFreq.contrib);
  updateReleasesTimeChart(charts.releasesTime, v.releases.timeSeries, activeFreq.releases);
  updateTimeChart(charts.discTime,    v.discussions.timeSeries, activeFreq.disc);
  updateTimeChart(charts.issueTime,   v.issues.timeSeries,      activeFreq.issue);
  updateHBar(charts.discStart,    v.discussions.topStarters.map(x=>x.user),   v.discussions.topStarters.map(x=>x.count));
  updateHBar(charts.discComment,  v.discussions.topCommenters.map(x=>x.user), v.discussions.topCommenters.map(x=>x.count));
  updateHBar(charts.issueStart,   v.issues.topStarters.map(x=>x.user),        v.issues.topStarters.map(x=>x.count));
  updateHBar(charts.issueComment, v.issues.topCommenters.map(x=>x.user),      v.issues.topCommenters.map(x=>x.count));
  refreshLabelsChart(v);
  refreshClosedByLabelChart(v);
  updateContribChart(charts.contrib, v.contributors.contributors);
  if (charts.contribByCompany && v.contributors.byCompany) {
    updateCompanyChart(charts.contribByCompany, v.contributors.byCompany);
  }
  refreshUpdates();
  // Tables
  resetTables(v);
}

/* ── Table reset ── */
function resetTables(v) {
  tbl.disc.filtered     = v.discussions.recent;   tbl.disc.page    = 0;
  tbl.issue.filtered    = v.issues.recent;        tbl.issue.page   = 0;
  tbl.contrib.filtered  = [...v.contributors.contributors]; tbl.contrib.page = 0; tbl.contrib.expanded = false;
  tbl.releases.filtered = v.releases.releases;    tbl.releases.page = 0;
  ["disc","issue"].forEach(p => {
    document.getElementById(`${p}-search`).value = "";
    document.getElementById(`${p}-status`).value = "all";
  });
  document.getElementById("contrib-search").value = "";
  document.getElementById("releases-search").value = "";
  renderTable("disc");
  renderTable("issue");
  renderContribHead();
  renderContribTable();
  renderReleasesTable();
}

/* ── Item table (disc / issue) ── */
function applyFilter(prefix) {
  const q      = document.getElementById(`${prefix}-search`).value.toLowerCase();
  const status = document.getElementById(`${prefix}-status`).value;
  const src    = prefix === "disc" ? getView().discussions.recent : getView().issues.recent;
  tbl[prefix].filtered = src.filter(item => {
    if (status === "open"   &&  item.closedAt) return false;
    if (status === "closed" && !item.closedAt) return false;
    if (q) {
      const hit = item.title.toLowerCase().includes(q)
               || item.author.toLowerCase().includes(q)
               || (item.labels||[]).some(l => l.name.toLowerCase().includes(q))
               || (item.repo||"").toLowerCase().includes(q);
      if (!hit) return false;
    }
    return true;
  });
  tbl[prefix].page = 0;
  renderTable(prefix);
}

function renderTable(prefix) {
  const { filtered, page } = tbl[prefix];
  const start   = page * PAGE_SIZE;
  const slice   = filtered.slice(start, start + PAGE_SIZE);
  const urlSeg  = prefix === "disc" ? "discussions" : "issues";
  const isIssue = prefix === "issue";
  const rc      = showRepoCol();

  document.getElementById(`${prefix}-thead`).innerHTML = `<tr>
    <th>#</th><th>Title</th><th>Author</th>
    ${rc ? "<th>Repo</th>" : ""}
    <th style="text-align:right">Comments</th>
    ${isIssue ? "<th>Labels</th>" : ""}
    <th>Created</th><th>Closed</th><th>Status</th>
  </tr>`;

  document.getElementById(`${prefix}-countInfo`).textContent =
    `Showing ${filtered.length ? start+1 : 0}–${Math.min(start+slice.length, filtered.length)} of ${filtered.length}`;

  document.getElementById(`${prefix}-tbody`).innerHTML = slice.map(item => `
    <tr>
      <td><a href="https://github.com/${esc(item.repo)}/${urlSeg}/${item.number}" target="_blank">#${item.number}</a></td>
      <td class="td-title"><a href="https://github.com/${esc(item.repo)}/${urlSeg}/${item.number}" target="_blank" title="${esc(item.title)}">${esc(item.title)}</a></td>
      <td><a href="https://github.com/${esc(item.author)}" target="_blank">${esc(item.author)}</a></td>
      ${rc ? `<td class="td-repo"><a href="https://github.com/${esc(item.repo)}" target="_blank">${esc(item.repo)}</a></td>` : ""}
      <td style="text-align:right">${item.commentCount}</td>
      ${isIssue ? `<td class="td-labels">${renderLabelBadges(item.labels)}</td>` : ""}
      <td style="white-space:nowrap">${fmtDate(item.createdAt)}</td>
      <td style="white-space:nowrap">${fmtDate(item.closedAt)}</td>
      <td><span class="badge ${item.closedAt?"closed":"open"}">${item.closedAt?"Closed":"Open"}</span></td>
    </tr>`).join("");

  renderPager(`${prefix}-pager`, filtered.length, page,
    prefix === "disc" ? "goDiscPage" : "goIssuePage");
}

function goDiscPage(n)  { tbl.disc.page  = n; renderTable("disc");  scrollTab("tab-discussions"); }
function goIssuePage(n) { tbl.issue.page = n; renderTable("issue"); scrollTab("tab-issues"); }
function scrollTab(id)  { document.getElementById(id).querySelector(".card:last-child").scrollIntoView({behavior:"smooth",block:"start"}); }

/* ── Releases tab ── */
function makeReleasesTimeChart(canvasId, ts, freq) {
  return new Chart(document.getElementById(canvasId).getContext("2d"), {
    type: "bar",
    data: {
      labels: ts[freq].labels,
      datasets: [
        { label: "Releases", data: ts[freq].count, backgroundColor: "rgba(191,135,0,.6)", borderColor: "rgba(191,135,0,.9)", borderWidth: 1, borderRadius: 2 },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: { y: { beginAtZero: true, ticks: { stepSize: 1, precision: 0 } } },
    },
  });
}
function updateReleasesTimeChart(chart, ts, freq) {
  chart.data.labels = ts[freq].labels;
  chart.data.datasets[0].data = ts[freq].count;
  chart.update();
}
function applyReleasesFilter() {
  const q   = document.getElementById("releases-search").value.toLowerCase();
  const src = getView().releases.releases;
  tbl.releases.filtered = !q ? src : src.filter(r =>
    r.name.toLowerCase().includes(q)
    || (r.tagName||"").toLowerCase().includes(q)
    || r.repos.some(x => (x.author||"").toLowerCase().includes(q) || (x.repo||"").toLowerCase().includes(q)));
  tbl.releases.page = 0;
  renderReleasesTable();
}
function renderReleasesTable() {
  const { filtered, page } = tbl.releases;
  const start = page * PAGE_SIZE;
  const slice = filtered.slice(start, start + PAGE_SIZE);
  const rc    = showRepoCol();

  document.getElementById("releases-thead").innerHTML = `<tr>
    <th>Name</th><th>Tag</th>
    ${rc ? "<th>Repo</th>" : ""}
    <th>Author</th><th>Published</th><th>Flags</th>
  </tr>`;

  document.getElementById("releases-countInfo").textContent =
    `Showing ${filtered.length ? start+1 : 0}–${Math.min(start+slice.length, filtered.length)} of ${filtered.length}`;

  document.getElementById("releases-tbody").innerHTML = slice.map(r => {
    const flags = [r.isPrerelease ? "Pre-release" : "", r.isDraft ? "Draft" : ""].filter(Boolean).join(", ") || "—";
    const primary = r.repos[0];
    const repoLinks = r.repos.map(x =>
      `<a href="https://github.com/${esc(x.repo)}" target="_blank">${esc(x.repo)}</a>`).join("<br>");
    const authorLinks = [...new Set(r.repos.map(x => x.author))].map(a =>
      `<a href="https://github.com/${esc(a)}" target="_blank">${esc(a)}</a>`).join("<br>");
    return `
    <tr>
      <td class="td-title"><a href="${esc(primary.url)}" target="_blank" title="${esc(r.name)}">${esc(r.name)}</a>${r.repos.length > 1 ? ` <span class="badge" title="Released in ${r.repos.length} repos">×${r.repos.length}</span>` : ""}</td>
      <td>${esc(r.tagName || "—")}</td>
      ${rc ? `<td class="td-repo">${repoLinks}</td>` : ""}
      <td>${authorLinks}</td>
      <td style="white-space:nowrap">${fmtDate(r.publishedAt || r.createdAt)}</td>
      <td>${flags}</td>
    </tr>`;
  }).join("");

  renderPager("releases-pager", filtered.length, page, "goReleasesPage");
}
function goReleasesPage(n) { tbl.releases.page = n; renderReleasesTable(); scrollTab("tab-releases"); }

/* ── Updates tab ── */
function rangeCutoff(range) {
  const days = range === "month" ? 30 : 7;
  const d = new Date();
  d.setUTCDate(d.getUTCDate() - days);
  return d;
}
function setUpdatesRange(range) {
  updatesState.range = range;
  document.querySelectorAll("#updates-range-btns .toggle-btn")
    .forEach(b => b.classList.toggle("active", b.dataset.range === range));
  refreshUpdates();
}
function toggleUpdateLabel(name) {
  if (updatesState.labels.has(name)) updatesState.labels.delete(name);
  else updatesState.labels.add(name);
  refreshUpdates();
}
function clearUpdateLabels() {
  updatesState.labels.clear();
  refreshUpdates();
}
function setUpdatesStatus(reason) {
  updatesState.closeReason = reason;
  refreshUpdates();
}
function refreshUpdates() {
  const cutoff  = rangeCutoff(updatesState.range);
  const inRange = getView().updates.closedItems.filter(it => new Date(it.closedAt) >= cutoff);

  const labelCounts = new Map();
  inRange.forEach(it => (it.labels || []).forEach(l =>
    labelCounts.set(l.name, (labelCounts.get(l.name) || 0) + 1)));
  const chipLabels = [...labelCounts.entries()].sort((a, b) => b[1] - a[1]);

  // Drop selected labels that no longer apply in this range.
  [...updatesState.labels].forEach(l => { if (!labelCounts.has(l)) updatesState.labels.delete(l); });

  const chipsEl = document.getElementById("updates-label-chips");
  chipsEl.innerHTML = chipLabels.length
    ? chipLabels.map(([name, count]) => `
        <button class="toggle-btn label-chip${updatesState.labels.has(name) ? " active" : ""}"
                data-label="${esc(name)}">${esc(name)} (${count})</button>`).join("")
      + (updatesState.labels.size ? `<button class="toggle-btn label-chip" data-clear="1">Clear filters</button>` : "")
    : `<span style="font-size:.8rem;opacity:.8">No labels on closed items in this range</span>`;

  // Close-reason (status) chips, single-select, based on the same time range.
  const reasonCounts = new Map();
  inRange.forEach(it => {
    const key = it.closeReason || "__none__";
    reasonCounts.set(key, (reasonCounts.get(key) || 0) + 1);
  });
  if (!reasonCounts.has(updatesState.closeReason) && updatesState.closeReason !== "all") {
    updatesState.closeReason = "all";
  }
  const reasonEntries = [...reasonCounts.entries()].sort((a, b) => b[1] - a[1]);
  const statusEl = document.getElementById("updates-status-btns");
  statusEl.innerHTML = `<button class="toggle-btn status-chip${updatesState.closeReason === "all" ? " active" : ""}"
                                 data-reason="all">All (${inRange.length})</button>`
    + reasonEntries.map(([reason, count]) => {
        const meta = CLOSE_REASON_META[reason] || { text: esc(reason) };
        return `<button class="toggle-btn status-chip${updatesState.closeReason === reason ? " active" : ""}"
                        data-reason="${esc(reason)}">${meta.text} (${count})</button>`;
      }).join("");

  const byLabel = updatesState.labels.size
    ? inRange.filter(it => (it.labels || []).some(l => updatesState.labels.has(l.name)))
    : inRange;
  const filtered = updatesState.closeReason === "all"
    ? byLabel
    : byLabel.filter(it => (it.closeReason || "__none__") === updatesState.closeReason);

  const authorFiltered = updatesState.author === "me" && PAYLOAD.githubUser
    ? filtered.filter(it => it.author === PAYLOAD.githubUser)
    : filtered;

  document.getElementById("updates-hero-count").textContent = authorFiltered.length.toLocaleString();
  document.getElementById("updates-hero-range-label").textContent =
    updatesState.range === "month" ? "in the last 30 days" : "in the last 7 days";
  document.getElementById("updates-tab-count").textContent = filtered.length;

  tbl.updates.filtered = authorFiltered;
  tbl.updates.page = 0;
  renderUpdatesTable();
}
function renderUpdatesTable() {
  const { filtered, page } = tbl.updates;
  const start = page * PAGE_SIZE;
  const slice = filtered.slice(start, start + PAGE_SIZE);
  const rc    = showRepoCol();

  document.getElementById("updates-thead").innerHTML = `<tr>
    <th>Type</th><th>#</th><th>Title</th><th>Author</th>
    ${rc ? "<th>Repo</th>" : ""}
    <th style="text-align:right">Comments</th><th>Labels</th><th>Closed As</th><th>Closed</th>
  </tr>`;

  document.getElementById("updates-countInfo").textContent =
    `Showing ${filtered.length ? start+1 : 0}–${Math.min(start+slice.length, filtered.length)} of ${filtered.length}`;

  document.getElementById("updates-tbody").innerHTML = slice.map(item => {
    const urlSeg = item.type === "discussion" ? "discussions" : "issues";
    const icon   = item.type === "discussion" ? "💬" : "🐛";
    const reasonMeta = CLOSE_REASON_META[item.closeReason || "__none__"] || { text: esc(item.closeReason) };
    return `
    <tr>
      <td style="white-space:nowrap">${icon} ${item.type === "discussion" ? "Discussion" : "Issue"}</td>
      <td><a href="https://github.com/${esc(item.repo)}/${urlSeg}/${item.number}" target="_blank">#${item.number}</a></td>
      <td class="td-title"><a href="https://github.com/${esc(item.repo)}/${urlSeg}/${item.number}" target="_blank" title="${esc(item.title)}">${esc(item.title)}</a></td>
      <td><a href="https://github.com/${esc(item.author)}" target="_blank">${esc(item.author)}</a></td>
      ${rc ? `<td class="td-repo"><a href="https://github.com/${esc(item.repo)}" target="_blank">${esc(item.repo)}</a></td>` : ""}
      <td style="text-align:right">${item.commentCount}</td>
      <td class="td-labels">${renderLabelBadges(item.labels)}</td>
      <td style="white-space:nowrap">${reasonMeta.text}</td>
      <td style="white-space:nowrap">${fmtDate(item.closedAt)}</td>
    </tr>`;
  }).join("");

  renderPager("updates-pager", filtered.length, page, "goUpdatesPage");
}
function goUpdatesPage(n) { tbl.updates.page = n; renderUpdatesTable(); scrollTab("tab-updates"); }

/* ── Contributor table ── */
function applyContribFilter() {
  const q = document.getElementById("contrib-search").value.toLowerCase();
  const src = getView().contributors.contributors;
  tbl.contrib.filtered = q ? src.filter(c => 
    c.user.toLowerCase().includes(q) ||
    (c.name && c.name.toLowerCase().includes(q)) ||
    (c.company && c.company.toLowerCase().includes(q)) ||
    (c.bio && c.bio.toLowerCase().includes(q))
  ) : [...src];
  sortContribData();
  tbl.contrib.page = 0;
  tbl.contrib.expanded = false;
  renderContribTable();
}

function sortContrib(col) {
  tbl.contrib.sortDir = tbl.contrib.sortCol === col ? -tbl.contrib.sortDir : (col === "user" ? 1 : -1);
  tbl.contrib.sortCol = col;
  sortContribData();
  tbl.contrib.page = 0;
  renderContribHead();
  renderContribTable();
}

function sortContribData() {
  const { sortCol, sortDir } = tbl.contrib;
  tbl.contrib.filtered.sort((a, b) => {
    const av = a[sortCol], bv = b[sortCol];
    // Handle null/undefined values - sort them last
    if (av === null || av === undefined) return sortDir * 1;
    if (bv === null || bv === undefined) return sortDir * -1;
    return typeof av === "string" ? sortDir * av.localeCompare(bv) : sortDir * (av - bv);
  });
}

function renderContribHead() {
  const { sortCol, sortDir } = tbl.contrib;
  const th = (col, label, align) => {
    const active = sortCol === col;
    const arrow  = active ? (sortDir === 1 ? " ↑" : " ↓") : "";
    return `<th class="sortable${active?" sort-active":""}" data-col="${col}"
              style="${align?`text-align:${align}`:""}" onclick="sortContrib('${col}')">
              ${label}${arrow}</th>`;
  };
  document.getElementById("contrib-thead").innerHTML = `<tr>
    <th>Rank</th>
    ${th("user","User")}
    ${th("company","Company")}
    ${th("discussionsOpened",  "💬 Disc. Opened",   "right")}
    ${th("discussionComments", "💬 Disc. Comments", "right")}
    ${th("issuesOpened",       "🐛 Issues Opened",  "right")}
    ${th("issueComments",      "🐛 Issue Comments", "right")}
    ${th("total",              "Total",             "right")}
  </tr>`;
}

function renderContribTable() {
  // Build rank map from the current view's original total-desc order
  const rankMap = {};
  getView().contributors.contributors.forEach((c, i) => { rankMap[c.user] = i + 1; });

  const { filtered, page, expanded } = tbl.contrib;
  let start, slice, otherRowHtml = "";

  if (!expanded) {
    start = 0;
    slice = filtered.slice(0, CONTRIB_TABLE_COLLAPSED_N);
    const remainder = filtered.length - slice.length;
    if (remainder > 0) {
      otherRowHtml = `<tr class="other-row" onclick="expandContribTable()" style="cursor:pointer">
        <td colspan="8" style="text-align:center;color:var(--accent);font-weight:600">
          Other (${remainder} more) — click to show all
        </td>
      </tr>`;
    }
  } else {
    start = page * PAGE_SIZE;
    slice = filtered.slice(start, start + PAGE_SIZE);
  }

  document.getElementById("contrib-countInfo").textContent = expanded
    ? `Showing ${filtered.length ? start+1 : 0}–${Math.min(start+slice.length, filtered.length)} of ${filtered.length}`
    : `Showing top ${slice.length} of ${filtered.length}`;
  document.getElementById("contrib-collapse-btn").style.display = expanded ? "" : "none";

  document.getElementById("contrib-tbody").innerHTML = slice.map(c => {
    const company = c.company ? esc(c.company) : "–";
    const companyTitle = c.company ? `title="${esc(c.company)}"` : "";
    return `
    <tr>
      <td style="color:var(--muted);font-weight:600">#${rankMap[c.user]||"–"}</td>
      <td><a href="https://github.com/${esc(c.user)}" target="_blank">${esc(c.user)}</a></td>
      <td ${companyTitle}>${company}</td>
      <td style="text-align:right">${c.discussionsOpened}</td>
      <td style="text-align:right">${c.discussionComments}</td>
      <td style="text-align:right">${c.issuesOpened}</td>
      <td style="text-align:right">${c.issueComments}</td>
      <td style="text-align:right;font-weight:600">${c.total}</td>
    </tr>`;}).join("") + otherRowHtml;

  renderPager("contrib-pager", expanded ? filtered.length : 0, page, "goContribPage");
}

function expandContribTable() {
  tbl.contrib.expanded = true;
  tbl.contrib.page = 0;
  renderContribTable();
}
function collapseContribTable() {
  tbl.contrib.expanded = false;
  tbl.contrib.page = 0;
  renderContribTable();
}

function goContribPage(n) {
  tbl.contrib.page = n;
  renderContribTable();
  scrollTab("tab-contributors");
}

/* ── Shared pager ── */
function renderPager(id, total, current, fnName) {
  const pager = document.getElementById(id);
  const pages = Math.ceil(total / PAGE_SIZE);
  if (pages <= 1) { pager.innerHTML = ""; return; }
  let html = "";
  for (let i = 0; i < pages; i++) {
    if (i===0 || i===pages-1 || Math.abs(i-current)<=2) {
      html += `<button class="pager-btn ${i===current?"active":""}" onclick="${fnName}(${i})">${i+1}</button>`;
    } else if (Math.abs(i-current)===3) {
      html += `<span style="padding:.3rem .3rem;color:var(--muted)">…</span>`;
    }
  }
  pager.innerHTML = html;
}

/* ── Bootstrap ── */
(function init() {
  initRepoSwitcher();
  const v = getView();

  charts.activityOverTime = makeActivityChart("activityOverTimeChart", v, activeFreq.contrib);
  charts.contribGrowth = makeGrowthChart("contribGrowthChart", v.contributors.growth);
  charts.backlogGrowth = makeBacklogChart("backlogGrowthChart", v, activeFreq.contrib);
  charts.releasesTime = makeReleasesTimeChart("releasesTimeChart", v.releases.timeSeries, activeFreq.releases);
  charts.discTime    = makeTimeChart("discTimeChart",   v.discussions.timeSeries);
  charts.issueTime   = makeTimeChart("issueTimeChart",  v.issues.timeSeries);
  charts.discStart   = makeHBar("discStartersChart",   v.discussions.topStarters.map(x=>x.user),   v.discussions.topStarters.map(x=>x.count),   "rgba(9,105,218,.7)");
  charts.discComment = makeHBar("discCommentersChart", v.discussions.topCommenters.map(x=>x.user), v.discussions.topCommenters.map(x=>x.count), "rgba(130,80,223,.7)");
  charts.issueStart  = makeHBar("issueStartersChart",  v.issues.topStarters.map(x=>x.user),        v.issues.topStarters.map(x=>x.count),        "rgba(9,105,218,.7)");
  charts.issueComment= makeHBar("issueCommentersChart",v.issues.topCommenters.map(x=>x.user),      v.issues.topCommenters.map(x=>x.count),      "rgba(130,80,223,.7)");
  charts.contrib     = makeContribChart("contribChart", v.contributors.contributors);
  if (v.contributors.byCompany && v.contributors.byCompany.length > 0) {
    charts.contribByCompany = makeCompanyChart("contribByCompanyChart", v.contributors.byCompany);
  }
  refreshLabelsChart(v);
  refreshClosedByLabelChart(v);

  document.getElementById("updates-range-btns").addEventListener("click", e => {
    const btn = e.target.closest(".toggle-btn"); if (!btn) return;
    setUpdatesRange(btn.dataset.range);
  });
  document.getElementById("updates-label-chips").addEventListener("click", e => {
    const btn = e.target.closest(".label-chip"); if (!btn) return;
    if (btn.dataset.clear) { clearUpdateLabels(); return; }
    toggleUpdateLabel(btn.dataset.label);
  });
  document.getElementById("updates-status-btns").addEventListener("click", e => {
    const btn = e.target.closest(".status-chip"); if (!btn) return;
    setUpdatesStatus(btn.dataset.reason);
  });
  if (PAYLOAD.githubUser) {
    document.getElementById("updates-author-section").style.display = "";
    document.getElementById("updates-author-btns").addEventListener("click", e => {
      const btn = e.target.closest(".toggle-btn"); if (!btn) return;
      document.querySelectorAll("#updates-author-btns .toggle-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      updatesState.author = btn.dataset.author;
      refreshUpdates();
    });
  }
  refreshUpdates();

  updateAllStats(v);
  tbl.disc.filtered     = v.discussions.recent;
  tbl.issue.filtered    = v.issues.recent;
  tbl.contrib.filtered  = [...v.contributors.contributors];
  tbl.releases.filtered = v.releases.releases;
  renderTable("disc");
  renderTable("issue");
  renderContribHead();
  renderContribTable();
  renderReleasesTable();

  const initialTab = location.hash.slice(1);
  if (initialTab && VALID_TABS.includes(initialTab)) switchTab(initialTab, false);
})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTML generation + Main
# ---------------------------------------------------------------------------

def generate_html(repos, fetched_at, payload, output_path):
    title = repos[0] if len(repos) == 1 else f"{repos[0]} (+{len(repos)-1} more)"
    payload_json = json.dumps(payload, separators=(",", ":"))
    html = (HTML_TEMPLATE
            .replace("TMPL_REPO_TITLE", title)
            .replace("TMPL_FETCHED_AT", fetched_at)
            .replace("TMPL_PAYLOAD_JSON", payload_json))
    Path(output_path).write_text(html, encoding="utf-8")


def load_env():
    env_path = Path(".env")
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ[key.strip()] = val.strip().strip("'\"")


def main():
    load_env()
    ap = argparse.ArgumentParser(
        description="Analyze GitHub Discussions & Issues across one or more repos."
    )
    ap.add_argument("repos", nargs="+", help="One or more repositories in owner/repo format")
    ap.add_argument("--refresh", action="store_true", help="Re-fetch all data, ignoring cache")
    ap.add_argument("--fetch-profiles", action="store_true", help="Fetch user profiles (company, name, etc.) - may take time for many users")
    args = ap.parse_args()

    for repo in args.repos:
        if "/" not in repo:
            print(f"Error: {repo!r} must be in owner/repo format", file=sys.stderr)
            sys.exit(1)

    data_dir = Path("data")
    data_dir.mkdir(exist_ok=True)
    reports_dir = Path("reports")
    reports_dir.mkdir(exist_ok=True)

    all_discussions, all_issues, all_releases = [], [], []
    repos_data = {}
    last_fetched_at = "unknown"

    for repo in args.repos:
        owner, repo_name = repo.split("/", 1)
        cache = data_dir / f"{owner}_{repo_name}.json"

        if args.refresh or not cache.exists():
            print(f"Fetching discussions for {repo} …")
            discussions = fetch_discussions(owner, repo_name)
            print(f"Fetching issues for {repo} …")
            issues = fetch_issues(owner, repo_name)
            print(f"Fetching releases for {repo} …")
            releases = fetch_releases(owner, repo_name)
            fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            cache.write_text(json.dumps(
                {"repo": repo, "fetchedAt": fetched_at, "discussions": discussions,
                 "issues": issues, "releases": releases},
                indent=2), encoding="utf-8")
            print(f"  Cached {len(discussions)} discussions + {len(issues)} issues + {len(releases)} releases -> {cache}")
        else:
            cached = json.loads(cache.read_text(encoding="utf-8"))
            discussions = cached.get("discussions", [])
            issues      = cached.get("issues")
            releases    = cached.get("releases")
            fetched_at  = cached.get("fetchedAt", "unknown")
            backfilled  = False

            if issues is None:
                print(f"Loaded {len(discussions)} cached discussions for {repo}.")
                print(f"Fetching issues for {repo} …")
                issues = fetch_issues(owner, repo_name)
                fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                cached.update({"issues": issues, "fetchedAt": fetched_at})
                cache.write_text(json.dumps(cached, indent=2), encoding="utf-8")
                print(f"  Updated cache with {len(issues)} issues -> {cache}")
                backfilled = True

            if releases is None:
                print(f"Fetching releases for {repo} …")
                releases = fetch_releases(owner, repo_name)
                fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                cached.update({"releases": releases, "fetchedAt": fetched_at})
                cache.write_text(json.dumps(cached, indent=2), encoding="utf-8")
                print(f"  Updated cache with {len(releases)} releases -> {cache}")
                backfilled = True

            if not backfilled:
                print(f"  {repo}: {len(discussions)} discussions, {len(issues)} issues, "
                      f"{len(releases)} releases (cached {fetched_at})")
                if len(args.repos) == 1:
                    print("  Tip: use --refresh to re-fetch live data")

        last_fetched_at = fetched_at

        # Tag items in-memory with their repo (not persisted to cache)
        for d in discussions: d["_repo"] = repo
        for i in issues:      i["_repo"] = repo
        for r in releases:    r["_repo"] = repo

        repos_data[repo] = {"discussions": discussions, "issues": issues, "releases": releases}
        all_discussions.extend(discussions)
        all_issues.extend(issues)
        all_releases.extend(releases)

    print("Analyzing …")

    # Collect all unique user logins from all data
    print("Collecting unique users…")
    all_logins = set()
    for d in all_discussions:
        if d.get("author"):
            all_logins.add(d["author"])
        for c in d.get("comments", []):
            if c:
                all_logins.add(c)
    for i in all_issues:
        if i.get("author"):
            all_logins.add(i["author"])
        for c in i.get("comments", []):
            if c:
                all_logins.add(c)
    for r in all_releases:
        if r.get("author"):
            all_logins.add(r["author"])
    
    print(f"  Found {len(all_logins)} unique users.")
    
    # Load user profiles (cached or fresh)
    user_profiles = {}
    if args.fetch_profiles:
        print("Fetching user profiles (this may take a while for many users)…")
        user_profiles = fetch_user_profiles(list(all_logins), data_dir=data_dir)
    else:
        # Try to load from cache
        user_profiles = load_user_profiles_cache(data_dir)
        if user_profiles:
            print(f"  Using {len(user_profiles)} cached user profiles.")
        else:
            print("  No cached profiles found. Use --fetch-profiles to enable.")

    by_repo = {
        repo: {
            "discussions":  analyze(data["discussions"]),
            "issues":       analyze(data["issues"]),
            "contributors": compute_contributors(data["discussions"], data["issues"], user_profiles),
            "updates":      compute_closed_items(data["discussions"], data["issues"]),
            "releases":     compute_releases(data["releases"]),
        }
        for repo, data in repos_data.items()
    }

    all_view = {
        "discussions":  analyze(all_discussions),
        "issues":       analyze(all_issues),
        "contributors": compute_contributors(all_discussions, all_issues, user_profiles),
        "updates":      compute_closed_items(all_discussions, all_issues),
        "releases":     compute_releases(all_releases),
    }

    github_user = os.getenv("GITHUB_USER", "")
    payload = {"repos": args.repos, "all": all_view, "byRepo": by_repo, "githubUser": github_user}

    safe_name = "+".join(r.replace("/", "_") for r in args.repos)
    out = reports_dir / f"report_{safe_name}.html"
    generate_html(args.repos, last_fetched_at, payload, out)

    print(f"\n[OK] Report saved: {out}")
    for repo in args.repos:
        ds  = by_repo[repo]["discussions"]["summary"]
        is_ = by_repo[repo]["issues"]["summary"]
        rs  = by_repo[repo]["releases"]["summary"]
        print(f"   {repo}: {ds['total']} discussions ({ds['open']} open), "
              f"{is_['total']} issues ({is_['open']} open), {rs['total']} releases")
    cc = all_view["contributors"]["contributors"]
    print(f"   Combined: {len(cc)} unique contributors")
    print(f"\n   Open it with:  start {out}")


if __name__ == "__main__":
    main()
