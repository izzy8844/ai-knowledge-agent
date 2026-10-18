import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_radar import core
from agent_radar import presentation, sources


class RadarTests(unittest.TestCase):
    def setUp(self):
        scratch = Path.cwd() / "work-tests"
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.db = core.connect(Path(self.temp.name) / "radar.sqlite3")

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_cross_source_dedup_and_star_snapshots(self):
        first = "2026-10-16T08:00:00Z"
        registry = {"server": {"name": "io.github.acme/tool", "description": "MCP tool",
                                "repository": {"url": "https://github.com/Acme/Tool/tree/main"}},
                    "_meta": {core.REGISTRY_META: {"status": "active", "updatedAt": first}}}
        rid = core.upsert(self.db, "registry", "io.github.acme/tool", core.registry_item(registry), first)
        repo = {"id": 42, "full_name": "Acme/Tool", "html_url": "https://github.com/Acme/Tool",
                "description": "Agent MCP skill", "stargazers_count": 10, "topics": ["mcp"],
                "updated_at": first}
        gid = core.upsert(self.db, "github", "42", core.github_item(repo), first)
        self.assertEqual(rid, gid)
        self.assertEqual(self.db.execute("SELECT count(*) FROM items").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM source_refs").fetchone()[0], 2)
        repo["stargazers_count"] = 18
        core.upsert(self.db, "github", "42", core.github_item(repo), "2026-10-17T08:00:00Z")
        self.assertEqual(core.star_delta(self.db, gid, "2026-10-17", 18), 8)

    def test_feedback_evidence_survives_incremental_upsert(self):
        seen = "2026-10-17T00:00:00Z"
        item = {"title": "acme/agent", "url": "https://github.com/acme/agent",
            "github_url": "https://github.com/acme/agent", "kind": "github",
            "stars": 100, "popularity": 100, "popularity_label": "Stars", "metadata": {"language": "Python"}}
        ident = core.upsert(self.db, "github", "10", item, seen)
        self.db.execute("UPDATE items SET metadata_json=? WHERE id=?",
            (json.dumps({"language": "Python", "feedback_checked_at": "2026-10-17"}), ident))
        item["stars"] = item["popularity"] = 101
        core.upsert(self.db, "github", "10", item, seen)
        meta = json.loads(self.db.execute("SELECT metadata_json FROM items WHERE id=?", (ident,)).fetchone()[0])
        self.assertEqual(meta["feedback_checked_at"], "2026-10-17")

    def test_registry_cursor_and_checkpoint(self):
        pages = [
            {"servers": [{"server": {"name": "com.example/one", "description": "MCP"}}],
             "metadata": {"nextCursor": "opaque:1"}},
            {"servers": [{"server": {"name": "com.example/two", "description": "MCP"}}],
             "metadata": {}},
        ]
        with patch.object(core, "request_json", side_effect=pages) as mock:
            count = core.collect_registry(self.db, {"lookback_days": 7, "overlap_hours": 2,
                                                    "registry_page_size": 100}, "2026-10-17T00:00:00Z")
        self.assertEqual(count, 2)
        self.assertIn("cursor=opaque%3A1", mock.call_args_list[1].args[0])
        self.assertEqual(core.state_get(self.db, "registry"), "2026-10-17T00:00:00Z")

    def test_github_incomplete_does_not_advance_checkpoint(self):
        config = {"github_queries": ["topic:mcp-server"], "lookback_days": 7,
                  "overlap_hours": 2, "github_pages_per_query": 1, "github_per_page": 100}
        with patch.object(core, "request_json", return_value={"items": [], "incomplete_results": True}) as mock:
            core.collect_github(self.db, config, "2026-10-17T00:00:00Z")
        self.assertIn("pushed%3A%3E%3D", mock.call_args.args[0])
        self.assertIsNone(core.state_get(self.db, "github:topic:mcp-server"))

    def test_github_configured_cap_advances_checkpoint(self):
        config = {"github_queries": ["topic:mcp-server"], "lookback_days": 7,
                  "overlap_hours": 2, "github_pages_per_query": 1, "github_per_page": 1}
        repo = {"id": 7, "full_name": "acme/mcp", "html_url": "https://github.com/acme/mcp",
                "description": "MCP", "stargazers_count": 3, "topics": []}
        with patch.object(core, "request_json", return_value={"items": [repo], "incomplete_results": False}):
            core.collect_github(self.db, config, "2026-10-17T00:00:00Z")
        self.assertEqual(core.state_get(self.db, "github:topic:mcp-server"), "2026-10-17T00:00:00Z")

    def test_exact_watchlist_catches_project_missed_by_topic_search(self):
        seen = "2026-10-17T00:00:00Z"
        repo = {"id": 999, "full_name": "morluto/rea", "html_url": "https://github.com/morluto/rea",
                "description": "Reverse engineer anything with agents", "stargazers_count": 300000,
                "topics": ["reverse-engineering"], "updated_at": seen}
        with patch.object(core, "request_json", return_value=repo):
            self.assertEqual(core.collect_github_discovery(self.db, {"github_discovery_queries": [],
                "github_watchlist": ["morluto/rea"], "github_min_stars": 100}, seen), 1)
        self.assertEqual(self.db.execute("SELECT stars FROM items WHERE title=?", ("morluto/rea",)).fetchone()[0], 300000)

    def test_report_contains_scored_candidate(self):
        seen = "2026-10-17T00:00:00Z"
        item_id = core.upsert(self.db, "github", "1", {"title": "acme/mcp-agent",
            "description": "A useful MCP agent", "url": "https://github.com/acme/mcp-agent",
            "github_url": "https://github.com/acme/mcp-agent", "kind": "github",
            "stars": 30, "topics": ["mcp"]}, seen)
        core.score_seen(self.db, seen)
        path = core.report(self.db, {"min_score": 3, "max_report_items": 30}, seen,
                           Path(self.temp.name) / "reports", {"github": 1}, [])
        content = path.read_text(encoding="utf-8")
        self.assertIn("acme/mcp-agent", content)
        self.assertIn("新发现", content)
        self.assertGreater(self.db.execute("SELECT score FROM items WHERE id=?", (item_id,)).fetchone()[0], 3)

    def test_html_chinese_intro_and_100_star_gate(self):
        seen = "2026-10-17T00:00:00Z"
        for ident, stars in ((1, 150), (2, 20)):
            core.upsert(self.db, "github", str(ident), {"title": f"acme/data-agent-{ident}",
                "description": "An agent for SQL data analytics", "url": f"https://github.com/acme/data-agent-{ident}",
                "github_url": f"https://github.com/acme/data-agent-{ident}", "kind": "github",
                "stars": stars, "popularity": stars, "popularity_label": "Stars"}, seen)
        core.score_seen(self.db, seen)
        presentation.enrich_items(self.db, seen)
        path = presentation.render_html(self.db, {"min_score": 3, "max_report_items": 30,
            "github_min_stars": 100}, seen, Path(self.temp.name), {"github": 2}, [])
        content = path.read_text(encoding="utf-8")
        self.assertIn("data-agent-1", content)
        self.assertNotIn("data-agent-2", content)
        self.assertIn("让 Agent 查询和分析企业数据", content)
        self.assertIn("近30天同指标增长", content)

    def test_30_day_growth_requires_old_snapshot(self):
        seen = "2026-10-17T00:00:00Z"
        ident = core.upsert(self.db, "github", "99", {"title": "acme/agent",
            "description": "agent", "url": "https://github.com/acme/agent",
            "github_url": "https://github.com/acme/agent", "kind": "github",
            "stars": 100, "popularity": 100, "popularity_label": "Stars"}, seen)
        self.assertIsNone(presentation.metric_growth(self.db, ident, "Stars", seen))
        self.db.execute("INSERT INTO metric_snapshots VALUES(?,?,?,?)", (ident, "Stars", "2026-09-17", 60))
        self.assertEqual(presentation.metric_growth(self.db, ident, "Stars", seen), 40)

    def test_hotter_project_ranks_above_category_preference(self):
        seen = "2026-10-17T00:00:00Z"
        for ident, title, stars in ((1, "acme/agent-memory", 120), (2, "acme/music-tool", 5000)):
            core.upsert(self.db, "github", str(ident), {"title": title, "description": title,
                "url": f"https://github.com/{title}", "github_url": f"https://github.com/{title}",
                "kind": "github", "stars": stars, "popularity": stars, "popularity_label": "Stars"}, seen)
        core.score_seen(self.db, seen)
        presentation.enrich_items(self.db, seen)
        path = presentation.render_html(self.db, {"min_score": 2, "max_report_items": 30,
            "github_min_stars": 100}, seen, Path(self.temp.name), {"github": 2}, [])
        page = path.read_text(encoding="utf-8")
        self.assertLess(page.index("acme/music-tool"), page.index("acme/agent-memory"))
        self.assertEqual(presentation.project_category(self.db.execute(
            "SELECT * FROM items WHERE title=?", ("acme/music-tool",)).fetchone(), "music tool"), "创作与设计")

    def test_three_sections_use_dated_history_when_recent_sources_are_empty(self):
        old = "2026-10-01T00:00:00Z"
        today = "2026-10-17T00:00:00Z"
        for ident, title, description in (
            (1, "acme/agent-tool", "AI agent tool for desktop workflows"),
            (2, "acme/data-agent", "Text-to-SQL data agent for enterprise analytics"),
        ):
            core.upsert(self.db, "github", str(ident), {"title": title, "description": description,
                "url": f"https://github.com/{title}", "github_url": f"https://github.com/{title}",
                "kind": "github", "stars": 200, "popularity": 200, "popularity_label": "Stars"}, old)
        core.upsert(self.db, "arxiv", "2610.00001", {"title": "Better Text-to-SQL Data Agent",
            "description": "Evaluation on BIRD for enterprise data analysis",
            "url": "https://arxiv.org/abs/2610.00001", "kind": "paper",
            "metadata": {"published": old}}, old)
        core.score_seen(self.db, old)
        presentation.enrich_items(self.db, old)
        page = presentation.render_html(self.db, {"github_min_stars": 100}, today,
            Path(self.temp.name), {}, []).read_text(encoding="utf-8")
        hot, usable, data = (page.split(f'<section class="lane" id="{name}">', 1)[1]
            .split('</section>', 1)[0] for name in ("hot", "usable", "data"))
        self.assertIn("acme/agent-tool", hot)
        self.assertIn("acme/agent-tool", usable)
        self.assertIn("acme/data-agent", data)
        self.assertIn("Better Text-to-SQL Data Agent", data)
        for section in (hot, usable, data):
            self.assertIn("历史补充 · 最近采集 2026-10-01", section)
        self.assertEqual(page.count('<section class="lane"'), 3)

    def test_new_sources_parse_official_shapes(self):
        seen = "2026-10-17T00:00:00Z"
        skill_payload = {"data": [{"id": "acme/skills/planner", "name": "planner", "slug": "planner",
            "source": "acme/skills", "installs": 1200, "installUrl": "https://github.com/acme/skills",
            "url": "https://skills.sh/acme/skills/planner"}]}
        with patch.dict("os.environ", {"SKILLS_SH_TOKEN": "test"}), patch.object(sources, "request_json", return_value=skill_payload):
            self.assertEqual(sources.collect_skills(self.db, {"skills_per_view": 1}, seen), 1)
        hf_payload = [{"id": "acme/music-agent", "likes": 300, "sdk": "gradio",
                       "cardData": {"title": "Music Agent", "description": "Music creation agent"}}]
        with patch.object(sources, "request_json", return_value=hf_payload):
            self.assertEqual(sources.collect_huggingface(self.db, {"huggingface_limit": 1}, seen), 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM source_refs").fetchone()[0], 2)
        self.assertEqual(self.db.execute("SELECT popularity FROM items WHERE kind='skill'").fetchone()[0], 1200)

    def test_arxiv_papers_get_their_own_report_lane(self):
        seen = "2026-10-17T00:00:00Z"
        atom = '''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
        <id>http://arxiv.org/abs/2610.12345</id><title>Better Text-to-SQL for Enterprise Data</title>
        <summary>We evaluate our data agent on BIRD and Spider 2.0.</summary>
        <published>2026-10-15T00:00:00Z</published><author><name>Test Author</name></author>
        </entry></feed>'''
        with patch.object(sources, "read_feed", return_value=sources.ET.fromstring(atom)):
            self.assertEqual(sources.collect_arxiv(self.db, {"arxiv_queries": ["all:text-to-sql"],
                "arxiv_per_query": 1, "paper_lookback_days": 45}, seen), 1)
        presentation.enrich_items(self.db, seen)
        page = presentation.render_html(self.db, {"min_score": 3, "max_report_items": 30,
            "github_min_stars": 100}, seen, Path(self.temp.name), {"arxiv": 1}, []).read_text(encoding="utf-8")
        self.assertIn("近期论文与评测线索", page)
        self.assertIn("Better Text-to-SQL", page)
        self.assertIn("BIRD", page)
        self.assertIn("https://arxiv.org/abs/2610.12345", page)

    def test_paper_search_falls_back_when_arxiv_is_unreachable(self):
        seen = "2026-10-17T00:00:00Z"
        result = [{"id": "2610.54321", "title": "A Data Agent for Analytics",
                   "summary": "We evaluate on BIRD.", "publishedAt": "2026-10-15T00:00:00Z",
                   "authors": [{"name": "Test Author"}], "upvotes": 12}]
        with patch.object(sources, "collect_arxiv", side_effect=OSError("connection failed")), \
                patch.object(sources, "request_json", return_value=result):
            self.assertEqual(sources.collect_papers(self.db, {"paper_search_queries": ["data agent"]}, seen), 1)
        self.assertEqual(core.state_get(self.db, "papers_provider"), "huggingface")

    def test_paper_comparison_names_baseline_without_claiming_breakthrough(self):
        seen = "2026-10-17T00:00:00Z"
        core.upsert(self.db, "arxiv", "2610.00001", {"title": "VeriPIE: Text-to-SQL",
            "description": "In single-run experiments on the complete Spider development set, VeriPIE achieves 72.05% test-suite accuracy (745/1,034), compared with DeepEye's 65.67%.",
            "url": "https://arxiv.org/abs/2610.00001", "kind": "paper", "popularity": 0,
            "popularity_label": "论文", "metadata": {"published": seen}}, seen)
        item = self.db.execute("SELECT * FROM items WHERE kind='paper'").fetchone()
        comparison = presentation.paper_comparison(item)
        self.assertIn("72.05%", comparison)
        self.assertIn("DeepEye", comparison)
        self.assertIn("65.67%", comparison)

    def test_public_feed_fallbacks_without_tokens(self):
        atom = '''<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>post-1</id>
        <title>Music Studio</title><link href="https://www.producthunt.com/posts/music-studio"/>
        <summary>AI music creation</summary></entry></feed>'''
        with patch.dict("os.environ", {}, clear=True), patch.object(sources, "read_feed",
                return_value=sources.ET.fromstring(atom)):
            self.assertIsNone(sources.collect_skills(self.db, {}, "2026-10-17T00:00:00Z"))
            self.assertEqual(sources.collect_producthunt(self.db, {}, "2026-10-17T00:00:00Z"), 1)
            self.assertEqual(sources.collect_reddit(self.db, {"reddit_subreddits": ["LocalLLaMA"]},
                "2026-10-17T00:00:00Z"), 1)

    def test_readme_install_evidence_is_not_claimed_as_verified_run(self):
        import base64
        seen = "2026-10-17T00:00:00Z"
        item_id = core.upsert(self.db, "github", "88", {"title": "acme/agent",
            "description": "Agent", "url": "https://github.com/acme/agent",
            "github_url": "https://github.com/acme/agent", "kind": "github",
            "stars": 500, "popularity": 500, "popularity_label": "Stars",
            "updated_at": seen}, seen)
        core.score_seen(self.db, seen)
        readme = base64.b64encode(b"# Installation\nUse pip install acme-agent").decode()
        def fake_request(url, token=None, **kwargs):
            return [{"title": "Suggestion", "body": "Looks good", "state": "open"}] if "/issues?" in url else {"encoding": "base64", "content": readme}
        with patch.object(presentation, "request_json", side_effect=fake_request):
            self.assertEqual(presentation.probe_deployment(self.db, {"deployment_probe_limit": 1,
                "github_min_stars": 100}, seen), 1)
        presentation.enrich_items(self.db, seen)
        label = self.db.execute("SELECT deployability FROM items WHERE id=?", (item_id,)).fetchone()[0]
        self.assertIn("近期反馈未见重大异常", label)
        self.assertIn("有安装说明", label)

    def test_producthunt_and_reddit_source_shapes(self):
        import io
        seen = "2026-10-17T00:00:00Z"
        product_payload = {"data": {"posts": {"edges": [{"node": {
            "id": "42", "name": "Music Studio", "tagline": "Make songs with AI",
            "url": "https://www.producthunt.com/posts/music-studio", "votesCount": 250,
            "website": "https://studio.example"}}]}}}
        with patch.dict("os.environ", {"PRODUCT_HUNT_TOKEN": "test"}), patch.object(sources, "request_json", return_value=product_payload):
            self.assertEqual(sources.collect_producthunt(self.db, {"producthunt_limit": 1}, seen), 1)
        reddit_payload = {"data": {"children": [{"data": {
            "name": "t3_abc", "title": "New open-source music agent",
            "permalink": "/r/LocalLLaMA/comments/abc/new_agent/", "url": "https://github.com/acme/music-agent",
            "score": 80, "over_18": False}}]}}
        with patch.dict("os.environ", {"REDDIT_ACCESS_TOKEN": "test"}), patch.object(
                sources, "urlopen", return_value=io.BytesIO(json.dumps(reddit_payload).encode())):
            self.assertEqual(sources.collect_reddit(self.db, {"reddit_subreddits": ["LocalLLaMA"],
                "reddit_per_subreddit": 1}, seen), 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM items WHERE kind='discussion'").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM items WHERE kind='product'").fetchone()[0], 1)

    def test_windows_failure_feedback_is_flagged(self):
        seen = "2026-10-17T00:00:00Z"
        item_id = core.upsert(self.db, "github", "501", {"title": "acme/agent", "description": "AI agent",
            "url": "https://github.com/acme/agent", "github_url": "https://github.com/acme/agent",
            "kind": "github", "stars": 500, "popularity": 500, "popularity_label": "Stars"}, seen)
        def response(url, token=None, **kwargs):
            if "/issues/comments" in url:
                return [{"body": "Cannot run on Windows 11"}]
            if "/issues?" in url:
                return []
            return {}
        with patch.object(presentation, "request_json", side_effect=response):
            presentation.probe_deployment(self.db, {"deployment_probe_limit": 1,
                "github_min_stars": 100}, seen)
        presentation.enrich_items(self.db, seen)
        label = self.db.execute("SELECT deployability FROM items WHERE id=?", (item_id,)).fetchone()[0]
        self.assertIn("Windows 运行失败反馈", label)


if __name__ == "__main__":
    unittest.main()
