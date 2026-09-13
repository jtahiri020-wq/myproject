#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
find_social_pages.py

أداة تقرأ ملف CSV يحتوي على أسماء مشاريع، مدن، وأعمدة روابط فيسبوك/انستغرام
(قد تكون فارغة لبعض الصفوف). تكتشف الصفوف الناقصة فقط، تبحث عبر SearXNG
عن صفحة الفيسبوك و/أو الانستغرام المطابقة، وتكتب النتيجة **في نفس
العمود الأصلي** إن كانت الثقة كافية. الصفوف المكتملة أصلاً لا تُمس إطلاقًا.

الاستخدام:
    python3 find_social_pages.py --input in.csv --output out.csv \
        --name-column اسم_المشروع --city-column المدينة \
        --facebook-column فيسبوك --instagram-column انستغرام

المتطلبات:
    pip install requests rapidfuzz
"""

import argparse
import csv
import re
import sys
import time
import unicodedata
from urllib.parse import urlparse

import requests

try:
    from rapidfuzz import fuzz

    def _similarity(a: str, b: str) -> float:
        if not a or not b:
            return 0.0
        return fuzz.token_sort_ratio(a, b) / 100.0
except ImportError:  # fallback بدون مكتبة خارجية
    from difflib import SequenceMatcher

    def _similarity(a: str, b: str) -> float:
        if not a or not b:
            return 0.0
        return SequenceMatcher(None, a, b).ratio()


ARABIC_DIACRITICS = re.compile(r"[\u0617-\u061A\u064B-\u0652\u0670\u0640]")

# أجزاء مسار فيسبوك التي تعني أن الرابط ليس صفحة/بروفايل حقيقيًا (مطابقة جزء كامل من المسار)
FB_NON_PAGE_SEGMENTS = {
    "sharer", "login", "watch", "groups", "help", "policies",
    "l.php", "dialog", "plugins", "tr", "ads", "marketplace",
    "posts", "photos", "videos", "photo.php", "video.php",
    "story.php", "permalink.php", "notes", "events", "reel",
    "hashtag", "live", "gaming",
}

# مسارات انستغرام التي تعني منشور/ريلز/قصة وليس صفحة حساب
IG_NON_PROFILE_FIRST_SEGMENTS = {
    "p", "reel", "reels", "tv", "stories", "explore", "accounts",
    "directory", "about", "developer", "web",
}


def normalize_text(text: str) -> str:
    """توحيد النص العربي/اللاتيني لتحسين المطابقة."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = ARABIC_DIACRITICS.sub("", text)
    text = text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    text = text.replace("ى", "ي").replace("ة", "ه")
    text = re.sub(r"[_\-\.]+", " ", text)
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def search_searxng(base_url: str, query: str, language: str = "ar", timeout: int = 15):
    params = {"q": query, "format": "json", "language": language}
    resp = requests.get(f"{base_url.rstrip('/')}/search", params=params, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    return data.get("results", [])


def filter_by_domain(results, domain: str):
    out = []
    for r in results:
        url = r.get("url", "")
        host = urlparse(url).netloc.lower()
        if domain in host:
            out.append(r)
    return out


def is_facebook_page_url(url: str) -> bool:
    """True فقط إذا كان الرابط يبدو صفحة/بروفايل حقيقيًا، وليس منشورًا أو غيره."""
    path = urlparse(url).path.strip("/").lower()
    if not path:
        return False

    segments = path.split("/")

    if any(seg in FB_NON_PAGE_SEGMENTS for seg in segments):
        return False

    if segments[0] == "pages":
        # الشكل: pages/الاسم/المعرف  -> صفحة حقيقية
        return len(segments) in (2, 3)

    if segments[0] == "profile.php":
        return True

    # أي أرقام طويلة في المسار عادة تعني معرف منشور/فيديو وليس اسم صفحة
    if any(seg.isdigit() and len(seg) > 6 for seg in segments):
        return False

    # صفحة عادية: مسار من جزء واحد فقط، مثل facebook.com/iqamat.alnour
    return len(segments) == 1


def is_instagram_profile_url(url: str) -> bool:
    """True فقط إذا كان الرابط حساب/بروفايل، وليس منشورًا أو ريلز أو قصة."""
    path = urlparse(url).path.strip("/").lower()
    if not path:
        return False
    segments = path.split("/")
    if segments[0] in IG_NON_PROFILE_FIRST_SEGMENTS:
        return False
    # حساب حقيقي: جزء واحد فقط في المسار، مثل instagram.com/iqamat.alnour
    return len(segments) == 1


def score_result(project_name: str, city: str, result: dict) -> float:
    title = result.get("title", "") or ""
    content = result.get("content", "") or ""
    url = result.get("url", "") or ""

    path_parts = [p for p in urlparse(url).path.split("/") if p]
    slug = path_parts[-1] if path_parts else ""
    slug = slug.replace("-", " ").replace(".", " ")

    n_project = normalize_text(project_name)
    n_title = normalize_text(title)
    n_slug = normalize_text(slug)

    title_score = _similarity(n_project, n_title)
    slug_score = _similarity(n_project, n_slug)
    base_score = max(title_score, slug_score)

    city_bonus = 0.0
    if city:
        n_city = normalize_text(city)
        haystack = normalize_text(title + " " + content)
        if n_city and n_city in haystack:
            city_bonus = 0.08

    return min(base_score + city_bonus, 1.0)


def find_best_matches(base_url, project_name, city, platform, top_n, delay):
    domain = "facebook.com" if platform == "facebook" else "instagram.com"
    query = f"{project_name} {city} {platform}".strip()

    time.sleep(delay)
    try:
        results = search_searxng(base_url, query)
    except Exception as exc:  # noqa: BLE001
        print(f"  [تحذير] فشل البحث عن '{project_name}' ({platform}): {exc}", file=sys.stderr)
        return []

    results = filter_by_domain(results, domain)
    if platform == "facebook":
        results = [r for r in results if is_facebook_page_url(r.get("url", ""))]
    else:
        results = [r for r in results if is_instagram_profile_url(r.get("url", ""))]

    scored = [(score_result(project_name, city, r), r) for r in results]
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:top_n]


