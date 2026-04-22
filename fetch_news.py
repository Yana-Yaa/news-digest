#!/usr/bin/env python3
"""Daily news digest — fetches RSS feeds, ranks and summarizes with Gemini, emails to Kindle."""

import feedparser
import smtplib
import ssl
import os
import html
import re
import time
import json
from datetime import datetime
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from ebooklib import epub
import requests
import trafilatura
from google import genai

TOP_FINNISH    = 8
TOP_GLOBAL     = 12
FETCH_PER_SOURCE = 8   # fetch more candidates, Gemini picks the best

FEEDS = {
    'Finnish': [
        ('YLE',            'https://feeds.yle.fi/uutiset/v1/majorHeadlines/YLE_UUTISET.rss'),
        ('Helsinki Times', 'https://www.helsinkitimes.fi/feeds/articles.rss'),
        ('Kauppalehti',    'https://www.kauppalehti.fi/5/i/rss/uutiset.rss'),
        ('Ilta-Sanomat',   'https://www.is.fi/rss/tuoreimmat.xml'),
    ],
    'Global': [
        ('BBC',            'http://feeds.bbci.co.uk/news/rss.xml'),
        ('Reuters',        'https://feeds.reuters.com/reuters/topNews'),
        ('AP News',        'https://rsshub.app/apnews/topics/apf-topnews'),
        ('The Guardian',   'https://www.theguardian.com/world/rss'),
        ('Al Jazeera',     'https://www.aljazeera.com/xml/rss/all.xml'),
        ('Deutsche Welle', 'https://rss.dw.com/rdf/rss-en-world'),
        ('France 24',      'https://www.france24.com/en/rss'),
        ('NPR',            'https://feeds.npr.org/1001/rss.xml'),
        ('Financial Times','https://www.ft.com/rss/home/uk'),
    ],
    'Social': [
        ('r/Finland', 'https://www.reddit.com/r/Finland/top.rss?t=day'),
        ('r/Suomi',   'https://www.reddit.com/r/Suomi/top.rss?t=day'),
    ],
}

CSS = '''
body { font-family: Georgia, serif; font-size: 1.15em; margin: 1.5em; line-height: 1.8; color: #111; }
h1   { font-size: 1.6em; border-bottom: 2px solid #222; padding-bottom: 0.3em; margin-bottom: 1em; }
h2   { font-size: 1.25em; margin: 0 0 0.2em 0; }
h3   { font-size: 1.1em; margin: 1em 0 0.3em 0; color: #333; }
.meta     { color: #888; font-size: 0.95em; font-style: italic; margin: 0 0 0.5em 0; }
.summary  { margin: 0; font-size: 1em; }
.original { font-size: 0.88em; color: #666; font-style: italic; margin-top: 0.7em;
            border-left: 3px solid #ddd; padding-left: 0.8em; }
.readmore { font-size: 0.95em; margin-top: 0.5em; }
.article  { margin-bottom: 2em; padding-bottom: 1.5em; border-bottom: 1px solid #ddd; }
.buzz     { line-height: 1.7; }
a { color: #0055aa; }
'''


# ── helpers ──────────────────────────────────────────────────────────────────

def strip_html(text: str) -> str:
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return html.unescape(text).strip()


def buzz_to_html(text: str) -> str:
    parts = []
    for line in text.split('\n'):
        line = line.strip()
        if not line:
            continue
        # Bold **text** → <strong>
        line = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', line)
        if re.match(r'^\d+\.', line):
            parts.append(f'<h3>{line}</h3>')
        else:
            parts.append(f'<p>{line}</p>')
    return ''.join(parts)


# ── Gemini ────────────────────────────────────────────────────────────────────

def init_gemini() -> genai.Client:
    return genai.Client(api_key=os.environ['GEMINI_API_KEY'])


def _call(client, prompt: str) -> str:
    for attempt in range(3):
        try:
            time.sleep(4)   # 15 RPM = 1 req/4s
            response = client.models.generate_content(
                model='gemini-2.0-flash-lite', contents=prompt)
            return response.text.strip()
        except Exception as e:
            msg = str(e)
            if '429' in msg and 'retryDelay' in msg:
                m = re.search(r'"retryDelay":\s*"(\d+)s"', msg)
                wait = int(m.group(1)) + 5 if m else 60
                print(f'[RATE LIMIT] waiting {wait}s...')
                time.sleep(wait)
            else:
                raise
    raise RuntimeError('Gemini rate limit: too many retries')


