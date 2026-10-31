#!/usr/bin/env python3
"""מחשבון מועדים לדוחות בישראל: חניה עירונית, תנועה, מצלמה ונת"צ.

מקבל את תאריך המסירה, תאריך העבירה וסוג הדוח, ומחזיר תאריכים מדויקים:
עד מתי בקשה לביטול או השגה, עד מתי בקשה להישפט או ערר, עד מתי לשלם
בלי תוספת פיגור, ודגלים לדוח שנשלח מאוחר מדי.

הכללים (כולם מגובים ב-evidence.json):
  מסלול פלילי (חוק סדר הדין הפלילי, סעיף 229):
    בקשה לביטול 30 יום מהמסירה; אם מישהו אחר נהג 90 יום (229(א)(1)).
    בקשה להישפט 90 יום מהמסירה, או 30 יום מההחלטה בבקשה לביטול (229(א)).
    תשלום 90 יום מהמסירה; אחריו תוספת פיגור (229(ב)).
  מסלול מנהלי (חוק הפרות תעבורה מינהליות):
    השגה 30 יום; אם מישהו אחר נהג 90 יום (סעיף 8(א)).
    ערר 90 יום מהמסירה, או 30 יום מההחלטה בהשגה (סעיף 19(ב)).
    תשלום 90 יום מהמסירה (סעיף 9(א)); אחריו תוספת פיגור 30% (סעיף 10(ב)).
  איזה מסלול: לפי תאריך העבירה (סעיפים 47(ג), 48(א)).
  מגבלת משלוח: מצלמה ונת"צ 4 חודשים (225א(א1)(1); סעיף 6(ב)(1)),
    כל דוח אחר שנה (225א(א); סעיף 6(א)). החוק סופר עד יום המשלוח.
  ספירה: היום של המסירה לא נספר; יום אחרון בשבת עובר ליום שאחריו
    (חוק הפרשנות, סעיף 10). דואר רשום: נחשב נמסר אחרי 15 יום מהמשלוח.

שימוש:
  python3 deadlines.py --type parking --served 2026-10-18 --offence 2026-10-12
  python3 deadlines.py --type camera --served 2026-10-25 --offence 2026-05-20 --sent 2026-10-08 --speeding
  python3 deadlines.py --type natz --served 2026-10-01 --offence 2026-09-20 --other-driver
  python3 deadlines.py ... --json      (פלט מכונה, לבדיקות)
  python3 deadlines.py ... --today 2026-10-30   (לקבע את "היום")

אין תלות ברשת ואין ספריות חיצוניות. הפלט הוא עזר. מה שמודפס על הדוח גובר.
"""
import argparse
import calendar
import json
import sys
from datetime import date, timedelta

STAGE1 = date(2026, 2, 8)   # חוק הפרות תעבורה מינהליות, יום התחילה
STAGE2 = date(2026, 8, 8)   # שישה חודשים אחרי יום התחילה (סעיף 47(ג)(2))
FINE_LEVELS = {100, 250, 500, 750, 1000, 1500}  # דרגות הקנס בתוספת הראשונה
CANCEL_DAYS = 30
OTHER_DRIVER_DAYS = 90
SECOND_DAYS = 90
PAY_DAYS = 90
AFTER_DECISION_DAYS = 30
MAIL_PRESUMPTION_DAYS = 15
CAMERA_LIMIT_MONTHS = 4
GENERAL_LIMIT_MONTHS = 12

HE_DAYS = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]
TYPE_LABEL = {"parking": "דוח חניה עירוני", "police": "דוח תנועה (משטרה)",
              "camera": "דוח מצלמה", "natz": 'דוח נת"צ'}


def parse(s):
    try:
        y, m, d = (int(x) for x in s.split("-"))
        return date(y, m, d)
    except Exception:
        sys.exit(f"תאריך לא תקין: {s}. הפורמט הוא YYYY-MM-DD.")


def add_months(d, months):
    """חוק הפרשנות סעיף 10(ב): מסתיים ביום שמספרו כמספר יום האירוע, או ביום האחרון של חודש חסר."""
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def fmt(d):
    return f"{d.strftime('%d.%m.%Y')} (יום {HE_DAYS[d.weekday()]})"


