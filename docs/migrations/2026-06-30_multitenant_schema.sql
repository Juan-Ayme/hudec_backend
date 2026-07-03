-- =====================================================================
-- KAWII — Esquema MULTI-TENANT v1 (DB nueva desde cero)
-- =====================================================================
-- Convierte el esquema single-tenant (v3) en multi-tenant:
--   • Tabla nueva `companies` (id, name, slug, bsale_token, etc.).
--   • Tabla nueva `user_companies` (pivote N-a-N: usuario ↔ empresa, con role).
--   • `app_users` queda GLOBAL (1 usuario, N empresas vía pivote). Sin role
--     (el role vive en la pivote, distinto por empresa).
--   • `app_config` con PK compuesta (company_id, key) — cada empresa tiene
--     sus propias exclusiones, umbrales, marca, etc.
--   • Las 17 tablas espejo de BSale pasan a PK compuesta
--     (company_id, bsale_X_id). Razón: BSale puede dar el mismo ID a
--     entidades de cuentas distintas — sin company_id en la PK chocarían.
--   • Las tablas locales (departments, categories, subcategories) mantienen
--     PK serial pero añaden UNIQUE (company_id, id) para servir como
--     destino de FK compuestas.
--
-- NO TIENE DROPs — está pensado para correrse sobre una DB nueva, vacía.
-- No puede sobreescribir la DB de Hudec actual por accidente.
--
-- Aplicar:
--   createdb -U postgres kawii_mt
--   psql -U postgres -d kawii_mt -f docs/migrations/2026-06-30_multitenant_schema.sql
--
-- Después: la DB nueva tiene una sola empresa precargada (Hudec, id=1) con
-- bsale_token=NULL. El token se carga en la FASE 4 (backend) desde el .env.
-- =====================================================================

BEGIN;

-- ---------------------------------------------------------------------
-- Extensiones
-- ---------------------------------------------------------------------
-- pgcrypto se usará en FASE 4 para cifrar bsale_token con pgp_sym_encrypt.
-- Por ahora el token queda en texto plano (campo nullable).
CREATE EXTENSION IF NOT EXISTS pgcrypto;


-- =====================================================================
-- 0. CORE MULTI-TENANT
-- =====================================================================

CREATE TABLE companies (
    id                    serial PRIMARY KEY,
    name                  varchar(200) NOT NULL,
    slug                  varchar(120) NOT NULL UNIQUE,
    bsale_token           text,                            -- TODO FASE 4: cifrar (bytea + pgp_sym_encrypt)
    brand_name            varchar(200),
    classification_label  varchar(120) DEFAULT 'Clasificación',
    timezone              varchar(60)  NOT NULL DEFAULT 'America/Lima',
    is_active             boolean      NOT NULL DEFAULT true,
    created_at            timestamptz  NOT NULL DEFAULT now()
);


-- =====================================================================
-- 1. APP / INFRAESTRUCTURA
-- =====================================================================

-- app_users es GLOBAL: un usuario puede pertenecer a N empresas vía user_companies.
-- username UNIQUE en TODO el sistema (no por empresa). Como GitHub: un login.
CREATE TABLE app_users (
    id              bigserial PRIMARY KEY,
    username        text NOT NULL UNIQUE,
    password_hash   text NOT NULL,
    is_active       boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    last_login_at   timestamptz
);
CREATE INDEX app_users_username_idx ON app_users (username);

-- Pivote N-a-N: define qué empresas ve cada usuario y con qué rol en cada una.
-- Un mismo usuario puede ser 'admin' en Hudec y 'viewer' en EmpresaB.
CREATE TABLE user_companies (
    user_id     bigint  NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    company_id  integer NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    role        text    NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, company_id),
    CONSTRAINT user_companies_role_check
        CHECK (role IN ('admin', 'operador', 'viewer'))
);
CREATE INDEX idx_user_companies_company ON user_companies (company_id);

-- app_config: ahora PK compuesta. Cada empresa tiene SU propio set de
-- claves: 'exclusions:departments', 'thresholds', 'goals', etc.
CREATE TABLE app_config (
    company_id  integer NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    key         text NOT NULL,
    value       text,
    updated_at  timestamptz DEFAULT now(),
    PRIMARY KEY (company_id, key)
);

