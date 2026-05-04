-- 추천 키워드 엔진 결과 저장 (기존 테이블 ALTER 없음, 신규 테이블만 추가)

CREATE TABLE IF NOT EXISTS recommended_keywords (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  batch_token VARCHAR(64) NOT NULL COMMENT '실행 단위 UUID',
  seed_keywords_raw TEXT NOT NULL COMMENT '요청 시드 문자열',
  rank_position INT UNSIGNED NOT NULL COMMENT '1부터 순위',
  keyword_text VARCHAR(255) NOT NULL,
  metric_basis VARCHAR(16) NOT NULL DEFAULT 'mobile' COMMENT 'mobile 단일 기준',
  monthly_search_volume INT UNSIGNED NOT NULL DEFAULT 0,
  product_count INT UNSIGNED NOT NULL DEFAULT 0,
  ctr_pct DECIMAL(9,4) NOT NULL DEFAULT 0.0000,
  demand_score DECIMAL(8,2) NOT NULL DEFAULT 0.00,
  competition_component DECIMAL(8,2) NOT NULL DEFAULT 0.00,
  ctr_component DECIMAL(8,2) NOT NULL DEFAULT 0.00,
  trend_score DECIMAL(8,2) NOT NULL DEFAULT 0.00,
  keyword_score DECIMAL(8,2) NOT NULL DEFAULT 0.00,
  sales_power DECIMAL(8,2) NOT NULL DEFAULT 0.00,
  final_score DECIMAL(8,2) NOT NULL DEFAULT 0.00,
  reason_text VARCHAR(768) NULL,
  extra_json JSON NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_batch_token (batch_token),
  KEY idx_final_score (final_score),
  KEY idx_keyword_text (keyword_text(191))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
