"""Classification and a standalone, readable HTML daily report."""

import base64
import html
import json
import math
import os
import re
import sqlite3
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

from .core import parse_stamp, request_json, stamp, state_get, state_set

CATEGORIES = [
    ("Agent 增强与进化", r"claude|codex|cursor|copilot|agent memory|agent skill|agent workflow|self.improv|self.evolv|multi.agent|agent orchestrat|agent framework|agentic"),
    ("独立 Agent 与 AI 应用", r"\bagent\b|assistant|autonomous|ai app|chatbot"),
    ("AI 编程与代码质量", r"coding|code review|refactor|debug|test generat|lint|developer tool|programming"),
    ("数据分析与产品", r"data agent|analytics|dashboard|database|sql|spreadsheet|data product|visuali[sz]ation"),
    ("创作与设计", r"music|audio|video|image|design|content creat|writing|podcast"),
    ("工作流自动化", r"automat|workflow|integration|connector|mcp server|model context protocol"),
    ("模型与基础设施", r"llm|inference|embedding|vector|model serving|rag|retrieval"),
]
INDUSTRIES = [
    ("生活", r"\blife\b|lifestyle|personal|health|fitness|travel|food|\bhome\b|calendar|finance|shopping|education"),
    ("数据产品", r"\bdata\b|analytics|dashboard|database|\bsql\b|spreadsheet|bi tool|visuali[sz]ation"),
    ("音乐", r"music|song|audio|sound|midi|podcast"),
    ("自媒体", r"social media|creator|content|video|youtube|tiktok|blog|newsletter"),
    ("AI", r"\bai\b|\bllm\b|model|agent|prompt|mcp"),
    ("游戏", r"game|gaming|unity|unreal"),
    ("设计", r"design|figma|ui|ux|image|visual"),
    ("开发者", r"code|coding|developer|programming|github|devops"),
]
PURPOSES = [
    (r"reverse.engineer|decompil|disassembl|binary.analysis", "分析应用、网站或二进制程序的实现与行为"),
    (r"text.?to.?sql|nl2sql|natural.language.to.sql", "把自然语言问题转换为 SQL 查询"),
    (r"data.?agent|enterprise.data|business.intelligence", "让 Agent 查询和分析企业数据"),
    (r"memory|context|knowledge", "管理 Agent 的记忆、知识或上下文"),
    (r"plan|project|task|kanban", "规划任务并协助项目管理"),
    (r"review|refactor|lint|quality", "检查和改进代码质量"),
    (r"test|debug", "辅助测试、调试和定位问题"),
    (r"music|song|midi|sound", "生成、编辑或分析音乐与音频"),
    (r"video|social media|content|blog", "辅助内容制作与发布"),
    (r"data|sql|analytics|dashboard", "查询、分析或展示数据"),
    (r"image|design|figma|ui|ux", "辅助视觉设计或界面制作"),
    (r"automat|workflow|orchestrat", "自动化多步骤工作流程"),
    (r"search|retriev|research", "检索资料并整理研究结果"),
    (r"mcp|connector|integration", "把外部工具或数据接入 Agent"),
]
CATEGORY_WEIGHT = {name: weight for name, weight in zip(
    [x[0] for x in CATEGORIES] + ["其他"], [8, 7, 6, 5, 4, 3, 2, 0])}
INDUSTRY_WEIGHT = {"生活": 5, "数据产品": 5, "音乐": 5, "自媒体": 2,
                   "AI": 2, "游戏": 2, "设计": 2, "开发者": 1, "通用": 0}


