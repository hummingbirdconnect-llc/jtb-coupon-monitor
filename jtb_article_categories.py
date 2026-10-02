"""JTB記事の分類に使う一次情報。分類は複数所属とし、根拠を残す。"""

import json
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

JST = timezone(timedelta(hours=9))
ARTICLE_CATEGORY_SOURCES = [
    ('LINE', 'https://www.jtb.co.jp/med/lineshinkicpn/', '公開ページのみ。友だち登録後の配信は受信者確認が必要'),
    ('会員限定（メルマガ/誕生日）', 'https://www.jtb.co.jp/myjtb/campaign/birthday/', '具体的な特典・コードは受信メールで確認'),
    ('会員限定（メルマガ/誕生日）', 'https://www.jtb.co.jp/myjtb/mailmagazine/', '会員宛の配信内容は受信者確認が必要'),
    ('JTB旅物語', 'https://www.jtb.co.jp/med/discountcoupon/', '専用一覧の掲載情報。配布状況は詳細条件で確認'),
    ('SNS', 'https://x.com/JTB_jp', '公式投稿の確認が必要。ログイン後の情報は自動取得対象外'),
]


def extract_category_fields(soup):
    conditions = {}
    for label in soup.select('.sec-conditions .item'):
        key = label.get_text(' ', strip=True).rstrip('：:')
        value = ' '.join(
            node.get_text(' ', strip=True) if hasattr(node, 'get_text') else str(node).strip()
            for node in label.next_siblings
        ).strip()
        if key and value:
            conditions[key] = value
    for item in soup.select('.conditionsItem'):
        label = item.select_one('.conditionsItem_tit')
        content = item.select_one('.conditionsItem_content')
        if label and content:
            conditions[label.get_text(' ', strip=True)] = content.get_text(' ', strip=True)
    plans = list(dict.fromkeys(
        node.get_text(' ', strip=True)
        for node in soup.select('.c-plan__item.is-active')
        if node.get_text(' ', strip=True)
    ))
    discounts = list(dict.fromkeys(
        node.get_text(' ', strip=True)
        for node in soup.select('.c-coupon__data .c-price, .couponTxt .price')
        if node.get_text(' ', strip=True)
    ))
    return {
        'usage_conditions': conditions,
        'booking_regions': conditions.get('予約対象地域', '') or conditions.get('目的地', ''),
        'eligible_users': conditions.get('利用者条件', ''),
        'available_plans': plans,
        'booking_channels': next((v for k, v in conditions.items() if k.startswith('利用可能箇所')), ''),
        'residence_condition': next((v for k, v in conditions.items() if k.startswith('居住地条件')), ''),
        'departure_area': conditions.get('出発地', ''),
        'target_brand': conditions.get('対象ブランド', ''),
        'target_products': conditions.get('対象', '') or conditions.get('対象ツアー', ''),
        'discount_rules': discounts,
    }


def classify_coupon(coupon):
    detail = coupon.get('detail_data') or {}
    evidence = {}
    category = coupon.get('category', '')
    if category in {'国内', '海外'}:
        evidence[category + '旅行'] = {'field': 'category', 'text': category}
    title = coupon.get('title', '')
    users = detail.get('eligible_users', '')
    # 一般的な「会員限定」だけで誕生日・メルマガ分類にはしない。
    for label, pattern in [
        ('初回限定', r'初回|初めて|新規会員'),
        ('LINE', r'LINE|ライン友だち'),
        ('会員限定（メルマガ/誕生日）', r'メルマガ|メールマガジン|誕生日'),
        ('SNS', r'SNS|Instagram|インスタグラム|X限定|Twitter'),
    ]:
        for field, value in [('title', title), ('eligible_users', users)]:
            if re.search(pattern, value, re.IGNORECASE):
                evidence[label] = {'field': field, 'text': value}
                break
    for plan in detail.get('available_plans', []):
        if 'JR' in plan and '宿泊' in plan:
            evidence['新幹線ツアー'] = {'field': 'available_plans', 'text': plan,
                'note': 'JR＋宿泊が利用可能。具体的な列車は対象プランで確認'}
    brand = detail.get('target_brand', '')
    if '旅物語' in brand or '旅物語' in title:
        evidence['JTB旅物語'] = {'field': 'target_brand' if brand else 'title', 'text': brand or title}
    for discount in [coupon.get('discount', ''), *detail.get('discount_rules', [])]:
        normalized = re.sub(r'\s+', '', discount)
        if re.search(r'(?<![\d,.])(?:3|5)[％%]|(?<![\d,.])500円(?:引|割引|OFF)', normalized, re.IGNORECASE):
            evidence['5％・3％・500円'] = {'field': 'discount_rules', 'text': discount}
            break
    return list(evidence), evidence


def collect_article_category_sources(headers, delay=2, detail_extractor=None):
    """公開一覧にない分類の公式窓口を保存。会員配信内容は捏造しない。"""
    results = []
    for category, url, limitation in ARTICLE_CATEGORY_SOURCES:
        item = {'article_category': category, 'source_url': url, 'limitation': limitation,
                'checked_at': datetime.now(JST).isoformat()}
        # Xのログイン・動的表示を、取得成功やクーポン0件として扱わない。
        if url.startswith('https://x.com/'):
            item.update(status='recipient_or_screen_check_required', evidence_text='')
            results.append(item)
            continue
        time.sleep(delay)
        try:
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            response.encoding = response.apparent_encoding
            item['final_url'] = response.url
            if response.url.split('?')[0].rstrip('/') != url.rstrip('/'):
                item.update(status='redirected_review_required', evidence_text='')
                results.append(item)
                continue
            soup = BeautifulSoup(response.text, 'html.parser')
            main = soup.select_one('.l-page') or soup.select_one('main')
            if not main:
                item.update(status='markup_review_required', evidence_text='')
                results.append(item)
                continue
            for node in main.select('script,style,header,footer,nav'):
                node.decompose()
            item.update(status='public_page_observed', evidence_text=main.get_text('\n', strip=True))
            if category == 'JTB旅物語':
                item['listed_offers'] = []
                for section_id, travel_category in [('domestic', '国内'), ('abroad', '海外')]:
                    for link in main.select(f'#{section_id} a[href*="detail/"]'):
                        offer = {'category': travel_category,
                            'detail_url': urljoin(response.url, link['href']),
                            'evidence_text': link.get_text(' ', strip=True),
                            'stock_status': '不明'}
                        if detail_extractor:
                            offer['detail_data'] = detail_extractor(offer['detail_url'])
                        item['listed_offers'].append(offer)
        except requests.RequestException as exc:
            item.update(status='fetch_failed', error=type(exc).__name__, evidence_text='')
        results.append(item)
    return results


def save_article_category_sources(data_dir, headers, delay=2, detail_extractor=None):
    data = collect_article_category_sources(headers, delay, detail_extractor)
    path = data_dir / 'article_category_sources_latest.json'
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
