"""Optional catalog and community collectors. Credentials stay in environment variables."""

import base64
import json
import os
import html
import re
import time
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode
from urllib.error import URLError
from urllib.request import Request, urlopen

from .core import github_url, parse_stamp, request_json, state_get, state_set, upsert


def read_feed(url: str, timeout: int = 30) -> ET.Element:
    request = Request(url, headers={"User-Agent": "agent-radar/0.3 (personal research)",
                                    "Accept": "application/atom+xml, application/rss+xml, application/xml"})
    with urlopen(request, timeout=timeout) as response:
        return ET.fromstring(response.read(2_000_000))


def relevant_data_paper(title: str, abstract: str) -> bool:
    return bool(re.search(r"text.?to.?sql|nl2sql|natural.language.to.sql|data.?agent|agentic.data|enterprise.data|business.intelligence|database.agent|multi.database.agent|sql.agent|semantic.layer", title.lower()))


def collect_arxiv(db: sqlite3.Connection, config: dict, seen_at: str) -> int:
    """Find recent data-agent and Text-to-SQL papers in arXiv's Atom API."""
    queries = config.get("arxiv_queries", [])
    cutoff = parse_stamp(seen_at) - timedelta(days=config.get("paper_lookback_days", 30))
    count = 0
    namespace = "{http://www.w3.org/2005/Atom}"
    for index, query in enumerate(queries):
        if index:
            time.sleep(3.1)
        params = urlencode({"search_query": query, "start": 0,
                            "max_results": config.get("arxiv_per_query", 25),
                            "sortBy": "submittedDate", "sortOrder": "descending"})
        root = read_feed("https://export.arxiv.org/api/query?" + params, timeout=10)
        for entry in root.findall(namespace + "entry"):
            ident = entry.findtext(namespace + "id") or ""
            title = " ".join((entry.findtext(namespace + "title") or "").split())
            published = entry.findtext(namespace + "published") or ""
            if not ident or not title or not published or parse_stamp(published) < cutoff:
                continue
            abstract = " ".join((entry.findtext(namespace + "summary") or "").split())
            if not relevant_data_paper(title, abstract):
                continue
            authors = [" ".join((node.findtext(namespace + "name") or "").split())
                       for node in entry.findall(namespace + "author")]
            paper_url = ident.replace("http://arxiv.org/", "https://arxiv.org/")
            item = {"title": title, "description": abstract[:3500], "url": paper_url,
                    "github_url": None, "kind": "paper", "topics": ["data research", query],
                    "popularity": 0, "popularity_label": "论文",
                    "metadata": {"authors": authors[:10], "published": published,
                                 "updated": entry.findtext(namespace + "updated"), "query": query}}
            upsert(db, "arxiv", ident, item, seen_at)
            count += 1
        db.commit()
    state_set(db, "arxiv", seen_at)
    db.commit()
    return count


def collect_hf_papers(db: sqlite3.Connection, config: dict, seen_at: str) -> int:
    """Search Hugging Face's public papers index when arXiv is unreachable."""
    cutoff = parse_stamp(seen_at) - timedelta(days=config.get("paper_lookback_days", 45))
    count = 0
    for query in config.get("paper_search_queries", ["text-to-sql", "data agent"]):
        result = request_json("https://huggingface.co/api/papers/search?" +
                              urlencode({"q": query, "limit": config.get("arxiv_per_query", 25)}),
                              os.getenv("HF_TOKEN"))
        if not isinstance(result, list):
            raise RuntimeError("unexpected Hugging Face paper search response")
        for entry in result:
            paper = entry.get("paper") or entry
            ident = paper.get("id") or paper.get("_id")
            title = paper.get("title") or ""
            published = paper.get("publishedAt") or paper.get("published_at") or ""
            if not ident or not title or not published:
                continue
            if not relevant_data_paper(title, paper.get("summary") or ""):
                continue
            try:
                if parse_stamp(published) < cutoff:
                    continue
            except ValueError:
                continue
            authors = [author.get("name", "") if isinstance(author, dict) else str(author)
                       for author in (paper.get("authors") or [])]
            item = {"title": " ".join(title.split()), "description": (paper.get("summary") or "")[:3500],
                    "url": "https://huggingface.co/papers/" + str(ident),
                    "github_url": None, "kind": "paper", "topics": ["data research", query],
                    "popularity": paper.get("upvotes") or 0, "popularity_label": "HF Upvotes",
                    "metadata": {"authors": authors[:10], "published": published,
                                 "linked_github": paper.get("githubRepo"), "query": query}}
            upsert(db, "hf_papers", str(ident), item, seen_at)
            count += 1
        db.commit()
    state_set(db, "hf_papers", seen_at)
    db.commit()
    return count


