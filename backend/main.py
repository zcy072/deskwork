import uvicorn

# Ensure FastAPI app is imported after settings/logger
from app import app  # noqa: E402
from app.config import settings
from app.utils import logger


def main():
    logger.info("Starting Deskwork on %s:%s", settings.APP_HOST, settings.APP_PORT)
    uvicorn.run(app, host=settings.APP_HOST, port=settings.APP_PORT)


if __name__ == "__main__":
    main()
