from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, TypedDict

try:
    import chromadb
except Exception:
    chromadb = None

try:
    from langgraph.graph import END, StateGraph
except Exception:
    END = None
    StateGraph = None


class InsightState(TypedDict):
    keyword: str
    payload: Dict[str, Any]
    retrieved_cases: List[Dict[str, Any]]
    risk_notes: List[str]
    recommendation: Dict[str, Any]
    logs: List[Dict[str, Any]]


@dataclass
class PipelineSettings:
    ai_enabled: bool
    langgraph_enabled: bool
    chroma_enabled: bool
    chroma_dir: str
    top_k: int
    max_context_chars: int
    retry_count: int
    model_version: str


class AIPipeline:
    """
    Cost-controlled AI insight pipeline.
    - LangGraph: optional orchestration
    - Chroma: optional case retrieval memory
    - Fallback: rule-based insight generation
    """

    def __init__(self, base_dir: str):
        ai_enabled = str(os.getenv("AI_INSIGHT_ENABLED", "true")).strip().lower() in {"1", "true", "yes", "on"}
        langgraph_enabled = str(os.getenv("LANGGRAPH_ENABLED", "true")).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        chroma_enabled = str(os.getenv("CHROMA_ENABLED", "true")).strip().lower() in {"1", "true", "yes", "on"}
        chroma_dir = str(os.getenv("CHROMA_PERSIST_DIR", os.path.join(base_dir, ".chroma"))).strip()
        top_k = int(str(os.getenv("AI_RETRIEVAL_TOP_K", "3")).strip() or "3")
        max_context_chars = int(str(os.getenv("AI_MAX_CONTEXT_CHARS", "3000")).strip() or "3000")
        retry_count = int(str(os.getenv("AI_RETRY_COUNT", "1")).strip() or "1")
        model_version = str(os.getenv("AI_MODEL_VERSION", "rule-based-v1")).strip() or "rule-based-v1"
        self.settings = PipelineSettings(
            ai_enabled=ai_enabled,
            langgraph_enabled=langgraph_enabled,
            chroma_enabled=chroma_enabled,
            chroma_dir=chroma_dir,
            top_k=max(1, min(top_k, 10)),
            max_context_chars=max(400, min(max_context_chars, 12000)),
            retry_count=max(0, min(retry_count, 2)),
            model_version=model_version,
        )
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._graph = self._build_graph()
        self._chroma_collection = None

    def _build_graph(self):
        if not self.settings.langgraph_enabled or StateGraph is None or END is None:
            return None
        graph = StateGraph(InsightState)
        graph.add_node("loadMetrics", self._node_load_metrics)
        graph.add_node("retrieveCases", self._node_retrieve_cases)
        graph.add_node("buildInsight", self._node_build_insight)
        graph.add_node("riskCheck", self._node_risk_check)
        graph.add_node("finalizeRecommendation", self._node_finalize)
        graph.set_entry_point("loadMetrics")
        graph.add_edge("loadMetrics", "retrieveCases")
        graph.add_edge("retrieveCases", "buildInsight")
        graph.add_edge("buildInsight", "riskCheck")
        graph.add_edge("riskCheck", "finalizeRecommendation")
        graph.add_edge("finalizeRecommendation", END)
        return graph.compile()

    def _get_collection(self):
        if not self.settings.chroma_enabled or chromadb is None:
            return None
        if self._chroma_collection is not None:
            return self._chroma_collection
        os.makedirs(self.settings.chroma_dir, exist_ok=True)
        client = chromadb.PersistentClient(path=self.settings.chroma_dir)
        self._chroma_collection = client.get_or_create_collection(name="blueocean_cases")
        return self._chroma_collection

    def upsert_case_memory(self, rows: List[Dict[str, Any]]) -> int:
        col = self._get_collection()
        if col is None or not rows:
            return 0
        ids: List[str] = []
        docs: List[str] = []
        metadatas: List[Dict[str, Any]] = []
        for row in rows:
            keyword = str(row.get("keyword_text", "")).strip()
            if not keyword:
                continue
            run_id = int(row.get("run_id", 0) or 0)
            doc = (
                f"keyword={keyword}; seed={row.get('seed_keyword', '')}; "
                f"opportunity={row.get('opportunity_score', 0)}; "
                f"commercial={row.get('commercial_score', 0)}; "
                f"final={row.get('final_score', 0)}; decision={row.get('decision_band', '')}"
            )
            doc = doc[: self.settings.max_context_chars]
            row_id = f"{run_id}:{keyword}"
            ids.append(hashlib.sha1(row_id.encode("utf-8")).hexdigest())
            docs.append(doc)
            metadatas.append(
                {
                    "keyword_text": keyword[:180],
                    "seed_keyword": str(row.get("seed_keyword", ""))[:180],
                    "decision_band": str(row.get("decision_band", ""))[:32],
                    "run_id": run_id,
                }
            )
        if not ids:
            return 0
        col.upsert(ids=ids, documents=docs, metadatas=metadatas)
        return len(ids)

    def _node_load_metrics(self, state: InsightState) -> InsightState:
        state["logs"].append({"node_name": "loadMetrics", "status": "SUCCESS"})
        return state

    def _node_retrieve_cases(self, state: InsightState) -> InsightState:
        keyword = state["keyword"]
        state["retrieved_cases"] = self.retrieve_cases(keyword)
        state["logs"].append(
            {
                "node_name": "retrieveCases",
                "status": "SUCCESS",
                "meta_json": json.dumps({"top_k": self.settings.top_k, "count": len(state["retrieved_cases"])}),
            }
        )
        return state

    def _node_build_insight(self, state: InsightState) -> InsightState:
        payload = state["payload"]
        opp = float(payload.get("opportunity_score", 0.0) or 0.0)
        com = float(payload.get("commercial_score", 0.0) or 0.0)
        final = float(payload.get("final_score", 0.0) or 0.0)
        decision = str(payload.get("decision_band", "WATCH"))
        summary = f"{state['keyword']}은(는) 기회점수 {opp:.1f}, 판매가치 {com:.1f}, 최종 {final:.1f}로 {decision} 구간입니다."
        action = "우선 테스트 판매를 진행하세요." if decision == "GO" else "시장 반응을 관찰하며 보수적으로 접근하세요."
        evidence = [
            f"월검색량={int(payload.get('monthly_search_volume_est', 0) or 0)}",
            f"상품수={int(payload.get('product_count', 0) or 0)}",
            f"CTR={float(payload.get('avg_ctr_pct', 0.0) or 0.0):.2f}%",
        ]
        if state["retrieved_cases"]:
            evidence.append(f"유사사례 {len(state['retrieved_cases'])}건 참조")
        state["recommendation"] = {
            "summary": summary,
            "action": action,
            "evidence": evidence,
            "confidence": round(min(0.95, max(0.45, final / 100.0)), 2),
            "model_version": self.settings.model_version,
        }
        state["logs"].append({"node_name": "buildInsight", "status": "SUCCESS"})
        return state

    def _node_risk_check(self, state: InsightState) -> InsightState:
        payload = state["payload"]
        notes: List[str] = []
        if int(payload.get("product_count", 0) or 0) > 200000:
            notes.append("경쟁 상품 수가 높아 초기 광고 효율 저하 위험이 있습니다.")
        if float(payload.get("trend_score", 1.0) or 1.0) < 0.95:
            notes.append("최근 트렌드 상승 탄력이 약합니다.")
        if payload.get("top10_avg_reviews") is None:
            notes.append("쿠팡 리뷰 데이터 부족으로 전환 추정 불확실성이 있습니다.")
        if not notes:
            notes.append("특이 리스크는 낮으나 소규모 테스트로 검증을 권장합니다.")
        state["risk_notes"] = notes
        state["logs"].append({"node_name": "riskCheck", "status": "SUCCESS"})
        return state

    def _node_finalize(self, state: InsightState) -> InsightState:
        rec = state["recommendation"]
        rec["risks"] = state["risk_notes"]
        rec["retrieved_cases"] = state["retrieved_cases"]
        state["recommendation"] = rec
        state["logs"].append({"node_name": "finalizeRecommendation", "status": "SUCCESS"})
        return state

    def retrieve_cases(self, keyword: str) -> List[Dict[str, Any]]:
        col = self._get_collection()
        if col is None or not str(keyword).strip():
            return []
        try:
            result = col.query(query_texts=[keyword], n_results=self.settings.top_k)
            docs = (result.get("documents") or [[]])[0]
            metas = (result.get("metadatas") or [[]])[0]
            rows: List[Dict[str, Any]] = []
            for i, doc in enumerate(docs):
                meta = metas[i] if i < len(metas) else {}
                rows.append({"doc": str(doc), "meta": meta or {}})
            return rows
        except Exception:
            return []

    def _cache_key(self, payload: Dict[str, Any]) -> str:
        cache_basis = {
            "keyword": payload.get("keyword_text", ""),
            "range": payload.get("date_range", ""),
            "model_version": self.settings.model_version,
            "scores": {
                "opportunity": payload.get("opportunity_score"),
                "commercial": payload.get("commercial_score"),
                "final": payload.get("final_score"),
            },
        }
        raw = json.dumps(cache_basis, ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def generate_insight(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self.settings.ai_enabled:
            return {
                "summary": "AI 인사이트 기능이 비활성화되어 규칙 기반 결과만 제공합니다.",
                "action": "환경변수 AI_INSIGHT_ENABLED=true 설정 후 사용하세요.",
                "risks": ["AI 기능 비활성"],
                "evidence": [],
                "confidence": 0.5,
                "model_version": self.settings.model_version,
                "pipeline_logs": [],
            }
        key = self._cache_key(payload)
        if key in self._cache:
            cached = dict(self._cache[key])
            cached["cache_hit"] = True
            return cached

        started = time.time()
        keyword = str(payload.get("keyword_text", "")).strip()
        base_state: InsightState = {
            "keyword": keyword,
            "payload": payload,
            "retrieved_cases": [],
            "risk_notes": [],
            "recommendation": {},
            "logs": [],
        }

        if self._graph is not None:
            state = self._graph.invoke(base_state)
        else:
            state = self._node_finalize(self._node_risk_check(self._node_build_insight(self._node_retrieve_cases(self._node_load_metrics(base_state)))))

        rec = dict(state["recommendation"])
        rec["pipeline_logs"] = state["logs"]
        rec["elapsed_ms"] = int((time.time() - started) * 1000)
        rec["token_usage_est"] = int(len(rec.get("summary", "")) / 4 + sum(len(e) for e in rec.get("evidence", [])) / 4)
        rec["cache_hit"] = False
        self._cache[key] = dict(rec)
        return rec

