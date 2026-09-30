import logging
import time

logger = logging.getLogger(__name__)

class RequestTimingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        start_time = time.perf_counter()

        response = self.get_response(request)

        duration = time.perf_counter() - start_time
        full_path = request.get_full_path()
        length = (
            response.headers.get("Content-Length")
            or (getattr(response, "content", b"") and len(response.content))
            or "-"
        )

        logger.info(
            '"%s %s" %s %s (%.4fs)',
            request.method,
            full_path,
            response.status_code,
            length,
            duration,
        )

        return response