def project_category(item: sqlite3.Row, text: str) -> str:
    """Classify what the project does, not every client it mentions."""
    title = item["title"].lower()
    kind = item["kind"]
    if kind == "paper":
        return "数据分析与产品"
    if re.search(r"code review|refactor|lint|debug|coding|code.?quality|programming|test generat", title):
        return "AI 编程与代码质量"
    if re.search(r"music|audio|song|video|image|design|writing|podcast|content creat", title):
        return "创作与设计"
    if re.search(r"analytics|dashboard|database|spreadsheet|\bsql\b|data product", title):
        return "数据分析与产品"
    if kind == "skill" or re.search(r"agent memory|agent skill|self.improv|self.evolv|agent orchestrat", title):
        return "Agent 增强与进化"
    if kind == "mcp" or re.search(r"mcp.?server|model context protocol|connector|integration", title):
        return "工作流自动化"
    if re.search(r"\bagent\b|assistant|autonomous|chatbot|ai app", title):
        return "独立 Agent 与 AI 应用"
    if re.search(r"code review|refactor|lint|debug|coding|code.?quality|programming", text):
        return "AI 编程与代码质量"
    if re.search(r"analytics|dashboard|database|spreadsheet|\bsql\b|data product", text):
        return "数据分析与产品"
    if re.search(r"music|audio|song|video|image|design|writing|podcast|content creat", text):
        return "创作与设计"
    if re.search(r"agent memory|agent skill|self.improv|self.evolv|agent orchestrat", text):
        return "Agent 增强与进化"
    if re.search(r"mcp server|model context protocol|connector|integration|workflow|automat", text):
        return "工作流自动化"
    if re.search(r"\bagent\b|assistant|autonomous|chatbot|ai app", text):
        return "独立 Agent 与 AI 应用"
    if re.search(r"llm|inference|embedding|vector|model serving|rag|retrieval", text):
        return "模型与基础设施"
    return "其他"


def metric_growth(db: sqlite3.Connection, item_id: int, metric: str | None, as_of: str) -> int | None:
    if not metric:
        return None
    cutoff = stamp(parse_stamp(as_of) - timedelta(days=30))[:10]
    cutoff_end = stamp(parse_stamp(as_of) - timedelta(days=27))[:10]
    old = db.execute("""SELECT value FROM metric_snapshots WHERE item_id=? AND metric=?
        AND snapshot_date BETWEEN ? AND ? ORDER BY snapshot_date ASC LIMIT 1""",
        (item_id, metric, cutoff, cutoff_end)).fetchone()
    current = db.execute("""SELECT value FROM metric_snapshots WHERE item_id=? AND metric=?
        ORDER BY snapshot_date DESC LIMIT 1""", (item_id, metric)).fetchone()
    return max(0, current[0] - old[0]) if old and current else None


def classify(item: sqlite3.Row, growth: int | None) -> dict:
    meta = json.loads(item["metadata_json"] or "{}")
    text = " ".join((item["title"], item["description"], item["topics_json"])).lower()
    category = project_category(item, text)
    industry = next((name for name, pattern in INDUSTRIES if re.search(pattern, text)), "通用")
    purpose = next((label for pattern, label in PURPOSES if re.search(pattern, text)), None)
    if item["kind"] == "paper":
        intro = paper_brief(item)
    elif purpose:
        intro = f"这是一个{category}项目，主要用于{purpose}。"
    elif item["kind"] == "skill":
        intro = "这是一个可供 Agent 使用的 Skill；公开目录信息不足，具体用途需查看技能页。"
    elif item["kind"] == "paper":
        deploy, deploy_bonus = "研究论文 · 代码待核查", 0
    else:
        intro = f"这是一个{category}项目；公开简介不足以确认具体功能，建议查看项目主页。"
    if item["github_url"] or item["kind"] == "github":
        if meta.get("windows_failure_count", 0):
            deploy, deploy_bonus = "Windows 运行失败反馈 · 需排查", 0
        elif meta.get("major_issue_count", 0):
            deploy, deploy_bonus = "近期运行异常反馈 · 需排查", 1
        elif meta.get("feedback_checked_at"):
            deploy, deploy_bonus = "代码公开 · 近期反馈未见重大异常", 5
        else:
            deploy, deploy_bonus = "代码公开 · 反馈待核查", 2
        if meta.get("install_guide"):
            deploy += " · 有安装说明"
            deploy_bonus += 1
    elif item["kind"] == "skill" and meta.get("install_url"):
        deploy, deploy_bonus = "提供安装入口", 4
    elif item["kind"] == "space":
        deploy, deploy_bonus = "在线可体验", 3
    else:
        deploy, deploy_bonus = "部署信息待核查", 0
    popularity = item["popularity"] or item["stars"] or 0
    heat = ("高关注" if popularity >= 10000 else "较高关注" if popularity >= 1000
            else "已有关注" if popularity >= 100 else "早期/待观察")
    if growth is not None and growth >= 100:
        heat += " · 近30天增长快"
    score = (CATEGORY_WEIGHT[category] + INDUSTRY_WEIGHT[industry]
             + 18 * math.log10(popularity + 1)
             + (min(18, 6 * math.log10(growth + 1)) if growth is not None else 0)
             + deploy_bonus)
    return {"category": category, "industry": industry, "cn_intro": intro,
            "deployability": deploy, "heat": heat, "recommend_score": round(score, 2)}


