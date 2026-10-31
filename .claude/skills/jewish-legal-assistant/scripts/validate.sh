#!/usr/bin/env bash
# Validates the jewish-legal-assistant skill.
# Usage: bash scripts/validate.sh        (from the repo root, or any directory)
# Exit code: 0 = valid, 1 = errors found.
#
# Checks:
#   1. SKILL.md frontmatter: YAML, name, kebab-case, no reserved words, license,
#      description length (<=1024 chars), no angle brackets, "Use when" trigger,
#      and every required Hebrew trigger word.
#   2. SKILL.md body <= 4,000 words; legal notice, warning and closing line present.
#   3. Required files exist; every path SKILL.md points to exists.
#   4. Every number in SKILL.md, references/ (except sources.md) and templates/
#      is backed by a record in evidence.json.
#   5. evidence.json records are complete; their numbers appear in the claim or
#      quote; appears_in files exist; gov.il sources carry a Wayback capture.
#   6. Every URL used outside sources.md is listed in sources.md; every gov.il URL
#      in sources.md has a Wayback link on the same row.
#   7. Scripts compile and answer --help; metadata.json is complete.
#   8. No secrets and no real personal details (only the fictional examples).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 - "$ROOT" <<'PY'
import json, re, sys, subprocess, pathlib, py_compile

ROOT = pathlib.Path(sys.argv[1])
errors, warnings = [], []
def err(m): errors.append(m)
def warn(m): warnings.append(m)

skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")

# ---------- 1. frontmatter ----------
if not skill.startswith("---\n"):
    err("SKILL.md must start with '---' frontmatter")
    fm, body = "", skill
else:
    _, fm, body = skill.split("---", 2)
try:
    import yaml
    meta = yaml.safe_load(fm)
except ImportError:
    meta = {}
    for line in fm.splitlines():
        m = re.match(r"^(\w+):\s*(.*)$", line)
        if m:
            v = m.group(2).strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1].replace("''", "'")
            meta[m.group(1)] = v
    warn("PyYAML not installed: used a minimal frontmatter parser")
except Exception as e:
    err(f"frontmatter is not valid YAML: {e}")
    meta = {}

name = str(meta.get("name", ""))
if name != "jewish-legal-assistant":
    err(f"name must be 'jewish-legal-assistant' (got '{name}')")
if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name or "x"):
    err("name must be kebab-case")
if re.search(r"claude|anthropic", name, re.I):
    err("name may not contain 'claude' or 'anthropic'")
if ROOT.name != "jewish-legal-assistant":
    warn(f"repo folder is '{ROOT.name}'; install it as ~/.claude/skills/jewish-legal-assistant so the folder matches the name")
if str(meta.get("license", "")) != "MIT":
    err("license must be MIT")
desc = str(meta.get("description", ""))
if not desc:
    err("description missing")
if len(desc) > 1024:
    err(f"description is {len(desc)} characters (max 1024)")
if re.search(r"[<>]", fm):
    err("frontmatter contains angle brackets")
if not re.search(r"use when", desc, re.I):
    err("description must say when to use it ('Use when ...')")
TRIGGERS = ["דוח", "דוח חניה", "קנס", "דוח תנועה", 'נת"צ', "מצלמה", "ערעור", "ביטול דוח", "טכנאי",
            "ביטול עסקה", "ספאם", "תביעה קטנה", "תקנון", "מדיניות פרטיות", "נגישות", "האתר שלי חוקי"]
missing = [t for t in TRIGGERS if t not in desc]
if missing:
    err("description is missing Hebrew trigger words: " + ", ".join(missing))

# ---------- 2. body ----------
words = len(body.split())
if words > 4000:
    err(f"SKILL.md body is {words} words (max 4,000)")
CLOSING = "זו המחשה שעוזרת לכם להכין את המסמכים, לא ייעוץ משפטי. בתיק רציני, או כשיש נקודות ברישיון, דברו עם עורך דין."
for label, needle in [("closing line", CLOSING),
                      ("Hebrew legal notice", "הסקיל הזה הוא כלי עזר חינמי שמופעל על ידי בינה מלאכותית"),
                      ("English legal notice", "This skill is a free helper run by artificial intelligence"),
                      ("don't-pay warning", 'אל תשלמו "ליתר ביטחון"'),
                      ("s.229(h) in warning", "229(ח)")]:
    if needle not in body:
        err(f"SKILL.md is missing the {label}")