CREATE TABLE app_config_history (
    id          bigserial PRIMARY KEY,
    company_id  integer NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    config_key  text NOT NULL,
    value       text,
    changed_at  timestamptz NOT NULL DEFAULT now(),
    label       text,
    source      text,
    is_manual   boolean NOT NULL DEFAULT false
);
CREATE INDEX app_config_history_key_time_idx
    ON app_config_history (company_id, config_key, changed_at DESC);

CREATE TABLE sync_log (
    id                serial PRIMARY KEY,
    company_id        integer NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    entity            varchar(80) NOT NULL,
    status            varchar(20) NOT NULL DEFAULT 'RUNNING',
    params            jsonb,
    records_fetched   integer NOT NULL DEFAULT 0,
    records_inserted  integer NOT NULL DEFAULT 0,
    records_updated   integer NOT NULL DEFAULT 0,
    records_skipped   integer NOT NULL DEFAULT 0,
    error_message     text,
    started_at        timestamptz NOT NULL DEFAULT now(),
    finished_at       timestamptz
);
CREATE INDEX idx_sync_log_company_entity ON sync_log (company_id, entity, started_at);

CREATE TABLE data_quality_issues (
    id           bigserial PRIMARY KEY,
    company_id   integer NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    entity       varchar(80) NOT NULL,
    bsale_id     integer,
    field        varchar(120),
    issue_type   varchar(60),
    description  text,
    raw_value    text,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_dqi_company_entity ON data_quality_issues (company_id, entity, created_at);

-- webhook_events: company_id NULLABLE a propósito. El webhook llega antes
-- de resolver de qué empresa es (BSale no manda company_id explícito).
-- El processor lo resuelve por el token / payload y hace UPDATE.
CREATE TABLE webhook_events (
    id                bigserial PRIMARY KEY,
    company_id        integer REFERENCES companies(id) ON DELETE CASCADE,
    received_at       timestamptz NOT NULL DEFAULT now(),
    source            text NOT NULL DEFAULT 'bsale',
    topic             text,
    action            text,
    resource_id       bigint,
    payload           jsonb NOT NULL,
    headers           jsonb,
    remote_addr       text,
    processed_at      timestamptz,
    processed_status  text,
    process_error     text
);
CREATE INDEX idx_webhook_events_pending
    ON webhook_events (received_at)
    WHERE processed_at IS NULL;
CREATE INDEX idx_webhook_events_topic_action
    ON webhook_events (topic, action, received_at);
CREATE INDEX idx_webhook_events_resource
    ON webhook_events (topic, resource_id)
    WHERE resource_id IS NOT NULL;
CREATE INDEX idx_webhook_events_company ON webhook_events (company_id, received_at);


-- =====================================================================
-- 2. ENTIDADES BASE BSALE (PK compuesta: company_id + bsale_X_id)
-- =====================================================================

CREATE TABLE offices (
    company_id       integer NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    bsale_office_id  integer NOT NULL,
    name             varchar(200) NOT NULL,
    address          text,
    district         varchar(150),
    city             varchar(150),
    country          varchar(100) DEFAULT 'Peru',
    is_virtual       boolean NOT NULL DEFAULT false,
    is_active        boolean NOT NULL DEFAULT true,
    synced_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, bsale_office_id)
);

CREATE TABLE document_types (
    company_id              integer NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    bsale_document_type_id  integer NOT NULL,
    name                    varchar(200) NOT NULL,
    code                    varchar(10),
    is_credit_note          boolean NOT NULL DEFAULT false,
    is_sales_note           boolean NOT NULL DEFAULT false,
    is_electronic           boolean NOT NULL DEFAULT false,
    is_active               boolean NOT NULL DEFAULT true,
    synced_at               timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, bsale_document_type_id)
);

CREATE TABLE users (
    company_id       integer NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    bsale_user_id    integer NOT NULL,
    first_name       varchar(150),
    last_name        varchar(150),
    email            varchar(250),
    bsale_office_id  integer,
    is_active        boolean NOT NULL DEFAULT true,
    synced_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, bsale_user_id),
    FOREIGN KEY (company_id, bsale_office_id)
        REFERENCES offices(company_id, bsale_office_id) ON DELETE SET NULL
);
CREATE INDEX idx_users_office ON users (company_id, bsale_office_id);


