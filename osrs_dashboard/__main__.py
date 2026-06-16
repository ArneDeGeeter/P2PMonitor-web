import argparse
import webbrowser
import threading
from . import create_app


def main():
    parser = argparse.ArgumentParser(description="OSRS Account Dashboard")
    parser.add_argument("--db", default="~/.osrs_dashboard.db", help="Path to SQLite database")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--interval", type=int, default=3600, help="Hiscores poll interval (seconds)")
    parser.add_argument("--no-browser", action="store_true", help="Don't open browser automatically")
    args = parser.parse_args()

    app = create_app(db_path=args.db, poll_interval=args.interval)

    if not args.no_browser:
        browser_host = "localhost" if args.host == "127.0.0.1" else args.host
        url = f"http://{browser_host}:{args.port}/unlock"
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
