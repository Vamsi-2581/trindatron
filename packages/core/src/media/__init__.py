"""Media creation — images, videos and (later) avatars, as TRINDATRON capabilities.

The layers, from the outside in:

  api/routes/media.py        HTTP: create, follow, list, download, delete.
  services/media_service.py  Rules: entitlement, quota, budget, idempotency,
                             job lifecycle, storage, accounting.
  media/plans.py             What each plan may create (counts, quality,
                             resolution, duration) — the only place limits live.
  media/engines.py           The contract an engine fulfils. Engines are chosen
                             by configuration and never named to the user.
  media/comfyui.py           Self-hosted engine: a ComfyUI server running
                             open-weight models from API-format workflows.
  media/dev_engine.py        Development-only image engine (refuses production).

Nothing in this package names a model or vendor in anything a user can read.
"""
