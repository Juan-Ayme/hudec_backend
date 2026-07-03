-- =====================================================================
-- KAWII — Migración: bsale_token pasa a bytea (cifrado con pgcrypto)
-- =====================================================================
-- Cambia companies.bsale_token de TEXT a BYTEA para almacenar el output
-- de pgp_sym_encrypt(). Como el campo está en NULL (todavía nadie cargó
-- tokens en la DB nueva kawii_mt), la conversión es trivial.
--
-- La clave maestra vive en .env como TOKEN_ENCRYPTION_KEY y NUNCA se
-- guarda en la DB. Sin la clave, los tokens en `bsale_token` son basura.
--
-- Ejemplo de uso desde SQL:
--   -- Escribir:
--   UPDATE companies SET bsale_token = pgp_sym_encrypt('tok-123', 'clave')
--   WHERE id = 1;
--   -- Leer:
--   SELECT pgp_sym_decrypt(bsale_token, 'clave')::text FROM companies
--   WHERE id = 1;
--
-- Aplicar:
--   psql -U postgres -d kawii_mt -f docs/migrations/2026-07-01_encrypt_bsale_token.sql
-- =====================================================================

BEGIN;

ALTER TABLE companies
    ALTER COLUMN bsale_token TYPE bytea USING NULL::bytea;

COMMENT ON COLUMN companies.bsale_token IS
    'Token BSale cifrado con pgp_sym_encrypt(). La clave vive en .env como TOKEN_ENCRYPTION_KEY.';

COMMIT;