def rank_articles(client, articles: list, top_n: int, section: str) -> list:
    """One Gemini call: select top_n most important unique articles."""
    lines = [f"{i}: {a['title']} — {a['summary'][:120]}"
             for i, a in enumerate(articles)]
    prompt = (
        f"You are a news editor. From these {len(articles)} {section} articles "
        f"select the {top_n} most important and unique stories. "
        f"Merge duplicates (same event from multiple sources — keep one). "
        f"Return ONLY a JSON array of integer indices in importance order, e.g. [3,0,7].\n\n"
        + '\n'.join(lines)
    )
    try:
        text = _call(client, prompt)
        m = re.search(r'\[[\d,\s]+\]', text)
        if m:
            indices = json.loads(m.group())
            seen, result = set(), []
            for i in indices:
                if i < len(articles) and i not in seen:
                    seen.add(i)
                    result.append(articles[i])
            return result[:top_n]
    except Exception as e:
        print(f'[WARN] Ranking failed: {e}')
    return articles[:top_n]


def _fetch_article_text(url: str, retries: int = 2) -> str:
    """Download article with timeout and extract clean text. Retries on connection errors."""
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, timeout=8,
                                headers={'User-Agent': 'Mozilla/5.0'})
            text = trafilatura.extract(resp.text, include_comments=False,
                                       include_tables=False, no_fallback=False)
            return text or ''
        except requests.exceptions.Timeout:
            return ''   # no point retrying a slow site
        except requests.exceptions.ConnectionError:
            if attempt < retries:
                time.sleep(2)
        except Exception:
            return ''
    return ''


def summarize(client, article: dict, translate: bool = False) -> dict:
    """Fetch full article content and summarize (+ optionally translate) with Gemini."""
    full = _fetch_article_text(article['link'])
    content = full[:5000] if len(full) > len(article['summary']) else article['summary']

    if translate:
        prompt = (
            "Translate this Finnish news article to English and summarize it in exactly "
            "4 clear, informative sentences for a daily digest reader.\n"
            "Respond in this exact format (nothing else):\n"
            "TITLE: [English title]\n"
            "SUMMARY: [4-sentence English summary]\n\n"
            f"Finnish title: {article['title']}\n\n{content}"
        )
        try:
            text = _call(client, prompt)
            title_m   = re.search(r'TITLE:\s*(.+)',          text)
            summary_m = re.search(r'SUMMARY:\s*([\s\S]+)', text)
            result = dict(article)
            result['title']        = title_m.group(1).strip()   if title_m   else article['title']
            result['summary']      = summary_m.group(1).strip() if summary_m else text
            result['title_orig']   = article['title']
            result['summary_orig'] = article['summary']
            result['translated']   = True
            return result
        except Exception as e:
            print(f'[WARN] Summarize/translate failed for "{article["title"][:40]}": {e}')
            return article
    else:
        prompt = (
            "Summarize this news article in exactly 4 clear, informative sentences "
            "for a daily digest reader. Be factual and capture the key points.\n\n"
            f"Title: {article['title']}\n\n{content}"
        )
        try:
            result = dict(article)
            result['summary'] = _call(client, prompt)
            return result
        except Exception as e:
            print(f'[WARN] Summarize failed for "{article["title"][:40]}": {e}')
            return article


