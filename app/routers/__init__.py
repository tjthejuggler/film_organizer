"""HTTP layer: one APIRouter module per feature domain.

Each module owns its routes AND its pydantic request models. Shared row
shaping helpers live in `common.py`. `app/main.py` stays the composition
root: app creation, startup hooks, middleware, router mounting, static.
"""
