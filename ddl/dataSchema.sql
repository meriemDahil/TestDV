-- ddl/schema.sql
-- Mock data model for Talend-to-SQL migration validation
CREATE TABLE stg_tax_rates (
    tax_code    TEXT,
    tax_rate    REAL
);
CREATE TABLE stg_vendor_ref (
    sap_vendor_id   TEXT,
    vendor_name     TEXT,
    country_code    TEXT,
    payment_terms   TEXT
);
CREATE TABLE stg_sap_input (
    bukrs               TEXT,
    belnr               TEXT,
    sap_vendor_id       TEXT,
    wrbtr               REAL,
    mwskz               TEXT,
    bldat               TEXT,
    zfbdt               TEXT,
    zlsch               TEXT,
    augdt               TEXT,
    payment_status_raw  TEXT,
    waers               TEXT
);