def legal_deadline(raw):
    """סעיף 10(ג): אם היום האחרון הוא שבת, המועד עובר ליום ראשון. חגים: בדיקה ידנית."""
    if raw.weekday() == 5:
        return raw + timedelta(days=1), True
    return raw, False


def decide_track(kind, offence, fine, speeding, forced):
    if forced != "auto":
        return forced, "נקבע ידנית (--track)"
    if kind == "parking":
        return "criminal", "דוח חניה עירוני נשאר במסלול הפלילי (בקשה לביטול, בקשה להישפט). בית הדין לתעבורה לא דן בחניה עירונית."
    if kind == "natz":
        if offence >= STAGE1:
            return "admin", "נת\"צ (500 ₪) עבר למסלול המנהלי לעבירות מ-08.02.2026."
        return "criminal", "העבירה לפני 08.02.2026, ולכן נשארת במסלול הפלילי (סעיף 48(א))."
    # police / camera
    if offence < STAGE1:
        return "criminal", "העבירה לפני 08.02.2026, ולכן במסלול הפלילי (סעיף 48(א))."
    if fine is not None and fine not in FINE_LEVELS:
        return "unknown", f"הסכום {fine} ₪ אינו אחת מדרגות התוספת הראשונה (100, 250, 500, 750, 1,000, 1,500). קראו בדוח איזה מסלול כתוב בו."
    if offence < STAGE2:
        if speeding:
            return "criminal", "עבירת מהירות לפני 08.08.2026 נשארת במסלול הפלילי."
        if fine is None:
            return "unknown", "בין 08.02.2026 ל-07.08.2026 המסלול תלוי בסכום: 100-500 ₪ מנהלי, 750-1,500 ₪ פלילי. חסר סכום הקנס."
        if fine <= 500:
            return "admin", "קנס של 100-500 ₪ (לא מהירות) עבר למסלול המנהלי מ-08.02.2026."
        return "criminal", "קנס של 750-1,500 ₪ עבר למסלול המנהלי רק מ-08.08.2026, והעבירה לפני כן."
    return "admin", "מ-08.08.2026 כל דרגות הקנס (100-1,500 ₪) ועבירות מהירות במסלול המנהלי."


def limitation_flag(kind, offence, sent, served):
    months = CAMERA_LIMIT_MONTHS if kind in ("camera", "natz") else GENERAL_LIMIT_MONTHS
    limit = add_months(offence, months)
    label = "4 חודשים" if months == CAMERA_LIMIT_MONTHS else "שנה"
    src = ("סעיף 225א(א1)(1) לחוק סדר הדין הפלילי / סעיף 6(ב)(1) לחוק הפרות תעבורה מינהליות"
           if months == CAMERA_LIMIT_MONTHS else
           "סעיף 225א(א) לחוק סדר הדין הפלילי / סעיף 6(א) לחוק הפרות תעבורה מינהליות")
    flag = {"limit_label": label, "limit_end": limit.isoformat(), "source": src,
            "level": None, "message": None}
    if sent and sent > limit:
        flag["level"] = "strong"
        flag["message"] = (f"יש על מה לערער: הדוח נשלח ב-{sent.strftime('%d.%m.%Y')}, אחרי שעברו {label} מהעבירה "
                           f"(המגבלה הסתיימה ב-{limit.strftime('%d.%m.%Y')}). זו טענה חזקה. ({src})")
    elif not sent and served > limit:
        flag["level"] = "check"
        flag["message"] = (f"יש על מה לערער: הדוח הגיע אחרי שעברו {label} מהעבירה. החוק סופר עד יום המשלוח, "
                           f"לא עד יום הקבלה, אז בדקו את תאריך המשלוח שמודפס על הדוח או על המעטפה. "
                           f"אם נשלח אחרי {limit.strftime('%d.%m.%Y')}, הטענה חזקה. ({src})")
    return flag


