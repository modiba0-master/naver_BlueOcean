-- recommended_keywords 운영 컬럼 보강 (기존 CREATE 이후 적용)

ALTER TABLE recommended_keywords
  ADD COLUMN IF NOT EXISTS keyword VARCHAR(255) NULL COMMENT 'keyword_text 동기' AFTER keyword_text;

ALTER TABLE recommended_keywords
  ADD COLUMN IF NOT EXISTS intent VARCHAR(32) NULL COMMENT '검색 의도' AFTER final_score;

ALTER TABLE recommended_keywords
  ADD COLUMN IF NOT EXISTS season_type VARCHAR(32) NULL COMMENT '시즌 유형' AFTER intent;