def is_data_research(item: sqlite3.Row) -> bool:
    text = " ".join((item["title"], item["description"])).lower()
    return bool(re.search(r"text.?to.?sql|nl2sql|natural.language.to.sql|data.?agent|data.?analys|analytics|business.intelligence|semantic.layer|enterprise.data|database.agent|sql.agent", text))


def is_relevant_paper(item: sqlite3.Row) -> bool:
    return bool(re.search(r"text.?to.?sql|nl2sql|natural.language.to.sql|data.?agent|agentic.data|enterprise.data|business.intelligence|database.agent|multi.database.agent|sql.agent|semantic.layer", item["title"].lower()))


def paper_brief(item: sqlite3.Row) -> str:
    text = " ".join((item["title"], item["description"])).lower()
    topic = "自然语言转 SQL" if re.search(r"text.?to.?sql|nl2sql|natural.language.to.sql", text) else "数据 Agent 与企业数据分析"
    benchmarks = [name for name in ("BIRD", "Spider 2.0", "Spider") if name.lower() in text]
    benchmark = "，摘要提到 " + "、".join(benchmarks[:2]) + " 评测" if benchmarks else ""
    if re.search(r"benchmark|\bbench\b", item["title"], re.I):
        return f"提出面向{topic}的评测基准{benchmark}，用于识别现有系统的能力缺口。"
    return f"研究主题：{topic}{benchmark}。效果提升来自作者摘要，需核对基线、数据集与实验设置。"


def paper_comparison(item: sqlite3.Row) -> str:
    abstract = item["description"] or ""
    match = re.search(r"([A-Za-z][A-Za-z0-9-]*) achieves (\d+(?:\.\d+)?%) (.{0,125}?)compared with ([A-Za-z][A-Za-z0-9-]*)(?:'s)? (\d+(?:\.\d+)?%)", abstract, re.I)
    if not match:
        return ""
    metric = "测试套件准确率" if "test-suite accuracy" in match.group(3).lower() else "论文所述指标"
    dataset = "Spider 开发集" if "spider development set" in abstract.lower() else "所述评测集"
    return f"作者报告：{match.group(1)} 在{dataset}的{metric}为 {match.group(2)}，对比 {match.group(4)} 的 {match.group(5)}。"


