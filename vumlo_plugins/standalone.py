from fastapi import FastAPI

def app_for(plugin_id: str, settings=None) -> FastAPI:
    app = FastAPI(title=plugin_id)
    @app.get("/manifest")
    def get_manifest():
        return {"id": plugin_id, "version": "1.0.0", "api": "1", "subscribes": [], "scopes": [], "ui": [], "settings": settings or {}}
    return app
