-- 인사이트 파이프라인: 주제어 → 쇼핑 카테고리 → 데이터랩 인기검색어 Top N → 지표 적재
-- 기존 analysis_runs / keyword_metrics 흐름과 분리

CREATE TABLE IF NOT EXISTS insight_discovery_runs (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  run_token VARCHAR(64) NOT NULL COMMENT '실행 식별',
  seed_keyword VARCHAR(255) NOT NULL,
  shopping_category_path TEXT NULL COMMENT '쇼핑 검색 다수결 카테고리 경로',
  datalab_category_id INT UNSIGNED NULL COMMENT '데이터랩 분야 코드(cid)',
  datalab_period_start DATE NOT NULL,
  datalab_period_end DATE NOT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'SUCCESS',
  note TEXT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_insight_run_token (run_token),
  KEY idx_insight_seed_time (seed_keyword, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS insight_discovery_keywords (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  run_id BIGINT UNSIGNED NOT NULL,
  row_kind VARCHAR(16) NOT NULL DEFAULT 'INSIGHT' COMMENT 'SEED | INSIGHT',
  insight_rank INT NULL COMMENT '데이터랩 인사이트 순위, 주제어 행은 NULL',
  keyword_text VARCHAR(255) NOT NULL,
  mobile_monthly_qc INT UNSIGNED NOT NULL DEFAULT 0,
  mobile_monthly_clk DECIMAL(12,1) NOT NULL DEFAULT 0.0,
  ctr_pct DECIMAL(9,4) NOT NULL DEFAULT 0.0000,
  product_count INT UNSIGNED NOT NULL DEFAULT 0,
  market_fit_score DECIMAL(12,4) NOT NULL DEFAULT 0.0000 COMMENT '수요·전환 대비 경쟁(상품수) 휴리스틱 0~100',
  vs_seed_volume_ratio DECIMAL(16,8) NULL,
  vs_seed_click_ratio DECIMAL(16,8) NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_idk_run (run_id),
  KEY idx_idk_kw (keyword_text),
  CONSTRAINT fk_idk_run FOREIGN KEY (run_id) REFERENCES insight_discovery_runs(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
