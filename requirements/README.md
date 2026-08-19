# Dependency Locks

The dependency ranges in `pyproject.toml` are the source contract. Regenerate the Python 3.12 locks with pip-tools 7.5.0:

```powershell
python -m piptools compile pyproject.toml --generate-hashes --strip-extras --output-file requirements/runtime.lock
python -m piptools compile pyproject.toml --extra dev --generate-hashes --strip-extras --output-file requirements/dev.lock
```

Install with hash verification:

```powershell
python -m pip install --require-hashes -r requirements/runtime.lock
```
