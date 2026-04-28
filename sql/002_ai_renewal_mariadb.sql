CREATE TABLE IF NOT EXISTS keyword_evaluations (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  run_id BIGINT UNSIGNED NOT NULL,
  metric_id BIGINT UNSIGNED NOT NULL,
  opportunity_score DECIMAL(12,4) NOT NULL DEFAULT 0.0000,
  commercial_score DECIMAL(12,4) NOT NULL DEFAULT 0.0000,
  final_score DECIMAL(12,4) NOT NULL DEFAULT 0.0000,
  decision_band VARCHAR(16) NOT NULL DEFAULT 'WATCH',
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_eval_metric (metric_id),
  KEY idx_eval_run (run_id),
  KEY idx_eval_band (decision_band),
  CONSTRAINT fk_eval_run
    FOREIGN KEY (run_id) REFERENCES analysis_runs(id)
    ON DELETE CASCADE,
  CONSTRAINT fk_eval_metric
    FOREIGN KEY (metric_id) REFERENCES keyword_metrics(id)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS ai_insights (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  run_id BIGINT UNSIGNED NOT NULL,
  metric_id BIGINT UNSIGNED NOT NULL,
  keyword_text VARCHAR(255) NOT NULL,
  summary_text VARCHAR(2000) NOT NULL,
  action_text VARCHAR(1000) NULL,
  risk_text VARCHAR(2000) NULL,
  evidence_json JSON NULL,
  confidence_score DECIMAL(6,4) NOT NULL DEFAULT 0.0000,
  model_version VARCHAR(64) NOT NULL DEFAULT 'rule-based-v1',
  token_usage_est INT UNSIGNED NOT NULL DEFAULT 0,
  cache_hit TINYINT(1) NOT NULL DEFAULT 0,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_ai_metric_created (metric_id, created_at),
  KEY idx_ai_run (run_id),
  CONSTRAINT fk_ai_run
    FOREIGN KEY (run_id) REFERENCES analysis_runs(id)
    ON DELETE CASCADE,
  CONSTRAINT fk_ai_metric
    FOREIGN KEY (metric_id) REFERENCES keyword_metrics(id)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS ai_pipeline_logs (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  run_id BIGINT UNSIGNED NOT NULL,
  metric_id BIGINT UNSIGNED NOT NULL,
  node_name VARCHAR(64) NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'SUCCESS',
  latency_ms INT UNSIGNED NOT NULL DEFAULT 0,
  token_usage_est INT UNSIGNED NOT NULL DEFAULT 0,
  meta_json VARCHAR(2000) NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_pipeline_metric (metric_id, created_at),
  KEY idx_pipeline_run (run_id),
  CONSTRAINT fk_pipeline_run
    FOREIGN KEY (run_id) REFERENCES analysis_runs(id)
    ON DELETE CASCADE,
  CONSTRAINT fk_pipeline_metric
    FOREIGN KEY (metric_id) REFERENCES keyword_metrics(id)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
