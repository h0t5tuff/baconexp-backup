#!/usr/bin/env python3
"""
baconexp-backup.py - mirror a Confluence Cloud site to a browsable offline tree.

The on-disk layout mirrors the live Content sidebar: every page becomes a
directory holding its own index.html, its attachments in _files/, and its child
pages as subdirectories, numbered so the sidebar order survives in any file
manager. Confluence storage format is converted to real HTML, so images,
code blocks, callouts and cross-page links actually render offline.

Config: config.env beside this script (chmod 600).
"""

import argparse
import html
import json
import os
import re
import shutil
import sys
import tarfile
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.entities import html5 as HTML5_ENTITIES
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth

# Everything this project owns lives beside this script, so the whole thing is
# one folder you can move or copy without hunting through ~/.config and ~/.local.
PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_DIR / "config.env"
STATE_DIR = PROJECT_DIR / "state"
PAGE_LIMIT = 100
MAX_RETRIES = 5
MAX_DEPTH = 15
NAME_MAX = 80


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def load_config():
    if not CONFIG_PATH.exists():
        sys.exit(f"FATAL: missing config file {CONFIG_PATH}")
    mode = CONFIG_PATH.stat().st_mode & 0o777
    if mode & 0o077:
        sys.exit(f"FATAL: {CONFIG_PATH} is mode {mode:o}; it holds an API token. "
                 f"Run: chmod 600 {CONFIG_PATH}")
    cfg = {}
    for line in CONFIG_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip().strip('"').strip("'")
    required = ["CONFLUENCE_SITE", "CONFLUENCE_EMAIL", "CONFLUENCE_API_TOKEN", "BACKUP_ROOT"]
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        sys.exit(f"FATAL: config missing keys: {', '.join(missing)}")
    return cfg


# NTFS reserved device names - a directory called "CON" cannot exist on the RAID.
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} \
                   | {f"LPT{i}" for i in range(1, 10)}


def safe_name(text, maxlen=NAME_MAX):
    """Filesystem-safe path component. The RAID is ntfs-3g, so Windows rules apply."""
    text = unicodedata.normalize("NFC", str(text or ""))
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > maxlen:
        text = text[:maxlen].rstrip()
    text = text.rstrip(". ")
    if text.upper() in WINDOWS_RESERVED:
        text += "_"
    return text or "untitled"


def rel(from_dir: Path, to_path: Path) -> str:
    """URL-ish relative path from a page's directory to another file."""
    r = os.path.relpath(to_path, from_dir)
    return "/".join(quote_segment(p) for p in Path(r).parts)


def quote_segment(seg):
    if seg == "..":
        return ".."
    # Keep it readable; only escape what genuinely breaks a file:// URL.
    return seg.replace("%", "%25").replace("#", "%23").replace("?", "%3F")


class Confluence:
    def __init__(self, site, email, token):
        self.site = site.rstrip("/")
        self.s = requests.Session()
        self.s.auth = HTTPBasicAuth(email, token)
        self.s.headers.update({"Accept": "application/json"})

    def _request(self, method, url, **kw):
        if url.startswith("/"):
            url = self.site + url
        for attempt in range(MAX_RETRIES):
            try:
                r = self.s.request(method, url, timeout=90, **kw)
            except requests.RequestException as e:
                wait = 2 ** attempt
                log(f"  network error ({e}); retry in {wait}s")
                time.sleep(wait)
                continue
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 2 ** attempt))
                log(f"  rate limited; sleeping {wait}s")
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                wait = 2 ** attempt
                log(f"  server {r.status_code}; retry in {wait}s")
                time.sleep(wait)
                continue
            return r
        raise RuntimeError(f"giving up after {MAX_RETRIES} attempts: {url}")

    def get_json(self, url, params=None):
        r = self._request("GET", url, params=params)
        if r.status_code == 401:
            sys.exit("FATAL: 401 Unauthorized - check CONFLUENCE_EMAIL and API token.")
        if r.status_code == 403:
            sys.exit("FATAL: 403 Forbidden - the account lacks permission for this endpoint.")
        r.raise_for_status()
        return r.json()

    def try_json(self, url, params=None):
        """Same as get_json but returns None instead of raising on 404/400."""
        try:
            r = self._request("GET", url, params=params)
            if r.status_code >= 400:
                return None
            return r.json()
        except Exception:
            return None

    def paginate(self, path, params=None):
        params = dict(params or {})
        params.setdefault("limit", PAGE_LIMIT)
        url = path
        while url:
            data = self.get_json(url, params=params)
            for item in data.get("results", []):
                yield item
            nxt = (data.get("_links") or {}).get("next")
            url, params = (nxt, None) if nxt else (None, None)

    def download(self, link, dest):
        r = self._request("GET", link, stream=True)
        if r.status_code != 200:
            return r.status_code
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(65536):
                fh.write(chunk)
        return 200


# --------------------------------------------------------------------------
# Confluence storage format -> HTML
#
# Storage format is XHTML sprinkled with ac:/ri: namespaced elements. Written
# straight to disk it renders as nothing: <ac:image> is not an <img>, and code
# blocks hide inside <ac:plain-text-body>. This converter turns it into HTML a
# browser can display with no Confluence, no network and no JavaScript.
# --------------------------------------------------------------------------

NS_TAG = re.compile(r"<\s*/?\s*([A-Za-z][\w.-]*):")
NS_ATTR = re.compile(r"[\s\"']([A-Za-z][\w.-]*):[\w.-]+\s*=")
ENTITY = re.compile(r"&([A-Za-z][A-Za-z0-9]*);")
KEEP_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}

VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
             "link", "meta", "param", "source", "track", "wbr"}
DROP_TAGS = {"script", "style", "noscript"}

CALLOUTS = {"info": "info", "note": "note", "warning": "warning", "tip": "tip",
            "panel": "panel", "success": "tip", "error": "warning",
            "attention": "warning", "caution": "warning"}

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".tif", ".tiff"}


def _entity_sub(m):
    name = m.group(1)
    if name in KEEP_ENTITIES:
        return m.group(0)
    ch = HTML5_ENTITIES.get(name + ";") or HTML5_ENTITIES.get(name)
    if ch is None:
        return "&amp;" + name + ";"
    return html.escape(ch, quote=False) if ch in "<>&" else ch


def parse_storage(body):
    """Parse storage XML into an ElementTree root, or return None if malformed.

    Namespace prefixes are bound to synthetic URNs so the prefix survives parsing;
    only the prefix and local name matter downstream.
    """
    prefixes = set(NS_TAG.findall(body)) | set(NS_ATTR.findall(body))
    prefixes.discard("xml")
    body = ENTITY.sub(_entity_sub, body)
    decl = " ".join(f'xmlns:{p}="urn:x-{p}"' for p in sorted(prefixes))
    try:
        return ET.fromstring(f"<root {decl}>{body}</root>")
    except ET.ParseError:
        return None