def probe_deployment(db: sqlite3.Connection, config: dict, seen_at: str) -> int:
    """Check public setup guidance and a bounded sample of recent issue feedback."""
    limit = config.get("deployment_probe_limit", 12)
    if not os.getenv("GITHUB_TOKEN"):
        limit = min(limit, 4)
    if limit <= 0:
        return 0
    rows = db.execute("""SELECT * FROM items WHERE kind='github' AND stars>=?
        AND last_seen_at=? ORDER BY stars DESC LIMIT ?""",
        (config.get("github_min_stars", 100), seen_at, limit)).fetchall()
    checked = 0
    for item in rows:
        probe_key = f"deploy:v2:{item['id']}"
        if state_get(db, probe_key) == seen_at[:10]:
            continue
        url = item["github_url"] or ""
        parts = urlparse(url).path.strip("/").split("/")
        if len(parts) != 2:
            continue
        base = f"https://api.github.com/repos/{parts[0]}/{parts[1]}"
        meta = json.loads(item["metadata_json"] or "{}")
        try:
            response = request_json(base + "/readme", os.getenv("GITHUB_TOKEN"), timeout=6, attempts=1)
            if response.get("encoding") == "base64":
                readme = base64.b64decode(response.get("content") or "")[:300000].decode("utf-8", errors="replace")
                meta["install_guide"] = bool(re.search(r"(?im)^#{1,3}\s*(install|quick.?start|getting started|setup|run locally|部署|安装)", readme)
                    and re.search(r"(?im)(pip install|uvx |npx |npm install|pnpm install|docker (compose|run)|git clone)", readme))
        except (RuntimeError, ValueError):
            pass
        try:
            issues = request_json(base + "/issues?state=all&sort=updated&direction=desc&per_page=30",
                                  os.getenv("GITHUB_TOKEN"), timeout=6, attempts=1)
            if isinstance(issues, list):
                issues = [issue for issue in issues if "pull_request" not in issue]
                failure = re.compile(r"can't run|cannot run|doesn't work|not working|fails? to (run|install|start)|installation failed|crash|fatal error|无法运行|安装失败|启动失败", re.I)
                windows = re.compile(r"\bwindows\b|\bwin(?:10|11)\b|\bwin32\b", re.I)
                comments = request_json(base + "/issues/comments?sort=updated&direction=desc&per_page=30",
                                        os.getenv("GITHUB_TOKEN"), timeout=6, attempts=1)
                comments = comments if isinstance(comments, list) else []
                severe = [issue for issue in issues if issue.get("state") == "open" and failure.search(
                    (issue.get("title") or "") + " " + (issue.get("body") or "")[:3000])]
                severe += [comment for comment in comments if failure.search((comment.get("body") or "")[:3000])]
                meta["major_issue_count"] = len(severe)
                meta["windows_failure_count"] = sum(bool(windows.search(
                    (issue.get("title") or "") + " " + (issue.get("body") or "")[:3000])) for issue in severe)
                meta["feedback_checked_at"] = seen_at[:10]
                meta["feedback_sample_size"] = len(issues) + len(comments)
                state_set(db, "deploy:last_error", "")
                checked += 1
        except (RuntimeError, ValueError) as error:
            state_set(db, "deploy:last_error", str(error)[:300])
        db.execute("UPDATE items SET metadata_json=? WHERE id=?", (json.dumps(meta), item["id"]))
        if meta.get("feedback_checked_at") == seen_at[:10]:
            state_set(db, probe_key, seen_at[:10])
    db.commit()
    return checked


def enrich_items(db: sqlite3.Connection, seen_at: str) -> None:
    cutoff = stamp(parse_stamp(seen_at) - timedelta(days=7))
    rows = db.execute("SELECT * FROM items WHERE last_seen_at>=?", (cutoff,)).fetchall()
    for item in rows:
        result = classify(item, metric_growth(db, item["id"], item["popularity_label"], seen_at))
        db.execute("""UPDATE items SET category=?,industry=?,heat=?,deployability=?,cn_intro=?,
            recommend_score=? WHERE id=?""",
            (result["category"], result["industry"], result["heat"], result["deployability"],
             result["cn_intro"], result["recommend_score"], item["id"]))
    db.commit()


def safe_url(value: str) -> str:
    parsed = urlparse(value or "")
    return html.escape(value, quote=True) if parsed.scheme == "https" and parsed.netloc else "#"


