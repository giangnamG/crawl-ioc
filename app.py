from ioc_app.app import create_app


app = create_app()


if __name__ == "__main__":
    app = create_app(start_worker=True)
    app.run(host="127.0.0.1", port=5000, debug=True, use_reloader=False)
