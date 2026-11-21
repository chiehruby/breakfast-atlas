import argparse
import calendar
import json
import os
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.parse import parse_qs, urljoin, urlsplit


SOURCES = {
    "QB": "https://www.qburger.com.tw/articles",
    "YQ": "https://www.youngqin.com.tw/index.php?code=list&ids=884",
}
DATE_PATTERN = re.compile(r"(?<!\d)(20\d\d)[./-](\d{1,2})[./-](\d{1,2})(?!\d)")
SYSTEM_PROMPT = """你是「早餐連鎖品牌動態整理員」，只處理 Q Burger 與麥味登。
輸入是程式抓取的網頁清單；每則含 source_url、title、page_date、body。

任務：
1. 判斷內容是否直接涉及 Q Burger 或麥味登。不相關者放入 excluded，寫 source_url、title、reason。
2. 相關內容各轉成一筆 events，只輸出：
   brand, event_date, announce_date, topic_tags, summary, partner,
   company_statement, original_quote, source_url, is_plan, limitations。
3. topic_tags 至少一項，只可選：
   門市與展店、加盟與合作、產品與定位、會員與數位服務、
   通路與平台、財務與營運、外部環境（待 PEST/PESTEL 確認）。
4. weekly_summary 每句一個物件，包含 sentence 與 event_refs；
   event_refs 是 events 陣列的零起始位置。
5. 沒有事件時，events 為空陣列，weekly_summary 只有一句「本週無新事件」且 event_refs 為空陣列。

限制：
- brand 只能是「Q Burger」或「麥味登」。
- source_url 必須原樣複製輸入網址，不得自行產生或修改。
- original_quote 必須逐字出自輸入原文，不得改寫或補寫。
- 沒有的日期、數字、門市或合作對象填 null；在 limitations 說明。
- 未來規劃標 is_plan=true；不得寫成已完成。
- 公司目的或效果放入 company_statement，標為公司說法。
- 不得由時間接近推論競爭回應或因果。
- 不得將門市數寫成市占率、營收或顧客偏好。
- 不要輸出 event_id、source_level、check_result、status、reviewer、
  review_date、review_note；由程式或人工補入。

只輸出 JSON：
{"events":[...],"excluded":[...],"weekly_summary":[...]}"""
EVENT_FIELDS = set("brand event_date announce_date topic_tags summary partner company_statement original_quote source_url is_plan limitations".split())
TAGS = {"門市與展店", "加盟與合作", "產品與定位", "會員與數位服務", "通路與平台", "財務與營運", "外部環境（待 PEST/PESTEL 確認）"}


def dates_in(text):
    return [date(*map(int, match.groups())) for match in DATE_PATTERN.finditer(text)]


def three_months_before(day):
    year, month = divmod(day.year * 12 + day.month - 4, 12)
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


VOID = set("area base br col embed hr img input link meta param source track wbr".split())
IGNORED = set("script style nav footer aside iframe noscript template svg".split())
BLOCKS = set("article main div section h1 h2 h3 h4 p li ul ol table tr td blockquote br hr time".split())


class Tag:
    def __init__(self, name, attrs=(), parent=None):
        self.name = name
        self.attrs = dict(attrs)
        self.parent = parent
        self.children = []
        classes = set((self.attrs.get("class") or "").lower().split())
        self.hidden = (
            name in IGNORED or "hidden" in self.attrs
            or self.attrs.get("aria-hidden") == "true"
            or bool(classes & {"ads", "advertisement", "adsbygoogle"})
            or bool(parent and parent.hidden)
        )

    def find(self, *names, include_hidden=False):
        for child in self.children:
            if isinstance(child, Tag) and (include_hidden or not child.hidden):
                if child.name in names:
                    yield child
                yield from child.find(*names, include_hidden=include_hidden)

    def chunks(self):
        if not self.hidden:
            for child in self.children:
                if isinstance(child, str):
                    yield re.sub(r"\s+", " ", child)
                else:
                    if child.name in BLOCKS and not child.hidden:
                        yield "\n"
                    yield from child.chunks()
                    if child.name in BLOCKS and not child.hidden:
                        yield "\n"

    def lines(self):
        return [line.strip() for line in "".join(self.chunks()).splitlines() if line.strip()]

    def text(self):
        return " ".join(self.lines())


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = Tag("document")
        self.stack = [self.root]
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = Tag(tag, attrs, self.stack[-1])
        node.parent.children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].name == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


class StopRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def get_page(opener, url):
    request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"})
    with opener.open(request, timeout=25) as response:
        content = response.read()
        charset = response.headers.get_content_charset()
        if not charset:
            meta = re.search(rb'<meta[^>]+charset=["\\\']?([\w-]+)', content[:8192], re.I)
            charset = meta[1].decode("ascii") if meta else "utf-8"
        try:
            html = content.decode(charset, errors="replace")
        except LookupError as error:
            raise ValueError(f"Unknown HTML encoding: {charset}") from error
    return Page(html).root


def link_kind(source, url):
    entry = urlsplit(SOURCES[source])
    target = urlsplit(url)
    if target.scheme != "https" or target.netloc != entry.netloc:
        return None
    query = parse_qs(target.query)
    if source == "QB":
        if target.path.startswith("/article/"):
            return "article"
        if target.path.rstrip("/") == "/articles":
            return "listing"
    elif target.path.lstrip("/") == "index.php" and query.get("ids") == ["884"]:
        if "article_id" in query or query.get("flag") == ["detail"]:
            return "article"
        if query.get("code") == ["list"]:
            return "listing"
    return None


def card_date(anchor):
    node = anchor
    for _ in range(5):
        if not isinstance(node, Tag):
            break
        found = set(dates_in(node.text()))
        if len(found) == 1:
            return next(iter(found))
        if len(found) > 1:
            break
        node = node.parent
    return None


def discover(session, source, first, last):
    pending = [SOURCES[source]]
    visited = set()
    articles = {}
    while pending:
        url = pending.pop(0)
        if url in visited:
            continue
        if len(visited) >= 40:
            raise ValueError(f"{source}: exceeded pagination limit")
        visited.add(url)
        soup = get_page(session, url)
        found = 0
        page_dates = []
        next_pages = []
        for anchor in soup.find("a", include_hidden=True):
            raw = (anchor.attrs.get("href") or "").strip()
            # A double slash before index.php can be an origin-relative CMS link.
            if raw.startswith("//index.php?"):
                raw = f"https://{urlsplit(url).netloc}{raw}"
            href = urljoin(url, raw).split("#", 1)[0]
            target = urlsplit(href)
            if source == "YQ" and target.scheme == "http" and target.netloc == urlsplit(SOURCES[source]).netloc:
                href = target._replace(scheme="https").geturl()
            kind = link_kind(source, href)
            if kind == "article":
                found += 1
                day = card_date(anchor)
                page_dates.append(day)
                if href not in articles or day:
                    articles[href] = day
            elif kind == "listing" and href not in visited:
                label = anchor.text()
                if "page" in parse_qs(urlsplit(href).query) or label.isdigit() or "next" in (anchor.attrs.get("rel") or "").split() or re.search(r"�U�@��|�U��|next|^[??>]$", label, re.I):
                    next_pages.append(href)
        if not found:
            raise ValueError(f"{source}: no article links at {url}")
        # These news lists are newest-first. Stop once a dated page crosses the cutoff,
        # after the date of every card on that page was identified.
        if not (all(page_dates) and min(page_dates) < first):
            pending.extend(next_pages)
    selected = {url: day for url, day in articles.items() if day is None or first <= day <= last}
    print(f"{source}: inspected {len(visited)} list page(s); {len(selected)} candidate article(s).")
    return selected


def article_text(soup, source, hint):
    root = next(soup.find("body"), soup)
    lines = root.lines()
    page_title = next((tag.text() for tag in soup.find("title")), "")
    if source == "YQ" and " > " in page_title:
        title = page_title.rsplit(" > ", 1)[-1].strip()
    else:
        candidates = [tag.text() for tag in root.find("h1", "h2", "h3")]
        candidates = set(candidates)
        title = ""
        for index, line in enumerate(lines):
            if DATE_PATTERN.fullmatch(line):
                title = next(
                    (candidate for candidate in lines[index + 1:index + 4]
                     if candidate in candidates and candidate not in {"最新消息", "更多消息"}),
                    "",
                )
                if title:
                    break
    stops = {"回上頁", "返回最新消息", "更多消息", "看更多", "回上一頁", "隱私權政策"}
    for index, line in enumerate(lines):
        if line != title:
            continue
        nearby = lines[max(0, index - 3):index]
        page_dates = [day for line in nearby if DATE_PATTERN.fullmatch(line) for day in dates_in(line)]
        if not page_dates:
            continue  # Skip a copy of the headline in the breadcrumb.
        end = next((i for i in range(index + 1, len(lines)) if lines[i] in stops), None)
        if end is None:
            raise ValueError(f"Article end marker not found: {title}")
        published = page_dates[-1]
        body = "\n".join(lines[index + 1:end])
        if not body and source != "YQ":
            raise ValueError(f"Article text is empty: {title}")
        return title, published, body
    raise ValueError(f"Article headline or publication date not found: {title or page_title}")


