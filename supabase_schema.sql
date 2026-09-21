-- =============================================================================
-- PainEasy Clinic Management — Supabase schema
-- Generated from SQLAlchemy models.
--
-- HOW TO RUN:
--   1. Open your Supabase project → SQL Editor → New query
--   2. Paste this entire file and click Run
--   3. You should see "Success. No rows returned."
--
-- After running, update backend/.env:
--   CLINIC_DATABASE_URL=postgresql+psycopg://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres
-- =============================================================================


-- ---------------------------------------------------------------------------
-- ENUMS
-- ---------------------------------------------------------------------------
CREATE TYPE appointmentstatus  AS ENUM ('BOOKED','CONFIRMED','CHECKED_IN','COMPLETED','CANCELLED','RESCHEDULED','NO_SHOW');
CREATE TYPE appointmentaction  AS ENUM ('CREATED','CONFIRMED','CHECKED_IN','COMPLETED','CANCELLED','RESCHEDULED','NO_SHOW','UPDATED');
CREATE TYPE auditaction        AS ENUM ('LOGIN','LOGIN_FAILED','LOGOUT','PASSWORD_CHANGED','CREATED','UPDATED','ENABLED','DISABLED','DELETED','APPOINTMENT_CREATED','APPOINTMENT_RESCHEDULED','APPOINTMENT_CANCELLED','SESSION_RECORDED','BILL_GENERATED','PAYMENT_RECORDED','NOTIFICATION_ACKNOWLEDGED');
CREATE TYPE billitemtype       AS ENUM ('SESSION_PACKAGE','CONSULTATION','PRODUCT','OTHER');
CREATE TYPE billstatus         AS ENUM ('DRAFT','FINALIZED','CANCELLED');
CREATE TYPE clinicstatus       AS ENUM ('ACTIVE','INACTIVE');
CREATE TYPE gender             AS ENUM ('MALE','FEMALE','OTHER','UNDISCLOSED');
CREATE TYPE messagechannel     AS ENUM ('EMAIL','WHATSAPP','SMS','IN_APP');
CREATE TYPE messagestatus      AS ENUM ('QUEUED','SENT','FAILED','SKIPPED');
CREATE TYPE notificationtype   AS ENUM ('NEW_APPOINTMENT','RESCHEDULED_APPOINTMENT','CANCELLED_APPOINTMENT','APPOINTMENT_REMINDER','BILL_GENERATED','SYSTEM');
CREATE TYPE packagestatus      AS ENUM ('ACTIVE','COMPLETED','CANCELLED');
CREATE TYPE paymentmethod      AS ENUM ('CASH','UPI','CARD','BANK_TRANSFER','OTHER');
CREATE TYPE paymentstatus      AS ENUM ('UNPAID','PARTIAL','PAID');
CREATE TYPE rolename           AS ENUM ('SUPERADMIN','ADMIN','CLINIC_USER');
CREATE TYPE adjustmenttype     AS ENUM ('CREDIT_NOTE','CANCELLATION','CORRECTION');


-- ---------------------------------------------------------------------------
-- TABLES  (in dependency order)
-- ---------------------------------------------------------------------------

CREATE TABLE clinics (
    id                      SERIAL PRIMARY KEY,
    name                    VARCHAR(150) NOT NULL,
    code                    VARCHAR(20)  NOT NULL,
    address                 VARCHAR(500),
    location                VARCHAR(150),
    city                    VARCHAR(100),
    state                   VARCHAR(100),
    pin_code                VARCHAR(12),
    phone                   VARCHAR(20),
    email                   VARCHAR(255),
    status                  VARCHAR(40)  NOT NULL,
    slot_duration_minutes   INTEGER      NOT NULL,
    capacity_per_slot       INTEGER      NOT NULL,
    brand_name              VARCHAR(120),
    brand_tagline           VARCHAR(160),
    logo_filename           VARCHAR(120),
    document_footer         TEXT,
    bill_number_prefix      VARCHAR(10),
    created_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT ck_clinics_slot_duration_positive CHECK (slot_duration_minutes > 0),
    CONSTRAINT ck_clinics_capacity_positive      CHECK (capacity_per_slot > 0)
);
CREATE UNIQUE INDEX ix_clinics_name   ON clinics (name);
CREATE UNIQUE INDEX ix_clinics_code   ON clinics (code);
CREATE        INDEX ix_clinics_status ON clinics (status);