-- =====================================================================
-- 3. JERARQUÍA LOCAL (departments / categories / subcategories)
--    PK serial + UNIQUE (company_id, id) para servir como destino de
--    FKs compuestas desde otras tablas.
-- =====================================================================

CREATE TABLE departments (
    id          serial PRIMARY KEY,
    company_id  integer NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    name        varchar(150) NOT NULL,
    slug        varchar(180) NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT departments_company_name_key UNIQUE (company_id, name),
    CONSTRAINT departments_company_slug_key UNIQUE (company_id, slug),
    CONSTRAINT departments_company_id_key   UNIQUE (company_id, id)  -- para FKs compuestas
);
CREATE INDEX idx_departments_company ON departments (company_id);

CREATE TABLE categories (
    id             serial PRIMARY KEY,
    company_id     integer NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    department_id  integer NOT NULL,
    name           varchar(200) NOT NULL,
    slug           varchar(230) NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT categories_company_department_name_key UNIQUE (company_id, department_id, name),
    CONSTRAINT categories_company_id_key UNIQUE (company_id, id),
    FOREIGN KEY (company_id, department_id)
        REFERENCES departments(company_id, id) ON DELETE CASCADE
);
CREATE INDEX idx_categories_company_dept ON categories (company_id, department_id);

CREATE TABLE subcategories (
    id           serial PRIMARY KEY,
    company_id   integer NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    category_id  integer NOT NULL,
    name         varchar(200) NOT NULL,
    slug         varchar(230) NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT subcategories_company_category_name_key UNIQUE (company_id, category_id, name),
    CONSTRAINT subcategories_company_id_key UNIQUE (company_id, id),
    FOREIGN KEY (company_id, category_id)
        REFERENCES categories(company_id, id) ON DELETE CASCADE
);
CREATE INDEX idx_subcategories_company_cat ON subcategories (company_id, category_id);


-- =====================================================================
-- 4. PRODUCTOS / TIPOS / VARIANTES (PK compuesta)
-- =====================================================================

CREATE TABLE product_types (
    company_id             integer NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    bsale_product_type_id  integer NOT NULL,
    name                   varchar(300) NOT NULL,
    subcategory_id         integer,
    is_active              boolean NOT NULL DEFAULT true,
    is_mapped              boolean NOT NULL DEFAULT false,
    synced_at              timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, bsale_product_type_id),
    FOREIGN KEY (company_id, subcategory_id)
        REFERENCES subcategories(company_id, id) ON DELETE SET NULL
);
CREATE INDEX idx_product_types_subcategory ON product_types (company_id, subcategory_id);

CREATE TABLE products (
    company_id             integer NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    bsale_product_id       integer NOT NULL,
    name                   varchar(500) NOT NULL,
    description            text,
    bsale_product_type_id  integer,
    subcategory_id         integer,
    stock_control          boolean NOT NULL DEFAULT true,
    allow_decimal          boolean NOT NULL DEFAULT false,
    is_active              boolean NOT NULL DEFAULT true,
    synced_at              timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, bsale_product_id),
    FOREIGN KEY (company_id, bsale_product_type_id)
        REFERENCES product_types(company_id, bsale_product_type_id) ON DELETE SET NULL,
    FOREIGN KEY (company_id, subcategory_id)
        REFERENCES subcategories(company_id, id) ON DELETE SET NULL
);
CREATE INDEX idx_products_product_type ON products (company_id, bsale_product_type_id);
CREATE INDEX idx_products_subcategory  ON products (company_id, subcategory_id);

CREATE TABLE product_type_attributes (
    company_id             integer NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    bsale_attribute_id     integer NOT NULL,
    bsale_product_type_id  integer NOT NULL,
    name                   varchar(200) NOT NULL,
    synced_at              timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, bsale_attribute_id),
    FOREIGN KEY (company_id, bsale_product_type_id)
        REFERENCES product_types(company_id, bsale_product_type_id) ON DELETE CASCADE
);
CREATE INDEX idx_ptype_attrs_ptype ON product_type_attributes (company_id, bsale_product_type_id);

