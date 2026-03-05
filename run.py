import os

import uvicorn


def main() -> None:
    port = int(os.getenv("PORT", "8000"))
    workers = max(1, int(os.getenv("WEB_CONCURRENCY", "1")))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, workers=workers)


if __name__ == "__main__":
    main()
