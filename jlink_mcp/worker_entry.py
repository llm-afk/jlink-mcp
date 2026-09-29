"""Private JSON-lines transport for the isolated native driver process."""
import json
import sys

from pydantic import TypeAdapter

from . import api_models as m
from .service import service, error_result

REQUESTS = {"session": m.SessionRequest, "read": m.ReadRequest, "write": m.WriteRequest,
            "control": m.ControlRequest, "breakpoint": m.BreakpointRequest,
            "inspect": m.InspectRequest, "capture": m.CaptureRequest,
            "firmware": m.FirmwareRequest, "channel": m.ChannelRequest}


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
                request = TypeAdapter(REQUESTS[method]).validate_python(message["request"])
                result = service.invoke(method, request)
        except Exception as exc:
            result = error_result(exc)
        sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        if method == "_close":
            return


if __name__ == "__main__":
    main()
