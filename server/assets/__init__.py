"""资产管理域（前端界面 API）。"""

from server.assets.api import assets_router, configure_assets_api

__all__ = ["assets_router", "configure_assets_api"]
