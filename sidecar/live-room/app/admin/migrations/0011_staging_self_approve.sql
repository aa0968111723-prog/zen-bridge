-- v10 -> v11：管理員自我核准開關（ZEN_ADMIN_SELF_APPROVE，預設關）。
-- 自動核准仍寫完整稽核；這兩欄讓「本人自動核准」和一般核准、強制核准查得出來。
ALTER TABLE staging_items ADD COLUMN approval_mode TEXT
  CHECK (approval_mode IS NULL OR approval_mode IN ('manual','override','self_auto'));
ALTER TABLE staging_audit ADD COLUMN mode TEXT
  CHECK (mode IS NULL OR mode IN ('manual','override','self_auto'));