CREATE TABLE variants (
    company_id            integer NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    bsale_variant_id      integer NOT NULL,
    bsale_product_id      integer,
    code                  varchar(100),
    bar_code              varchar(100),
    display_code          varchar(100) NOT NULL,
    description           text,
    unit                  varchar(50),
    allow_negative_stock  boolean NOT NULL DEFAULT false,
    is_active             boolean NOT NULL DEFAULT true,
    synced_at             timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, bsale_variant_id),
    FOREIGN KEY (company_id, bsale_product_id)
        REFERENCES products(company_id, bsale_product_id) ON DELETE SET NULL
);
CREATE INDEX idx_variants_product  ON variants (company_id, bsale_product_id);
CREATE INDEX idx_variants_code     ON variants (company_id, code);
CREATE INDEX idx_variants_bar_code ON variants (company_id, bar_code);

CREATE TABLE variant_attribute_values (
    company_id          integer NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    bsale_av_id         integer NOT NULL,
    bsale_variant_id    integer NOT NULL,
    bsale_attribute_id  integer NOT NULL,
    description         varchar(500) NOT NULL,
    synced_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, bsale_av_id),
    CONSTRAINT vav_company_variant_attr_key
        UNIQUE (company_id, bsale_variant_id, bsale_attribute_id),
    FOREIGN KEY (company_id, bsale_variant_id)
        REFERENCES variants(company_id, bsale_variant_id) ON DELETE CASCADE,
    FOREIGN KEY (company_id, bsale_attribute_id)
        REFERENCES product_type_attributes(company_id, bsale_attribute_id) ON DELETE CASCADE
);
CREATE INDEX idx_vav_variant ON variant_attribute_values (company_id, bsale_variant_id);
CREATE INDEX idx_vav_attr    ON variant_attribute_values (company_id, bsale_attribute_id);

CREATE TABLE variant_costs (
    company_id        integer NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    bsale_variant_id  integer NOT NULL,
    average_cost      numeric(20,4) NOT NULL DEFAULT 0,
    latest_cost       numeric(20,4) NOT NULL DEFAULT 0,
    cost_source       varchar(20)   NOT NULL DEFAULT 'NONE',
    effective_cost    numeric(20,4) NOT NULL DEFAULT 0,
    synced_at         timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, bsale_variant_id),
    FOREIGN KEY (company_id, bsale_variant_id)
        REFERENCES variants(company_id, bsale_variant_id) ON DELETE CASCADE
);


-- =====================================================================
-- 5. METAS POR CATEGORÍA / OFICINA
-- =====================================================================

CREATE TABLE category_targets (
    company_id           integer NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    category_id          integer NOT NULL,
    bsale_office_id      integer NOT NULL,
    rol                  varchar(20),
    meta_mensual_pen     numeric(12,2),
    pvp_min              numeric(8,2),
    pvp_max              numeric(8,2),
    margen_objetivo_pct  numeric(5,2),
    skus_min             integer,
    skus_max             integer,
    nota                 text,
    PRIMARY KEY (company_id, category_id, bsale_office_id),
    FOREIGN KEY (company_id, category_id)
        REFERENCES categories(company_id, id) ON DELETE CASCADE,
    FOREIGN KEY (company_id, bsale_office_id)
        REFERENCES offices(company_id, bsale_office_id) ON DELETE CASCADE
);


-- =====================================================================
-- 6. DOCUMENTOS DE VENTA
-- =====================================================================

CREATE TABLE documents (
    company_id              integer NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    bsale_document_id       integer NOT NULL,
    bsale_document_type_id  integer NOT NULL,
    bsale_office_id         integer,
    bsale_user_id           integer,
    emission_date           timestamptz NOT NULL,
    generation_date         timestamptz,
    serial_number           varchar(50),
    doc_number              integer,
    total_amount            numeric(20,2) NOT NULL DEFAULT 0,
    net_amount              numeric(20,2) NOT NULL DEFAULT 0,
    tax_amount              numeric(20,2) NOT NULL DEFAULT 0,
    exempt_amount           numeric(20,2) NOT NULL DEFAULT 0,
    is_credit_note          boolean NOT NULL DEFAULT false,
    is_active               boolean NOT NULL DEFAULT true,
    token                   varchar(60),
    synced_at               timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, bsale_document_id),
    FOREIGN KEY (company_id, bsale_document_type_id)
        REFERENCES document_types(company_id, bsale_document_type_id),
    FOREIGN KEY (company_id, bsale_office_id)
        REFERENCES offices(company_id, bsale_office_id),
    FOREIGN KEY (company_id, bsale_user_id)
        REFERENCES users(company_id, bsale_user_id) ON DELETE SET NULL
);
CREATE INDEX idx_documents_doctype  ON documents (company_id, bsale_document_type_id);
CREATE INDEX idx_documents_office   ON documents (company_id, bsale_office_id);
CREATE INDEX idx_documents_emission ON documents (company_id, emission_date);
CREATE INDEX idx_documents_credit   ON documents (company_id, is_credit_note);
CREATE INDEX idx_documents_user     ON documents (company_id, bsale_user_id);