def compute(a):
    offence = parse(a.offence)
    sent = parse(a.sent) if a.sent else None
    presumed = False
    if a.served:
        served = parse(a.served)
    elif sent:
        served = sent + timedelta(days=MAIL_PRESUMPTION_DAYS)
        presumed = True
    else:
        sys.exit("חסר --served (מתי הדוח הגיע בפועל). אם ידוע רק תאריך המשלוח, תנו --sent.")
    today = parse(a.today) if a.today else date.today()
    if served < offence:
        sys.exit("תאריך המסירה מוקדם מתאריך העבירה. בדקו את התאריכים.")

    track, why = decide_track(a.type, offence, a.fine, a.speeding, a.track)

    def item(name, raw, note=""):
        legal, moved = legal_deadline(raw)
        left = (legal - today).days
        return {"name": name, "raw": raw.isoformat(), "deadline": legal.isoformat(),
                "moved_from_saturday": moved, "days_left": left, "note": note}

    tracks = ["criminal", "admin"] if track == "unknown" else [track]
    plans = {}
    for t in tracks:
        if t == "criminal":
            first = "בקשה לביטול (לעירייה או לתובע)" if a.type != "parking" else "בקשה לביטול לעירייה"
            second = "בקשה להישפט"
            first_src, second_src, pay_src = "סעיף 229(א)(1)", "סעיף 229(א)", "סעיף 229(א); תוספת לפי 229(ב)"
            surcharge = "תוספת פיגור, ריבית ודמי פיגורים לפי סעיף 67 לחוק העונשין (229(ב))"
        else:
            first, second = "השגה", "ערר לבית הדין לתעבורה"
            first_src, second_src, pay_src = "סעיף 8(א)", "סעיף 19(ב)", "סעיף 9(א); תוספת לפי סעיף 10(ב)"
            surcharge = "תוספת פיגור של 30% מהקנס, ועוד ריבית והצמדה (סעיף 10(ב))"
        items = []
        items.append(item(f"{first}", served + timedelta(days=CANCEL_DAYS), f"30 יום מהמסירה ({first_src})"))
        items.append(item(f"{first} כשמישהו אחר נהג (הסבה לנהג)", served + timedelta(days=OTHER_DRIVER_DAYS),
                          f"90 יום מהמסירה ({first_src})"))
        items.append(item(second, served + timedelta(days=SECOND_DAYS),
                          f"90 יום מהמסירה. אם הגשתם {first.split(' (')[0]}: 30 יום מההודעה על ההחלטה בה ({second_src})"))
        items.append(item("תשלום בלי תוספת פיגור", served + timedelta(days=PAY_DAYS),
                          f"90 יום מהמסירה ({pay_src}). אחרי זה: {surcharge}"
                          + ("" if t == "criminal" else ". אחרי השגה שנדחתה: 30 יום מההחלטה (סעיף 8(ג))")))
        plans[t] = {"first_step": first, "second_step": second, "items": items}

    out = {
        "type": a.type, "type_label": TYPE_LABEL[a.type],
        "offence": offence.isoformat(), "sent": sent.isoformat() if sent else None,
        "served": served.isoformat(), "served_presumed_from_mail": presumed,
        "today": today.isoformat(), "track": track, "track_reason": why,
        "other_driver": bool(a.other_driver), "plans": plans,
        "limitation": limitation_flag(a.type, offence, sent, served),
        "warning": "לא לשלם 'ליתר ביטחון': במסלול הפלילי תשלום = הודאה והרשעה (סעיף 229(ח)); במסלול המנהלי התשלום הופך את ההחלטה לסופית (סעיף 9(ב)).",
        "notes": [
            "הגשת בקשה או השגה לא עוצרת את מועד התשלום. בדקו בהחלטה את מועד התשלום החדש.",
            "היום של המסירה לא נספר. יום אחרון שיוצא בשבת עובר ליום ראשון (חוק הפרשנות, סעיף 10(ג)). חגים לא נבדקים כאן: אם היום האחרון חג, הוא עובר ליום שאחריו. עדיף להגיש כמה ימים לפני.",
        ],
    }
    if presumed:
        out["notes"].insert(0, f"לא ידוע מתי הדוח הגיע. לפי חזקת המסירה (דואר רשום) הוא נחשב נמסר 15 יום אחרי המשלוח: {served.strftime('%d.%m.%Y')}.")
    return out


