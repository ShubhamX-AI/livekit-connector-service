import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "standalone_google_meet.api.control:app",
        host=os.environ.get("CONTROL_HOST", "0.0.0.0"),
        port=int(os.environ.get("CONTROL_PORT", "8080")),
    )
