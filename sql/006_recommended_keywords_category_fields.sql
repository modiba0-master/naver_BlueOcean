-- 추천 키워드 카테고리 분류 필드 추가 (키워드별 카테고리 경로 추적)

ALTER TABLE recommended_keyword_candidates
  ADD COLUMN IF NOT EXISTS category_path VARCHAR(512) NULL AFTER product_count;

ALTER TABLE recommended_keyword_candidates
  ADD COLUMN IF NOT EXISTS category_l1 VARCHAR(128) NULL AFTER category_path;

ALTER TABLE recommended_keyword_candidates
  ADD COLUMN IF NOT EXISTS category_l2 VARCHAR(128) NULL AFTER category_l1;

ALTER TABLE recommended_keyword_candidates
  ADD COLUMN IF NOT EXISTS category_l3 VARCHAR(128) NULL AFTER category_l2;

ALTER TABLE recommended_keyword_candidates
  ADD COLUMN IF NOT EXISTS category_l4 VARCHAR(128) NULL AFTER category_l3;

ALTER TABLE recommended_keywords
  ADD COLUMN IF NOT EXISTS category_path VARCHAR(512) NULL AFTER product_count;

ALTER TABLE recommended_keywords
  ADD COLUMN IF NOT EXISTS category_l1 VARCHAR(128) NULL AFTER category_path;

ALTER TABLE recommended_keywords
  ADD COLUMN IF NOT EXISTS category_l2 VARCHAR(128) NULL AFTER category_l1;

ALTER TABLE recommended_keywords
  ADD COLUMN IF NOT EXISTS category_l3 VARCHAR(128) NULL AFTER category_l2;

ALTER TABLE recommended_keywords
  ADD COLUMN IF NOT EXISTS category_l4 VARCHAR(128) NULL AFTER category_l3;