# ---------- 3. files ----------
REQUIRED = ["README.md", "LICENSE", "metadata.json", "evidence.json", "CHANGELOG.md",
            "references/fines.md", "references/consumer.md", "references/small-claims.md",
            "references/website.md", "references/sources.md",
            "templates/parking-cancellation.md", "templates/traffic-objection.md", "templates/natz-objection.md",
            "templates/driver-declaration.md", "templates/demand-letter.md", "templates/small-claim.md",
            "templates/terms-of-service.md", "templates/cancellation-policy.md", "templates/privacy-policy.md",
            "templates/accessibility-statement.md",
            "scripts/deadlines.py", "scripts/small_claims_fee.py", "scripts/site_check.py"]
for f in REQUIRED:
    if not (ROOT / f).exists():
        err(f"missing file: {f}")
scen = sorted((ROOT / "tests" / "scenarios").glob("*.md")) if (ROOT / "tests" / "scenarios").exists() else []
if len(scen) != 8:
    err(f"tests/scenarios must hold 8 scenarios (found {len(scen)})")
for ref in set(re.findall(r"`((?:references|templates|scripts)/[\w.\-]+)`", skill)):
    if not (ROOT / ref).exists():
        err(f"SKILL.md points to a missing file: {ref}")
lic = (ROOT / "LICENSE").read_text(encoding="utf-8") if (ROOT / "LICENSE").exists() else ""
if "Portions Copyright (c) 2026 Skills IL (Yootech)" not in lic:
    err("LICENSE must carry 'Portions Copyright (c) 2026 Skills IL (Yootech)'")

# ---------- 4. numbers backed by evidence ----------
ev = json.loads((ROOT / "evidence.json").read_text(encoding="utf-8"))
claims = ev.get("claims", [])
def norm(n): return re.sub(r"(?<=\d),(?=\d{3}\b)", "", n)
backed = {norm(n) for c in claims for n in c.get("numbers", [])}
backed |= {norm(s["value"]) for s in ev.get("structural_numbers", [])}
HEB = "֐-׿"
def tokens(text):
    out = []
    in_fence = False
    for i, line in enumerate(text.splitlines(), 1):
        if line.strip().startswith("```"):
            in_fence = not in_fence
        line = re.sub(r"^\s*(#+\s*)?\d+(\.\d+)*[.)]\s", "", line)       # list/heading numbering
        line = re.sub(r"^\s*\d+\.\d+\s+(?=\*\*)", "", line)              # 3.1 **...** letter paragraphs
        line = re.sub(r"\((\d+[א-ת]?)\)", "", line)                      # (1) subsection markers
        line = re.sub(r"https?://\S+", "", line)
        line = re.sub(r"(נספח|שלב|פרט)\s+\d+", "", line)
        line = re.sub(r"YYYY-MM-DD|DD\.MM\.YYYY", "", line)
        for m in re.finditer(r"(?<![%s\w])\d+(?:[.,:]\d+)*" % HEB, line):
            out.append((i, norm(m.group(0).rstrip(".,:"))))
    return out
scan = [ROOT / "SKILL.md"] + [p for p in sorted((ROOT / "references").glob("*.md")) if p.name != "sources.md"] \
       + sorted((ROOT / "templates").glob("*.md"))
unbacked = 0
for p in scan:
    text = p.read_text(encoding="utf-8")
    if p.name == "SKILL.md":
        text = text.split("---", 2)[2]
    for line, tok in tokens(text):
        if tok not in backed:
            unbacked += 1
            err(f"{p.relative_to(ROOT)}:{line}: number '{tok}' has no record in evidence.json")