def render_html(db: sqlite3.Connection, config: dict, seen_at: str, report_dir: Path,
                counts: dict[str, int], errors: list[str]) -> Path:
    cutoff = stamp(parse_stamp(seen_at) - timedelta(days=7))
    min_stars = config.get("github_min_stars", 100)
    recent = db.execute("""SELECT * FROM items WHERE last_seen_at>=? AND status='active'
        AND kind IN ('github','mcp','skill','space') AND (github_url IS NULL OR stars>=?)
        ORDER BY COALESCE(popularity,0) DESC, recommend_score DESC""",
        (cutoff, min_stars)).fetchall()
    historical = db.execute("""SELECT * FROM items WHERE last_seen_at<? AND status='active'
        AND kind IN ('github','mcp','skill','space') AND (github_url IS NULL OR stars>=?)
        ORDER BY COALESCE(popularity,0) DESC, recommend_score DESC""",
        (cutoff, min_stars)).fetchall()
    candidates = list(recent) + list(historical)
    rows = candidates[:config.get("hot_items", 15)]
    usable_candidates = [item for item in candidates if item["kind"] in ("skill", "space") or
        re.search(r"mcp|cli|app|desktop|tool|agent|assistant|server|reverse|skill|platform|studio|workflow|analys", item["title"] + " " + item["description"], re.I)]
    usable_rows = []
    category_count: dict[str, int] = {}
    for item in usable_candidates:
        category = item["category"] or "其他"
        if category_count.get(category, 0) >= 3:
            continue
        usable_rows.append(item)
        category_count[category] = category_count.get(category, 0) + 1
        if len(usable_rows) >= config.get("usable_items", 10):
            break
    data_rows = [item for item in candidates if item["kind"] != "space" and is_data_research(item)]
    data_rows = data_rows[:config.get("data_project_items", 8)]
    paper_candidates = db.execute("""SELECT * FROM items WHERE kind='paper'
        ORDER BY CASE WHEN last_seen_at>=? THEN 0 ELSE 1 END,
        json_extract(metadata_json,'$.published') DESC""", (cutoff,)).fetchall()
    papers = [paper for paper in paper_candidates if is_relevant_paper(paper)][:config.get("paper_items", 8)]
    discussions = db.execute("""SELECT * FROM items WHERE kind='discussion' AND last_seen_at>=?
        ORDER BY COALESCE(popularity,0) DESC LIMIT 5""", (cutoff,)).fetchall()
    new_products = db.execute("""SELECT * FROM items WHERE kind='product' AND last_seen_at>=?
        ORDER BY last_seen_at DESC LIMIT 8""", (cutoff,)).fetchall()
    esc = lambda v: html.escape(str(v or ""), quote=True)
    def freshness(item: sqlite3.Row) -> str:
        last_seen = (item["last_seen_at"] or "")[:10]
        if item["last_seen_at"] < cutoff:
            return f'<span class="age stale">历史补充 · 最近采集 {esc(last_seen)}</span>'
        return f'<span class="age">最近采集 {esc(last_seen)}</span>'
    cards = []
    for rank, item in enumerate(rows, 1):
        growth = metric_growth(db, item["id"], item["popularity_label"], seen_at)
        growth_label = f"+{growth:,}" if growth is not None else "数据积累中"
        sources = ", ".join(r[0] for r in db.execute("SELECT source FROM source_refs WHERE item_id=?", (item["id"],)))
        intro = item["llm_summary"] or item["cn_intro"] or "简介待补充"
        metric = f"{item['popularity_label'] or '关注'} {item['popularity'] or 0:,}"
        cards.append(f"""<article class="card" data-category="{esc(item['category'])}" data-industry="{esc(item['industry'])}">
<div class="card-top"><span class="rank">{rank:02d}</span><span class="source">{esc(sources)}</span></div>
<h3><a href="{safe_url(item['url'])}" target="_blank" rel="noopener noreferrer">{esc(item['title'])} ↗</a></h3>
{freshness(item)}
<p class="intro">{esc(intro)}</p>
<div class="tags"><span>{esc(item['category'])}</span><span>{esc(item['industry'])}</span><span>{esc(item['deployability'])}</span></div>
<div class="stats"><span><b>{esc(metric)}</b><small>当前热度</small></span><span><b>{esc(growth_label)}</b><small>近30天同指标增长</small></span><span><b>{esc(item['heat'])}</b><small>热度判断</small></span></div>
        <details><summary>查看公开简介与评估依据</summary><p>{esc(item['description'] or '暂无公开描述')}</p><p>热度优先排序 · 推荐分 {item['recommend_score']:.1f} · 30天增长缺失时不会推算；运行反馈只抽查近期公开 Issue。</p></details>
</article>""")
    def compact_card(item: sqlite3.Row, rank: int, paper: bool = False) -> str:
        meta = json.loads(item["metadata_json"] or "{}")
        metric = ("论文 · " + (meta.get("published") or "")[:10]) if paper else f"{item['popularity_label'] or 'Stars'} {item['popularity'] or item['stars'] or 0:,}"
        intro = item["llm_summary"] or item["cn_intro"] or "公开简介待补充"
        extra = "、".join(meta.get("authors") or []) if paper else item["deployability"] or "部署信息待核查"
        original = item["description"] or "暂无公开摘要"
        evidence = f'<p class="claim">{esc(paper_comparison(item))}</p>' if paper and paper_comparison(item) else ""
        return f'''<article class="card compact" data-category="{esc(item['category'])}" data-industry="{esc(item['industry'])}">
<div class="card-top"><span class="rank">{rank:02d}</span><span class="source">{esc(metric)}</span></div>
<h3><a href="{safe_url(item['url'])}" target="_blank" rel="noopener noreferrer">{esc(item['title'])} ↗</a></h3>
{freshness(item)}
<p class="intro">{esc(intro)}</p>{evidence}<div class="tags"><span>{esc(item['category'])}</span><span>{esc(item['industry'])}</span><span>{esc(extra[:140])}</span></div>
<details><summary>{'查看论文摘要与作者' if paper else '查看项目原始简介'}</summary><p>{esc(original[:1500])}</p></details></article>'''
    usable_cards = "".join(compact_card(item, index) for index, item in enumerate(usable_rows, 1))
    data_cards = "".join(compact_card(item, index) for index, item in enumerate(data_rows, 1))
    paper_cards = "".join(compact_card(item, index, True) for index, item in enumerate(papers, 1))
    signal_cards = "".join(f'<li><a href="{safe_url(r["url"])}" target="_blank" rel="noopener noreferrer">{esc(r["title"])}</a><span>社区分数 {r["popularity"] or 0}</span></li>' for r in discussions)
    product_cards = "".join(f'<li><a href="{safe_url(r["url"])}" target="_blank" rel="noopener noreferrer">{esc(r["title"])}</a> · {esc(r["cn_intro"] or r["description"] or "简介待补充")}</li>' for r in new_products)
    warnings = []
    for name, key, variable in (("skills.sh", "skills.sh", "VERCEL_OIDC_TOKEN"),
                                ("Product Hunt", "producthunt", "官方 RSS 或 PRODUCT_HUNT_TOKEN"),
                                ("Reddit", "reddit", "RSS 或 OAuth")):
        if key not in counts:
            warnings.append(f"{name} 未启用或采集失败（凭据：{variable}）")
    warnings += errors
    probe_error = state_get(db, "deploy:last_error")
    if probe_error:
        warnings.append("GitHub 运行反馈抽查未完成：API 限流或请求失败；配置 GITHUB_TOKEN 后可重试。")
    source_summary = " · ".join(f"{esc(k)} {v}" for k, v in counts.items())
    paper_provider = state_get(db, "papers_provider")
    if paper_provider and "papers" in counts:
        source_summary += f" · 论文来源 {esc(paper_provider)}"
    warning_list = "".join(f"<li>{esc(w)}</li>" for w in warnings)
    category_options = "".join(f'<option value="{esc(x)}">{esc(x)}</option>' for x in CATEGORY_WEIGHT)
    industry_options = "".join(f'<option value="{esc(x)}">{esc(x)}</option>' for x in INDUSTRY_WEIGHT)
    template = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI 技术情报 · @@DATE@@</title><style>
