"""Keep Canvas connection credentials out of request telemetry and access logs."""

import logging


class CanvasAccessFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Uvicorn's standard access args: client, method, path+query, HTTP version, status.
        if isinstance(record.args, tuple) and len(record.args) == 5:
            args = list(record.args)
            if isinstance(args[2], str) and args[2].split("?", 1)[0] == "/connect/canvas":
                args[2] = "/connect/canvas"
                record.args = tuple(args)
        return True


def install_access_filter() -> None:
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, CanvasAccessFilter) for f in logger.filters):
        logger.addFilter(CanvasAccessFilter())


def is_private_request(event: dict) -> bool:
    url = str((event.get("request") or {}).get("url", ""))
    return "/connect/canvas" in url or "/feeds/calendars/" in url or "/webhooks/photon" in url