def qname(tag):
    """('ac', 'image') for a namespaced tag, ('', 'p') for a plain one."""
    if tag.startswith("{urn:x-"):
        uri, _, local = tag[1:].partition("}")
        return uri[len("urn:x-"):], local
    if tag.startswith("{"):
        return "", tag.partition("}")[2]
    return "", tag


def attr(el, prefix, name, default=None):
    return el.get(f"{{urn:x-{prefix}}}{name}", el.get(name, default))


def esc(text):
    return html.escape(text or "", quote=False)


def child(el, prefix, local):
    for c in el:
        if qname(c.tag) == (prefix, local):
            return c
    return None


def macro_params(el):
    out = {}
    for c in el:
        if qname(c.tag) == ("ac", "parameter"):
            out[attr(c, "ac", "name", "")] = (c.text or "").strip(), c
    return out


def emoji_from_id(hex_ids):
    """Codepoint ids like "1f953-1f3fb" become characters.

    Custom Atlassian emoji use a UUID here instead, whose chunks are not
    codepoints at all - those yield nothing rather than a bogus glyph.
    """
    parts = [h for h in str(hex_ids or "").split("-") if h]
    if not parts:
        return ""
    out = []
    for h in parts:
        try:
            cp = int(h, 16)
        except (ValueError, TypeError):
            return ""
        if not 0 < cp <= 0x10FFFF:
            return ""
        out.append(chr(cp))
    return "".join(out)


