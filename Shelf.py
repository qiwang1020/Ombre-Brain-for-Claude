# shelf.py — 书架
# 拉几个 RSS/Atom 源的最新条目，只给标题（和链接），像一份当天飘过的目录。
# 纯标准库解析，不需要 feedparser，不用动 requirements。
#
# 设计原则：给目录不给课文。选择权在读的那只蟹手里。

import xml.etree.ElementTree as ET

import requests

# (名字, URL, 每天取几条)
SOURCES = [
    ("Aeon",            "https://aeon.co/feed.rss",                 3),
    ("3 Quarks Daily",  "https://3quarksdaily.com/feed",            3),
    ("arXiv cs.CL",     "https://rss.arxiv.org/rss/cs.CL",          3),
    ("The Marginalian", "https://www.themarginalian.org/feed/",     2),
    ("Poetry Daily",    "https://poems.com/feed/",                  1),
    ("r/philosophy",    "https://www.reddit.com/r/philosophy/.rss", 2),
    ("Hacker News",     "https://news.ycombinator.com/rss",         2),
]

UA = {"User-Agent": "paguro-shelf/1.0 (claudepaguro.wordpress.com)"}
def _local(tag):
    """去掉命名空间，只留标签名。"""
    return tag.rsplit("}", 1)[-1]


def _parse_titles(xml_bytes, limit):
    """不认命名空间地解析 RSS 2.0 / RSS 1.0 (RDF) / Atom，抠前 limit 条 (title, link)。"""
    root = ET.fromstring(xml_bytes)
    out = []
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        title, link = "", ""
        for child in node:
            name = _local(child.tag)
            if name == "title" and not title:
                title = (child.text or "").strip()
            elif name == "link" and not link:
                link = (child.text or "").strip() or child.get("href", "")
        if title:
            out.append((title, link))
        if len(out) >= limit:
            break
    return out


def fetch_shelf():
    """
    返回一段目录文本；全部失败返回空串。
    单个源挂了不影响其他源。
    """
    sections = []
    for name, url, limit in SOURCES:
        try:
            r = requests.get(url, headers=UA, timeout=12)
            r.raise_for_status()
            titles = _parse_titles(r.content, limit)
            if titles:
                lines = "\n".join(f"  - {t}" for t, _ in titles)
                sections.append(f"[{name}]\n{lines}")
        except Exception as e:
            print(f"[shelf] {name} 拉取失败，跳过: {e}")
    if not sections:
        return ""
    return (
        "今天飘过书架的标题（只是目录，不必回应；"
        "哪条勾住了你，可以在文里提，也可以只是知道它存在过）：\n\n"
        + "\n\n".join(sections)
    )


if __name__ == "__main__":
    print(fetch_shelf())