def collect(session, first, last):
    articles = {}
    errors = []
    for source in SOURCES:
        count = 0
        try:
            candidates = discover(session, source, first, last)
        except (URLError, ValueError) as error:
            errors.append(f"{source} listing: {error}")
            print(f"{source}: collection failed; article count unavailable.")
            continue
        for url, hint in candidates.items():
            try:
                title, published, body = article_text(get_page(session, url), source, hint)
                if first <= published <= last:
                    if not body:
                        print(f"{source}: no machine-readable body (headline evidence only): {url}")
                    count += 1
                    source_id = f"{source}-{count:03}"
                    articles[source_id] = dict(source_id=source_id, title=title, published_date=published.isoformat(), body=body, url=url)
            except (URLError, ValueError) as error:
                errors.append(f"{source} article {url}: {error}")
        print(f"{source}: collected {count} article(s) in the selected period.")
    for error in errors:
        print(f"COLLECTION ERROR: {error}", file=sys.stderr)
    if errors:
        raise ValueError("Collection incomplete. Resolve the errors before sending the batch.")
    return articles


def ask_gemini(model, key, articles):
    if not re.fullmatch(r"[a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+)*", model):
        raise ValueError("Invalid Gemini model name.")
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    batch = [
        {"source_url": item["url"], "title": item["title"],
         "page_date": item["published_date"], "body": item["body"]}
        for item in articles.values()
    ]
    request = Request(endpoint, headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                      method="POST", data=json.dumps({
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT + "\n\nFor each input article, return exactly one record in events or excluded. Copy source_url verbatim, including all slashes, percent escapes and query parameter order. Never reuse a URL across records."}]},
        "contents": [{"parts": [{"text": json.dumps(batch, ensure_ascii=False)}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }, ensure_ascii=False).encode("utf-8"))
    try:
        with build_opener(StopRedirects()).open(request, timeout=180) as response:
            data = json.load(response)
    except HTTPError as error:
        with error:
            try:
                detail = json.load(error).get("error", {}).get("message", error.reason)
            except (ValueError, AttributeError):
                detail = error.reason
        raise ValueError(f"Gemini API HTTP {error.code}: {detail}") from None
    except (ValueError, UnicodeError) as error:
        raise ValueError(f"Gemini API response JSON parsing: {error}") from None
    if not isinstance(data, dict):
        raise ValueError("Gemini API response JSON: expected an object.")
    candidates = data.get("candidates") or []
    if not candidates or candidates[0].get("finishReason") != "STOP":
        reason = candidates[0].get("finishReason") if candidates else data.get("promptFeedback", {})
        raise ValueError(f"Gemini API returned no complete candidate: {reason}")
    parts = candidates[0].get("content", {}).get("parts", [])
    try:
        parsed = json.loads("".join(part.get("text", "") for part in parts if not part.get("thought")))
        validate_output(parsed, articles)
    except ValueError as error:
        raise ValueError(f"Gemini event JSON: {error}") from None
    print("Gemini usage:", json.dumps(data.get("usageMetadata", {}), ensure_ascii=False))
    return parsed


def validate_output(payload, articles):
    sections = {"events", "excluded", "weekly_summary"}
    if not isinstance(payload, dict) or set(payload) != sections:
        raise ValueError("Expected events, excluded and weekly_summary.")
    if any(not isinstance(payload[name], list) for name in sections):
        raise ValueError("Expected three JSON arrays.")
    events = payload["events"]
    seen = set()
    urls = {article["url"] for article in articles.values()}
    for index, event in enumerate(events):
        if not isinstance(event, dict) or set(event) != EVENT_FIELDS:
            raise ValueError(f"Invalid event fields at events[{index}].")
        url = event["source_url"]
        if not isinstance(url, str) or url not in urls:
            raise ValueError(f"source_url does not exactly match an input URL at events[{index}].")
        if url in seen:
            raise ValueError(f"Repeated input source_url at events[{index}].")
        seen.add(url)
        if type(event["is_plan"]) is not bool or not isinstance(event["original_quote"], str):
            raise ValueError(f"Invalid is_plan or original_quote at events[{index}].")
        tags = event["topic_tags"]
        if event["brand"] not in ("Q Burger", "麥味登") or not isinstance(tags, list) or not tags or any(not isinstance(tag, str) or tag not in TAGS for tag in tags):
            raise ValueError(f"Invalid brand or topic_tags at events[{index}].")
    for index, item in enumerate(payload["excluded"]):
        if not isinstance(item, dict) or set(item) != {"source_url", "title", "reason"} or not all(isinstance(value, str) for value in item.values()) or item["source_url"] not in urls:
            raise ValueError(f"Invalid excluded[{index}].")
        if item["source_url"] in seen:
            raise ValueError(f"Duplicate source_url at excluded[{index}].")
        seen.add(item["source_url"])
    if seen != urls:
        raise ValueError(f"Missing event or exclusion for {len(urls - seen)} article(s).")
    if not payload["weekly_summary"]:
        raise ValueError("weekly_summary cannot be empty.")
    for index, item in enumerate(payload["weekly_summary"]):
        if not isinstance(item, dict) or set(item) != {"sentence", "event_refs"} or not isinstance(item["sentence"], str) or not isinstance(item["event_refs"], list) or any(type(ref) is not int or not 0 <= ref < len(events) for ref in item["event_refs"]):
            raise ValueError(f"Invalid weekly_summary[{index}].")
    if not events and payload["weekly_summary"] != [{"sentence": "本週無新事件", "event_refs": []}]:
        raise ValueError("Missing required empty-events summary.")