class Renderer:
    """Turns a parsed storage tree into HTML.

    `ctx` supplies the link/attachment resolution that makes the output work
    offline: filenames map to real files in _files/, page references map to
    relative paths inside the mirrored tree.
    """

    def __init__(self, ctx):
        self.ctx = ctx
        self.headings = []

    # -- entry point ------------------------------------------------------
    def render(self, root):
        return self.children(root)

    def children(self, el):
        parts = []
        if el.text:
            parts.append(esc(el.text))
        for c in el:
            parts.append(self.element(c))
            if c.tail:
                parts.append(esc(c.tail))
        return "".join(parts)

    def element(self, el):
        if not isinstance(el.tag, str):      # comments / processing instructions
            return ""
        prefix, local = qname(el.tag)
        if prefix == "ac":
            return self.ac(el, local)
        if prefix == "ri":
            return ""                        # resource identifiers are metadata
        if prefix:
            return self.children(el)         # unknown namespace: keep the text
        return self.plain(el, local)

    # -- plain XHTML ------------------------------------------------------
    def plain(self, el, tag):
        tag = tag.lower()
        if tag in DROP_TAGS:
            return ""
        inner = self.children(el)
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            text = re.sub(r"<[^>]+>", "", inner).strip()
            anchor = "h-" + re.sub(r"[^\w-]+", "-", text.lower()).strip("-")[:60]
            self.headings.append((int(tag[1]), text, anchor))
            return f'<{tag} id="{html.escape(anchor)}">{inner}</{tag}>'
        attrs = []
        for k, v in el.attrib.items():
            kp, kl = qname(k)
            if kp or kl.startswith("on") or kl == "style":
                continue
            if kl == "href":
                v = self.ctx.rewrite_href(v)
            if kl == "src":
                v = self.ctx.external_url(v) if v.startswith("http") \
                    else self.ctx.rewrite_href(v)
            attrs.append(f'{kl}="{html.escape(v, quote=True)}"')
        a = (" " + " ".join(attrs)) if attrs else ""
        if tag in VOID_TAGS:
            return f"<{tag}{a}>"
        return f"<{tag}{a}>{inner}</{tag}>"

    # -- ac: elements -----------------------------------------------------
    def ac(self, el, local):
        fn = getattr(self, "ac_" + local.replace("-", "_"), None)
        if fn:
            return fn(el)
        return self.children(el)

    def ac_image(self, el):
        src = None
        alt = attr(el, "ac", "alt") or ""
        att = child(el, "ri", "attachment")
        url = child(el, "ri", "url")
        label = ""
        if att is not None:
            label = attr(att, "ri", "filename", "") or ""
            page_ref = child(att, "ri", "page")
            page_title = attr(page_ref, "ri", "content-title") if page_ref is not None else None
            src = self.ctx.attachment_url(label, page_title)
        elif url is not None:
            label = attr(url, "ri", "value", "") or ""
            src = self.ctx.external_url(label)   # mirror it so offline still shows it
        if not src:
            return (f'<span class="cb-missing" title="not present in this backup">'
                    f'&#9888; missing image: {esc(label)}</span>')
        bits = [f'src="{html.escape(src, quote=True)}"',
                f'alt="{html.escape(alt or label, quote=True)}"', 'loading="lazy"']
        w = attr(el, "ac", "width")
        if w and str(w).isdigit():
            bits.append(f'width="{w}"')
        align = (attr(el, "ac", "align") or attr(el, "ac", "layout") or "").lower()
        cls = " cb-center" if "center" in align else (" cb-right" if "right" in align else "")
        img = f'<img {" ".join(bits)}>'
        link = f'<a href="{html.escape(src, quote=True)}">{img}</a>'
        cap = child(el, "ac", "caption")
        caption = f"<figcaption>{self.children(cap)}</figcaption>" if cap is not None else ""
        return f'<figure class="cb-fig{cls}">{link}{caption}</figure>'

    def ac_link(self, el):
        body_el = child(el, "ac", "link-body") or child(el, "ac", "plain-text-link-body")
        text = self.children(body_el) if body_el is not None else ""
        anchor = attr(el, "ac", "anchor") or ""
        href, label = None, ""
        page = child(el, "ri", "page") or child(el, "ri", "blog-post")
        att = child(el, "ri", "attachment")
        user = child(el, "ri", "user")
        if page is not None:
            label = attr(page, "ri", "content-title", "") or ""
            href = self.ctx.page_url(title=label, space_key=attr(page, "ri", "space-key"))
        elif att is not None:
            label = attr(att, "ri", "filename", "") or ""
            page_ref = child(att, "ri", "page")
            href = self.ctx.attachment_url(
                label, attr(page_ref, "ri", "content-title") if page_ref is not None else None)
        elif user is not None:
            label = self.ctx.user_label(attr(user, "ri", "account-id") or "")
            return f'<span class="cb-user">@{esc(label)}</span>'
        text = text or esc(label)
        if not href:
            return f'<span class="cb-deadlink" title="target not in this backup">{text}</span>'
        if anchor:
            href += "#h-" + re.sub(r"[^\w-]+", "-", anchor.lower()).strip("-")[:60]
        return f'<a href="{html.escape(href, quote=True)}">{text}</a>'

    def ac_emoticon(self, el):
        return esc(attr(el, "ac", "emoji-fallback")
                   or emoji_from_id(attr(el, "ac", "emoji-id", ""))
                   or attr(el, "ac", "name", ""))

    def ac_task_list(self, el):
        items = []
        for t in el:
            if qname(t.tag) != ("ac", "task"):
                continue
            status = child(t, "ac", "status")
            done = (status is not None and (status.text or "").strip() == "complete")
            body = child(t, "ac", "body")
            mark = "checked" if done else ""
            cls = ' class="cb-done"' if done else ""
            items.append(f'<li{cls}><input type="checkbox" disabled {mark}> '
                         f'{self.children(body) if body is not None else ""}</li>')
        return f'<ul class="cb-tasks">{"".join(items)}</ul>'

    def ac_layout(self, el):
        return f'<div class="cb-layout">{self.children(el)}</div>'

    def ac_layout_section(self, el):
        t = attr(el, "ac", "type", "single")
        return f'<div class="cb-section cb-{esc(t)}">{self.children(el)}</div>'

    def ac_layout_cell(self, el):
        return f'<div class="cb-cell">{self.children(el)}</div>'

    def ac_inline_comment_marker(self, el):
        return self.children(el)

    def ac_placeholder(self, el):
        return ""

    def ac_adf_extension(self, el):
        fb = child(el, "ac", "adf-fallback")
        return self.children(fb) if fb is not None else self.children(el)

    def file_embed(self, fname, height=None):
        """Render an attached file where the page puts it, not just in a list."""
        href = self.ctx.attachment_url(fname)
        if not href:
            return (f'<span class="cb-missing" title="not present in this backup">'
                    f'&#9888; missing file: {esc(fname)}</span>')
        safe = html.escape(href, quote=True)
        ext = os.path.splitext(fname)[1].lower()
        if ext in IMAGE_EXT:
            return (f'<figure class="cb-fig"><a href="{safe}">'
                    f'<img src="{safe}" alt="{html.escape(fname, quote=True)}" loading="lazy">'
                    f'</a><figcaption>{esc(fname)}</figcaption></figure>')
        path = self.ctx.attachment_path(fname)
        size = ""
        if path is not None:
            try:
                size = f" &middot; {path.stat().st_size / 1e6:.1f} MB"
            except OSError:
                pass
        if ext == ".pdf":
            h = height if (height or "").isdigit() else "760"
            return (f'<figure class="cb-embed"><object data="{safe}" '
                    f'type="application/pdf" width="100%" height="{h}">'
                    f'<p>{esc(fname)}</p></object>'
                    f'<figcaption><a href="{safe}">&#128206; {esc(fname)}</a>{size}'
                    f'</figcaption></figure>')
        icon = "&#128202;" if ext in (".csv", ".tsv", ".xls", ".xlsx") else "&#128206;"
        return (f'<p class="cb-file"><a href="{safe}">{icon} {esc(fname)}</a>'
                f'<span class="cb-meta">{size}</span></p>')

    def macro_filename(self, params, key="name"):
        text, node = params.get(key, ("", None))
        if text:
            return text
        if node is not None:
            att = child(node, "ri", "attachment")
            if att is not None:
                return attr(att, "ri", "filename", "") or ""
        return ""

    def ac_structured_macro(self, el):
        name = (attr(el, "ac", "name") or "").lower()
        params = macro_params(el)
        rich = child(el, "ac", "rich-text-body")
        plain = child(el, "ac", "plain-text-body")
        body = self.children(rich) if rich is not None else ""
        raw = (plain.text or "") if plain is not None else ""

        def p(key, default=""):
            return params.get(key, (default, None))[0] or default

        if name in ("code", "codeblock"):
            lang = p("language", "text")
            title = p("title")
            head = f'<figcaption>{esc(title)}</figcaption>' if title else ""
            return (f'<figure class="cb-code">{head}<pre><code class="language-{esc(lang)}">'
                    f'{esc(raw)}</code></pre></figure>')
        if name in ("noformat", "preformatted"):
            return f"<pre>{esc(raw)}</pre>"
        if name in CALLOUTS:
            kind = CALLOUTS[name]
            title = p("title")
            head = f'<div class="cb-callout-title">{esc(title)}</div>' if title else ""
            return f'<div class="cb-callout cb-{kind}">{head}{body or esc(raw)}</div>'
        if name == "expand":
            return (f'<details class="cb-expand"><summary>{esc(p("title", "Details"))}'
                    f'</summary>{body}</details>')
        if name == "toc":
            return "<!--CB_TOC-->"
        if name in ("children", "pagetree"):
            return "<!--CB_CHILDREN-->"
        if name == "attachments":
            return "<!--CB_ATTACHMENTS-->"
        if name == "anchor":
            a = re.sub(r"[^\w-]+", "-", p("").lower() or p("name").lower()).strip("-")
            return f'<a id="h-{esc(a)}"></a>'
        if name == "status":
            colour = (p("colour") or p("color") or "grey").lower()
            return f'<span class="cb-status cb-s-{esc(colour)}">{esc(p("title"))}</span>'
        if name in ("excerpt", "excerpt-include", "section", "column", "div", "panel"):
            return body or esc(raw)
        if name in ("view-file", "viewfile", "viewpdf", "viewdoc", "viewxls",
                    "viewppt", "multimedia", "widget"):
            fname = self.macro_filename(params)
            if fname:
                return self.file_embed(fname, p("height"))
        if name == "drawio":
            base = p("diagramName") or p("diagramDisplayName")
            for cand in (f"{base}.png", f"{base}.drawio.png", base):
                if cand and self.ctx.attachment_path(cand):
                    return self.file_embed(cand)
        if name in ("recently-updated", "blog-posts", "content-by-label", "pagetree-search"):
            rows = "".join(
                f'<li><a href="{html.escape(rel(self.ctx.node.dir, n.page), quote=True)}">'
                f'{esc(n.label)}</a> <span class="cb-meta">{esc(when[:10])}</span></li>'
                for when, n in self.ctx.recent_pages(int(p("max", "15") or 15)
                                                     if p("max", "15").isdigit() else 15))
            return (f'<div class="cb-macro cb-static"><div class="cb-callout-title">'
                    f'Recently updated <span class="cb-meta">(snapshot taken at backup time)'
                    f'</span></div><ul class="cb-grid">{rows}</ul></div>')
        if name in ("livesearch", "roadmap", "jira", "gliffy", "iframe"):
            return (f'<div class="cb-macro-note">A <code>{esc(name)}</code> macro sat here. '
                    f'It renders from live Confluence data and has no offline equivalent; '
                    f'use the sidebar search instead.</div>')
        if name in ("gallery", "attachments-gallery"):
            return "<!--CB_ATTACHMENTS-->"
        if body:
            return f'<div class="cb-macro cb-macro-{esc(name)}">{body}</div>'
        detail = p("url") or p("key") or p("name") or p("title")
        return (f'<div class="cb-macro-note">unrendered Confluence macro: '
                f'<code>{esc(name)}</code>{(" - " + esc(detail)) if detail else ""}</div>')


# --------------------------------------------------------------------------
# The mirrored tree
# --------------------------------------------------------------------------

