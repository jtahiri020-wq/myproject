#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
find_social_pages.py

أداة تقرأ ملف CSV يحتوي على أسماء مشاريع ومدن، وتبحث عبر SearXNG
عن صفحات فيسبوك وانستغرام المطابقة لكل مشروع، مع تقييم دقة كل نتيجة
عبر مطابقة ضبابية (fuzzy matching) بين اسم المشروع والعنوان/الرابط.

الاستخدام:
    python3 find_social_pages.py --input in.csv --output out.csv \
        --name-column اسم_المشروع --city-column المدينة

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


BAD_FB_PATH_FRAGMENTS = [
    "/sharer", "/login", "/watch", "/groups/", "/help/", "/policies/",
    "/l.php", "/dialog/", "/plugins/", "/tr/", "/ads/", "/marketplace/",
]

ARABIC_DIACRITICS = re.compile(r"[\u0617-\u061A\u064B-\u0652\u0670\u0640]")


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


def is_good_facebook_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return not any(frag in path for frag in BAD_FB_PATH_FRAGMENTS)


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
        results = [r for r in results if is_good_facebook_url(r.get("url", ""))]

    scored = [(score_result(project_name, city, r), r) for r in results]
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:top_n]


def format_alternatives(scored_results):
    parts = []
    for score, r in scored_results:
        parts.append(f"{r.get('url','')} (ثقة {score:.0%})")
    return " | ".join(parts)


def main():
    parser = argparse.ArgumentParser(description="بحث عن صفحات فيسبوك/انستغرام عبر SearXNG")
    parser.add_argument("--input", required=True, help="ملف CSV المدخل")
    parser.add_argument("--output", required=True, help="ملف CSV المخرج")
    parser.add_argument("--name-column", required=True, help="اسم عمود اسم المشروع")
    parser.add_argument("--city-column", required=True, help="اسم عمود المدينة")
    parser.add_argument("--url", default="http://localhost:8080", help="رابط SearXNG")
    parser.add_argument("--top-n", type=int, default=3, help="عدد الاحتمالات المحفوظة لكل منصة")
    parser.add_argument("--delay", type=float, default=2.0, help="ثواني الانتظار بين كل بحث")
    parser.add_argument("--min-score", type=float, default=0.55, help="أدنى درجة ثقة تُعتبر مقبولة تلقائيًا")
    args = parser.parse_args()

    with open(args.input, newline="", encoding="utf-8-sig") as f_in:
        reader = csv.DictReader(f_in)
        rows = list(reader)
        if args.name_column not in reader.fieldnames or args.city_column not in reader.fieldnames:
            print(
                f"خطأ: الأعمدة المتاحة في الملف هي: {reader.fieldnames}\n"
                f"وأنت طلبت: '{args.name_column}' و '{args.city_column}'",
                file=sys.stderr,
            )
            sys.exit(1)

    output_fields = [
        args.name_column,
        args.city_column,
        "رابط_فيسبوك",
        "ثقة_فيسبوك",
        "عنوان_فيسبوك",
        "بدائل_فيسبوك",
        "رابط_انستغرام",
        "ثقة_انستغرام",
        "عنوان_انستغرام",
        "بدائل_انستغرام",
        "يحتاج_مراجعة",
    ]

    total = len(rows)
    with open(args.output, "w", newline="", encoding="utf-8-sig") as f_out:
        writer = csv.DictWriter(f_out, fieldnames=output_fields)
        writer.writeheader()

        for i, row in enumerate(rows, start=1):
            project = (row.get(args.name_column) or "").strip()
            city = (row.get(args.city_column) or "").strip()
            print(f"[{i}/{total}] البحث عن: {project} ({city})")

            fb_matches = find_best_matches(args.url, project, city, "facebook", args.top_n, args.delay)
            ig_matches = find_best_matches(args.url, project, city, "instagram", args.top_n, args.delay)

            fb_best_score, fb_best = (fb_matches[0] if fb_matches else (0.0, {}))
            ig_best_score, ig_best = (ig_matches[0] if ig_matches else (0.0, {}))

            needs_review = (
                (not fb_matches or fb_best_score < args.min_score)
                and (not ig_matches or ig_best_score < args.min_score)
            ) or (
                (fb_matches and fb_best_score < args.min_score)
                or (ig_matches and ig_best_score < args.min_score)
            )

            writer.writerow({
                args.name_column: project,
                args.city_column: city,
                "رابط_فيسبوك": fb_best.get("url", ""),
                "ثقة_فيسبوك": f"{fb_best_score:.0%}" if fb_matches else "",
                "عنوان_فيسبوك": fb_best.get("title", ""),
                "بدائل_فيسبوك": format_alternatives(fb_matches),
                "رابط_انستغرام": ig_best.get("url", ""),
                "ثقة_انستغرام": f"{ig_best_score:.0%}" if ig_matches else "",
                "عنوان_انستغرام": ig_best.get("title", ""),
                "بدائل_انستغرام": format_alternatives(ig_matches),
                "يحتاج_مراجعة": "نعم" if needs_review else "لا",
            })
            f_out.flush()

    print(f"\nتم! النتائج محفوظة في: {args.output}")


if __name__ == "__main__":
    main()
