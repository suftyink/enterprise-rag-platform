from fastapi import APIRouter

from erp_assistant.web import auth_routes, release_routes, routes

router = APIRouter()
router.include_router(auth_routes.router)
router.include_router(routes.router)
router.include_router(release_routes.router)