CREATE TABLE outbound_messages (
    id                    SERIAL PRIMARY KEY,
    channel               VARCHAR(40)  NOT NULL,
    provider              VARCHAR(60)  NOT NULL,
    recipient             VARCHAR(255) NOT NULL,
    subject               VARCHAR(255),
    body                  TEXT,
    template_key          VARCHAR(80),
    status                VARCHAR(40)  NOT NULL,
    error_message         TEXT,
    provider_message_id   VARCHAR(120),
    sent_at               TIMESTAMPTZ,
    related_entity_type   VARCHAR(60),
    related_entity_id     INTEGER,
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX ix_outbound_channel_status ON outbound_messages (channel, status);


CREATE TABLE patient_sources (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(80) NOT NULL UNIQUE,
    is_active   BOOLEAN     NOT NULL,
    sort_order  INTEGER     NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);


CREATE TABLE roles (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(40)  NOT NULL,
    description VARCHAR(255),
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ix_roles_name ON roles (name);


CREATE TABLE clinic_breaks (
    id           SERIAL PRIMARY KEY,
    clinic_id    INTEGER      NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    day_of_week  INTEGER,
    start_time   TIME         NOT NULL,
    end_time     TIME         NOT NULL,
    label        VARCHAR(50)  NOT NULL,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT ck_clinic_breaks_dow CHECK (day_of_week IS NULL OR (day_of_week >= 0 AND day_of_week <= 6))
);
CREATE INDEX ix_clinic_breaks_clinic_day ON clinic_breaks (clinic_id, day_of_week);


CREATE TABLE clinic_holidays (
    id            SERIAL PRIMARY KEY,
    clinic_id     INTEGER REFERENCES clinics (id) ON DELETE CASCADE,
    holiday_date  DATE         NOT NULL,
    reason        VARCHAR(255),
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_clinic_holiday_date UNIQUE (clinic_id, holiday_date)
);
CREATE INDEX ix_clinic_holidays_date ON clinic_holidays (holiday_date);


CREATE TABLE clinic_working_hours (
    id           SERIAL PRIMARY KEY,
    clinic_id    INTEGER      NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    day_of_week  INTEGER      NOT NULL,
    open_time    TIME         NOT NULL,
    close_time   TIME         NOT NULL,
    is_closed    BOOLEAN      NOT NULL,
    label        VARCHAR(50),
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT ck_working_hours_dow CHECK (day_of_week >= 0 AND day_of_week <= 6)
);
CREATE INDEX ix_working_hours_clinic_day ON clinic_working_hours (clinic_id, day_of_week);


CREATE TABLE service_items (
    id             SERIAL PRIMARY KEY,
    clinic_id      INTEGER REFERENCES clinics (id) ON DELETE CASCADE,
    name           VARCHAR(120)   NOT NULL,
    item_type      VARCHAR(40)    NOT NULL,
    default_price  NUMERIC(12,2)  NOT NULL,
    description    TEXT,
    is_active      BOOLEAN        NOT NULL,
    sort_order     INTEGER        NOT NULL,
    created_at     TIMESTAMPTZ    NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ    NOT NULL DEFAULT now(),
    CONSTRAINT uq_service_items_clinic_name    UNIQUE (clinic_id, name),
    CONSTRAINT ck_service_items_price_non_negative CHECK (default_price >= 0)
);
CREATE INDEX ix_service_items_clinic_id ON service_items (clinic_id);
CREATE INDEX ix_service_items_name      ON service_items (name);


CREATE TABLE users (
    id                  SERIAL PRIMARY KEY,
    username            VARCHAR(64)  NOT NULL,
    email               VARCHAR(255),
    full_name           VARCHAR(150) NOT NULL,
    phone               VARCHAR(20),
    employee_id         VARCHAR(40),
    registration_number VARCHAR(60),
    hashed_password     VARCHAR(255) NOT NULL,
    role_id             INTEGER      NOT NULL REFERENCES roles (id) ON DELETE RESTRICT,
    is_active           BOOLEAN      NOT NULL,
    last_login_at       TIMESTAMPTZ,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ix_users_username    ON users (username);
CREATE UNIQUE INDEX ix_users_email       ON users (email);
CREATE UNIQUE INDEX ix_users_employee_id ON users (employee_id);
CREATE        INDEX ix_users_role_id     ON users (role_id);


CREATE TABLE audit_logs (
    id           SERIAL PRIMARY KEY,
    user_id      INTEGER REFERENCES users (id) ON DELETE SET NULL,
    username     VARCHAR(64),
    action       VARCHAR(40)  NOT NULL,
    entity_type  VARCHAR(60),
    entity_id    INTEGER,
    clinic_id    INTEGER REFERENCES clinics (id) ON DELETE SET NULL,
    description  TEXT,
    details      JSON,
    ip_address   VARCHAR(45),
    user_agent   VARCHAR(255),
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX ix_audit_logs_entity      ON audit_logs (entity_type, entity_id);
CREATE INDEX ix_audit_logs_action      ON audit_logs (action);
CREATE INDEX ix_audit_logs_user_created ON audit_logs (user_id, created_at);
CREATE INDEX ix_audit_logs_created_at  ON audit_logs (created_at);


CREATE TABLE clinic_users (
    id                  SERIAL PRIMARY KEY,
    user_id             INTEGER      NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    clinic_id           INTEGER      NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    is_primary          BOOLEAN      NOT NULL,
    designation         VARCHAR(100),
    assigned_by_user_id INTEGER REFERENCES users (id) ON DELETE SET NULL,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_clinic_users_user_clinic UNIQUE (user_id, clinic_id)
);
CREATE INDEX ix_clinic_users_user_id   ON clinic_users (user_id);
CREATE INDEX ix_clinic_users_clinic_id ON clinic_users (clinic_id);


CREATE TABLE patients (
    id                   SERIAL PRIMARY KEY,
    patient_code         VARCHAR(20)  NOT NULL,
    full_name            VARCHAR(150) NOT NULL,
    mobile               VARCHAR(20)  NOT NULL,
    whatsapp_number      VARCHAR(20),
    email                VARCHAR(255),
    age                  INTEGER,
    date_of_birth        DATE,
    gender               VARCHAR(40),
    address              VARCHAR(500),
    chief_complaint      TEXT,
    diagnosis            TEXT,
    source_id            INTEGER REFERENCES patient_sources (id) ON DELETE SET NULL,
    source_detail        VARCHAR(255),
    registration_date    DATE,
    primary_clinic_id    INTEGER REFERENCES clinics (id) ON DELETE SET NULL,
    created_by_user_id   INTEGER REFERENCES users (id) ON DELETE SET NULL,
    is_profile_complete  BOOLEAN      NOT NULL,
    is_active            BOOLEAN      NOT NULL,
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT ck_patients_age_range CHECK (age IS NULL OR (age >= 0 AND age <= 130))
);
CREATE UNIQUE INDEX ix_patients_patient_code      ON patients (patient_code);
CREATE        INDEX ix_patients_mobile            ON patients (mobile);
CREATE        INDEX ix_patients_whatsapp_number   ON patients (whatsapp_number);
CREATE        INDEX ix_patients_full_name         ON patients (full_name);
CREATE        INDEX ix_patients_name_mobile       ON patients (full_name, mobile);
CREATE        INDEX ix_patients_source_id         ON patients (source_id);
CREATE        INDEX ix_patients_primary_clinic_id ON patients (primary_clinic_id);


CREATE TABLE treatment_packages (
    id                    SERIAL PRIMARY KEY,
    patient_id            INTEGER        NOT NULL REFERENCES patients (id) ON DELETE CASCADE,
    clinic_id             INTEGER        NOT NULL REFERENCES clinics (id) ON DELETE RESTRICT,
    package_name          VARCHAR(120),
    sessions_registered   INTEGER        NOT NULL,
    sessions_taken        INTEGER        NOT NULL,
    price_per_session     NUMERIC(12,2)  NOT NULL,
    status                VARCHAR(40)    NOT NULL,
    start_date            DATE,
    end_date              DATE,
    notes                 TEXT,
    created_by_user_id    INTEGER REFERENCES users (id) ON DELETE SET NULL,
    created_at            TIMESTAMPTZ    NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ    NOT NULL DEFAULT now(),
    CONSTRAINT ck_package_registered_positive     CHECK (sessions_registered > 0),
    CONSTRAINT ck_package_taken_non_negative      CHECK (sessions_taken >= 0),
    CONSTRAINT ck_package_taken_within_registered CHECK (sessions_taken <= sessions_registered)
);
CREATE INDEX ix_treatment_packages_clinic_id ON treatment_packages (clinic_id);
CREATE INDEX ix_packages_patient_status      ON treatment_packages (patient_id, status);


CREATE TABLE appointments (
    id                   SERIAL PRIMARY KEY,
    appointment_code     VARCHAR(24)  NOT NULL,
    patient_id           INTEGER      NOT NULL REFERENCES patients (id) ON DELETE RESTRICT,
    clinic_id            INTEGER      NOT NULL REFERENCES clinics (id) ON DELETE RESTRICT,
    appointment_date     DATE         NOT NULL,
    start_time           TIME         NOT NULL,
    end_time             TIME         NOT NULL,
    duration_minutes     INTEGER      NOT NULL,
    status               VARCHAR(40)  NOT NULL,
    chief_complaint      TEXT,
    notes                TEXT,
    package_id           INTEGER REFERENCES treatment_packages (id) ON DELETE SET NULL,
    rescheduled_from_id  INTEGER REFERENCES appointments (id) ON DELETE SET NULL,
    created_by_user_id   INTEGER REFERENCES users (id) ON DELETE SET NULL,
    confirmed_at         TIMESTAMPTZ,
    checked_in_at        TIMESTAMPTZ,
    completed_at         TIMESTAMPTZ,
    cancelled_at         TIMESTAMPTZ,
    cancellation_reason  VARCHAR(500),
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ix_appointments_appointment_code ON appointments (appointment_code);
CREATE        INDEX ix_appointments_clinic_date_time ON appointments (clinic_id, appointment_date, start_time);
CREATE        INDEX ix_appointments_patient_date     ON appointments (patient_id, appointment_date);
CREATE        INDEX ix_appointments_status_date      ON appointments (status, appointment_date);


CREATE TABLE bills (
    id                       SERIAL PRIMARY KEY,
    bill_number              VARCHAR(30)    NOT NULL,
    clinic_id                INTEGER        NOT NULL REFERENCES clinics (id) ON DELETE RESTRICT,
    patient_id               INTEGER        NOT NULL REFERENCES patients (id) ON DELETE RESTRICT,
    package_id               INTEGER REFERENCES treatment_packages (id) ON DELETE SET NULL,
    bill_date                DATE           NOT NULL,
    sessions_purchased       INTEGER,
    sessions_taken_snapshot  INTEGER,
    price_per_session        NUMERIC(12,2),
    subtotal_amount          NUMERIC(12,2)  NOT NULL,
    discount_amount          NUMERIC(12,2)  NOT NULL,
    tax_amount               NUMERIC(12,2)  NOT NULL,
    total_amount             NUMERIC(12,2)  NOT NULL,
    amount_paid              NUMERIC(12,2)  NOT NULL,
    status                   VARCHAR(40)    NOT NULL,
    payment_status           VARCHAR(40)    NOT NULL,
    notes                    TEXT,
    created_by_user_id       INTEGER REFERENCES users (id) ON DELETE SET NULL,
    created_at               TIMESTAMPTZ    NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ    NOT NULL DEFAULT now(),
    CONSTRAINT ck_bills_total_non_negative CHECK (total_amount >= 0),
    CONSTRAINT ck_bills_paid_non_negative  CHECK (amount_paid >= 0)
);
CREATE UNIQUE INDEX ix_bills_bill_number   ON bills (bill_number);
CREATE        INDEX ix_bills_patient       ON bills (patient_id);
CREATE        INDEX ix_bills_clinic_date   ON bills (clinic_id, bill_date);
CREATE        INDEX ix_bills_payment_status ON bills (payment_status);


CREATE TABLE appointment_history (
    id                   SERIAL PRIMARY KEY,
    appointment_id       INTEGER      NOT NULL REFERENCES appointments (id) ON DELETE CASCADE,
    action               VARCHAR(40)  NOT NULL,
    old_date             DATE,
    old_time             TIME,
    new_date             DATE,
    new_time             TIME,
    old_status           VARCHAR(40),
    new_status           VARCHAR(40),
    old_clinic_id        INTEGER REFERENCES clinics (id) ON DELETE SET NULL,
    new_clinic_id        INTEGER REFERENCES clinics (id) ON DELETE SET NULL,
    reason               VARCHAR(500),
    changed_by_user_id   INTEGER REFERENCES users (id) ON DELETE SET NULL,
    changed_at           TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX ix_appointment_history_appointment ON appointment_history (appointment_id, changed_at);


CREATE TABLE bill_adjustments (
    id                  SERIAL PRIMARY KEY,
    bill_id             INTEGER        NOT NULL REFERENCES bills (id) ON DELETE CASCADE,
    adjustment_type     VARCHAR(40)    NOT NULL,
    amount              NUMERIC(12,2)  NOT NULL,
    reason              VARCHAR(500)   NOT NULL,
    created_by_user_id  INTEGER REFERENCES users (id) ON DELETE SET NULL,
    created_at          TIMESTAMPTZ    NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ    NOT NULL DEFAULT now()
);
CREATE INDEX ix_bill_adjustments_bill_id ON bill_adjustments (bill_id);


CREATE TABLE bill_items (
    id               SERIAL PRIMARY KEY,
    bill_id          INTEGER        NOT NULL REFERENCES bills (id) ON DELETE CASCADE,
    item_type        VARCHAR(40)    NOT NULL,
    service_item_id  INTEGER REFERENCES service_items (id) ON DELETE SET NULL,
    description      VARCHAR(255)   NOT NULL,
    quantity         INTEGER        NOT NULL,
    unit_price       NUMERIC(12,2)  NOT NULL,
    amount           NUMERIC(12,2)  NOT NULL,
    created_at       TIMESTAMPTZ    NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ    NOT NULL DEFAULT now(),
    CONSTRAINT ck_bill_items_quantity_positive    CHECK (quantity > 0),
    CONSTRAINT ck_bill_items_price_non_negative   CHECK (unit_price >= 0)
);
CREATE INDEX ix_bill_items_bill_id ON bill_items (bill_id);


CREATE TABLE notifications (
    id                    SERIAL PRIMARY KEY,
    clinic_id             INTEGER REFERENCES clinics (id) ON DELETE CASCADE,
    target_user_id        INTEGER REFERENCES users (id) ON DELETE CASCADE,
    notification_type     VARCHAR(40)  NOT NULL,
    title                 VARCHAR(200) NOT NULL,
    message               TEXT         NOT NULL,
    payload               JSON,
    appointment_id        INTEGER REFERENCES appointments (id) ON DELETE SET NULL,
    patient_id            INTEGER REFERENCES patients (id) ON DELETE SET NULL,
    is_acknowledged       BOOLEAN      NOT NULL,
    acknowledged_at       TIMESTAMPTZ,
    requires_sound_alert  BOOLEAN      NOT NULL,
    created_by_user_id    INTEGER REFERENCES users (id) ON DELETE SET NULL,
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX ix_notifications_clinic_id      ON notifications (clinic_id);
CREATE INDEX ix_notifications_clinic_ack     ON notifications (clinic_id, is_acknowledged);
CREATE INDEX ix_notifications_is_acknowledged ON notifications (is_acknowledged);
CREATE INDEX ix_notifications_created        ON notifications (created_at);


CREATE TABLE patient_sessions (
    id                  SERIAL PRIMARY KEY,
    patient_id          INTEGER      NOT NULL REFERENCES patients (id) ON DELETE CASCADE,
    package_id          INTEGER REFERENCES treatment_packages (id) ON DELETE SET NULL,
    clinic_id           INTEGER      NOT NULL REFERENCES clinics (id) ON DELETE RESTRICT,
    appointment_id      INTEGER REFERENCES appointments (id) ON DELETE SET NULL,
    session_number      INTEGER      NOT NULL,
    session_date        DATE         NOT NULL,
    therapist_user_id   INTEGER REFERENCES users (id) ON DELETE SET NULL,
    treatment_provided  TEXT,
    notes               TEXT,
    remarks             TEXT,
    is_voided           BOOLEAN      NOT NULL,
    void_reason         VARCHAR(500),
    voided_by_user_id   INTEGER REFERENCES users (id) ON DELETE SET NULL,
    voided_at           TIMESTAMPTZ,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_session_package_number UNIQUE (package_id, session_number),
    CONSTRAINT ck_session_number_positive CHECK (session_number > 0)
);
CREATE INDEX ix_sessions_patient_date      ON patient_sessions (patient_id, session_date);
CREATE INDEX ix_sessions_clinic_date       ON patient_sessions (clinic_id, session_date);
CREATE INDEX ix_patient_sessions_is_voided ON patient_sessions (is_voided);


CREATE TABLE payments (
    id                   SERIAL PRIMARY KEY,
    bill_id              INTEGER        NOT NULL REFERENCES bills (id) ON DELETE CASCADE,
    amount               NUMERIC(12,2)  NOT NULL,
    payment_method       VARCHAR(40)    NOT NULL,
    payment_date         DATE           NOT NULL,
    reference_number     VARCHAR(100),
    received_by_user_id  INTEGER REFERENCES users (id) ON DELETE SET NULL,
    notes                VARCHAR(500),
    created_at           TIMESTAMPTZ    NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ    NOT NULL DEFAULT now(),
    CONSTRAINT ck_payments_amount_positive CHECK (amount > 0)
);
CREATE INDEX ix_payments_bill_date ON payments (bill_id, payment_date);


CREATE TABLE notification_acknowledgements (
    id                SERIAL PRIMARY KEY,
    notification_id   INTEGER      NOT NULL REFERENCES notifications (id) ON DELETE CASCADE,
    user_id           INTEGER      NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    acknowledged_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_notification_ack_user UNIQUE (notification_id, user_id)
);
CREATE INDEX ix_notification_acknowledgements_notification_id ON notification_acknowledgements (notification_id);
CREATE INDEX ix_notification_acknowledgements_user_id         ON notification_acknowledgements (user_id);


-- ---------------------------------------------------------------------------
-- SEED: roles  (application requires these exact names)
-- ---------------------------------------------------------------------------
INSERT INTO roles (name, description) VALUES
    ('SUPERADMIN', 'Full system access across all clinics'),
    ('ADMIN',      'Chain-wide administrative access'),
    ('CLINIC_USER','Receptionist / therapist at one clinic');


-- ---------------------------------------------------------------------------
-- SEED: patient_sources
-- ---------------------------------------------------------------------------
INSERT INTO patient_sources (name, is_active, sort_order) VALUES
    ('Google',           true, 0),
    ('Google Ads',       true, 1),
    ('Instagram',        true, 2),
    ('Facebook',         true, 3),
    ('Doctor Referral',  true, 4),
    ('Patient Referral', true, 5),
    ('Apartment',        true, 6),
    ('Corporate',        true, 7),
    ('Walk-in',          true, 8),
    ('Other',            true, 9);


-- ---------------------------------------------------------------------------
-- SEED: service catalogue  (chain-wide items, clinic_id NULL)
-- ---------------------------------------------------------------------------
INSERT INTO service_items (clinic_id, name, item_type, default_price, is_active, sort_order) VALUES
    (NULL, 'Initial consultation',      'CONSULTATION',    500.00, true, 0),
    (NULL, 'Follow-up consultation',    'CONSULTATION',    300.00, true, 1),
    (NULL, 'Laser therapy (single)',    'SESSION_PACKAGE', 800.00, true, 2),
    (NULL, 'Dry needling (single)',     'SESSION_PACKAGE', 700.00, true, 3),
    (NULL, 'Shockwave therapy (single)','SESSION_PACKAGE',1200.00, true, 4),
    (NULL, 'Home visit surcharge',      'OTHER',           400.00, true, 5),
    (NULL, 'Knee support brace',        'PRODUCT',         950.00, true, 6),
    (NULL, 'Resistance band set',       'PRODUCT',         450.00, true, 7);


-- ---------------------------------------------------------------------------
-- ALEMBIC version stamp  (tells Alembic all migrations are applied)
-- ---------------------------------------------------------------------------
CREATE TABLE alembic_version (
    version_num VARCHAR(32) NOT NULL,
    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);
INSERT INTO alembic_version (version_num) VALUES ('e20e54ab7386');
