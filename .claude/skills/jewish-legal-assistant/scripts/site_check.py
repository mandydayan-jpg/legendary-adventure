#!/usr/bin/env python3
"""בדיקה ראשונית של אתר מכירה מול החוק הישראלי. אופציונלי: הסקיל עובד גם בלי.

מושך את דף הבית, מחפש קישורים לתקנון, ביטולים והחזרות, פרטיות, נגישות,
משלוחים ויצירת קשר, מושך אותם, ומדפיס שלד טבלה עם ממצאים ראשוניים.
כל סימון כאן אוטומטי ומבוסס על חיפוש טקסט. צריך לאשר אותו מול הדף עצמו.
הסעיפים והתיקונים המלאים: references/website.md.

שימוש:
  python3 site_check.py https://example.co.il
  python3 site_check.py https://example.co.il --json
  python3 site_check.py --file saved_page.html     (לבדיקה מקומית של דף שמור)

ספריית התקן בלבד. התוכן שנמשך מהאתר מטופל כנתונים בלבד ואינו מורץ.
"""
import argparse
import gzip
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

UA = "Mozilla/5.0 (compatible; jewish-legal-assistant-site-check/1.0)"
MAX_BYTES = 2_000_000
TIMEOUT = 15

LINK_KEYS = {
    "terms": ["תקנון", "תנאי שימוש", "תנאי השימוש", "terms", "conditions"],
    "returns": ["ביטול עסק", "מדיניות ביטול", "החזרות", "החזרת", "החזר", "ביטולים", "return", "refund", "cancel"],
    "privacy": ["פרטיות", "privacy"],
    "accessibility": ["נגישות", "accessibility"],
    "shipping": ["משלוח", "shipping", "delivery"],
    "contact": ["צור קשר", "צרו קשר", "אודות", "contact", "about"],
}
TRACKERS = ["googletagmanager.com", "gtag(", "google-analytics.com", "connect.facebook.net", "fbq(",
            "hotjar", "tiktok.com/i18n/pixel", "analytics.tiktok", "clarity.ms", "snap.licdn"]
NEWSLETTER = ["דיוור", "ניוזלטר", "עדכונים", "פרסומ", "מבצעים", "newsletter", "marketing", "subscribe", "promotions"]


class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self.text, self.checkboxes = [], [], []
        self._a = None
        self._skip = 0
        self._label = []
        self._last_cb = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        if tag == "a" and a.get("href"):
            self._a = [a["href"], ""]
        if tag == "input" and (a.get("type") or "").lower() == "checkbox":
            cb = {"checked": "checked" in a, "attrs": " ".join(f"{k}={v}" for k, v in a.items() if v), "label": ""}
            self.checkboxes.append(cb)
            self._last_cb = cb

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1
        if tag == "a" and self._a:
            self.links.append(tuple(self._a))
            self._a = None
        if tag in ("label", "form", "p", "div", "li"):
            self._last_cb = None

    def handle_data(self, data):
        if self._skip:
            return
        d = data.strip()
        if not d:
            return
        self.text.append(d)
        if self._a is not None:
            self._a[1] += " " + d
        if self._last_cb is not None and len(self._last_cb["label"]) < 200:
            self._last_cb["label"] += " " + d


def fetch(url):
    if url.startswith("file://"):
        path = urllib.parse.unquote(urllib.parse.urlparse(url).path)
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                return 200, f.read(MAX_BYTES), url
        except OSError as e:
            return None, str(e), url
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "he,en;q=0.8",
                                               "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read(MAX_BYTES)
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            charset = r.headers.get_content_charset() or "utf-8"
            return r.status, raw.decode(charset, errors="ignore"), r.geturl()
    except urllib.error.HTTPError as e:
        return e.code, "", url
    except Exception as e:  # network errors, timeouts, bad TLS
        return None, str(e), url


def parse(html):
    p = Page()
    try:
        p.feed(html)
    except Exception:
        pass
    return p


def find_links(base, page):
    found = {}
    host = urllib.parse.urlparse(base).netloc
    for href, label in page.links:
        full = urllib.parse.urljoin(base, href)
        if urllib.parse.urlparse(full).netloc not in (host, "www." + host, host.replace("www.", "")):
            continue
        hay = (label + " " + urllib.parse.unquote(href)).lower()
        for key, words in LINK_KEYS.items():
            if key not in found and any(w.lower() in hay for w in words):
                found[key] = full
    return found


