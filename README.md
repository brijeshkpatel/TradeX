# Gamma Trading System

An institutional options trading dashboard for Nifty 50 built with Python, Streamlit, DhanHQ API, and PostgreSQL.

## Local Setup
1. Install dependencies: `pip install -r requirements.txt`
2. Update your Dhan credentials in `GammaDashboard.py`.
3. Run the PostgreSQL script `db_setup.sql` in your local database.
4. Run the application: `streamlit run GammaDashboard.py`

## Deployment
1. Upload the files to an Ubuntu VPS.
2. Setup PostgreSQL using `db_setup.sql`.
3. Copy `gamma_app.service` to `/etc/systemd/system/`.
4. Enable and start: `sudo systemctl enable --now gamma_app`