def print_human(o):
    print(f"=== {o['type_label']} ===")
    print(f"עבירה: {fmt(date.fromisoformat(o['offence']))}")
    if o["sent"]:
        print(f"נשלח: {fmt(date.fromisoformat(o['sent']))}")
    print(f"נמסר: {fmt(date.fromisoformat(o['served']))}" + (" (חזקת מסירה)" if o["served_presumed_from_mail"] else ""))
    print(f"היום: {fmt(date.fromisoformat(o['today']))}")
    print()
    print("⚠️ " + o["warning"])
    print()
    track_he = {"criminal": "מסלול פלילי", "admin": "מסלול מנהלי", "unknown": "לא ברור (מוצגים שני המסלולים)"}[o["track"]]
    print(f"מסלול: {track_he}. {o['track_reason']}")
    lim = o["limitation"]
    if lim["level"]:
        print()
        print(("🚩 " if lim["level"] == "strong" else "🔎 ") + lim["message"])
    for t, plan in o["plans"].items():
        print()
        if len(o["plans"]) > 1:
            print(f"--- אם זה {'מסלול פלילי' if t == 'criminal' else 'מסלול מנהלי'} ---")
        for it in plan["items"]:
            if "כשמישהו אחר נהג" in it["name"] and not o["other_driver"]:
                prefix = "   (אם מישהו אחר נהג) "
            else:
                prefix = "• "
            d = date.fromisoformat(it["deadline"])
            left = it["days_left"]
            status = "עבר" if left < 0 else ("היום!" if left == 0 else f"נשארו {left} ימים")
            line = f"{prefix}{it['name']}: עד {fmt(d)}, {status}"
            if it["moved_from_saturday"]:
                line += f" [המועד המקורי {date.fromisoformat(it['raw']).strftime('%d.%m.%Y')} יוצא בשבת]"
            print(line)
            print(f"     {it['note']}")
    print()
    for n in o["notes"]:
        print("ℹ️ " + n)
    print("הפלט הוא עזר. מה שמודפס על הדוח גובר.")


def main():
    p = argparse.ArgumentParser(description="מחשבון מועדים לדוחות בישראל (חניה, תנועה, מצלמה, נת\"צ).")
    p.add_argument("--type", required=True, choices=["parking", "police", "camera", "natz"],
                   help="parking=חניה עירונית, police=תנועה (שוטר), camera=מצלמה, natz=נת\"צ")
    p.add_argument("--served", help="מתי הדוח הגיע בפועל (YYYY-MM-DD). לא תאריך העבירה.")
    p.add_argument("--offence", required=True, help="תאריך העבירה (YYYY-MM-DD). קובע את המסלול.")
    p.add_argument("--sent", help="תאריך המשלוח או ההפקה שמודפס על הדוח (YYYY-MM-DD), אם יש.")
    p.add_argument("--fine", type=int, help="סכום הקנס שעל הדוח, בשקלים. נדרש לקביעת המסלול בדוח משטרה או מצלמה.")
    p.add_argument("--speeding", action="store_true", help="עבירת מהירות (תקנה 54).")
    p.add_argument("--other-driver", action="store_true", help="מישהו אחר נהג (הסבה לנהג, 90 יום).")
    p.add_argument("--track", default="auto", choices=["auto", "criminal", "admin"],
                   help="לכפות מסלול, למשל לפי מה שכתוב על הדוח.")
    p.add_argument("--today", help="לקבע את התאריך של היום (YYYY-MM-DD), לבדיקות.")
    p.add_argument("--json", action="store_true", help="פלט JSON.")
    a = p.parse_args()
    o = compute(a)
    if a.json:
        print(json.dumps(o, ensure_ascii=False, indent=2))
    else:
        print_human(o)


if __name__ == "__main__":
    main()