def collect_papers(db: sqlite3.Connection, config: dict, seen_at: str) -> int:
    try:
        count = collect_arxiv(db, config, seen_at)
        state_set(db, "papers_provider", "arxiv")
    except (URLError, OSError, RuntimeError, ET.ParseError):
        count = collect_hf_papers(db, config, seen_at)
        state_set(db, "papers_provider", "huggingface")
    db.commit()
    return count


def collect_skills(db: sqlite3.Connection, config: dict, seen_at: str) -> int | None:
    token = os.getenv("SKILLS_SH_TOKEN") or os.getenv("VERCEL_OIDC_TOKEN")
    if not token:
        return None
    limit = config.get("skills_per_view", 50)
    seen = set()
    for view in ("trending", "hot"):
        result = request_json("https://skills.sh/api/v1/skills?" + urlencode({"view": view, "per_page": limit}), token)
        for skill in result.get("data", []):
            ident = skill.get("id")
            if not ident:
                continue
            seen.add(ident)
            item = {"title": skill.get("name") or skill.get("slug") or ident,
                    "description": skill.get("description") or "",
                    "url": skill.get("url") or "https://skills.sh/" + ident,
                    "github_url": None, "kind": "skill", "topics": ["skill"],
                    "popularity": skill.get("installs") or 0, "popularity_label": "安装量",
                    "metadata": {"install_url": skill.get("installUrl"), "source": skill.get("source"),
                                 "source_type": skill.get("sourceType"), "view": view,
                                 "is_duplicate": skill.get("isDuplicate", False)}}
            upsert(db, "skills.sh", ident, item, seen_at)
        db.commit()
    state_set(db, "skills.sh", seen_at)
    db.commit()
    return len(seen)


def collect_huggingface(db: sqlite3.Connection, config: dict, seen_at: str) -> int:
    limit = config.get("huggingface_limit", 80)
    params = urlencode({"sort": "trendingScore", "direction": "-1", "limit": limit, "full": "true"})
    spaces = request_json("https://huggingface.co/api/spaces?" + params)
    if not isinstance(spaces, list):
        raise RuntimeError("unexpected Hugging Face Spaces response")
    count = 0
    for space in spaces:
        ident = space.get("id")
        if not ident or space.get("private"):
            continue
        card = space.get("cardData") or {}
        description = (card.get("short_description") or card.get("description") or
                       space.get("ai_short_description") or "") if isinstance(card, dict) else ""
        item = {"title": (card.get("title") if isinstance(card, dict) else None) or ident,
                "description": str(description)[:1000],
                "url": "https://huggingface.co/spaces/" + quote(ident, safe="/"),
                "github_url": None, "kind": "space", "topics": space.get("tags") or [],
                "popularity": space.get("likes") or 0, "popularity_label": "Likes",
                "metadata": {"sdk": space.get("sdk"), "trending_score": space.get("trendingScore"),
                             "created_at": space.get("createdAt"), "last_modified": space.get("lastModified")}}
        upsert(db, "huggingface", ident, item, seen_at)
        count += 1
    state_set(db, "huggingface", seen_at)
    db.commit()
    return count


def collect_producthunt(db: sqlite3.Connection, config: dict, seen_at: str) -> int | None:
    token = os.getenv("PRODUCT_HUNT_TOKEN")
    if not token:
        root = read_feed("https://www.producthunt.com/feed")
        entries = root.findall("{http://www.w3.org/2005/Atom}entry")
        count = 0
        for entry in entries[:config.get("producthunt_limit", 40)]:
            title = entry.findtext("{http://www.w3.org/2005/Atom}title") or ""
            link_node = entry.find("{http://www.w3.org/2005/Atom}link")
            url = link_node.get("href") if link_node is not None else ""
            ident = entry.findtext("{http://www.w3.org/2005/Atom}id") or url
            if not title or not url:
                continue
            raw_description = (entry.findtext("{http://www.w3.org/2005/Atom}summary") or
                               entry.findtext("{http://www.w3.org/2005/Atom}content") or "")
            description = html.unescape(re.sub(r"<[^>]+>", " ", raw_description))
            description = " ".join(description.split())[:700]
            item = {"title": title, "description": description,
                    "url": url, "github_url": None, "kind": "product", "topics": [],
                    "popularity": 0, "popularity_label": "RSS 新上榜",
                    "metadata": {"feed_published": entry.findtext("{http://www.w3.org/2005/Atom}published"),
                                 "metric_unavailable": True}}
            upsert(db, "producthunt", ident, item, seen_at)
            count += 1
        state_set(db, "producthunt", seen_at)
        db.commit()
        return count
    limit = config.get("producthunt_limit", 40)
    query = "query { posts(first: %d) { edges { node { id name tagline url votesCount website } } } }" % limit
    result = request_json("https://api.producthunt.com/v2/api/graphql", token, {"query": query})
    if result.get("errors"):
        raise RuntimeError("Product Hunt GraphQL: " + str(result["errors"][0].get("message", "unknown error")))
    edges = ((result.get("data") or {}).get("posts") or {}).get("edges") or []
    count = 0
    for edge in edges:
        post = edge.get("node") or {}
        if not post.get("id") or not post.get("url"):
            continue
        item = {"title": post.get("name") or "Product Hunt project",
                "description": post.get("tagline") or "", "url": post["url"],
                "github_url": None, "kind": "product", "topics": [],
                "popularity": post.get("votesCount") or 0, "popularity_label": "Votes",
                "metadata": {"website": post.get("website"), "github_website": github_url(post.get("website"))}}
        upsert(db, "producthunt", str(post["id"]), item, seen_at)
        count += 1
    state_set(db, "producthunt", seen_at)
    db.commit()
    return count


