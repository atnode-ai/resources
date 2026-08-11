#!/usr/bin/env python3
"""
substack_to_markdown.py
=======================

Extract **free** Substack blog posts to Markdown files.

Adapted for a sandboxed / headless / proxied environment from
timf34/Substack2Markdown (MIT License, https://github.com/timf34/Substack2Markdown).

This version implements the FREE-content path only:
    requests + BeautifulSoup + html2text
It has NO Selenium / browser dependency, so it cannot log in or read
premium / paywalled posts. Paywalled posts are detected and skipped.
(For premium content you must run the upstream project locally with a
browser and your own Substack credentials.)

Post discovery order (first that yields results wins):
    1. Substack archive API   <pub>/api/v1/archive?sort=new   (newest-first)
    2. sitemap.xml            <pub>/sitemap.xml
    3. feed.xml               <pub>/feed.xml                  (~22 most recent)

Per-post extraction selectors mirror the upstream tool:
    title    -> h1.post-title, h2
    subtitle -> h3.subtitle, div.subtitle-*
    date/author/cover-image -> <script type="application/ld+json">
    likes    -> div.like-button-container button div.label
    body     -> div.available-content   (paywall -> h2.paywall-title -> skip)

Usage
-----
    # Whole publication (all free posts)
    python3 substack_to_markdown.py --url https://example.substack.com

    # Latest N posts only
    python3 substack_to_markdown.py --url https://example.substack.com --number 5

    # A single post
    python3 substack_to_markdown.py --url https://example.substack.com/p/some-post

    # Download images locally and rewrite links; emit MDX YAML frontmatter
    python3 substack_to_markdown.py --url https://example.substack.com --images --frontmatter mdx

    # Just list discovered post URLs, don't scrape
    python3 substack_to_markdown.py --url https://example.substack.com --list-only

Output
------
    <output-dir>/<writer>/<slug>.md      one markdown file per post
    <output-dir>/<writer>/_manifest.json metadata for the scraped posts
"""

import argparse
import hashlib
import json
import mimetypes
import os
import random
import re
import sys
from datetime import datetime
from pathlib import Path
from time import sleep
from urllib.parse import unquote, urlparse
from xml.etree import ElementTree as ET

import html2text
import requests
from bs4 import BeautifulSoup

DEFAULT_OUTPUT_DIR = "substack_md_files"
DEFAULT_KEYWORDS = ["about", "archive", "podcast"]
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# tqdm is optional; fall back to a no-op iterator wrapper if unavailable.
try:
    from tqdm import tqdm
except Exception:  # pragma: no cover
    def tqdm(iterable=None, **kwargs):
        return iterable if iterable is not None else _NullBar()

    class _NullBar:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def update(self, *a, **k):
            pass

        def write(self, msg):
            print(msg)


# ---------------------------------------------------------------------------
# URL / image helpers (ported from upstream)
# ---------------------------------------------------------------------------
def is_post_url(url: str) -> bool:
    """A specific post URL contains '/p/'."""
    return "/p/" in url