def verify(session, events, articles):
    pages = {}
    by_url = {article["url"]: article for article in articles.values()}
    for event in events:
        url = event.get("source_url")
        article = by_url.get(url) if isinstance(url, str) else None
        quote = event.get("original_quote")
        result = {"url_opens": False, "original_quote_matches": False}
        event["check_result"] = result
        event["source_level"] = None
        if not article:
            result["error"] = "URL does not match a collected article."
            continue
        event["source_level"] = "A"
        if url not in pages:
            try:
                page = get_page(session, url)
            except URLError as error:
                pages[url] = (False, "", "", str(error))
            else:
                try:
                    title, _, body = article_text(page, article["source_id"].split("-")[0], date.fromisoformat(article["published_date"]))
                    pages[url] = (True, body, title, "")
                except ValueError as error:
                    pages[url] = (True, "", "", str(error))
        opens, body, title, error = pages[url]
        headline_only = not article["body"]
        result["quote_location"] = "title" if headline_only else "body"
        original = article["title"] if headline_only else article["body"]
        current = title if headline_only else body
        result["url_opens"] = opens
        result["original_quote_matches"] = isinstance(quote, str) and bool(quote.strip()) and quote in current and quote in original
        if error:
            result["error"] = error
        elif not result["original_quote_matches"]:
            result["error"] = "Quote was empty or not found in the specified article text."


def main():
    parser = argparse.ArgumentParser(description="W12 Role 4 D1 trend-agent test")
    taipei_today = datetime.now(timezone(timedelta(hours=8))).date()
    parser.add_argument("--date", type=date.fromisoformat, default=taipei_today)
    parser.add_argument("--model", default="gemini-2.5-flash")
    parser.add_argument("--collect-only", action="store_true")
    args = parser.parse_args()
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    stage = "collection"
    try:
        session = build_opener()
        start = three_months_before(args.date)
        print(f"Publication date range: {start} through {args.date}")
        articles = collect(session, start, args.date)
        if args.collect_only:
            return 0
        stage = "Gemini request or JSON parsing"
        if not key:
            raise ValueError("GEMINI_API_KEY is missing in this PowerShell process.")
        payload = ask_gemini(args.model, key, articles)
        events = payload["events"]
        stage = "event verification and output"
        verify(session, events, articles)
        encoded = json.dumps(payload, ensure_ascii=False, indent=2)
        if key in encoded:
            raise ValueError("Output matched the API key and was not written.")
        target = Path(__file__).with_name("events.json")
        temporary = target.with_suffix(".tmp")
        temporary.write_text(encoded + "\n", encoding="utf-8")
        temporary.replace(target)
        levels = Counter(event["source_level"] for event in events)
        passed = sum(all(event["check_result"][field] for field in ("url_opens", "original_quote_matches")) for event in events)
        print(f"Events: {len(events)} | A: {levels['A']} | B: {levels['B']} | C: {levels['C']} | Unclassified: {levels[None]}")
        print(f"Passed both checks: {passed}/{len(events)}")
        print(f"Output: {target}")
        for index, event in enumerate(events):
            error = event["check_result"].get("error")
            if error:
                print(f"CHECK FAILED events[{index}]: {error.replace(key, '[REDACTED]')}", file=sys.stderr)
        return 0 if passed == len(events) else 2
    except (ValueError, OSError, TypeError, AttributeError) as error:
        message = str(error)
        if key:
            message = message.replace(key, "[REDACTED]")
        print(f"D1 FAILED [{stage}]: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
