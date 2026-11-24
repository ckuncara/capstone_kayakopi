-- Apply once to an existing kayakopi database before enabling OCR invoice sync.
ALTER TABLE supplier_invoice
    MODIFY supplier_address VARCHAR(255) NULL,
    MODIFY gst_registration_number VARCHAR(30) NULL,
    MODIFY invoice_date DATE NULL,
    MODIFY bill_to VARCHAR(255) NULL,
    ADD COLUMN currency CHAR(3) NOT NULL DEFAULT 'SGD',
    ADD COLUMN is_gst_inclusive BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE ocr_invoice_sync (
    upload_id CHAR(32) PRIMARY KEY,
    supplier_invoice_id BIGINT NOT NULL UNIQUE,
    created_invoice BOOLEAN NOT NULL,
    CONSTRAINT fk_ocr_sync_invoice FOREIGN KEY (supplier_invoice_id)
        REFERENCES supplier_invoice(supplier_invoice_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE ocr_invoice_source (
    upload_id CHAR(32) NOT NULL,
    source_document_id BIGINT NOT NULL UNIQUE,
    PRIMARY KEY (upload_id, source_document_id),
    CONSTRAINT fk_ocr_source_sync FOREIGN KEY (upload_id) REFERENCES ocr_invoice_sync(upload_id),
    CONSTRAINT fk_ocr_source_document FOREIGN KEY (source_document_id)
        REFERENCES source_document(source_document_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