class Node:
    __slots__ = ("id", "type", "title", "emoji", "children", "parent",
                 "dir", "space_key", "order", "meta", "files")

    def __init__(self, nid, ntype, title, parent=None, space_key=""):
        self.id = str(nid)
        self.type = ntype
        self.title = title or f"{ntype}-{nid}"
        self.emoji = ""
        self.children = []
        self.parent = parent
        self.dir = None
        self.space_key = space_key
        self.order = 0
        self.meta = {}
        self.files = {}          # original attachment filename -> Path on disk

    @property
    def label(self):
        return f"{self.emoji} {self.title}".strip()

    @property
    def page(self):
        return self.dir / "index.html"

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()


CHILD_ENDPOINT = {"page": "pages", "folder": "folders", "whiteboard": "whiteboards",
                  "database": "databases", "embed": "embeds", "smartlink": "embeds"}


def build_tree(conf, node, depth, seen, stats):
    """Recursively expand a node's direct children, preserving sidebar order."""
    if depth >= MAX_DEPTH or node.type not in ("page", "folder"):
        return
    ep = CHILD_ENDPOINT.get(node.type)
    data = conf.try_json(f"/wiki/api/v2/{ep}/{node.id}/direct-children", {"limit": PAGE_LIMIT})
    if not data:
        return
    results = list(data.get("results", []))
    nxt = (data.get("_links") or {}).get("next")
    while nxt:
        more = conf.try_json(nxt)
        if not more:
            break
        results.extend(more.get("results", []))
        nxt = (more.get("_links") or {}).get("next")
    results.sort(key=lambda c: c.get("childPosition") or 0)
    for i, c in enumerate(results, 1):
        cid = str(c.get("id"))
        if cid in seen:
            continue
        seen.add(cid)
        kid = Node(cid, c.get("type", "page"), c.get("title"), node, node.space_key)
        kid.order = i
        node.children.append(kid)
        build_tree(conf, kid, depth + 1, seen, stats)


def assign_dirs(node, base):
    """Give every node a directory. The NN prefix keeps sidebar order visible."""
    for i, c in enumerate(node.children, 1):
        c.dir = base / f"{i:02d} {safe_name(c.label)}"
        assign_dirs(c, c.dir)


class LinkContext:
    """Resolves Confluence references to paths inside the mirrored tree."""

    def __init__(self, site, by_id, by_title, users, conf=None, ext_dir=None):
        self.site = site
        self.by_id = by_id
        self.by_title = by_title
        self.users = users
        self.conf = conf
        self.ext_dir = ext_dir
        self.ext_cache = {}
        self.node = None          # current page
        self.unresolved = 0
        self.external = 0

    def external_url(self, url):
        """Mirror a remotely hosted image; a backup that needs the network is not one."""
        if not (self.conf and self.ext_dir) or not re.match(r"^https?://", url or ""):
            return url
        if url in self.ext_cache:
            path = self.ext_cache[url]
            return self._link_to(path) if path else url
        from urllib.parse import urlparse, unquote
        import hashlib
        base = os.path.basename(unquote(urlparse(url).path)) or "image"
        stem, dot, ext = base.rpartition(".")
        if not dot or len(ext) > 5:
            stem, ext = base, "img"
        name = f"{safe_name(stem, 60)}-{hashlib.sha1(url.encode()).hexdigest()[:8]}.{ext}"
        dest = self.ext_dir / name
        ok = dest.exists()
        if not ok:
            try:
                ok = self.conf.download(url, dest) == 200
            except Exception:
                ok = False
        self.ext_cache[url] = dest if ok else None
        if ok:
            self.external += 1
            return self._link_to(dest)
        return url

    def for_node(self, node):
        self.node = node
        return self

    def _link_to(self, path):
        return rel(self.node.dir, path)

    def attachment_url(self, filename, page_title=None):
        if not filename:
            return None
        target = self.node
        if page_title:
            target = self.by_title.get((self.node.space_key, page_title)) \
                     or self.by_title.get(("", page_title)) or self.node
        path = target.files.get(filename)
        if path is None and target is not self.node:
            path = self.node.files.get(filename)
        if path is None:
            self.unresolved += 1
            return None
        return self._link_to(path)

    def attachment_path(self, filename):
        return self.node.files.get(filename) if self.node else None

    def recent_pages(self, limit=15):
        """Static stand-in for the dynamic recently-updated / blog-posts macros."""
        items = []
        for n in self.by_id.values():
            if n.type != "page" or n.dir is None or n is self.node:
                continue
            when = (n.meta.get("version") or {}).get("createdAt", "")
            items.append((when, n))
        items.sort(key=lambda kv: kv[0], reverse=True)
        return items[:limit]

    def page_url(self, title=None, pid=None, space_key=None):
        node = None
        if pid:
            node = self.by_id.get(str(pid))
        if node is None and title:
            node = self.by_title.get((space_key or self.node.space_key, title)) \
                   or self.by_title.get((self.node.space_key, title))
            if node is None:
                matches = [n for (sk, t), n in self.by_title.items() if t == title]
                node = matches[0] if len(matches) == 1 else None
        if node is None or node.dir is None:
            self.unresolved += 1
            return None
        return self._link_to(node.page)

    def user_label(self, account_id):
        return self.users.get(account_id, account_id[:8] or "user")

    def rewrite_href(self, href):
        if not href:
            return href
        h = href.strip()
        if h.startswith("#") or h.startswith("mailto:") or h.startswith("data:"):
            return h
        local = h
        if local.startswith(self.site):
            local = local[len(self.site):]
        elif re.match(r"^https?://", local):
            return h                                  # genuinely external
        m = re.search(r"/pages/(\d+)", local)
        if m:
            resolved = self.page_url(pid=m.group(1))
            if resolved:
                frag = local.partition("#")[2]
                return resolved + (("#" + frag) if frag else "")
        m = re.search(r"/download/attachments/(\d+)/([^?#]+)", local)
        if m:
            node = self.by_id.get(m.group(1))
            if node is not None:
                from urllib.parse import unquote
                path = node.files.get(unquote(m.group(2)))
                if path:
                    return self._link_to(path)
        if local.startswith("/"):
            return self.site + local                  # cannot mirror it; keep it clickable
        return h


# --------------------------------------------------------------------------
# Presentation
# --------------------------------------------------------------------------