def has(text, *words):
    return any(w.lower() in text.lower() for w in words)


def row(item, status, law, finding, fix):
    return {"item": item, "status": status, "law": law, "finding": finding, "fix": fix}


def audit(home_html, home_url, pages, raw_html_all):
    home = parse(home_html)
    all_text = " ".join(home.text)
    sub = {k: parse(v[1]) for k, v in pages.items() if v and v[0] == 200}
    sub_text = {k: " ".join(p.text) for k, p in sub.items()}
    every = all_text + " " + " ".join(sub_text.values())
    rows = []

    # 1. ביטול עסקה
    rt = sub_text.get("returns", "") or sub_text.get("terms", "")
    if not rt:
        rows.append(row("ביטול עסקה ודמי ביטול", "❌", "סעיף 14ג(א)(7), 14ג(ג)(1), 14ה(ב)(1) לחוק הגנת הצרכן",
                        "לא נמצא דף מדיניות ביטול או תקנון", "templates/cancellation-policy.md"))
    else:
        bits = []
        bits.append("14 יום: " + ("נמצא" if re.search(r"14\s*(יום|ימים|ימי)|ארבעה\s*עשר", rt) else "לא נמצא"))
        bits.append("4 חודשים לזכאים: " + ("נמצא" if re.search(r"4\s*חודשים|ארבעה\s*חודשים", rt) else "לא נמצא"))
        bits.append("תקרת דמי ביטול 5% / 100 ₪: " + ("נמצא" if ("5%" in rt and "100" in rt) else "לא נמצא"))
        red = [w for w in ["זיכוי בלבד", "לא יינתן החזר כספי", "לא יתקבלו החזרות"] if w in rt]
        if red:
            bits.append("ניסוח בעייתי: " + ", ".join(red))
        ok = "נמצא" in bits[0] and not red and "נמצא" in bits[2]
        rows.append(row("ביטול עסקה ודמי ביטול", "✅" if ok and "נמצא" in bits[1] else "⚠️",
                        "סעיף 14ג(א)(7), 14ג(ג)(1), 14ג1(ג), 14ה(ב)(1) לחוק הגנת הצרכן", "; ".join(bits),
                        "להשלים לפי templates/cancellation-policy.md"))

    # 2. מחירים
    ils = has(every, "₪", 'ש"ח', "ש״ח", "ILS")
    foreign = re.search(r"[$€]\s?\d|\d\s?(USD|EUR)", every)
    no_vat = re.search(r'(לא\s*כולל|\+\s*)מע["״]?מ', every)
    st = "⚠️"
    fnd = [("מחירים בשקלים נמצאו" if ils else "לא נמצאו מחירים בשקלים בדפים שנמשכו")]
    if foreign:
        fnd.append("נמצא מחיר במטבע זר"); st = "❌"
    if no_vat:
        fnd.append('נמצא "לא כולל מע"מ" או "+ מע"מ"'); st = "❌"
    fnd.append("דמי משלוח לפני התשלום: לבדוק ידנית בסל ובקופה")
    rows.append(row('מחירים: כולל מע"מ, בשקלים, משלוח לפני התשלום', st,
                    "סעיפים 17א, 17ד, 14ג(א)(3), 14ג(ו) לחוק הגנת הצרכן", "; ".join(fnd),
                    "מחיר סופי אחד בשקלים כולל מע\"מ; דמי משלוח לפני הזנת כרטיס"))

    # 3. מבצע
    sale = has(every, "מבצע", "הנחה", "sale", "% הנחה")
    strike = re.search(r"<(del|s|strike)\b", raw_html_all, re.I) or has(every, "במקום", "מחיר קודם", "מחיר רגיל")
    rows.append(row("מבצע: המחיר הקודם ליד המחיר החדש",
                    ("⚠️" if strike else "❌") if sale else "⚠️",
                    "תקנה 6(1) לתקנות הגנת הצרכן (מכירות באשראי, מכירה מיוחדת ועסקה ברוכלות), 1983",
                    ("יש מבצעים באתר; " + ("נמצא מחיר קודם מסומן" if strike else "לא נמצא מחיר קודם ליד מחיר המבצע"))
                    if sale else "לא נמצאו מבצעים בדפים שנמשכו; לבדוק בדפי המוצרים",
                    "להציג את המחיר הקודם בפועל; אם היה בתוקף פחות מ-21 יום, לציין מתי"))

    # 4. פרטי העסק
    biz_id = re.search(r'(ח\.?\s?["״]?פ|ע\.?\s?["״]?מ|עוסק\s*מורשה|מספר\s*עוסק|ח\.פ\.?)\D{0,6}\d{8,9}', every)
    phone = re.search(r"(\*\d{4}|0\d{1,2}[-\s]?\d{3}[-\s]?\d{4})", every)
    email = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", every)
    address = re.search(r"(רחוב|רח['׳]|שד['׳]|שדרות|ת\.ד|מיקוד)\s*\S+", every)
    missing = [n for n, v in [("ח\"פ / מספר עוסק", biz_id), ("כתובת", address)] if not v]
    soft = [n for n, v in [("טלפון", phone), ("מייל", email)] if not v]
    st = "❌" if missing else ("⚠️" if soft else "✅")
    rows.append(row("פרטי העסק", st, "סעיף 14ג(א)(1) לחוק הגנת הצרכן; סעיף 11 לחוק הגנת הפרטיות",
                    ("חסר: " + ", ".join(missing + soft)) if (missing or soft) else "נמצאו מספר עוסק, כתובת, טלפון ומייל",
                    "שורה בפוטר: שם העסק, ח\"פ/ע\"מ, כתובת, טלפון, מייל"))

    # 5. נגישות
    at = sub_text.get("accessibility", "")
    if not at:
        rows.append(row("נגישות והצהרת נגישות", "❌", "תקנות 35א, 35ה לתקנות הנגישות לשירות (2013)",
                        "לא נמצא קישור להצהרת נגישות", "templates/accessibility-statement.md + בדיקת נגישות מקצועית"))
    else:
        bits = ["הצהרה נמצאה",
                "תקן 5568: " + ("כן" if "5568" in at else "לא מוזכר"),
                "AA: " + ("כן" if "AA" in at else "לא מוזכר"),
                "פרטי קשר: " + ("כן" if re.search(r"@|0\d{1,2}-?\d{7}", at) else "לא נמצאו")]
        rows.append(row("נגישות והצהרת נגישות", "⚠️", "תקנות 35א, 35ה לתקנות הנגישות לשירות (2013)",
                        "; ".join(bits) + ". נגישות האתר עצמו דורשת בדיקה מקצועית",
                        "להשלים את ההצהרה; בדיקה מקצועית לפי ת\"י 5568 ברמה AA"))

    # 6. פרטיות ועוגיות
    pt = sub_text.get("privacy", "")
    trackers = [t for t in TRACKERS if t in raw_html_all]
    banner = has(all_text, "עוגיות", "cookies", "cookie")
    reject = has(all_text, "דחה", "דחייה", "לא מסכים", "reject", "decline")
    fnd = ["מדיניות פרטיות: " + ("נמצאה" if pt else "לא נמצאה"),
           "באנר עוגיות: " + ("נמצא" if banner else "לא נמצא"),
           "כפתור דחייה: " + ("נמצא" if reject else "לא נמצא")]
    if trackers:
        fnd.append("כלי מעקב בקוד: " + ", ".join(trackers) + " (לבדוק שלא נטענים לפני הסכמה)")
    rows.append(row("פרטיות ועוגיות", "❌" if not pt else "⚠️",
                    "סעיף 11 לחוק הגנת הפרטיות; גילוי דעת הרשות להגנת הפרטיות בנושא הסכמה (25.2.2026)",
                    "; ".join(fnd), "templates/privacy-policy.md; באנר עם \"אשר הכל\" ו\"דחה הכל\" באותה בולטות (המלצה)"))

    # 7. תיבת דיוור
    cbs = home.checkboxes + [cb for p in sub.values() for cb in p.checkboxes]
    news = [cb for cb in cbs if has(cb["attrs"] + " " + cb["label"], *NEWSLETTER)]
    pre = [cb for cb in news if cb["checked"]]
    if pre:
        rows.append(row("תיבת הסכמה לדיוור", "❌", "סעיף 30א(ב) לחוק התקשורת (בזק ושידורים)",
                        "נמצאה תיבת דיוור מסומנת מראש: " + pre[0]["label"].strip()[:80],
                        "תיבה ריקה כברירת מחדל; \"פרסומת\" בתחילת כל הודעה וקישור הסרה (30א(ה))"))
    elif news:
        rows.append(row("תיבת הסכמה לדיוור", "✅", "סעיף 30א(ב) לחוק התקשורת (בזק ושידורים)",
                        "נמצאה תיבת דיוור לא מסומנת", "-"))
    else:
        rows.append(row("תיבת הסכמה לדיוור", "⚠️", "סעיף 30א(ב) לחוק התקשורת (בזק ושידורים)",
                        "לא נמצא טופס דיוור בדפים שנמשכו (ייתכן שהוא בקופה או נטען ב-JavaScript)",
                        "לבדוק ידנית בטופס ההרשמה ובקופה"))

    # 8. מסמך גילוי / תקנון
    rows.append(row("מסמך גילוי בכתב (תקנון / אישור הזמנה)", "⚠️" if sub_text.get("terms") else "❌",
                    "סעיף 14ג(ב) לחוק הגנת הצרכן",
                    "תקנון נמצא, לבדוק את התוכן" if sub_text.get("terms") else "לא נמצא תקנון",
                    "templates/terms-of-service.md; לשאול מה נשלח במייל אישור ההזמנה"))
    return rows