-- document_details: SIN FK a variants (variantes-fantasma históricas, ver v3).
CREATE TABLE document_details (
    company_id            integer NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    bsale_detail_id       integer NOT NULL,
    bsale_document_id     integer NOT NULL,
    bsale_variant_id      integer NOT NULL,
    quantity              numeric(20,4) NOT NULL DEFAULT 0,
    net_unit_value        numeric(20,4) NOT NULL DEFAULT 0,
    net_unit_value_raw    numeric(20,4) NOT NULL DEFAULT 0,
    total_unit_value      numeric(20,4) NOT NULL DEFAULT 0,
    net_amount            numeric(20,2) NOT NULL DEFAULT 0,
    tax_amount            numeric(20,2) NOT NULL DEFAULT 0,
    total_amount          numeric(20,2) NOT NULL DEFAULT 0,
    discount_percentage   numeric(6,2)  NOT NULL DEFAULT 0,
    net_discount          numeric(20,2) NOT NULL DEFAULT 0,
    is_gratuity           boolean NOT NULL DEFAULT false,
    synced_at             timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, bsale_detail_id),
    FOREIGN KEY (company_id, bsale_document_id)
        REFERENCES documents(company_id, bsale_document_id) ON DELETE CASCADE
);
CREATE INDEX idx_doc_details_document ON document_details (company_id, bsale_document_id);
CREATE INDEX idx_doc_details_variant  ON document_details (company_id, bsale_variant_id);


-- =====================================================================
-- 7. RECEPCIONES
-- =====================================================================

CREATE TABLE receptions (
    company_id            integer NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    bsale_reception_id    integer NOT NULL,
    bsale_office_id       integer NOT NULL,
    bsale_user_id         integer,
    admission_date        timestamptz NOT NULL,
    admission_date_raw    varchar(30),
    document_ref          varchar(150),
    document_number       varchar(100),
    note                  text,
    is_internal_dispatch  boolean NOT NULL DEFAULT false,
    is_transfer           boolean NOT NULL DEFAULT false,
    synced_at             timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, bsale_reception_id),
    FOREIGN KEY (company_id, bsale_office_id)
        REFERENCES offices(company_id, bsale_office_id) ON DELETE CASCADE,
    FOREIGN KEY (company_id, bsale_user_id)
        REFERENCES users(company_id, bsale_user_id) ON DELETE SET NULL
);
CREATE INDEX idx_receptions_office ON receptions (company_id, bsale_office_id);
CREATE INDEX idx_receptions_date   ON receptions (company_id, admission_date);
CREATE INDEX idx_receptions_user   ON receptions (company_id, bsale_user_id);

CREATE TABLE reception_details (
    company_id                 integer NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    bsale_reception_detail_id  integer NOT NULL,
    bsale_reception_id         integer NOT NULL,
    bsale_variant_id           integer NOT NULL,
    quantity                   numeric(20,4) NOT NULL DEFAULT 0,
    cost                       numeric(20,4) NOT NULL DEFAULT 0,
    synced_at                  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, bsale_reception_detail_id),
    FOREIGN KEY (company_id, bsale_reception_id)
        REFERENCES receptions(company_id, bsale_reception_id) ON DELETE CASCADE
);
CREATE INDEX idx_reception_details_reception ON reception_details (company_id, bsale_reception_id);
CREATE INDEX idx_reception_details_variant   ON reception_details (company_id, bsale_variant_id);


