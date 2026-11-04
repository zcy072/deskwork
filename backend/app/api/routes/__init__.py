from app.api.routes.conversations import capabilities_router, conversations_router, memories_router
from app.api.routes.internals import internals_router
from app.api.routes.stream import stream_router

routes = [
    internals_router,
    stream_router,
    conversations_router,
    memories_router,
    capabilities_router,
]
