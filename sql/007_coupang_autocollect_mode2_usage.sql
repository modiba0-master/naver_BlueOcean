-- 4번 탭 · 2번(단일창 연속) 자동 쿠팡 수집에서 처리한 키워드 기록(동일 배치 재선택 방지)

CREATE TABLE IF NOT EXISTS coupang_autocollect_mode2_usage (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  batch_token VARCHAR(64) NOT NULL,
  keyword_text VARCHAR(255) NOT NULL,
  success TINYINT(1) NOT NULL DEFAULT 0,
  item_count INT UNSIGNED NOT NULL DEFAULT 0,
  reason_short VARCHAR(255) NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_mode2_batch_keyword (batch_token, keyword_text(191))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
