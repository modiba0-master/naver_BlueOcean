-- BlueOcean MariaDB schema (Railway)
-- 목적: Excel 일회성 파일 대신 실행 이력/키워드 지표/월별 트렌드 누적 저장

CREATE TABLE IF NOT EXISTS analysis_runs (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  run_token VARCHAR(64) NOT NULL COMMENT '클라이언트 생성 실행 토큰',
  seed_keywords_raw TEXT NOT NULL COMMENT '원본 시드 키워드 입력',
  start_date DATE NOT NULL,
  end_date DATE NOT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'RUNNING' COMMENT 'RUNNING/SUCCESS/FAILED',
  result_count INT UNSIGNED NOT NULL DEFAULT 0,
  error_message TEXT NULL,
  started_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  finished_at DATETIME(6) NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_run_token (run_token),
  KEY idx_status_time (status, started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


CREATE TABLE IF NOT EXISTS keyword_metrics (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  run_id BIGINT UNSIGNED NOT NULL,
  seed_keyword VARCHAR(255) NOT NULL,
  keyword_text VARCHAR(255) NOT NULL,
  monthly_search_volume_est INT UNSIGNED NOT NULL DEFAULT 0,
  monthly_click_est DECIMAL(12,1) NOT NULL DEFAULT 0.0,
  avg_ctr_pct DECIMAL(7,2) NOT NULL DEFAULT 0.00,
  product_count INT UNSIGNED NOT NULL DEFAULT 0,
  top10_avg_reviews DECIMAL(12,2) NULL,
  top10_avg_price DECIMAL(12,2) NULL,
  blue_ocean_score DECIMAL(12,4) NOT NULL DEFAULT 0.0000,
  strategy_text VARCHAR(512) NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_run_seed_keyword (run_id, seed_keyword, keyword_text),
  KEY idx_keyword_text (keyword_text),
  KEY idx_score (blue_ocean_score),
  CONSTRAINT fk_keyword_metrics_run
    FOREIGN KEY (run_id) REFERENCES analysis_runs(id)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

ALTER TABLE keyword_metrics
  ADD COLUMN IF NOT EXISTS top10_avg_reviews DECIMAL(12,2) NULL AFTER product_count,
  ADD COLUMN IF NOT EXISTS top10_avg_price DECIMAL(12,2) NULL AFTER top10_avg_reviews;


CREATE TABLE IF NOT EXISTS keyword_trends_monthly (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  metric_id BIGINT UNSIGNED NOT NULL,
  trend_month CHAR(7) NOT NULL COMMENT 'YYYY-MM',
  ratio_value DECIMAL(12,6) NOT NULL DEFAULT 0.000000,
  est_search_volume INT UNSIGNED NOT NULL DEFAULT 0,
  est_click_volume DECIMAL(12,1) NOT NULL DEFAULT 0.0,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_metric_month (metric_id, trend_month),
  KEY idx_month (trend_month),
  CONSTRAINT fk_trends_metric
    FOREIGN KEY (metric_id) REFERENCES keyword_metrics(id)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
