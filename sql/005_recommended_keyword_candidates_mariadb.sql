-- 추천 엔진: 쿠팡·sales_power 적용 전 키워드 스코어 후보 (중간 단계 검증용)

CREATE TABLE IF NOT EXISTS recommended_keyword_candidates (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  batch_token VARCHAR(64) NOT NULL COMMENT '실행 단위 UUID (recommended_keywords.batch_token 과 동일 체계)',
  seed_keywords_raw TEXT NOT NULL,
  rank_position INT UNSIGNED NOT NULL COMMENT 'keyword_score 내림차순 순위',
  keyword_text VARCHAR(255) NOT NULL,
  keyword VARCHAR(255) NULL,
  metric_basis VARCHAR(16) NOT NULL DEFAULT 'mobile',
  monthly_search_volume INT UNSIGNED NOT NULL DEFAULT 0,
  product_count INT UNSIGNED NOT NULL DEFAULT 0,
  ctr_pct DECIMAL(9,4) NOT NULL DEFAULT 0.0000,
  demand_score DECIMAL(8,2) NOT NULL DEFAULT 0.00,
  competition_component DECIMAL(8,2) NOT NULL DEFAULT 0.00,
  ctr_component DECIMAL(8,2) NOT NULL DEFAULT 0.00,
  trend_score DECIMAL(8,2) NOT NULL DEFAULT 0.00,
  keyword_score DECIMAL(8,2) NOT NULL DEFAULT 0.00,
  intent VARCHAR(32) NULL,
  season_type VARCHAR(32) NULL,
  reason_text VARCHAR(768) NULL,
  extra_json JSON NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_batch_token (batch_token),
  KEY idx_keyword_score (keyword_score),
  KEY idx_keyword_text (keyword_text(191))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