def get_publication_url(url: str) -> str:
    """Base publication URL (scheme://netloc/) from any URL."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/"


def get_post_slug(url: str) -> str:
    """Extract the post slug from a '/p/<slug>' URL."""
    match = re.search(r"/p/([^/?#]+)", url)
    return match.group(1) if match else "unknown_post"


def extract_main_part(url: str) -> str:
    """Human-friendly publication name from the host.

    niallferguson.substack.com -> niallferguson
    www.theinformation.com      -> theinformation
    """
    netloc = urlparse(url).netloc
    parts = [p for p in netloc.split(".") if p]
    if parts and parts[0] == "www":
        parts = parts[1:]
    if len(parts) >= 3 and parts[-2] == "substack" and parts[-1] == "com":
        return parts[0]
    if len(parts) >= 2:
        return parts[-2]
    return parts[0] if parts else "substack"


def slug_from_url(url: str) -> str:
    """Filename-safe slug for any post URL."""
    if is_post_url(url):
        return get_post_slug(url)
    tail = url.rstrip("/").split("/")[-1]
    tail = re.sub(r"[<>:\"/\\|?*]", "", tail)
    return tail or "post"


def resolve_image_url(url: str) -> str:
    """Recover the original image URL from a Substack CDN fetch URL."""
    if url.startswith("https://substackcdn.com/image/fetch/"):
        bits = url.split("/https%3A%2F%2F")
        if len(bits) > 1:
            url = "https://" + unquote(bits[1])
    return url


def clean_linked_images(md_content: str) -> str:
    """Convert [![alt](img)](link) to plain ![alt](img)."""
    return re.sub(r"\[!\[(.*?)\]\((.*?)\)\]\(.*?\)", r"![\1](\2)", md_content)


def sanitize_image_filename(url: str, session: requests.Session) -> str:
    url = resolve_image_url(url)
    filename = url.split("/")[-1].split("?")[0]
    filename = re.sub(r"[<>:\"/\\|?*]", "", filename)
    if len(filename) > 100 or not filename:
        h = hashlib.md5(url.encode()).hexdigest()
        try:
            ext = mimetypes.guess_extension(
                session.head(url, timeout=20).headers.get("content-type", "")
            ) or ".jpg"
        except Exception:
            ext = ".jpg"
        filename = f"{h}{ext}"
    return filename


def download_image(url: str, save_path: Path, session: requests.Session) -> bool:
    try:
        r = session.get(resolve_image_url(url), stream=True, timeout=60)
        if r.status_code == 200:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            with open(save_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            return True
    except Exception as e:
        print(f"  ! image download failed {url}: {e}")
    return False


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------
class SubstackScraper:
    def __init__(
        self,
        url: str,
        output_dir: str = DEFAULT_OUTPUT_DIR,
        download_images: bool = False,
        frontmatter_format: str = "legacy",
        keywords=None,
        delay: float = 0.5,
        quiet: bool = False,
    ):
        if frontmatter_format not in ("legacy", "mdx"):
            raise ValueError("frontmatter_format must be 'legacy' or 'mdx'")
        self.frontmatter_format = frontmatter_format
        self.delay = delay
        self.quiet = quiet
        self.download_images = download_images
        self.keywords = keywords if keywords is not None else list(DEFAULT_KEYWORDS)

        self.is_single_post = is_post_url(url)
        self.original_url = url
        self.base_url = get_publication_url(url)
        if not self.base_url.endswith("/"):
            self.base_url += "/"

        self.writer_name = extract_main_part(self.base_url)
        self.md_save_dir = os.path.join(output_dir, self.writer_name)
        self.image_dir = os.path.join(output_dir, self.writer_name, "images")
        os.makedirs(self.md_save_dir, exist_ok=True)

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    def log(self, *a):
        if not self.quiet:
            print(*a)

    # --- post discovery ---------------------------------------------------
    def discover_post_urls(self, max_posts: int = 0):
        if self.is_single_post:
            return [self.original_url]
        urls = self._from_archive_api(max_posts)
        source = "archive-api"
        if not urls:
            urls = self._from_sitemap()
            source = "sitemap.xml"
        if not urls:
            urls = self._from_feed()
            source = "feed.xml"
        urls = self._filter(urls)
        self.log(f"Discovered {len(urls)} post URLs via {source}.")
        return urls

    def _from_archive_api(self, max_posts: int = 0):
        urls, offset, page = [], 0, 50
        while True:
            api = (
                f"{self.base_url}api/v1/archive?sort=new&search=&"
                f"offset={offset}&limit={page}"
            )
            try:
                r = self.session.get(api, timeout=30)
                if not r.ok:
                    break
                data = r.json()
            except Exception:
                break
            if not data:
                break
            for item in data:
                cu = item.get("canonical_url")
                if cu:
                    urls.append(cu)
            offset += page
            if max_posts and len(urls) >= max_posts:
                break
            if len(data) < page:
                break
            sleep(self.delay)
        return urls

    def _from_sitemap(self):
        try:
            r = self.session.get(f"{self.base_url}sitemap.xml", timeout=30)
            if not r.ok:
                self.log(f"sitemap.xml -> {r.status_code}")
                return []
            root = ET.fromstring(r.content)
            ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}loc"
            return [e.text for e in root.iter(ns) if e.text]
        except Exception as e:
            self.log(f"sitemap.xml error: {e}")
            return []

    def _from_feed(self):
        try:
            r = self.session.get(f"{self.base_url}feed.xml", timeout=30)
            if not r.ok:
                return []
            root = ET.fromstring(r.content)
            out = []
            for item in root.findall(".//item"):
                link = item.find("link")
                if link is not None and link.text:
                    out.append(link.text)
            return out
        except Exception as e:
            self.log(f"feed.xml error: {e}")
            return []

    def _filter(self, urls):
        seen, out = set(), []
        for u in urls:
            if not u or u in seen:
                continue
            # keep only real posts; drop section/about/archive/podcast pages
            if "/p/" not in u:
                continue
            if any(k in u for k in self.keywords):
                continue
            seen.add(u)
            out.append(u)
        return out

    # --- fetch + convert --------------------------------------------------
    def get_soup(self, url: str, max_attempts: int = 5):
        for attempt in range(1, max_attempts + 1):
            try:
                page = self.session.get(url, timeout=45)
            except Exception as e:
                raise ValueError(f"Error fetching {url}: {e}") from e
            soup = BeautifulSoup(page.content, "html.parser")
            if soup.find("h2", class_="paywall-title"):
                self.log(f"Skipping premium (paywalled) article: {url}")
                return None
            pre = soup.select_one("body > pre")
            if pre and "too many requests" in pre.text.lower():
                if attempt == max_attempts:
                    raise RuntimeError(f"Rate limited (max attempts) for {url}")
                base = 2 ** attempt
                wait = base + random.uniform(-0.2 * base, 0.2 * base)
                self.log(f"[{attempt}/{max_attempts}] 429, retrying in {wait:.1f}s...")
                sleep(wait)
                continue
            return soup
        raise RuntimeError(f"Failed to fetch after {max_attempts} attempts: {url}")

    @staticmethod
    def html_to_md(html_content: str) -> str:
        h = html2text.HTML2Text()
        h.ignore_links = False
        h.body_width = 0
        return h.handle(html_content or "")

    def extract(self, soup: BeautifulSoup):
        title_el = soup.select_one("h1.post-title, h2")
        title = title_el.text.strip() if title_el else "Untitled"

        sub_el = soup.select_one("h3.subtitle")
        subtitle = sub_el.text.strip() if sub_el else ""
        # Guard against truncated teaser elements ("...") and pure punctuation.
        if subtitle and set(subtitle) <= set(". …"):
            subtitle = ""

        date, author, cover_image = "", "", ""
        ld = soup.find("script", {"type": "application/ld+json"})
        if ld and ld.string:
            try:
                data = json.loads(ld.string)
                if data.get("datePublished"):
                    dt = datetime.fromisoformat(
                        data["datePublished"].replace("Z", "+00:00")
                    )
                    date = dt.strftime("%Y-%m-%d")
                a = data.get("author")
                if isinstance(a, list) and a:
                    author = a[0].get("name", "")
                elif isinstance(a, dict):
                    author = a.get("name", "")
                img = data.get("image")
                if isinstance(img, list) and img:
                    first = img[0]
                    cover_image = first.get("url", "") if isinstance(first, dict) else str(first)
                elif isinstance(img, dict):
                    cover_image = img.get("url", "")
            except (json.JSONDecodeError, ValueError, KeyError):
                pass
        if not date:
            date = "Date not found"

        like_el = soup.select_one("div.like-button-container button div.label")
        like_count = (
            like_el.text.strip()
            if like_el and like_el.text.strip().isdigit()
            else "0"
        )

        content_el = soup.select_one("div.available-content")
        md_body = self.html_to_md(str(content_el) if content_el else "")
        ok = title != "Untitled" and content_el is not None
        return {
            "title": title,
            "subtitle": subtitle,
            "author": author,
            "date": date,
            "cover_image": cover_image,
            "like_count": like_count,
            "md_body": md_body,
            "ok": ok,
        }

    def combine(self, m) -> str:
        if self.frontmatter_format == "mdx":
            def q(s):
                return (s or "").replace('"', '\\"')
            fm = ["---", f'title: "{q(m["title"])}"']
            if m["subtitle"]:
                fm.append(f'subtitle: "{q(m["subtitle"])}"')
            fm.append(f'date: "{m["date"]}"')
            fm.append(f'author: "{q(m["author"])}"')
            if m["cover_image"]:
                fm.append(f'image: "{m["cover_image"]}"')
            fm.append("---\n")
            return "\n".join(fm) + "\n" + m["md_body"]

        disp = m["date"]
        if m["date"] and m["date"] != "Date not found":
            try:
                disp = datetime.fromisoformat(m["date"]).strftime("%b %d, %Y")
            except ValueError:
                pass
        head = f"# {m['title']}\n\n"
        if m["subtitle"]:
            head += f"## {m['subtitle']}\n\n"
        head += f"**{disp}**\n\n"
        head += f"**Likes:** {m['like_count']}\n\n"
        return head + m["md_body"]

    def process_images(self, md_content: str, slug: str) -> str:
        md_content = clean_linked_images(md_content)
        out_dir = Path(self.image_dir) / slug

        def repl(match):
            alt, url = match.group(1), match.group(2)
            if not url.startswith("http"):
                return match.group(0)
            fname = sanitize_image_filename(url, self.session)
            local = out_dir / fname
            if download_image(url, local, self.session):
                rel = os.path.relpath(local, self.md_save_dir).replace("\\", "/")
                return f"![{alt}]({rel})"
            return match.group(0)

        return re.sub(r"!\[(.*?)\]\((.*?)\)", repl, md_content)

    # --- main loop --------------------------------------------------------
    def run(self, num_posts: int = 0, list_only: bool = False):
        post_urls = self.discover_post_urls(max_posts=num_posts)
        if num_posts and len(post_urls) > num_posts:
            post_urls = post_urls[:num_posts]

        if list_only:
            for u in post_urls:
                print(u)
            return {"writer": self.writer_name, "discovered": len(post_urls), "saved": 0,
                    "skipped": 0, "files": [], "output_dir": self.md_save_dir}

        manifest = []
        skipped = 0
        with tqdm(total=len(post_urls), desc="Scraping posts") as pbar:
            for url in post_urls:
                slug = slug_from_url(url)
                md_path = os.path.join(self.md_save_dir, f"{slug}.md")
                if os.path.exists(md_path):
                    pbar.write(f"exists, skipping: {md_path}")
                    pbar.update(1)
                    continue
                try:
                    soup = self.get_soup(url)
                    if soup is None:  # paywalled
                        skipped += 1
                        pbar.update(1)
                        sleep(self.delay)
                        continue
                    m = self.extract(soup)
                    if not m["ok"]:
                        pbar.write(f"[skip] extraction failed: {url}")
                        skipped += 1
                        pbar.update(1)
                        sleep(self.delay)
                        continue
                    md = self.combine(m)
                    if self.download_images:
                        md = self.process_images(md, slug)
                    with open(md_path, "w", encoding="utf-8") as f:
                        f.write(md)
                    manifest.append({
                        "title": m["title"], "subtitle": m["subtitle"],
                        "author": m["author"], "date": m["date"],
                        "like_count": m["like_count"], "url": url,
                        "file": md_path,
                    })
                except Exception as e:
                    pbar.write(f"[error] {url}: {e}")
                    skipped += 1
                pbar.update(1)
                sleep(self.delay)

        manifest_path = os.path.join(self.md_save_dir, "_manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

        summary = {
            "writer": self.writer_name,
            "discovered": len(post_urls),
            "saved": len(manifest),
            "skipped": skipped,
            "files": [m["file"] for m in manifest],
            "output_dir": self.md_save_dir,
            "manifest": manifest_path,
        }
        self.log(
            f"\nDone. writer={self.writer_name} discovered={len(post_urls)} "
            f"saved={len(manifest)} skipped(premium/failed)={skipped}"
        )
        self.log(f"Markdown in: {self.md_save_dir}")
        return summary


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Extract free Substack posts to Markdown.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-u", "--url", required=True,
                   help="Substack publication URL or single /p/<slug> post URL.")
    p.add_argument("-o", "--output-dir", default=DEFAULT_OUTPUT_DIR,
                   help=f"Base output directory (default: {DEFAULT_OUTPUT_DIR}).")
    p.add_argument("-n", "--number", type=int, default=0,
                   help="Max number of (newest) posts to scrape (0 = all).")
    p.add_argument("--images", action="store_true",
                   help="Download images locally and rewrite markdown links.")
    p.add_argument("--frontmatter", choices=["legacy", "mdx"], default="legacy",
                   help="Header style: 'legacy' (default) or 'mdx' YAML frontmatter.")
    p.add_argument("--keywords", default=",".join(DEFAULT_KEYWORDS),
                   help="Comma-separated URL substrings to skip.")
    p.add_argument("--delay", type=float, default=0.5,
                   help="Seconds between requests (politeness; default 0.5).")
    p.add_argument("--list-only", action="store_true",
                   help="Only list discovered post URLs; do not scrape.")
    p.add_argument("--quiet", action="store_true", help="Reduce logging.")
    p.add_argument("--json", action="store_true",
                   help="Print the run summary as JSON on stdout.")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    scraper = SubstackScraper(
        url=args.url,
        output_dir=args.output_dir,
        download_images=args.images,
        frontmatter_format=args.frontmatter,
        keywords=keywords,
        delay=args.delay,
        quiet=args.quiet,
    )
    summary = scraper.run(num_posts=args.number, list_only=args.list_only)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