-- =====================================================================
-- 8. CONSUMOS
-- =====================================================================

CREATE TABLE consumptions (
    company_id            integer NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    bsale_consumption_id  integer NOT NULL,
    bsale_office_id       integer NOT NULL,
    consumption_date      timestamptz NOT NULL,
    note                  varchar(255),
    created_at            timestamptz DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (company_id, bsale_consumption_id),
    FOREIGN KEY (company_id, bsale_office_id)
        REFERENCES offices(company_id, bsale_office_id) ON DELETE CASCADE
);
CREATE INDEX idx_consumptions_office ON consumptions (company_id, bsale_office_id);
CREATE INDEX idx_consumptions_date   ON consumptions (company_id, consumption_date);

CREATE TABLE consumption_details (
    company_id                    integer NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    bsale_consumption_detail_id   integer NOT NULL,
    bsale_consumption_id          integer NOT NULL,
    bsale_variant_id              integer NOT NULL,
    quantity                      numeric NOT NULL,
    created_at                    timestamptz DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (company_id, bsale_consumption_detail_id),
    FOREIGN KEY (company_id, bsale_consumption_id)
        REFERENCES consumptions(company_id, bsale_consumption_id) ON DELETE CASCADE
);
CREATE INDEX idx_consumption_details_cons ON consumption_details (company_id, bsale_consumption_id);
CREATE INDEX idx_consumption_details_var  ON consumption_details (company_id, bsale_variant_id);


-- =====================================================================
-- 9. STOCK
-- =====================================================================

CREATE TABLE stock_levels (
    company_id          integer NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    bsale_stock_id      integer NOT NULL,
    bsale_variant_id    integer NOT NULL,
    bsale_office_id     integer NOT NULL,
    quantity            numeric(20,4) NOT NULL DEFAULT 0,
    quantity_reserved   numeric(20,4) NOT NULL DEFAULT 0,
    quantity_available  numeric(20,4) NOT NULL DEFAULT 0,
    synced_at           timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, bsale_stock_id),
    CONSTRAINT stock_levels_variant_office_key
        UNIQUE (company_id, bsale_variant_id, bsale_office_id),
    FOREIGN KEY (company_id, bsale_variant_id)
        REFERENCES variants(company_id, bsale_variant_id) ON DELETE CASCADE,
    FOREIGN KEY (company_id, bsale_office_id)
        REFERENCES offices(company_id, bsale_office_id) ON DELETE CASCADE
);
CREATE INDEX idx_stock_levels_variant ON stock_levels (company_id, bsale_variant_id);
CREATE INDEX idx_stock_levels_office  ON stock_levels (company_id, bsale_office_id);

CREATE TABLE stock_history (
    id                  bigserial PRIMARY KEY,
    company_id          integer NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    snapshot_date       date NOT NULL,
    bsale_variant_id    integer NOT NULL,
    bsale_office_id     integer NOT NULL,
    quantity            numeric(20,4) NOT NULL DEFAULT 0,
    quantity_reserved   numeric(20,4) NOT NULL DEFAULT 0,
    quantity_available  numeric(20,4) NOT NULL DEFAULT 0,
    created_at          timestamptz   NOT NULL DEFAULT now(),
    CONSTRAINT stock_history_snapshot_variant_office_key
        UNIQUE (company_id, snapshot_date, bsale_variant_id, bsale_office_id),
    FOREIGN KEY (company_id, bsale_variant_id)
        REFERENCES variants(company_id, bsale_variant_id) ON DELETE CASCADE,
    FOREIGN KEY (company_id, bsale_office_id)
        REFERENCES offices(company_id, bsale_office_id) ON DELETE CASCADE
);
CREATE INDEX idx_stock_history_date    ON stock_history (company_id, snapshot_date);
CREATE INDEX idx_stock_history_variant ON stock_history (company_id, bsale_variant_id);
CREATE INDEX idx_stock_history_office  ON stock_history (company_id, bsale_office_id);


-- =====================================================================
-- 10. DECISIONES DE COMPRA
-- =====================================================================

