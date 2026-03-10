import time

from app import create_app

app = create_app(enable_scheduler=True)

if __name__ == "__main__":
    app.logger.info("Crawler worker started")
    while True:
        time.sleep(3600)
