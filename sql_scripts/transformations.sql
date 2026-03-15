SELECT
    s.belnr                              AS invoice_id,
    s.sap_vendor_id                      AS vendor_id,
    v.vendor_name                        AS vendor_name,
    v.country_code                       AS country_code,
    v.payment_terms                      AS payment_terms,
    s.payment_status_raw                 AS payment_status,
    s.waers                              AS currency,
    s.wrbtr                              AS gross_amount,
    s.mwskz                              AS tax_code,
    t.tax_rate                           AS tax_rate,
    ROUND(s.wrbtr * (1 - t.tax_rate), 2) AS net_amount,
    s.bldat                              AS posting_date,
    s.zfbdt                              AS payment_date,
    s.zlsch                              AS payment_method
FROM stg_sap_input s
LEFT JOIN stg_vendor_ref v ON s.sap_vendor_id = v.sap_vendor_id
LEFT JOIN stg_tax_rates  t ON s.mwskz = t.tax_code
WHERE s.payment_status_raw IN ('PAID', 'PENDING')
LIMIT 50;
