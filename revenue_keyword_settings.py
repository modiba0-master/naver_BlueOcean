"""
추천 엔진 설정: 우선순위 UI 입력 > 환경변수 > YAML > 코드 기본값.
알고리즘(정규화·가중 합·sales_power·final_score)은 변경하지 않고 로딩만 담당.
"""
from __future__ import annotations

import copy
import os
from typing import Any, Dict, List, Optional, Tuple

import yaml

_ROOT = os.path.dirname(os.path.abspath(__file__))


def _guide_yaml_path() -> str:
    return os.path.join(_ROOT, "config", "revenue_keyword_guide.yaml")


DEFAULTS: Dict[str, Any] = {
    "filter": {"min_search": 500, "min_ctr": 1.0},
    "step4": {"top_n": 100, "semaphore": 4},
    "score": {"demand": 0.25, "competition": 0.25, "ctr": 0.25, "trend": 0.25},
    "pool": {"max_products": 50_000},
    "coupang": {
        "topk_validate": 3,
        "min_review_topk": 100,
        "min_avg_price_topk": 8000.0,
    },
}


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> None:
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def _nested_get(d: Dict[str, Any], path: str, default: Any = None) -> Any:
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _nested_set(d: Dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cur = d
    for p in parts[:-1]:
        if p not in cur or not isinstance(cur[p], dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value


def load_recommend_settings(
    ui_overrides: Optional[Dict[str, Any]] = None,
    discovery_preset: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """
    설정 dict와 로그 문자열 목록 반환.
    우선순위: DEFAULTS → discovery_preset(매출규모·풀·쿠팡) → YAML → ENV → UI
    ENV 적용 시 [Config] KEY=value 형태 로그 포함.
    """
    cfg = copy.deepcopy(DEFAULTS)
    if discovery_preset and isinstance(discovery_preset, dict):
        _deep_merge(cfg, discovery_preset)
    logs: List[str] = []

    ypath = _guide_yaml_path()
    if os.path.isfile(ypath):
        try:
            with open(ypath, "r", encoding="utf-8") as f:
                y = yaml.safe_load(f) or {}
            if isinstance(y, dict):
                _deep_merge(cfg, y)
        except Exception:
            pass

    env_map: List[Tuple[str, str, Any]] = [
        ("step4.top_n", "TOP_N", int),
        ("step4.semaphore", "SEM", int),
        ("filter.min_search", "MIN_SEARCH", int),
        ("filter.min_ctr", "MIN_CTR", float),
    ]
    for path, env_name, conv in env_map:
        raw = os.getenv(env_name)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            val = conv(str(raw).strip())
            _nested_set(cfg, path, val)
            logs.append(f"[Config] {env_name}={val}")
        except Exception:
            logs.append(f"[Config] {env_name}={raw!r} (parse failed, ignored)")

    if ui_overrides and isinstance(ui_overrides, dict):
        ui_clean = {
            k: v
            for k, v in ui_overrides.items()
            if k not in ("discovery", "precoupang", "skip_coupang")
        }
        _deep_merge(cfg, ui_clean)

    # score 가중치 합 1.0 근접 보정 (알고리즘 비율만 재분배, 함수 형태는 동일)
    sc = cfg.get("score") or {}
    if isinstance(sc, dict):
        s = sum(float(sc.get(k, 0) or 0) for k in ("demand", "competition", "ctr", "trend"))
        if s > 1e-9 and abs(s - 1.0) > 0.02:
            for k in ("demand", "competition", "ctr", "trend"):
                if k in sc:
                    sc[k] = float(sc[k]) / s
            cfg["score"] = sc

    return cfg, logs


def settings_to_engine_kwargs(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """RecommendedKeywordEngine.run()에 넘길 평탄 kwargs."""
    max_search = _nested_get(cfg, "filter.max_search", None)
    out: Dict[str, Any] = {
        "min_volume": int(_nested_get(cfg, "filter.min_search", 500)),
        "min_ctr_pct": float(_nested_get(cfg, "filter.min_ctr", 1.0)),
        "top_after_score": int(_nested_get(cfg, "step4.top_n", 100)),
        "coupang_semaphore": int(_nested_get(cfg, "step4.semaphore", 4)),
        "score_weights": dict(cfg.get("score") or DEFAULTS["score"]),
        "max_products": int(_nested_get(cfg, "pool.max_products", 50_000)),
        "topk_validate": int(_nested_get(cfg, "coupang.topk_validate", 3)),
        "min_review_topk": int(_nested_get(cfg, "coupang.min_review_topk", 100)),
        "min_avg_price_topk": float(_nested_get(cfg, "coupang.min_avg_price_topk", 8000.0)),
    }
    if max_search is not None:
        try:
            out["max_volume"] = int(max_search)
        except (TypeError, ValueError):
            pass
    return out