def fetch_social_buzz(client) -> str:
    """Fetch top Reddit posts from Finnish communities and ask Gemini for themes."""
    headers = {
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json',
    }
    posts = []
    for subreddit in ('Finland', 'Suomi'):
        fetched = False
        for base in ('https://www.reddit.com', 'https://old.reddit.com'):
            try:
                url = f'{base}/r/{subreddit}/top.json?t=day&limit=20'
                resp = requests.get(url, headers=headers, timeout=15)
                if not resp.text.strip():
                    continue
                data = resp.json()
                titles = [child['data']['title']
                          for child in data['data']['children']
                          if not child['data'].get('stickied')]
                for t in titles[:15]:
                    posts.append(f'[r/{subreddit}] {t}')
                print(f'[OK]   r/{subreddit}: {len(titles)} posts')
                fetched = True
                break
            except Exception as e:
                print(f'[WARN] r/{subreddit} ({base}): {e}')
        if not fetched:
            print(f'[WARN] r/{subreddit}: all sources failed')

    if not posts:
        return ''

    prompt = (
        "These are today's top posts from Finnish Reddit communities (r/Finland and r/Suomi).\n"
        "Identify the 5 most discussed themes and write 2-3 sentences about each, "
        "describing what Finns are talking about today.\n"
        "Format as a numbered list with a bold theme title.\n\n"
        + '\n'.join(posts)
    )
    try:
        return _call(client, prompt)
    except Exception as e:
        print(f'[WARN] Social buzz failed: {e}')
        return ''


# ── RSS fetching ──────────────────────────────────────────────────────────────

def fetch_candidates(feeds: list) -> list:
    """Fetch raw RSS articles (no AI processing yet)."""
    articles, seen = [], set()
    for source, url in feeds:
        try:
            feed = feedparser.parse(url, request_headers={'User-Agent': 'Mozilla/5.0'})
            count = 0
            for entry in feed.entries:
                if count >= FETCH_PER_SOURCE:
                    break
                title   = strip_html(entry.get('title', ''))
                summary = strip_html(entry.get('summary', entry.get('description', '')))
                link    = entry.get('link', '')
                if not title or title in seen:
                    continue
                seen.add(title)
                articles.append({'title': title, 'summary': summary, 'link': link,
                                  'source': source, 'translated': False})
                count += 1
            print(f'[OK]   {source}: {count} candidates')
        except Exception as exc:
            print(f'[WARN] {source}: {exc}')
    return articles


# ── rendering ─────────────────────────────────────────────────────────────────

def article_html(art: dict) -> str:
    orig = ''
    if art.get('translated'):
        orig = (f'<p class="original">'
                f'<strong>Suomeksi:</strong> {art["title_orig"]}<br/>'
                f'{art["summary_orig"]}</p>')
    return (
        f'<div class="article">'
        f'<h2>{art["title"]}</h2>'
        f'<p class="meta">{art["source"]}</p>'
        f'<p class="summary">{art["summary"]}</p>'
        f'{orig}'
        f'<p class="readmore"><a href="{art["link"]}">Full article →</a></p>'
        f'</div>'
    )


def build_epub(finnish: list, global_news: list, buzz: str, label: str) -> str:
    book = epub.EpubBook()
    book.set_identifier(f'news-{label}')
    book.set_title(f'News Digest — {label}')
    book.set_language('en')

    css_item = epub.EpubItem(uid='css', file_name='style.css',
                              media_type='text/css', content=CSS)
    book.add_item(css_item)

    def make_chapter(title: str, body_html: str, fname: str) -> epub.EpubHtml:
        ch = epub.EpubHtml(title=title, file_name=fname, lang='en')
        ch.content = f'<html><body><h1>{title}</h1>{body_html}</body></html>'
        ch.add_item(css_item)
        book.add_item(ch)
        return ch

    ch1 = make_chapter('Finnish News', ''.join(article_html(a) for a in finnish), 'finnish.xhtml')
    ch2 = make_chapter('Global News',  ''.join(article_html(a) for a in global_news), 'global.xhtml')
    chapters = [ch1, ch2]

    if buzz:
        ch3 = make_chapter('Finnish Social Buzz', f'<div class="buzz">{buzz_to_html(buzz)}</div>', 'buzz.xhtml')
        chapters.append(ch3)

    book.toc   = chapters
    book.spine = ['nav'] + chapters
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    path = f'news_digest_{label}.epub'
    epub.write_epub(path, book)
    return path


# ── email ─────────────────────────────────────────────────────────────────────

def _smtp_send(sender: str, password: str, recipient: str, msg) -> None:
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL('smtp.gmail.com', 465, context=ctx) as server:
        server.login(sender, password)
        server.sendmail(sender, recipient, msg.as_string())
    print(f'Sent to {recipient}')


