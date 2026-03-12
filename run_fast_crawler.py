"""Entry point for the fast HTTP-based Toutiao crawler."""

import asyncio
import logging
import sys

from app import create_app
from app.fast_crawler import FastCrawler

app = create_app(enable_scheduler=False)


async def main():
    crawler = FastCrawler(app)
    await crawler.run_loop()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        stream=sys.stdout,
    )
    app.logger.info("Fast crawler worker starting")
    asyncio.run(main())
