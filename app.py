import os
from ioc_app.app import create_app


app = create_app()


if __name__ == "__main__":
    app = create_app(start_worker=bool(int(os.environ.get("AUTO_WORKER_ENABLED", "0"))))
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