def format_alternatives(scored_results):
    parts = []
    for score, r in scored_results:
        parts.append(f"{r.get('url','')} (ثقة {score:.0%})")
    return " | ".join(parts)


def main():
    parser = argparse.ArgumentParser(
        description="يكمل روابط فيسبوك/انستغرام الناقصة في ملف CSV عبر SearXNG"
    )
    parser.add_argument("--input", required=True, help="ملف CSV المدخل")
    parser.add_argument("--output", required=True, help="ملف CSV المخرج")
    parser.add_argument("--name-column", required=True, help="اسم عمود اسم المشروع")
    parser.add_argument("--city-column", required=True, help="اسم عمود المدينة")
    parser.add_argument("--facebook-column", required=True, help="اسم العمود الأصلي لروابط فيسبوك")
    parser.add_argument("--instagram-column", required=True, help="اسم العمود الأصلي لروابط انستغرام")
    parser.add_argument("--url", default="http://localhost:8080", help="رابط SearXNG")
    parser.add_argument("--top-n", type=int, default=3, help="عدد الاحتمالات المحفوظة عند عدم اليقين")
    parser.add_argument("--delay", type=float, default=2.0, help="ثواني الانتظار بين كل بحث")
    parser.add_argument("--min-score", type=float, default=0.55,
                         help="أدنى درجة ثقة لتعبئة الرابط تلقائيًا في العمود الأصلي")
    args = parser.parse_args()

    with open(args.input, newline="", encoding="utf-8-sig") as f_in:
        reader = csv.DictReader(f_in)
        rows = list(reader)
        original_fields = list(reader.fieldnames or [])

    required_cols = [args.name_column, args.city_column, args.facebook_column, args.instagram_column]
    missing = [c for c in required_cols if c not in original_fields]
    if missing:
        print(
            f"خطأ: الأعمدة المتاحة في الملف هي: {original_fields}\n"
            f"والأعمدة التالية غير موجودة: {missing}",
            file=sys.stderr,
        )
        sys.exit(1)

    extra_cols = ["اقتراح_فيسبوك_غير_مؤكد", "اقتراح_انستغرام_غير_مؤكد", "يحتاج_مراجعة"]
    output_fields = original_fields + [c for c in extra_cols if c not in original_fields]

    total = len(rows)
    stats = {"تم تخطيه (مكتمل أصلاً)": 0, "تمت تعبئته تلقائيًا": 0, "يحتاج مراجعة يدوية": 0, "لم يُعثر عليه": 0}

    with open(args.output, "w", newline="", encoding="utf-8-sig") as f_out:
        writer = csv.DictWriter(f_out, fieldnames=output_fields)
        writer.writeheader()

        for i, row in enumerate(rows, start=1):
            project = (row.get(args.name_column) or "").strip()
            city = (row.get(args.city_column) or "").strip()
            fb_existing = (row.get(args.facebook_column) or "").strip()
            ig_existing = (row.get(args.instagram_column) or "").strip()

            needs_fb = not fb_existing
            needs_ig = not ig_existing

            row.setdefault("اقتراح_فيسبوك_غير_مؤكد", "")
            row.setdefault("اقتراح_انستغرام_غير_مؤكد", "")
            row.setdefault("يحتاج_مراجعة", "لا")

            if not needs_fb and not needs_ig:
                print(f"[{i}/{total}] تخطي (مكتمل أصلاً): {project}")
                stats["تم تخطيه (مكتمل أصلاً)"] += 1
                writer.writerow(row)
                f_out.flush()
                continue

            print(f"[{i}/{total}] البحث عن: {project} ({city}) "
                  f"[فيسبوك: {'ناقص' if needs_fb else 'موجود'}, "
                  f"انستغرام: {'ناقص' if needs_ig else 'موجود'}]")

            row_needs_review = False

            if needs_fb:
                matches = find_best_matches(args.url, project, city, "facebook", args.top_n, args.delay)
                if matches:
                    score, best = matches[0]
                    if score >= args.min_score:
                        row[args.facebook_column] = best.get("url", "")
                        stats["تمت تعبئته تلقائيًا"] += 1
                    else:
                        row["اقتراح_فيسبوك_غير_مؤكد"] = format_alternatives(matches)
                        row_needs_review = True
                        stats["يحتاج مراجعة يدوية"] += 1
                else:
                    row_needs_review = True
                    stats["لم يُعثر عليه"] += 1

            if needs_ig:
                matches = find_best_matches(args.url, project, city, "instagram", args.top_n, args.delay)
                if matches:
                    score, best = matches[0]
                    if score >= args.min_score:
                        row[args.instagram_column] = best.get("url", "")
                        stats["تمت تعبئته تلقائيًا"] += 1
                    else:
                        row["اقتراح_انستغرام_غير_مؤكد"] = format_alternatives(matches)
                        row_needs_review = True
                        stats["يحتاج مراجعة يدوية"] += 1
                else:
                    row_needs_review = True
                    stats["لم يُعثر عليه"] += 1

            row["يحتاج_مراجعة"] = "نعم" if row_needs_review else "لا"
            writer.writerow(row)
            f_out.flush()

    print(f"\nتم! النتائج محفوظة في: {args.output}")
    print("ملخص:")
    for k, v in stats.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
