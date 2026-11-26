# DoctorsOn on Vercel

## Deploy the existing Flask app

1. Push the contents of this `doctorson` folder to the connected Git repository.
2. In Vercel → Project → Settings → General, set **Root Directory** to `doctorson` only if the repository contains this folder at its top level. If this folder's contents are already the repository root, leave Root Directory as `./`.
3. Redeploy with the latest commit. Vercel detects the Flask `app` in `app.py`; no custom API wrapper or rewrite is needed.
4. In Vercel → Settings → Environment Variables, set at least:
   - `DOCTORSON_SECRET`: a long, random secret (same value across deployments/instances).
   - `DOCTORSON_ADMIN_PASSWORD`: a strong admin password (replace the development default).
   - `DOCTORSON_TZ`: `Asia/Kolkata`
   - `DOCTORSON_SEED`: `1` only if you want the sample data when the database is empty.
   - `OPENAI_API_KEY`: optional; only if AI prescription extraction is enabled.
5. Redeploy after changing environment variables.

## Important persistence limitation

Vercel's function filesystem is ephemeral. This compatibility patch moves SQLite and prescription uploads into `/tmp` so the function can start without trying to write into the read-only deployment bundle. If the bundled `doctorson.db` is present at deploy time, it is copied to `/tmp` on a cold start; otherwise the schema is created and optional sample data is seeded.

**Do not use this temporary SQLite setup for real patient/order data.** Data written to `/tmp` can disappear on a cold start, deployment, scale-out, or instance replacement, and separate instances do not share the same database or uploads. For production, move the database to a persistent hosted database and prescription files to private object storage before accepting real users or prescriptions. The existing ZIP does not include credentials or a configured persistent database/storage provider, so those cannot be safely provisioned automatically.

## Local run

The normal local SQLite path remains `doctorson.db` in this folder. Run `python app.py` as before.
