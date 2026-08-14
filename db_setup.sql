-- PostgreSQL Database Setup
CREATE DATABASE gamma_db;
CREATE USER postgres WITH ENCRYPTED PASSWORD 'root';
GRANT ALL PRIVILEGES ON DATABASE gamma_db TO gamma_user;

\c gamma_db;

CREATE TABLE IF NOT EXISTS gamma_metrics (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMP NOT NULL,
    spot_price FLOAT,
    zero_gamma FLOAT,
    call_wall FLOAT,
    put_wall FLOAT,
    cw_velocity FLOAT,
    pw_velocity FLOAT,
    vol_skew FLOAT,
    iv_vwap FLOAT
);