# ---------- 5. evidence records ----------
FIELDS = ["id", "claim", "numbers", "source_url", "quote", "verified_at", "verified_via", "appears_in"]
seen = set()
for c in claims:
    for f in FIELDS:
        if f not in c or c[f] in ("", None):
            err(f"evidence {c.get('id')}: missing field '{f}'")
    if c.get("id") in seen:
        err(f"evidence: duplicate id {c.get('id')}")
    seen.add(c.get("id"))
    blob = norm(c.get("claim", "") + " " + c.get("quote", "") + " " + " ".join(c.get("related_quotes", [])))
    for n in c.get("numbers", []):
        if norm(n) not in blob:
            err(f"evidence {c['id']}: number {n} is not in its claim or quote")
    for f in c.get("appears_in", []):
        if not (ROOT / f).exists():
            err(f"evidence {c['id']}: appears_in lists a missing file {f}")
    urls = [c.get("source_url", ""), c.get("related_source_url", "")]
    if any("gov.il" in u and "tel-aviv.gov.il" not in u for u in urls) and "web.archive.org" not in c.get("wayback", ""):
        err(f"evidence {c['id']}: gov.il source without a Wayback capture")

# ---------- 6. URLs ----------
sources = (ROOT / "references" / "sources.md").read_text(encoding="utf-8")
URL = re.compile(r"https?://[^\s)|>`\"]+")
for p in [ROOT / "SKILL.md"] + sorted((ROOT / "references").glob("*.md")) + sorted((ROOT / "templates").glob("*.md")):
    if p.name == "sources.md":
        continue
    for u in URL.findall(p.read_text(encoding="utf-8")):
        if "example.com" in u:
            continue
        if u.rstrip(".,") not in sources:
            err(f"{p.relative_to(ROOT)}: URL not listed in references/sources.md: {u}")
for line in sources.splitlines():
    for u in URL.findall(line):
        if re.match(r"https://(www\.)?gov\.il/", u) and "web.archive.org" not in line:
            err(f"sources.md: gov.il URL without a Wayback capture on its row: {u}")
    if line.startswith("|") and "http" in line and "לא להשתמש" not in line:
        if not re.search(r"\d{1,2}\.\d{1,2}\.\d{4}", line) and "wikisource" not in line:
            warn(f"sources.md row without a verification date: {line[:80]}")

# ---------- 7. scripts + metadata ----------
for s in ["deadlines.py", "small_claims_fee.py", "site_check.py"]:
    path = ROOT / "scripts" / s
    try:
        py_compile.compile(str(path), doraise=True)
        r = subprocess.run([sys.executable, str(path), "--help"], capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            err(f"scripts/{s} --help failed: {r.stderr[:200]}")
    except Exception as e:
        err(f"scripts/{s} does not compile: {e}")
try:
    md = json.loads((ROOT / "metadata.json").read_text(encoding="utf-8"))
    for k in ["display_name", "display_description", "tags"]:
        if not (isinstance(md.get(k), dict) and md[k].get("he") and md[k].get("en")):
            err(f"metadata.json: '{k}' must have both 'he' and 'en'")
    if md.get("version") != "1.0.0":
        err("metadata.json: version must be 1.0.0")
    if not md.get("author"):
        err("metadata.json: author missing")
except Exception as e:
    err(f"metadata.json invalid: {e}")

# ---------- 8. secrets and personal data ----------
SECRET = re.compile(r"(sk-[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16}|ghp_[A-Za-z0-9]{36}|password:\s*\S+|api_key\s*=)")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
ALLOWED_EMAIL = re.compile(r"@example\.com$")
for p in ROOT.rglob("*"):
    if p.is_dir() or ".git" in p.parts or "_sources" in p.parts or p.suffix in (".pyc",) or p.name.endswith(".raw.json"):
        continue
    try:
        t = p.read_text(encoding="utf-8")
    except Exception:
        continue
    rel = p.relative_to(ROOT)
    if SECRET.search(t) and rel.as_posix() != "scripts/validate.sh":
        err(f"{rel}: looks like a secret")
    for e in EMAIL.findall(t):
        if not ALLOWED_EMAIL.search(e):
            err(f"{rel}: real-looking e-mail address {e}")
    for idn in re.findall(r"(?<!\d)\d{9}(?!\d)", t):
        if idn != "999999999":
            warn(f"{rel}: 9-digit number {idn} (check it is not a real ID)")

# ---------- report ----------
print(f"SKILL.md body: {words} words | description: {len(desc)} chars | evidence records: {len(claims)}")
if warnings:
    print("\nWARNINGS:")
    for w in warnings: print("  - " + w)
if errors:
    print(f"\nERRORS ({len(errors)}):")
    for e in errors: print("  - " + e)
    sys.exit(1)
print("\nAll checks passed.")
PY
