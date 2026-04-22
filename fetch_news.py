#!/usr/bin/env python3
"""Daily news digest — fetches RSS feeds, generates EPUB, emails to Kindle."""

import feedparser
import smtplib
import ssl
import os
import html
import re
from datetime import datetime
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from ebooklib import epub

MAX_ARTICLES = 20
MAX_PER_SOURCE = 4

FEEDS = {
    'Finnish': [
        ('YLE',            'https://feeds.yle.fi/uutiset/v1/majorHeadlines/YLE_UUTISET.rss'),
        ('Helsinki Times', 'https://www.helsinkitimes.fi/feeds/articles.rss'),
        ('Kauppalehti',    'https://www.kauppalehti.fi/5/i/rss/uutiset.rss'),
        ('Ilta-Sanomat',   'https://www.is.fi/rss/tuoreimmat.xml'),
    ],
    'Global': [
        ('BBC',           'http://feeds.bbci.co.uk/news/rss.xml'),
        ('Reuters',       'https://feeds.reuters.com/reuters/topNews'),
        ('AP News',       'https://rsshub.app/apnews/topics/apf-topnews'),
        ('The Guardian',  'https://www.theguardian.com/world/rss'),
        ('Al Jazeera',    'https://www.aljazeera.com/xml/rss/all.xml'),
        ('Deutsche Welle','https://rss.dw.com/rdf/rss-en-world'),
        ('France 24',     'https://www.france24.com/en/rss'),
        ('NPR',           'https://feeds.npr.org/1001/rss.xml'),
        ('Financial Times','https://www.ft.com/rss/home/uk'),
    ],
}

CSS = '''
body { font-family: Georgia, serif; margin: 1.5em; line-height: 1.75; color: #111; }
h1   { font-size: 1.5em; border-bottom: 2px solid #222; padding-bottom: 0.3em; margin-bottom: 1em; }
h2   { font-size: 1.1em; margin: 0 0 0.2em 0; }
.meta    { color: #888; font-size: 0.85em; font-style: italic; margin: 0 0 0.5em 0; }
.summary { margin: 0; }
.readmore{ font-size: 0.85em; margin-top: 0.4em; }
.article { margin-bottom: 2em; padding-bottom: 1.5em; border-bottom: 1px solid #ddd; }
a { color: #0055aa; }
'''


def strip_html(text: str) -> str:
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return html.unescape(text).strip()


def fetch_section(feeds: list, max_total: int) -> list:
    articles = []
    seen = set()
    for source, url in feeds:
        try:
            feed = feedparser.parse(url, request_headers={'User-Agent': 'Mozilla/5.0'})
            count = 0
            for entry in feed.entries:
                if count >= MAX_PER_SOURCE:
                    break
                title   = strip_html(entry.get('title', ''))
                summary = strip_html(entry.get('summary', entry.get('description', '')))
                link    = entry.get('link', '')
                if not title or title in seen:
                    continue
                seen.add(title)
                articles.append({'title': title, 'summary': summary,
                                  'link': link, 'source': source})
                count += 1
            print(f'[OK]   {source}: {count} articles')
        except Exception as exc:
            print(f'[WARN] {source}: {exc}')
    return articles[:max_total]


def article_html(art: dict) -> str:
    return (
        f'<div class="article">'
        f'<h2>{art["title"]}</h2>'
        f'<p class="meta">{art["source"]}</p>'
        f'<p class="summary">{art["summary"]}</p>'
        f'<p class="readmore"><a href="{art["link"]}">Full article →</a></p>'
        f'</div>'
    )


def build_epub(finnish: list, global_news: list, label: str) -> str:
    book = epub.EpubBook()
    book.set_identifier(f'news-{label}')
    book.set_title(f'News Digest — {label}')
    book.set_language('en')

    css_item = epub.EpubItem(uid='css', file_name='style.css',
                              media_type='text/css', content=CSS)
    book.add_item(css_item)

    def make_chapter(title: str, articles: list, fname: str) -> epub.EpubHtml:
        body = f'<h1>{title}</h1>' + ''.join(article_html(a) for a in articles)
        ch = epub.EpubHtml(title=title, file_name=fname, lang='en')
        ch.content = f'<html><body>{body}</body></html>'
        ch.add_item(css_item)
        book.add_item(ch)
        return ch

    ch1 = make_chapter('Finnish News',  finnish,     'finnish.xhtml')
    ch2 = make_chapter('Global News',   global_news, 'global.xhtml')

    book.toc   = [ch1, ch2]
    book.spine = ['nav', ch1, ch2]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    path = f'news_digest_{label}.epub'
    epub.write_epub(path, book)
    return path


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


def send_html_email(finnish: list, global_news: list, label: str) -> None:
    sender    = os.environ['GMAIL_ADDRESS']
    password  = os.environ['GMAIL_APP_PASSWORD']
    recipient = os.environ['GMAIL_ADDRESS']

    def section(title: str, articles: list) -> str:
        items = ''.join(
            f'<div style="margin-bottom:1.8em;padding-bottom:1.5em;border-bottom:1px solid #ddd">'
            f'<h2 style="font-size:1.05em;margin:0 0 0.2em 0">{a["title"]}</h2>'
            f'<p style="color:#888;font-size:0.85em;margin:0 0 0.4em 0">{a["source"]}</p>'
            f'<p style="margin:0">{a["summary"]}</p>'
            f'<p style="margin:0.4em 0 0 0"><a href="{a["link"]}" style="color:#0055aa;font-size:0.85em">Full article →</a></p>'
            f'</div>'
            for a in articles
        )
        return (f'<h1 style="font-size:1.3em;border-bottom:2px solid #222;padding-bottom:0.3em">'
                f'{title}</h1>{items}')

    body = (
        f'<html><body style="font-family:Georgia,serif;max-width:700px;margin:auto;'
        f'padding:1.5em;line-height:1.75;color:#111">'
        f'{section("Finnish News", finnish)}'
        f'{section("Global News", global_news)}'
        f'</body></html>'
    )

    msg = MIMEMultipart('alternative')
    msg['From']    = sender
    msg['To']      = recipient
    msg['Subject'] = f'News Digest {label}'
    msg.attach(MIMEText(body, 'html'))

    _smtp_send(sender, password, recipient, msg)


def main() -> None:
    label       = datetime.now().strftime('%Y-%m-%d %H%M')
    finnish     = fetch_section(FEEDS['Finnish'], max_total=8)
    global_news = fetch_section(FEEDS['Global'],  max_total=MAX_ARTICLES - len(finnish))

    print(f'Finnish: {len(finnish)}, Global: {len(global_news)}')

    epub_path = build_epub(finnish, global_news, label)
    send_to_kindle(epub_path, label)
    os.remove(epub_path)
    send_html_email(finnish, global_news, label)
    print('Done.')


if __name__ == '__main__':
    main()