STYLE = """
:root{--bg:#fff;--fg:#1b1f24;--muted:#5c6773;--line:#e3e6ea;--accent:#0b66c3;
      --nav:#f7f8fa;--code:#f4f6f8;--sel:#e6f0fb}
@media (prefers-color-scheme:dark){
  :root{--bg:#14171a;--fg:#e6e8ea;--muted:#9aa4ad;--line:#2a2f35;--accent:#69aef7;
        --nav:#191d21;--code:#1d2126;--sel:#243244}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
     font:16px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.cb-wrap{display:flex;align-items:flex-start;min-height:100vh}
.cb-nav{width:20rem;flex:0 0 20rem;background:var(--nav);border-right:1px solid var(--line);
        height:100vh;position:sticky;top:0;overflow-y:auto;padding:1rem .75rem 3rem;font-size:.9rem}
.cb-brand{font-weight:700;font-size:1.05rem;padding:.25rem .4rem .75rem;display:block}
.cb-brand a{color:var(--fg)}
.cb-find{width:100%;padding:.45rem .6rem;margin-bottom:.75rem;border:1px solid var(--line);
         border-radius:6px;background:var(--bg);color:var(--fg);font-size:.9rem}
.cb-nav ul{list-style:none;margin:0;padding-left:.85rem}
.cb-nav>ul{padding-left:0}
.cb-nav li{margin:.05rem 0}
.cb-nav a{display:block;padding:.2rem .4rem;border-radius:5px;color:var(--fg)}
.cb-nav a:hover{background:var(--sel);text-decoration:none}
.cb-nav .cb-here>a,.cb-nav .cb-here>details>summary>a{background:var(--sel);color:var(--accent);font-weight:600}
.cb-nav summary{cursor:pointer;list-style:none;display:flex;align-items:flex-start}
.cb-nav summary::-webkit-details-marker{display:none}
.cb-nav summary::before{content:"\\25B8";color:var(--muted);flex:0 0 .9rem;
        padding-top:.2rem;transition:transform .12s}
.cb-nav details[open]>summary::before{transform:rotate(90deg)}
.cb-nav summary a{flex:1}
.cb-t{font-size:.7rem;color:var(--muted);border:1px solid var(--line);border-radius:3px;
      padding:0 .25rem;margin-left:.3rem;vertical-align:middle}
.cb-main{flex:1;min-width:0;max-width:56rem;padding:2rem 2.5rem 5rem}
.cb-crumbs{font-size:.85rem;color:var(--muted);margin-bottom:.5rem}
.cb-crumbs a{color:var(--muted)}
h1,h2,h3,h4{line-height:1.25}
h1{margin:.2rem 0 .3rem;font-size:2rem}
h2{margin-top:2rem;border-bottom:1px solid var(--line);padding-bottom:.3rem}
.cb-meta{color:var(--muted);font-size:.82rem;border-bottom:1px solid var(--line);
         padding-bottom:.8rem;margin-bottom:1.6rem}
img{max-width:100%;height:auto}
.cb-fig{margin:1.2rem 0}
.cb-fig.cb-center{text-align:center}
.cb-fig.cb-right{text-align:right}
figcaption{color:var(--muted);font-size:.85rem;margin-top:.35rem}
table{border-collapse:collapse;margin:1rem 0;display:block;overflow-x:auto;max-width:100%}
td,th{border:1px solid var(--line);padding:.4rem .6rem;text-align:left;vertical-align:top}
th{background:var(--code)}
pre{background:var(--code);padding:.85rem 1rem;overflow-x:auto;border-radius:6px;
    border:1px solid var(--line)}
code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.88em}
:not(pre)>code{background:var(--code);padding:.1rem .3rem;border-radius:4px}
.cb-code{margin:1.2rem 0}
.cb-code figcaption{margin:0 0 .3rem;font-weight:600;color:var(--fg)}
.cb-callout{border-left:4px solid var(--accent);background:var(--code);padding:.7rem 1rem;
            margin:1.1rem 0;border-radius:0 6px 6px 0}
.cb-callout-title{font-weight:700;margin-bottom:.25rem}
.cb-warning{border-color:#d9822b}.cb-note{border-color:#8777d9}.cb-tip{border-color:#36b37e}
.cb-missing{color:#b45309;background:#fdf3e3;border:1px dashed #e0b070;padding:.15rem .45rem;
            border-radius:4px;font-size:.9em}
.cb-deadlink{color:var(--muted);border-bottom:1px dotted var(--muted)}
.cb-macro-note{color:var(--muted);font-size:.85rem;border:1px dashed var(--line);
               padding:.4rem .7rem;margin:.8rem 0;border-radius:5px}
.cb-status{font-size:.78rem;font-weight:700;text-transform:uppercase;padding:.1rem .45rem;
           border-radius:3px;background:var(--code);border:1px solid var(--line)}
.cb-tasks{list-style:none;padding-left:.2rem}
.cb-tasks .cb-done{color:var(--muted);text-decoration:line-through}
.cb-section{display:flex;gap:1.5rem;flex-wrap:wrap}
.cb-cell{flex:1;min-width:14rem}
.cb-embed{margin:1.4rem 0;border:1px solid var(--line);border-radius:6px;overflow:hidden}
.cb-embed object{display:block;background:var(--code)}
.cb-embed figcaption{padding:.5rem .8rem;background:var(--code);margin:0;
                     border-top:1px solid var(--line)}
.cb-file{margin:.6rem 0}
.cb-file a{display:inline-block;border:1px solid var(--line);border-radius:5px;
           padding:.35rem .7rem;background:var(--code)}
.cb-static{border:1px solid var(--line);border-radius:6px;padding:.7rem 1rem;margin:1.2rem 0}
.cb-expand{margin:.8rem 0}
.cb-expand summary{cursor:pointer;font-weight:600}
.cb-panel{margin-top:2.5rem;border-top:1px solid var(--line);padding-top:1rem}
.cb-panel h2{border:0;font-size:1rem;text-transform:uppercase;letter-spacing:.04em;
             color:var(--muted);margin:1.2rem 0 .4rem}
.cb-files{list-style:none;padding:0;display:flex;flex-wrap:wrap;gap:.4rem}
.cb-files li a{display:block;border:1px solid var(--line);border-radius:5px;
               padding:.3rem .6rem;background:var(--code);font-size:.88rem}
.cb-grid{list-style:none;padding:0}
.cb-grid li{padding:.25rem 0;border-bottom:1px solid var(--line)}
.cb-toc{background:var(--code);border:1px solid var(--line);border-radius:6px;
        padding:.7rem 1rem;margin:1.2rem 0}
.cb-toc ul{margin:.2rem 0;padding-left:1.2rem}
.cb-user{color:var(--muted)}
@media (max-width:900px){
  .cb-wrap{flex-direction:column}
  .cb-nav{width:100%;flex:none;height:auto;position:static;border-right:0;
          border-bottom:1px solid var(--line)}
  .cb-main{padding:1.25rem}}
"""

FIND_JS = """
(function(){var b=document.getElementById('cb-find');if(!b)return;
b.addEventListener('input',function(){var q=b.value.trim().toLowerCase();
var links=document.querySelectorAll('.cb-nav a[data-t]');
if(!q){document.querySelectorAll('.cb-nav li').forEach(function(li){li.hidden=false});
 document.querySelectorAll('.cb-nav details').forEach(function(d){d.open=d.dataset.o==='1'});return;}
document.querySelectorAll('.cb-nav li').forEach(function(li){li.hidden=true});
links.forEach(function(a){if(a.dataset.t.indexOf(q)<0)return;
 var n=a.closest('li');while(n){if(n.tagName==='LI')n.hidden=false;
  if(n.tagName==='DETAILS')n.open=true;n=n.parentElement;}});});})();
"""