def reddit_token() -> str:
    cached = os.getenv("REDDIT_ACCESS_TOKEN")
    if cached:
        return cached
    client_id, client_secret = os.getenv("REDDIT_CLIENT_ID"), os.getenv("REDDIT_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError("未启用：需要 REDDIT_CLIENT_ID 和 REDDIT_CLIENT_SECRET，或 REDDIT_ACCESS_TOKEN")
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    request = Request("https://www.reddit.com/api/v1/access_token", data=b"grant_type=client_credentials",
                      headers={"Authorization": "Basic " + credentials,
                               "Content-Type": "application/x-www-form-urlencoded",
                               "User-Agent": os.getenv("REDDIT_USER_AGENT", "agent-radar/0.2 (personal research) ")})
    with urlopen(request, timeout=30) as response:
        result = json.load(response)
    if not result.get("access_token"):
        raise RuntimeError("Reddit did not return an access token")
    return result["access_token"]


def collect_reddit(db: sqlite3.Connection, config: dict, seen_at: str) -> int | None:
    if not os.getenv("REDDIT_ACCESS_TOKEN") and not (os.getenv("REDDIT_CLIENT_ID") and os.getenv("REDDIT_CLIENT_SECRET")):
        count = 0
        for subreddit in config.get("reddit_subreddits", ["LocalLLaMA", "ClaudeAI", "ChatGPTCoding", "selfhosted"]):
            if not subreddit.replace("_", "").isalnum():
                raise ValueError("invalid subreddit name")
            root = read_feed(f"https://www.reddit.com/r/{subreddit}/new/.rss?limit=" +
                             str(config.get("reddit_per_subreddit", 25)))
            for entry in root.findall("{http://www.w3.org/2005/Atom}entry"):
                title = entry.findtext("{http://www.w3.org/2005/Atom}title") or ""
                ident = entry.findtext("{http://www.w3.org/2005/Atom}id") or ""
                link_node = entry.find("{http://www.w3.org/2005/Atom}link")
                url = link_node.get("href") if link_node is not None else ""
                if not title or not ident or not url:
                    continue
                upsert(db, "reddit", ident, {"title": title[:300], "description": "Reddit 社区讨论线索",
                    "url": url, "github_url": None, "kind": "discussion", "topics": [subreddit],
                    "popularity": 0, "popularity_label": "RSS 新帖", "metadata": {"subreddit": subreddit}}, seen_at)
                count += 1
        state_set(db, "reddit", seen_at)
        db.commit()
        return count
    token = reddit_token()
    subreddits = config.get("reddit_subreddits", ["LocalLLaMA", "ClaudeAI", "ChatGPTCoding", "selfhosted"])
    per_subreddit = config.get("reddit_per_subreddit", 25)
    count = 0
    for subreddit in subreddits:
        if not subreddit.replace("_", "").isalnum():
            raise ValueError("invalid subreddit name")
        url = f"https://oauth.reddit.com/r/{subreddit}/new?" + urlencode({"limit": per_subreddit})
        request = Request(url, headers={"Authorization": f"Bearer {token}",
            "User-Agent": os.getenv("REDDIT_USER_AGENT", "agent-radar/0.2 (personal research)"),
            "Accept": "application/json"})
        with urlopen(request, timeout=30) as response:
            result = json.load(response)
        for child in ((result.get("data") or {}).get("children") or []):
            post = child.get("data") or {}
            ident = post.get("name") or post.get("id")
            if not ident or not post.get("title") or post.get("over_18"):
                continue
            item = {"title": post["title"][:300], "description": "Reddit 社区讨论线索",
                    "url": "https://www.reddit.com" + (post.get("permalink") or ""),
                    "github_url": None, "kind": "discussion", "topics": [subreddit],
                    "popularity": post.get("score") or 0, "popularity_label": "社区分数",
                    "metadata": {"subreddit": subreddit, "outbound_url": post.get("url"),
                                 "linked_github": github_url(post.get("url")),
                                 "created_utc": post.get("created_utc")}}
            upsert(db, "reddit", ident, item, seen_at)
            count += 1
        db.commit()
    state_set(db, "reddit", seen_at)
    db.commit()
    return count
