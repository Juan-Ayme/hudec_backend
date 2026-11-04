-- =====================================================================
-- KAWII — Medios de pago y devoluciones (espejo de BSale)
-- =====================================================================
-- Tablas que alimenta harvester/sync_payments.py:
--   payment_types   ← /payment_types.json  (EFECTIVO, YAPE, PLIN, IZIPAY, ...)
--   payments        ← /payments.json       (un registro por pago de un documento)
--   returns         ← /returns.json        (devoluciones / anulaciones de venta)
--   return_details  ← returns.details      (líneas devueltas)
--
-- Notas de diseño:
--   * payments guarda TAMBIÉN los pagos anulados en BSale (state != 0) con
--     is_active = false, para que un pago anulado quede marcado y no sume.
--   * payments NO tiene FK a documents: el sync de documentos solo trae
--     documentos activos (state=0) y una ventana de días, mientras que un pago
--     puede apuntar a un documento anulado o fuera de la ventana. Tampoco a
--     payment_types/users, para que un tipo o usuario nuevo en BSale nunca
--     rompa el batch; la integridad se verifica con las consultas del final.
--   * payments.amount es lo que ENTREGÓ el cliente: en efectivo incluye el
--     vuelto (doc de 26.50 pagado con 30.00 → amount = 30.00). Para el monto
--     cobrado por documento usar LEAST(documents.total_amount, SUM(amount)).
--     Verificado oct-2026: con ese tope los pagos cuadran al centavo con los
--     documentos; los únicos documentos sin pago son TRASLADO INTERNO.
--   * BSale no trae la variante en el detalle de una devolución, solo
--     documentDetailId. bsale_variant_id se completa desde document_details
--     (UPDATE posterior en el sync) y puede quedar NULL si ese detalle no está
--     sincronizado localmente.
--   * Nada de esta migración toca tablas existentes.
--
-- RLS: mismo patrón EXACTO que docs/migrations/2026-11-02_product_attributes.sql.
-- Idempotente: CREATE ... IF NOT EXISTS + DROP POLICY IF EXISTS.
--
-- Aplicar:
--   .venv/Scripts/python.exe -m tools.maintenance.apply_sql docs/migrations/2026-11-03_payments_returns.sql
-- =====================================================================

BEGIN;

-- ---------------------------------------------------------------------
-- 1. Medios de pago
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payment_types (
    company_id              integer      NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    bsale_payment_type_id   integer      NOT NULL,
    name                    varchar(120) NOT NULL,
    is_active               boolean      NOT NULL DEFAULT true,
    is_cash                 boolean      NOT NULL DEFAULT false,  -- isCash
    is_client_credit        boolean      NOT NULL DEFAULT false,  -- isClientCredit (CREDITO, CREDITO A CUOTAS)
    is_credit_memo          boolean      NOT NULL DEFAULT false,  -- isCreditMemo (ABONO DE CLIENTE)
    is_credit_note          boolean      NOT NULL DEFAULT false,  -- isCreditNote (NOTA CREDITO DEVOLUCION)
    synced_at               timestamptz  NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, bsale_payment_type_id)
);

-- ---------------------------------------------------------------------
-- 2. Pagos (~200K para Kawii)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payments (
    company_id              integer       NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    bsale_payment_id        integer       NOT NULL,
    bsale_document_id       integer,
    bsale_payment_type_id   integer,
    bsale_office_id         integer,
    bsale_user_id           integer,
    amount                  numeric(20,2) NOT NULL DEFAULT 0,
    record_date             timestamptz   NOT NULL,   -- recordDate (medianoche UTC del día, como emissionDate)
    bsale_created_at        timestamptz,              -- createdAt (hora real de registro)
    is_credit_payment       boolean       NOT NULL DEFAULT false,
    is_active               boolean       NOT NULL DEFAULT true,  -- false = anulado en BSale (state != 0)
    synced_at               timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, bsale_payment_id)
);
CREATE INDEX IF NOT EXISTS idx_payments_document
    ON payments (company_id, bsale_document_id);
CREATE INDEX IF NOT EXISTS idx_payments_record_date
    ON payments (company_id, record_date, bsale_payment_type_id);

-- ---------------------------------------------------------------------
-- 3. Devoluciones
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS returns (
    company_id                    integer       NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    bsale_return_id               integer       NOT NULL,
    code                          varchar(60),
    return_date                   timestamptz   NOT NULL,
    motive                        varchar(500),
    return_type                   integer,          -- type de BSale (-2 = anular venta, 0, 3, ...)
    price_adjustment              boolean       NOT NULL DEFAULT false,
    edit_texts                    boolean       NOT NULL DEFAULT false,
    amount                        numeric(20,2) NOT NULL DEFAULT 0,
    bsale_office_id               integer,
    bsale_user_id                 integer,
    bsale_reference_document_id   integer,          -- documento de venta devuelto
    bsale_credit_note_id          integer,          -- nota de crédito emitida (NULL al anular venta)
    synced_at                     timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, bsale_return_id)
);
CREATE INDEX IF NOT EXISTS idx_returns_reference_document
    ON returns (company_id, bsale_reference_document_id);
CREATE INDEX IF NOT EXISTS idx_returns_date
    ON returns (company_id, return_date);

CREATE TABLE IF NOT EXISTS return_details (
    company_id                  integer       NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    bsale_return_detail_id      integer       NOT NULL,
    bsale_return_id             integer       NOT NULL,
    bsale_document_detail_id    integer,          -- documentDetailId (→ document_details.bsale_detail_id)
    bsale_variant_id            integer,          -- resuelto desde document_details
    quantity                    numeric(20,4) NOT NULL DEFAULT 0,
    quantity_dev_stock          numeric(20,4) NOT NULL DEFAULT 0,  -- unidades devueltas al stock
    variant_cost                numeric(20,4) NOT NULL DEFAULT 0,
    synced_at                   timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, bsale_return_detail_id),
    FOREIGN KEY (company_id, bsale_return_id)
        REFERENCES returns(company_id, bsale_return_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_return_details_return
    ON return_details (company_id, bsale_return_id);
CREATE INDEX IF NOT EXISTS idx_return_details_variant
    ON return_details (company_id, bsale_variant_id);

-- ---------------------------------------------------------------------
-- 4. Row-Level Security (mismo patrón que las demás tablas tenant)
-- ---------------------------------------------------------------------
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'payment_types', 'payments', 'returns', 'return_details'
    ] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE  ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I '
            'USING (current_company_id() IS NULL OR company_id = current_company_id()) '
            'WITH CHECK (company_id = current_company_id())',
            t
        );
    END LOOP;
END $$;

COMMIT;

-- =====================================================================
-- VERIFICACIÓN (correr aparte):
--   -- Pagos por medio en un mes
--   SELECT pt.name, count(*), sum(p.amount)
--   FROM payments p
--   LEFT JOIN payment_types pt USING (company_id, bsale_payment_type_id)
--   WHERE p.is_active AND p.record_date >= '2026-10-01' AND p.record_date < '2026-11-01'
--   GROUP BY 1 ORDER BY 3 DESC;
--
--   -- Pagos con tipo desconocido (debería dar 0)
--   SELECT count(*) FROM payments p
--   WHERE NOT EXISTS (SELECT 1 FROM payment_types pt
--                     WHERE pt.company_id = p.company_id
--                       AND pt.bsale_payment_type_id = p.bsale_payment_type_id);
--
--   -- Devoluciones sin variante resuelta
--   SELECT count(*) FROM return_details WHERE bsale_variant_id IS NULL;
-- =====================================================================