def send_to_kindle(epub_path: str, label: str) -> None:
    sender    = os.environ['GMAIL_ADDRESS']
    password  = os.environ['GMAIL_APP_PASSWORD']
    recipient = os.environ['KINDLE_ADDRESS']

    msg = MIMEMultipart()
    msg['From']    = sender
    msg['To']      = recipient
    msg['Subject'] = f'News Digest {label}'
    msg.attach(MIMEText('Your news digest is attached.', 'plain'))

    with open(epub_path, 'rb') as f:
        part = MIMEBase('application', 'epub+zip')
        part.set_payload(f.read())
    encoders.encode_base64(part)
    part.add_header('Content-Disposition',
                    f'attachment; filename="{os.path.basename(epub_path)}"')
    msg.attach(part)

    _smtp_send(sender, password, recipient, msg)


def send_html_email(finnish: list, global_news: list, buzz: str, label: str) -> None:
    sender    = os.environ['GMAIL_ADDRESS']
    password  = os.environ['GMAIL_APP_PASSWORD']
    recipient = os.environ['GMAIL_ADDRESS']

    def art_block(a: dict) -> str:
        orig = ''
        if a.get('translated'):
            orig = (f'<div style="font-size:0.88em;color:#666;font-style:italic;'
                    f'margin-top:0.7em;border-left:3px solid #ddd;padding-left:0.8em">'
                    f'<strong>Suomeksi:</strong> {a["title_orig"]}<br/>{a["summary_orig"]}</div>')
        return (
            f'<div style="margin-bottom:1.8em;padding-bottom:1.5em;border-bottom:1px solid #ddd">'
            f'<h2 style="font-size:1.2em;margin:0 0 0.2em 0">{a["title"]}</h2>'
            f'<p style="color:#888;font-size:0.95em;margin:0 0 0.4em 0">{a["source"]}</p>'
            f'<p style="margin:0">{a["summary"]}</p>'
            f'{orig}'
            f'<p style="margin:0.5em 0 0 0">'
            f'<a href="{a["link"]}" style="color:#0055aa;font-size:0.95em">Full article →</a></p>'
            f'</div>'
        )

    def section_html(title: str, inner: str) -> str:
        return (f'<h1 style="font-size:1.5em;border-bottom:2px solid #222;'
                f'padding-bottom:0.3em;margin-top:1.5em">{title}</h1>{inner}')

    buzz_html = ''
    if buzz:
        buzz_html = section_html('Finnish Social Buzz',
                                 f'<div style="line-height:1.8">{buzz_to_html(buzz)}</div>')

    body = (
        f'<html><body style="font-family:Georgia,serif;font-size:18px;max-width:740px;'
        f'margin:auto;padding:1.5em;line-height:1.8;color:#111">'
        f'{section_html("Finnish News",  "".join(art_block(a) for a in finnish))}'
        f'{section_html("Global News",   "".join(art_block(a) for a in global_news))}'
        f'{buzz_html}'
        f'</body></html>'
    )

    msg = MIMEMultipart('alternative')
    msg['From']    = sender
    msg['To']      = recipient
    msg['Subject'] = f'News Digest {label}'
    msg.attach(MIMEText(body, 'html'))

    _smtp_send(sender, password, recipient, msg)


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    label  = datetime.now().strftime('%Y-%m-%d %H%M')
    client = init_gemini()

    print('Fetching candidates...')
    finnish_cands = fetch_candidates(FEEDS['Finnish'])
    global_cands  = fetch_candidates(FEEDS['Global'])

    print('Ranking articles...')
    finnish     = rank_articles(client, finnish_cands, TOP_FINNISH, 'Finnish')
    global_news = rank_articles(client, global_cands,  TOP_GLOBAL,  'global')

    print('Summarizing Finnish articles...')
    finnish = [summarize(client, a, translate=True) for a in finnish]

    print('Summarizing global articles...')
    global_news = [summarize(client, a, translate=False) for a in global_news]

    print('Fetching social buzz...')
    buzz = fetch_social_buzz(client)

    print(f'Finnish: {len(finnish)}, Global: {len(global_news)}, Buzz: {bool(buzz)}')

    epub_path = build_epub(finnish, global_news, buzz, label)
    send_to_kindle(epub_path, label)
    os.remove(epub_path)
    send_html_email(finnish, global_news, buzz, label)
    print('Done.')


if __name__ == '__main__':
    main()
