"""Private JSON-lines transport for the isolated native driver process."""
import json
import sys

from pydantic import TypeAdapter

from . import api_models as m
from .service import service, error_result

REQUEST_ADAPTERS = {name: TypeAdapter(model) for name, model in m.TOOL_REQUESTS.items()}


def main():
    for line in sys.stdin:
        method = None
        try:
            message = json.loads(line)
            method = message["method"]
            if method == "_close":
                result = service.invoke("session", m.CloseSession(action="close", session_id=service.session_id)) if service.session_id else {"success": True}
            elif method == "discover":
                result = service.invoke("discover")
            else:
                request = REQUEST_ADAPTERS[method].validate_python(message["request"])
                result = service.invoke(method, request)
        except Exception as exc:
            result = error_result(exc)
        # ASCII escapes keep the JSON pipe compatible with Windows console encodings.
        sys.stdout.write(json.dumps(result, ensure_ascii=True, separators=(',', ':')) + "\n")
        sys.stdout.flush()
        if method == "_close":
            return


if __name__ == "__main__":
    main()
