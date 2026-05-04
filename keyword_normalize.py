"""키워드 정규화·브랜드/노이즈 필터 (추천 엔진·중복 제거 공통)."""
from __future__ import annotations

import re
from typing import Callable, Iterable, List, Optional, Set

# 부분 문자열 매칭으로 제외 (과매칭 완화 위해 짧은 토큰은 제한적으로 사용)
DEFAULT_BRAND_SUBSTRINGS: tuple[str, ...] = (
    "나이키",
    "아디다스",
    "삼성",
    "LG전자",
    "애플",
    "APPLE",
    "정품인증",
    "공식몰",
    "공식 스토어",
)

_BRAND_RE = re.compile(
    r"|".join(re.escape(x) for x in DEFAULT_BRAND_SUBSTRINGS),
    re.IGNORECASE,
)


def normalize_keyword(text: str) -> str:
    """공백 제거 + 소문자 (BlueOceanTool 내부 규칙과 동일)."""
    return "".join(str(text or "").split()).lower()


def is_brand_or_noise(keyword: str, extra_substrings: Optional[Iterable[str]] = None) -> bool:
    """브랜드·노이즈 후보 제외."""
    s = str(keyword or "").strip()
    if not s:
        return True
    if _BRAND_RE.search(s):
        return True
    if extra_substrings:
        low = s.lower()
        for frag in extra_substrings:
            if frag and str(frag).strip() and str(frag).strip().lower() in low:
                return True
    return False


def dedupe_items_by_keyword(
    items: List[dict],
    keyword_field: str = "relKeyword",
    normalize_fn: Callable[[str], str] = normalize_keyword,
) -> List[dict]:
    """동일 정규화 키는 첫 항목만 유지."""
    seen: Set[str] = set()
    out: List[dict] = []
    for it in items:
        kw = str(it.get(keyword_field) or "").strip()
        nk = normalize_fn(kw)
        if not nk or nk in seen:
            continue
        seen.add(nk)
        out.append(it)
    return out