def main():
    ap = argparse.ArgumentParser(description="בדיקה ראשונית של אתר מכירה מול החוק הישראלי (שלד טבלה).")
    ap.add_argument("url", nargs="?", help="כתובת האתר")
    ap.add_argument("--file", help="קובץ HTML שמור של דף הבית (בלי רשת)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if not a.url and not a.file:
        ap.error("צריך כתובת אתר או --file")

    if a.file:
        import pathlib
        home_url = pathlib.Path(a.file).resolve().as_uri()
        status, home_html, _ = fetch(home_url)
        links = find_links(home_url, parse(home_html))
        pages = {k: fetch(v) for k, v in links.items()}
    else:
        url = a.url if a.url.startswith("http") else "https://" + a.url
        status, home_html, home_url = fetch(url)
        if status != 200 or len(re.sub(r"<[^>]+>", "", home_html)) < 300:
            msg = (f"האתר לא נקרא (סטטוס {status}). ייתכן שהוא חוסם בקשות אוטומטיות או בנוי כולו ב-JavaScript. "
                   "בקשו צילומי מסך: פוטר, תקנון, מדיניות ביטול, פרטיות, הצהרת נגישות, דף מוצר, שלב התשלום, "
                   "טופס דיוור ובאנר עוגיות.")
            print(json.dumps({"blocked": True, "status": status, "message": msg}, ensure_ascii=False) if a.json else msg)
            sys.exit(2)
        links = find_links(home_url, parse(home_html))
        pages = {k: fetch(v) for k, v in links.items()}

    raw_all = home_html + " ".join(v[1] for v in pages.values() if v)
    rows = audit(home_html, home_url, pages, raw_all)
    found = {k: v[2] for k, v in pages.items() if v and v[0] == 200}
    if a.json:
        print(json.dumps({"blocked": False, "pages": found, "rows": rows}, ensure_ascii=False, indent=2))
        return
    print(f"בדיקה ראשונית: {home_url}")
    print("דפים שנמצאו: " + (", ".join(f"{k}={v}" for k, v in found.items()) or "אין"))
    print()
    print("| פריט | מצב (אוטומטי) | סעיף החוק | מה נמצא / מה חסר | איך מתקנים |")
    print("|---|---|---|---|---|")
    for r in rows:
        cells = [str(r[k]).replace("|", "/") for k in ("item", "status", "law", "finding", "fix")]
        print("| " + " | ".join(cells) + " |")
    print()
    print("כל סימון כאן אוטומטי ומבוסס על חיפוש טקסט. לאמת מול הדף עצמו לפני שמציגים למשתמש.")


if __name__ == "__main__":
    main()
