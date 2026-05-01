CREATE TABLE IF NOT EXISTS coupang_search_runs (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  collected_at DATETIME(6) NOT NULL,
  source_type VARCHAR(32) NOT NULL DEFAULT 'smoke',
  keyword_text VARCHAR(255) NOT NULL,
  page_url VARCHAR(1000) NULL,
  page_title VARCHAR(500) NULL,
  html_len INT UNSIGNED NULL,
  card_count INT UNSIGNED NULL,
  organic_count INT UNSIGNED NULL,
  raw_json JSON NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_coupang_runs_keyword_time (keyword_text, collected_at),
  KEY idx_coupang_runs_time (collected_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS coupang_search_ranked_items (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  run_id BIGINT UNSIGNED NOT NULL,
  rank_no INT UNSIGNED NOT NULL,
  product_title VARCHAR(1000) NULL,
  price_text VARCHAR(128) NULL,
  shipping_text VARCHAR(255) NULL,
  review_count_text VARCHAR(64) NULL,
  review_score_text VARCHAR(64) NULL,
  product_url VARCHAR(1200) NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_coupang_run_rank (run_id, rank_no),
  KEY idx_coupang_item_title (product_title(255)),
  CONSTRAINT fk_coupang_items_run
    FOREIGN KEY (run_id) REFERENCES coupang_search_runs(id)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