TYPE_TAG = {"folder": "folder", "embed": "link", "blogpost": "blog",
            "whiteboard": "whiteboard", "database": "database"}


def render_nav(nodes, current, space_root):
    """The whole space tree, with the current page's ancestors expanded."""
    ancestors = set()
    n = current
    while n is not None:
        ancestors.add(n.id)
        n = n.parent

    def branch(items):
        out = []
        for node in items:
            here = ' class="cb-here"' if node.id == current.id else ""
            href = rel(current.dir, node.page)
            tag = TYPE_TAG.get(node.type, "")
            badge = f'<span class="cb-t">{tag}</span>' if tag else ""
            link = (f'<a href="{html.escape(href, quote=True)}" '
                    f'data-t="{html.escape(node.label.lower(), quote=True)}">'
                    f'{esc(node.label)}{badge}</a>')
            if node.children:
                op = "1" if node.id in ancestors else "0"
                out.append(f'<li{here}><details data-o="{op}"{" open" if op == "1" else ""}>'
                           f'<summary>{link}</summary>{branch(node.children)}</details></li>')
            else:
                out.append(f"<li{here}>{link}</li>")
        return f'<ul>{"".join(out)}</ul>'

    return branch(nodes)


def crumbs(node, space_root):
    chain, n = [], node.parent
    while n is not None:
        chain.append(n)
        n = n.parent
    chain.reverse()
    parts = [f'<a href="{html.escape(rel(node.dir, a.page), quote=True)}">{esc(a.label)}</a>'
             for a in chain]
    return " / ".join(parts)


def build_toc(headings):
    if len(headings) < 2:
        return ""
    top = min(h[0] for h in headings)
    items = "".join(f'<li style="margin-left:{(lv - top) * 1.1:.1f}rem">'
                    f'<a href="#{html.escape(anc, quote=True)}">{esc(txt)}</a></li>'
                    for lv, txt, anc in headings)
    return f'<div class="cb-toc"><strong>On this page</strong><ul>{items}</ul></div>'


