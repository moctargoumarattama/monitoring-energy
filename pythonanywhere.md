# PythonAnywhere Deployment

Minimal setup for this Flask app on PythonAnywhere.

## App entry point

Use `wsgi.py` as the WSGI file target:

```python
from app import app as application
```

## Suggested PythonAnywhere settings

- Working directory: the project root
- Virtualenv: your `.venv` or a PythonAnywhere virtualenv
- Static files:
  - URL: `/static/`
  - Directory: `<project-root>/static`

## Environment variables

Optional:

- `APP_TOKEN`
- `DB_PATH`

If you do not set them, the app uses the built-in defaults.

## Notes

- `energy.db` lives in the project root by default.
- The dashboard and API are served by the Flask app directly.
- The ESP32 code currently points to a local network IP, so if you want the device to reach PythonAnywhere later, update `SERVER_BASE` in the ESP32 sketch to your public domain.
