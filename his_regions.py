#!/usr/bin/env python3
"""HIS地域版の定義と、地域対応データの互換ヘルパー。"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable


@dataclass(frozen=True)
class HisRegion:
    """HIS公式サイトの発着地域。"""

    code: str
    label: str
    param: int
    aliases: tuple[str, ...] = ()

    @property
    def switch_url(self) -> str:
        return f"https://www.his-j.com/Default.aspx?param={self.param}"


PRIMARY_REGION_CODE = "kanto"

# 公式の「発着地変更」に表示される11地域。首都圏を先頭にすることで、
# 従来の首都圏版ID・証拠画像を地域対応後も維持する。
HIS_REGIONS: tuple[HisRegion, ...] = (
    HisRegion("kanto", "首都圏版", 1, ("首都圏", "関東版")),
    HisRegion("hokkaido", "北海道版", 2, ("北海道",)),
    HisRegion("tohoku", "東北版", 3, ("東北",)),
    HisRegion(
        "koshinetsu",
        "甲信越（新潟）版",
        4,
        ("甲信越版", "甲信越発", "甲信越(新潟)版"),
    ),
    HisRegion("chubu", "中部版", 5, ("中部",)),
    HisRegion("hokuriku", "北陸版", 10, ("北陸",)),
    HisRegion("kansai", "関西版", 6, ("関西",)),
    HisRegion("chugoku", "中国版", 7, ("中国",)),
    HisRegion("shikoku", "四国版", 11, ("四国",)),
    HisRegion("kyushu", "九州版", 8, ("九州",)),
    HisRegion("okinawa", "沖縄版", 9, ("沖縄",)),
)

REGION_BY_CODE = {region.code: region for region in HIS_REGIONS}
ALL_REGION_CODES = tuple(region.code for region in HIS_REGIONS)


def _normalize_area_label(value: str) -> str:
    value = str(value or "").strip()
    value = value.replace("(", "（").replace(")", "）")
    return re.sub(r"\s+", "", value)


def area_matches_region(actual_label: str, region: HisRegion) -> bool:
    """公式画面の「現在のエリア」が選択地域と一致するか判定する。"""
    actual = _normalize_area_label(actual_label)
    expected = (region.label, *region.aliases)
    return actual in {_normalize_area_label(label) for label in expected}


def coupon_region_codes(coupon: dict) -> list[str]:
    """地域対応前のHISデータを首都圏版として扱いつつ地域コードを返す。"""
    raw_codes = coupon.get("region_codes")
    if isinstance(raw_codes, list):
        codes = [str(code) for code in raw_codes if str(code) in REGION_BY_CODE]
        if codes:
            return list(dict.fromkeys(codes))

    area = _normalize_area_label(coupon.get("official_area", ""))
    if area and area not in {"全国共通", "全11地域版"}:
        matched = [
            region.code
            for region in HIS_REGIONS
            if area_matches_region(area, region)
            or _normalize_area_label(region.label) in area
        ]
        if matched:
            return matched

    # 地域対応前の監視は常に首都圏版だった。
    return [PRIMARY_REGION_CODE]


def coupon_applies_to_region(coupon: dict, region_code: str) -> bool:
    return region_code in coupon_region_codes(coupon)


def select_region_coupons(
    coupons: Iterable[dict], region_code: str = PRIMARY_REGION_CODE
) -> list[dict]:
    """指定地域に表示されたクーポンだけを返す。"""
    if region_code not in REGION_BY_CODE:
        raise ValueError(f"unknown HIS region code: {region_code}")
    return [coupon for coupon in coupons if coupon_applies_to_region(coupon, region_code)]