:root{--ink:#172534;--muted:#627180;--line:#dce6eb;--blue:#14639b;--sea:#0d7d75;--bg:#f4f8fa}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
.hero{background:linear-gradient(115deg,#112a45,#145b76 65%,#1e897e);color:white;padding:58px max(24px,calc((100vw - 1160px)/2)) 52px}
.eyebrow{letter-spacing:.16em;text-transform:uppercase;font-size:12px;opacity:.75;font-weight:700}
h1{font-size:clamp(32px,5vw,54px);line-height:1.1;letter-spacing:-.04em;margin:16px 0}.hero p{max-width:760px;opacity:.87;font-size:17px}
.hero-meta{display:flex;gap:16px;flex-wrap:wrap;margin-top:26px}.hero-meta span{background:#ffffff22;padding:7px 13px;border:1px solid #ffffff30;border-radius:50px}
main{max-width:1208px;margin:auto;padding:34px 24px 80px}.toolbar{display:flex;gap:12px;flex-wrap:wrap;align-items:center;justify-content:space-between;margin:0 0 22px}
.toolbar h2{margin:0;font-size:23px}select,input{border:1px solid var(--line);border-radius:10px;background:white;padding:10px 12px;color:var(--ink);font:inherit;max-width:100%}
.filters{display:flex;gap:8px;flex-wrap:wrap}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}
.card{background:white;border:1px solid var(--line);border-radius:18px;padding:22px;box-shadow:0 8px 30px #1739540a;display:flex;flex-direction:column}.card[hidden]{display:none}
.card-top{display:flex;justify-content:space-between;align-items:center;color:var(--muted);font-size:12px}.rank{font-size:19px;font-weight:800;color:var(--sea)}
.source{text-transform:uppercase;letter-spacing:.08em}h3{font-size:21px;line-height:1.25;margin:14px 0 10px;word-break:break-word}a{color:var(--blue);text-decoration:none}a:hover{text-decoration:underline}
.intro{margin:0 0 16px;min-height:46px}.tags{display:flex;gap:7px;flex-wrap:wrap;margin:auto 0 16px}.tags span{border-radius:50px;background:#eaf4f4;color:#145e62;padding:4px 9px;font-size:12px}
.age{display:inline-block;color:var(--muted);font-size:12px;margin:-4px 0 12px}.age.stale{color:#9b591c;background:#fff4e7;border-radius:12px;padding:2px 8px;align-self:flex-start}
.stats{display:grid;grid-template-columns:repeat(3,1fr);border-top:1px solid var(--line);padding-top:16px;gap:10px}
.stats span{display:flex;flex-direction:column;min-width:0}.stats b{font-size:14px;overflow-wrap:anywhere}.stats small{font-size:11px;color:var(--muted)}
details{border-top:1px solid var(--line);margin-top:15px;padding-top:12px;color:var(--muted);font-size:13px}summary{cursor:pointer;color:var(--blue)}
.panel{background:white;border:1px solid var(--line);border-radius:16px;padding:22px;margin-top:28px}.panel h2{font-size:19px;margin:0 0 12px}.panel ul{margin:0;padding-left:20px}.panel li{margin:7px 0}.signals li{display:flex;justify-content:space-between;gap:20px}.signals span{white-space:nowrap;color:var(--muted)}
.note{color:var(--muted);font-size:13px;margin-top:26px}.lane{margin-top:42px}.lane-head{display:flex;gap:16px;align-items:baseline;flex-wrap:wrap;margin:0 0 18px}.lane-head h2{font-size:27px;margin:0}.lane-head p{color:var(--muted);margin:0}.compact .intro{min-height:0}.claim{border-left:3px solid var(--sea);background:#f0f8f5;padding:9px 12px;font-size:13px}.jump{display:flex;gap:10px;flex-wrap:wrap;margin-top:20px}.jump a{background:#ffffff20;border:1px solid #ffffff4a;border-radius:20px;padding:7px 12px;color:white}.eyebrow-pill{font-size:12px;letter-spacing:.12em;color:var(--sea);font-weight:800}
@media(max-width:800px){.grid{grid-template-columns:1fr}.stats{gap:5px}.hero{padding:42px 24px}}
</style></head><body><header class="hero"><div class="eyebrow">AI Agent · MCP · Skills Intelligence</div><h1>每日技术情报</h1>
<p>看见热门 AI 项目，找到可以亲自使用的工具，追踪数据 Agent 与 Text-to-SQL 的新研究。</p>
<div class="hero-meta"><span>@@DATE@@</span><span>热门 @@COUNT@@ 项</span><span>可用工具 @@USECOUNT@@ 项</span><span>数据研究 @@DATACOUNT@@ 项</span><span>GitHub ≥ @@STARS@@ Stars</span></div>
<nav class="jump"><a href="#hot">01 热门项目</a><a href="#usable">02 拿来用</a><a href="#data">03 数据 Agent 前沿</a></nav></header>
<main><div class="toolbar"><h2>探索今日情报</h2><div class="filters"><input id="search" placeholder="搜索项目" aria-label="搜索项目">
<select id="category" aria-label="按项目分类筛选"><option value="">全部类别</option>@@CATEGORIES@@</select>
<select id="industry" aria-label="按行业筛选"><option value="">全部行业</option>@@INDUSTRIES@@</select></div></div>
<section class="lane" id="hot"><div class="eyebrow-pill">01 / 看见趋势</div><div class="lane-head"><h2>热门 AI 项目在做什么</h2><p>近期采集项目按平台热度排列；不足时补充历史收录并标日期。Star 不等于质量或 30 天增长。</p></div><div class="grid" id="cards">@@CARDS@@</div><div class="panel"><h3>新品与社区线索</h3><p>Product Hunt 的 RSS 提供新品线索，不提供投票数；有 API 令牌时可获取 Votes。</p><ul>@@PRODUCTS@@</ul><ul class="signals">@@SIGNALS@@</ul></div></section>
<section class="lane" id="usable"><div class="eyebrow-pill">02 / 亲自试试</div><div class="lane-head"><h2>哪些工具可以拿来用</h2><p>公开源码、Skill 安装入口或在线体验；运行反馈按可获得的证据标注。</p></div><div class="grid">@@USABLE@@</div></section>
<section class="lane" id="data"><div class="eyebrow-pill">03 / 研究与产品</div><div class="lane-head"><h2>数据 Agent 与企业分析前沿</h2><p>单独保留项目和论文席位，避免被通用热门项目挤掉。</p></div><h3>新工具与开源项目</h3><div class="grid">@@DATA@@</div><h3>近期论文与评测线索</h3><p class="note">“效果更好”只在论文或公开评测给出可核对的基线、数据集和指标时成立；以下摘要不自动认定突破。可对照 <a href="https://bird-bench.github.io/" target="_blank" rel="noopener noreferrer">BIRD</a> 与 <a href="https://spider2-sql.github.io/" target="_blank" rel="noopener noreferrer">Spider 2.0</a> 的任务定义。</p><div class="grid">@@PAPERS@@</div></section>
<footer class="panel"><h2>来源与覆盖</h2><p>@@SOURCES@@</p><ul>@@WARNINGS@@</ul></footer>
<p class="note">中文简介由公开元数据归纳，需打开原项目核对；30 天增长仅在保存足够久的同指标快照后显示。不同平台的热度指标不可直接互比。运行判断基于公开代码、安装资料和近期 Issue 抽样，不代表完整检查。</p></main>
<script>const q=document.getElementById('search'),c=document.getElementById('category'),i=document.getElementById('industry');function filter(){for(const x of document.querySelectorAll('.card')){x.hidden=!!((q.value&&!x.textContent.toLowerCase().includes(q.value.toLowerCase()))||(c.value&&x.dataset.category!==c.value)||(i.value&&x.dataset.industry!==i.value));}}[q,c,i].forEach(x=>x.addEventListener('input',filter));</script></body></html>"""
    replacements = {
        "@@DATE@@": seen_at[:10], "@@COUNT@@": str(len(rows)), "@@USECOUNT@@": str(len(usable_rows)),
        "@@DATACOUNT@@": str(len(data_rows) + len(papers)), "@@STARS@@": str(min_stars),
        "@@CATEGORIES@@": category_options, "@@INDUSTRIES@@": industry_options,
        "@@CARDS@@": "".join(cards) or "<p>当前没有符合门槛的候选项目。</p>",
        "@@USABLE@@": usable_cards or "<p>目前缺少可核对的候选工具。</p>",
        "@@DATA@@": data_cards or "<p>本次没有符合门槛的数据项目。</p>",
        "@@PAPERS@@": paper_cards or "<p>近期论文尚未采集到；请查看来源与覆盖。</p>",
        "@@PRODUCTS@@": product_cards or "<li>暂无新品数据。</li>",
        "@@SIGNALS@@": signal_cards or "<li>暂无 Reddit 线索；配置 API 凭据后将显示。</li>",
        "@@SOURCES@@": source_summary or "本次没有采集记录。",
        "@@WARNINGS@@": warning_list or "<li>各已配置来源均正常。</li>",
    }
    for token, value in replacements.items():
        template = template.replace(token, value)
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"{seen_at[:10]}.html"
    path.write_text(template, encoding="utf-8")
    return path
