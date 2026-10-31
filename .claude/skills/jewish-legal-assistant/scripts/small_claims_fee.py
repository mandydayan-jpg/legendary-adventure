#!/usr/bin/env python3
"""אגרת תביעה קטנה: 1% מסכום התביעה, מעוגל כלפי מעלה לכפולה של 5 ₪, ולא פחות מ-50 ₪
(תקנה 6(א) לתקנות שיפוט בתביעות קטנות (סדרי דין)). התקרה: 39,900 ₪ נכון ל-1.1.2026
(סעיף 60 לחוק בתי המשפט). מגובה ב-evidence.json (sc-reg6-fee, courts-60-ceiling-39900).

שימוש:
  python3 small_claims_fee.py --amount 4800
  python3 small_claims_fee.py --amount 4800 --json

"""
import argparse
import json
import math
import sys

CEILING = 39900          # נכון ל-1.1.2026; מתעדכן מדי שנה
MIN_FEE = 50
ROUND_TO = 5


def fee_for(amount):
    """מחשב בשלמי אגורות כדי להימנע משגיאות עיגול (8,000.50 -> 85)."""
    agorot = round(amount * 100)
    one_percent_agorot = agorot / 100          # 1% מהסכום, באגורות
    fee = math.ceil(one_percent_agorot / 100 / ROUND_TO) * ROUND_TO
    return max(fee, MIN_FEE)


def main():
    p = argparse.ArgumentParser(description="אגרת תביעה קטנה (1%, עיגול כלפי מעלה ל-5 ₪, מינימום 50 ₪).")
    p.add_argument("--amount", type=float, required=True, help="סכום התביעה בשקלים")
    p.add_argument("--json", action="store_true", help="פלט JSON")
    a = p.parse_args()
    if not math.isfinite(a.amount) or a.amount <= 0:
        sys.exit("הסכום צריך להיות מספר חיובי.")

    over = a.amount > CEILING
    claimed = min(a.amount, CEILING)
    fee = fee_for(claimed)
    out = {
        "amount": a.amount,
        "ceiling": CEILING,
        "over_ceiling": over,
        "amount_for_fee": claimed,
        "one_percent": round(claimed / 100, 2),
        "fee": fee,
        "minimum_applied": claimed / 100 < MIN_FEE,
    }
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return
    print(f"סכום התביעה: {a.amount:,.0f} ₪")
    if over:
        print(f"⚠️ מעל התקרה ({CEILING:,} ₪ נכון ל-1.1.2026). אפשר לוותר על העודף ולתבוע {CEILING:,} ₪,")
        print("   או לתבוע את הכול בבית משפט השלום. החישוב למטה הוא על התקרה.")
    if out["minimum_applied"]:
        print(f"1% הוא {out['one_percent']:,.2f} ₪, פחות מהמינימום, ולכן האגרה: {fee} ₪ (המינימום).")
    else:
        print(f"1% הוא {out['one_percent']:,.2f} ₪, מעוגל כלפי מעלה לכפולה של 5: האגרה {fee} ₪.")
    print("אותה אגרה גם על תביעה שכנגד ועל הודעה לצד שלישי. מי שמתקשה לשלם יכול לבקש פטור מאגרה.")
    print("אם התביעה מתקבלת, בית המשפט מחייב בדרך כלל את הנתבע להחזיר את האגרה, אבל זה לא מובטח.")


if __name__ == "__main__":
    main()