CREATE TABLE purchase_decisions (
    id                       bigserial PRIMARY KEY,
    company_id               integer NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    bsale_variant_id         integer NOT NULL,
    bsale_office_id          integer NOT NULL,
    decision                 text NOT NULL,
    quantity                 integer,
    notes                    text,
    classification_snapshot  jsonb,
    created_at               timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT purchase_decisions_decision_check
        CHECK (decision IN ('ordenar', 'comprar_similar', 'posponer', 'ignorar')),
    FOREIGN KEY (company_id, bsale_variant_id)
        REFERENCES variants(company_id, bsale_variant_id) ON DELETE CASCADE,
    FOREIGN KEY (company_id, bsale_office_id)
        REFERENCES offices(company_id, bsale_office_id) ON DELETE CASCADE
);
CREATE INDEX purchase_decisions_variant_office_idx
    ON purchase_decisions (company_id, bsale_variant_id, bsale_office_id, created_at DESC);


-- =====================================================================
-- 11. VISTAS (siguen funcionando — los JOINs implícitos llevan company_id
--     porque las PKs y FKs son compuestas)
-- =====================================================================

CREATE VIEW v_product_types_full AS
SELECT
    pt.company_id,
    pt.bsale_product_type_id,
    pt.name      AS product_type_name,
    pt.is_active,
    pt.is_mapped,
    s.id         AS subcategory_id,
    s.name       AS subcategory,
    c.id         AS category_id,
    c.name       AS category,
    d.id         AS department_id,
    d.name       AS department
FROM product_types pt
LEFT JOIN subcategories s ON s.company_id = pt.company_id AND s.id = pt.subcategory_id
LEFT JOIN categories    c ON c.company_id = s.company_id  AND c.id = s.category_id
LEFT JOIN departments   d ON d.company_id = c.company_id  AND d.id = c.department_id;

CREATE VIEW v_products_full AS
SELECT
    p.company_id,
    p.bsale_product_id,
    p.name                          AS product_name,
    p.is_active,
    pt.bsale_product_type_id,
    pt.name                         AS product_type_name,
    pt.is_mapped,
    p.subcategory_id IS NOT NULL    AS has_override,
    s.name                          AS subcategory,
    c.name                          AS category,
    d.name                          AS department
FROM products p
LEFT JOIN product_types pt
       ON pt.company_id = p.company_id AND pt.bsale_product_type_id = p.bsale_product_type_id
LEFT JOIN subcategories s
       ON s.company_id = p.company_id AND s.id = COALESCE(p.subcategory_id, pt.subcategory_id)
LEFT JOIN categories c
       ON c.company_id = s.company_id AND c.id = s.category_id
LEFT JOIN departments d
       ON d.company_id = c.company_id AND d.id = c.department_id;


-- =====================================================================
-- 12. SEED INICIAL: Hudec como company_id = 1
-- =====================================================================
-- bsale_token queda NULL — se carga en FASE 4 desde el .env / vault.
-- Los IDs operativos (OFFICES_TIENDA, TIPOS_VENTA, etc.) se sembrarán en
-- app_config como parte de la FASE 2 del backend.
INSERT INTO companies (id, name, slug, bsale_token, brand_name, classification_label, timezone)
VALUES (1, 'Hudec', 'hudec', NULL, 'Hudec', 'Clasificación', 'America/Lima');

-- Reseteo de secuencia para que la próxima empresa sea id=2.
SELECT setval('companies_id_seq', (SELECT MAX(id) FROM companies), true);


COMMIT;

-- =====================================================================
-- VERIFICACIÓN POST-INSTALACIÓN (correr aparte si quieres):
--   SELECT count(*) AS tablas FROM information_schema.tables
--   WHERE table_schema = 'public' AND table_type = 'BASE TABLE';
--   -- Debe dar 27 (companies, user_companies, app_users, app_config,
--   --            app_config_history, sync_log, data_quality_issues,
--   --            webhook_events, offices, document_types, users,
--   --            departments, categories, subcategories, product_types,
--   --            products, product_type_attributes, variants,
--   --            variant_attribute_values, variant_costs, category_targets,
--   --            documents, document_details, receptions, reception_details,
--   --            consumptions, consumption_details, stock_levels,
--   --            stock_history, purchase_decisions) = 30 tablas
--
--   SELECT * FROM companies;  -- debe mostrar Hudec, id=1
-- =====================================================================