PAGE_TMPL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<link rel="stylesheet" href="{css}">
</head><body><div class="cb-wrap">
<nav class="cb-nav">
<div class="cb-brand"><a href="{space_home}">{space_label}</a></div>
<input id="cb-find" class="cb-find" type="search" placeholder="Search by title" autocomplete="off">
{nav}
</nav>
<main class="cb-main">
<div class="cb-crumbs">{crumbs}</div>
<h1>{heading}</h1>
<div class="cb-meta">{meta}</div>
{toc}
{body}
{panel}
</main></div><script>{js}</script></body></html>
"""


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def fetch_emoji(conf, page_id):
    data = conf.try_json(f"/wiki/api/v2/pages/{page_id}/properties", {"limit": 25})
    if not data:
        return ""
    props = {p.get("key"): p.get("value") for p in data.get("results", [])}
    val = props.get("emoji-title-published") or props.get("emoji-title-draft")
    return emoji_from_id(val) if val else ""


def fetch_attachments(conf, node, source_dir, stats, missing):
    """Download every attachment next to its page, so <img> can point at a real file."""
    if node.type not in ("page", "blogpost"):
        return
    kind = "pages" if node.type == "page" else "blogposts"
    try:
        atts = list(conf.paginate(f"/wiki/api/v2/{kind}/{node.id}/attachments"))
    except Exception as e:
        log(f"    ! attachment list failed for {node.id}: {e}")
        return
    if not atts:
        return
    (source_dir / f"{node.id}.attachments.json").write_text(
        json.dumps(atts, indent=2, ensure_ascii=False), encoding="utf-8")
    fdir = node.dir / "_files"
    used = set()
    for a in atts:
        link = a.get("downloadLink") or ""
        if not link:
            continue
        if not link.startswith("/"):
            link = "/" + link
        if not link.startswith("/wiki"):
            link = "/wiki" + link
        original = a.get("title") or a["id"]
        fname = safe_name(original, maxlen=100)
        if fname.lower() in used:
            stem, dot, ext = fname.rpartition(".")
            fname = f"{stem or fname}-{a['id']}{dot}{ext}" if dot else f"{fname}-{a['id']}"
        used.add(fname.lower())
        status = conf.download(link, fdir / fname)
        if status == 200:
            node.files[original] = fdir / fname
            stats["attachments"] += 1
        else:
            stats["failed_attachments"] += 1
            missing.append((node.space_key, node.title, original, status))


def fetch_body(conf, node, source_dir):
    """Pull one node's content and stash the raw storage XML for fidelity."""
    if node.type == "page":
        data = conf.try_json(f"/wiki/api/v2/pages/{node.id}", {"body-format": "storage"})
    elif node.type == "blogpost":
        data = conf.try_json(f"/wiki/api/v2/blogposts/{node.id}", {"body-format": "storage"})
    elif node.type == "folder":
        data = conf.try_json(f"/wiki/api/v2/folders/{node.id}")
    elif node.type == "embed":
        data = conf.try_json(f"/wiki/api/v2/embeds/{node.id}")
    else:
        data = conf.try_json(f"/wiki/api/v2/{CHILD_ENDPOINT.get(node.type, 'pages')}/{node.id}")
    if not data:
        return
    node.meta = data
    (source_dir / f"{node.id}.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    body = ((data.get("body") or {}).get("storage") or {}).get("value", "")
    if body:
        (source_dir / f"{node.id}.xhtml").write_text(body, encoding="utf-8")


def fetch_comments(conf, node):
    if node.type not in ("page", "blogpost"):
        return []
    kind = "pages" if node.type == "page" else "blogposts"
    out = []
    for ctype in ("footer-comments", "inline-comments"):
        try:
            for c in conf.paginate(f"/wiki/api/v2/{kind}/{node.id}/{ctype}",
                                   {"body-format": "storage"}):
                c["_comment_type"] = ctype
                out.append(c)
        except Exception:
            pass
    return out


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------

def render_body(node, ctx, users):
    body = ((node.meta.get("body") or {}).get("storage") or {}).get("value", "")
    if not body.strip():
        return "", []
    root = parse_storage(body)
    if root is None:
        return (f'<div class="cb-macro-note">This page could not be converted to HTML; '
                f'the original Confluence markup is preserved in '
                f'<code>_source/{node.id}.xhtml</code>.</div>'
                f"<pre>{esc(body)}</pre>"), []
    r = Renderer(ctx.for_node(node))
    return r.render(root), r.headings


def side_panel(node, ctx, comments, users):
    """Attachments, child list and discussion, appended under the page body."""
    blocks = []
    if node.files:
        items = []
        for original, path in sorted(node.files.items(), key=lambda kv: kv[0].lower()):
            href = rel(node.dir, path)
            icon = "&#128444;" if path.suffix.lower() in IMAGE_EXT else "&#128206;"
            items.append(f'<li><a href="{html.escape(href, quote=True)}">'
                         f'{icon} {esc(original)}</a></li>')
        blocks.append(f'<h2>Attachments ({len(items)})</h2>'
                      f'<ul class="cb-files">{"".join(items)}</ul>')
    if node.children:
        items = []
        for c in node.children:
            tag = TYPE_TAG.get(c.type, "")
            badge = f'<span class="cb-t">{tag}</span>' if tag else ""
            items.append(f'<li><a href="{html.escape(rel(node.dir, c.page), quote=True)}">'
                         f'{esc(c.label)}</a>{badge}</li>')
        blocks.append(f'<h2>Child pages ({len(items)})</h2>'
                      f'<ul class="cb-grid">{"".join(items)}</ul>')
    if comments:
        items = []
        for c in comments:
            root = parse_storage(((c.get("body") or {}).get("storage") or {}).get("value", ""))
            text = Renderer(ctx.for_node(node)).render(root) if root is not None else ""
            who = users.get((c.get("version") or {}).get("authorId", ""), "someone")
            when = (c.get("version") or {}).get("createdAt", "")[:10]
            items.append(f'<li><strong>{esc(who)}</strong> '
                         f'<span class="cb-meta">{esc(when)}</span>{text}</li>')
        blocks.append(f'<h2>Comments ({len(items)})</h2>'
                      f'<ul class="cb-grid">{"".join(items)}</ul>')
    if not blocks:
        return ""
    return f'<div class="cb-panel">{"".join(blocks)}</div>'


def write_node(node, ctx, roots, space_node, site, users, comments, css_path):
    node.dir.mkdir(parents=True, exist_ok=True)
    body, headings = render_body(node, ctx, users)

    if node.type == "embed":
        url = node.meta.get("embedUrl") or ""
        body = (f'<p>This is a link card in Confluence, not stored content.</p>'
                f'<p><a href="{html.escape(url, quote=True)}">{esc(url)}</a></p>')
    elif node.type == "folder" and not body:
        body = "<p>Folder.</p>" if node.children else "<p>Empty folder.</p>"

    panel = side_panel(node, ctx, comments, users)
    toc = ""
    if "<!--CB_TOC-->" in body:
        body = body.replace("<!--CB_TOC-->", build_toc(headings))
    if "<!--CB_CHILDREN-->" in body:
        kids = "".join(f'<li><a href="{html.escape(rel(node.dir, c.page), quote=True)}">'
                       f'{esc(c.label)}</a></li>' for c in node.children)
        body = body.replace("<!--CB_CHILDREN-->", f'<ul class="cb-grid">{kids}</ul>')
    if "<!--CB_ATTACHMENTS-->" in body:
        files = "".join(f'<li><a href="{html.escape(rel(node.dir, p), quote=True)}">'
                        f'{esc(o)}</a></li>' for o, p in sorted(node.files.items()))
        body = body.replace("<!--CB_ATTACHMENTS-->", f'<ul class="cb-files">{files}</ul>')

    version = (node.meta.get("version") or {}).get("number", "?")
    modified = (node.meta.get("version") or {}).get("createdAt", "")[:10]
    author = users.get((node.meta.get("version") or {}).get("authorId", ""), "")
    webui = ((node.meta.get("_links") or {}).get("webui") or "")
    meta_bits = [f"Space {esc(space_node.title)}", f"ID {node.id}", f"v{version}"]
    if modified:
        meta_bits.append(f"updated {modified}")
    if author:
        meta_bits.append(f"by {esc(author)}")
    meta = " &middot; ".join(meta_bits)
    if webui:
        meta += (f'<br><a href="{html.escape(site + "/wiki" + webui, quote=True)}">'
                 f"view live in Confluence</a>")

    node.page.write_text(PAGE_TMPL.format(
        title=esc(node.title),
        css=rel(node.dir, css_path),
        space_home=rel(node.dir, space_node.page),
        space_label=esc(space_node.label),
        nav=render_nav(roots, node, space_node),
        crumbs=crumbs(node, space_node),
        heading=esc(node.label),
        meta=meta,
        toc=toc,
        body=body,
        panel=panel,
        js=FIND_JS,
    ), encoding="utf-8")


def write_stub(node, space_node, css_path, err):
    """A page that would not convert still gets a file, so the tree stays whole."""
    try:
        node.dir.mkdir(parents=True, exist_ok=True)
        node.page.write_text(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>{esc(node.title)}</title><link rel="stylesheet" href="{rel(node.dir, css_path)}">
</head><body><main class="cb-main"><h1>{esc(node.label)}</h1>
<div class="cb-macro-note">This page could not be converted to HTML
({esc(type(err).__name__)}). Its original Confluence markup and metadata are
preserved under <code>_source/{esc(node.id)}.*</code> in this space folder, and its
attachments are in <code>_files/</code>.</div></main></body></html>""", encoding="utf-8")
    except Exception:
        pass


def write_site_index(run_dir, space_entries, css_path, stamp, site):
    rows = "".join(
        f'<li><a href="{html.escape(rel(run_dir, e["home"]), quote=True)}">{esc(e["label"])}</a>'
        f' <span class="cb-t">{e["pages"]} pages</span></li>' for e in space_entries)
    (run_dir / "index.html").write_text(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Confluence backup {esc(stamp)}</title>
<link rel="stylesheet" href="{rel(run_dir, css_path)}"></head>
<body><main class="cb-main" style="margin:0 auto">
<h1>Confluence backup</h1>
<div class="cb-meta">{esc(site)} &middot; captured {esc(stamp)}</div>
<p>Every space below is a self-contained folder. Open any <code>index.html</code>
in a browser - no Confluence, no network, no login.</p>
<h2>Spaces</h2><ul class="cb-grid">{rows}</ul>
</main></body></html>""", encoding="utf-8")


def resolve_users(conf, ids):
    users = {}
    for aid in ids:
        if not aid:
            continue
        d = conf.try_json("/wiki/rest/api/user", {"accountId": aid})
        users[aid] = (d or {}).get("displayName") or aid[:8]
    return users


def space_tree(conf, sp, stats):
    """Root node for a space, expanded to the full Content tree in sidebar order."""
    key = sp.get("key") or str(sp["id"])
    home_id = sp.get("homepageId")
    if home_id:
        root = Node(home_id, "page", sp.get("name") or key, None, key)
        root.meta = {}
    else:
        root = Node(f"space-{sp['id']}", "folder", sp.get("name") or key, None, key)
    seen = {root.id}
    build_tree(conf, root, 0, seen, stats)

    # Anything the tree walk missed still has to be backed up.
    orphans, blogs = [], []
    for pg in conf.paginate("/wiki/api/v2/pages", {"space-id": sp["id"]}):
        if str(pg["id"]) not in seen:
            orphans.append(pg)
            seen.add(str(pg["id"]))
    for bp in conf.paginate("/wiki/api/v2/blogposts", {"space-id": sp["id"]}):
        blogs.append(bp)

    for label, items, ntype in (("_unfiled pages", orphans, "page"),
                                ("_blog posts", blogs, "blogpost")):
        if not items:
            continue
        holder = Node(f"{label}-{sp['id']}", "folder", label, root, key)
        holder.meta = {}
        root.children.append(holder)
        for i, it in enumerate(items, 1):
            n = Node(it["id"], ntype, it.get("title"), holder, key)
            n.order = i
            holder.children.append(n)
    return root


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--space", action="append", default=[],
                    help="only back up this space key (repeatable)")
    ap.add_argument("--out", help="write the tree here instead of BACKUP_ROOT/runs/<stamp>")
    ap.add_argument("--no-archive", action="store_true", help="skip the tarball step")
    args = ap.parse_args()

    cfg = load_config()
    root_dir = Path(cfg["BACKUP_ROOT"])
    keep = int(cfg.get("KEEP_ARCHIVES", "12"))

    # Refuse to run if the target drive is not mounted. Without this, cron would
    # happily fill the root filesystem through an empty mountpoint.
    guard = cfg.get("REQUIRE_MOUNT", "").strip()
    if guard and not os.path.ismount(guard):
        sys.exit(f"FATAL: {guard} is not mounted; refusing to write backups. "
                 f"Mount the drive and re-run.")

    site = cfg["CONFLUENCE_SITE"].rstrip("/")
    conf = Confluence(site, cfg["CONFLUENCE_EMAIL"], cfg["CONFLUENCE_API_TOKEN"])

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    runs, archives = root_dir / "runs", root_dir / "archives"
    run_dir = Path(args.out) if args.out else runs / stamp
    if run_dir.exists() and args.out:
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    css_path = run_dir / "_assets" / "style.css"
    css_path.parent.mkdir(parents=True, exist_ok=True)
    css_path.write_text(STYLE, encoding="utf-8")

    # Ship the tooling inside the backup. A tree that explains how it was made,
    # and can be regenerated from itself, survives the loss of this machine.
    for extra in (Path(__file__).resolve(),
                  PROJECT_DIR / "baconexp-backup-status"):
        if extra.exists():
            shutil.copy2(extra, css_path.parent / extra.name)

    log(f"Backing up {site} -> {run_dir}")
    stats = dict(spaces=0, pages=0, folders=0, embeds=0, blogposts=0, comments=0,
                 attachments=0, failed_attachments=0, unresolved_links=0,
                 external_images=0, render_errors=0, fetch_errors=0)
    missing = []

    spaces = list(conf.paginate("/wiki/api/v2/spaces"))
    if args.space:
        wanted = {s.lower() for s in args.space}
        spaces = [s for s in spaces if (s.get("key") or "").lower() in wanted]
    log(f"Found {len(spaces)} spaces")
    (run_dir / "_assets" / "spaces.json").write_text(
        json.dumps(spaces, indent=2, ensure_ascii=False), encoding="utf-8")

    space_entries = []
    for sp in spaces:
        key = sp.get("key") or str(sp["id"])
        name = sp.get("name") or key
        log(f"  space {key} ({name})")
        stats["spaces"] += 1

        root = space_tree(conf, sp, stats)
        nodes = list(root.walk())

        # Page emoji lives in page properties, and it is part of the title in the
        # sidebar - so it has to be known before directories are named.
        for n in nodes:
            if n.type == "page":
                n.emoji = fetch_emoji(conf, n.id)

        space_dir = run_dir / safe_name(f"{key} - {name}")
        root.dir = space_dir
        assign_dirs(root, space_dir)

        by_id = {n.id: n for n in nodes}
        by_title = {}
        for n in nodes:
            by_title.setdefault((key, n.title), n)
        source_dir = space_dir / "_source"
        source_dir.mkdir(parents=True, exist_ok=True)

        log(f"    {len(nodes)} items in tree; fetching content")
        comments_by_id = {}
        for n in nodes:
            try:
                fetch_body(conf, n, source_dir)
                fetch_attachments(conf, n, source_dir, stats, missing)
            except Exception as e:
                stats["fetch_errors"] += 1
                log(f"    ! fetch failed for {n.type} {n.id} ({n.title}): "
                    f"{type(e).__name__}: {e}")
            cmts = fetch_comments(conf, n)
            if cmts:
                comments_by_id[n.id] = cmts
                stats["comments"] += len(cmts)
            stats[{"page": "pages", "folder": "folders", "embed": "embeds",
                   "blogpost": "blogposts"}.get(n.type, "pages")] += 1

        author_ids = set()
        for n in nodes:
            author_ids.add((n.meta.get("version") or {}).get("authorId", ""))
            for c in comments_by_id.get(n.id, []):
                author_ids.add((c.get("version") or {}).get("authorId", ""))
        users = resolve_users(conf, author_ids)

        ext_dir = run_dir / "_assets" / "external"
        ctx = LinkContext(site, by_id, by_title, users, conf, ext_dir)
        for n in nodes:
            try:
                write_node(n, ctx, root.children, root, site, users,
                           comments_by_id.get(n.id, []), css_path)
            except Exception as e:
                stats["render_errors"] += 1
                log(f"    ! could not render {n.type} {n.id} ({n.title}): "
                    f"{type(e).__name__}: {e}")
                write_stub(n, root, css_path, e)
        stats["unresolved_links"] += ctx.unresolved
        stats["external_images"] += ctx.external

        space_entries.append({"label": f"{key} - {name}", "home": root.page,
                              "pages": sum(1 for n in nodes if n.type == "page")})

    write_site_index(run_dir, space_entries, css_path, stamp, site)

    log("Totals: " + ", ".join(f"{k}={v}" for k, v in stats.items()))
    if missing:
        log(f"{len(missing)} attachments could not be downloaded "
            f"(Confluence returned an error for the file itself):")
        for sk, title, fname, status in missing[:25]:
            log(f"    HTTP {status}  {sk} / {title} / {fname}")

    if stats["pages"] == 0:
        sys.exit("FATAL: captured zero pages - refusing to call this a backup.")

    if not args.no_archive and not args.out:
        archives.mkdir(parents=True, exist_ok=True)
        tarball = archives / f"baconexp-{stamp}.tar.gz"
        log(f"Archiving -> {tarball}")
        with tarfile.open(tarball, "w:gz") as tf:
            tf.add(run_dir, arcname=stamp)
        log(f"Archive size: {tarball.stat().st_size / 1e6:.1f} MB")
        old = sorted(archives.glob("baconexp-*.tar.gz"))[:-keep]
        for o in old:
            log(f"Pruning old archive {o.name}")
            o.unlink()

    if not args.out:
        link = root_dir / "latest"
        tmp = root_dir / ".latest.tmp"
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        tmp.symlink_to(run_dir)
        os.replace(tmp, link)
        stale = sorted(p for p in runs.iterdir() if p.is_dir())[:-keep]
        for s in stale:
            log(f"Pruning old run {s.name}")
            shutil.rmtree(s, ignore_errors=True)

    log("Backup complete.")


if __name__ == "__main__":
    main()
