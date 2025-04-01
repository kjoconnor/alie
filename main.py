import glob
import logging
import os
import random
import tomllib

from pathlib import Path
from urllib.parse import urljoin

import aiohttp
import zstandard as zstd  # Cloudflare seems to use this?

from aiohttp import web
from bs4 import BeautifulSoup


class Alie:
    def __init__(self, config: dict) -> None:
        self.upstream_url = config["upstream"]["url"]
        self._config = config
        self._session = None
        self._ua_contains = []
        self._image_replace_method = None

        for _, bot in config["bots"].items():
            if bot.get("enabled", True):
                self._ua_contains.extend(
                    [
                        ua_contains.casefold()
                        for ua_contains in bot.get("user_agent_contains", [])
                    ]
                )
        logger.debug(f"Bot UAs: {self._ua_contains}")

        if "image" in config.keys():
            self._image_replace_method = config["image"]["replace_method"]
            self._images = []
            for file in glob.glob(os.path.join(config["image"]["replace_source"], "*")):
                path = Path(file)
                print(path)
                print(path.suffix)
                if path.suffix in (".jpg", ".jpeg", ".png"):
                    self._images.append(file)

            logger.debug(f"Image list: {self._images}")

    async def start(self) -> None:
        self._session = aiohttp.ClientSession()

    async def stop(self) -> None:
        if self._session:
            await self._session.close()

    def _get_new_image_src(self) -> str:
        return random.choice(self._images)

    def rewrite(self, content, bot_detected: bool) -> str:
        soup = BeautifulSoup(content, "html.parser")
        rewrite_counter = 0

        for lie_tag in soup.find_all(config["rewrite_tag"]["lie_tag_name"]):
            if bot_detected:
                lie_tag.unwrap()
            else:
                lie_tag.decompose()
            rewrite_counter += 1
        for true_tag in soup.find_all(config["rewrite_tag"]["true_tag_name"]):
            if bot_detected:
                true_tag.decompose()
            else:
                true_tag.unwrap()
            rewrite_counter += 1

        if bot_detected and self._image_replace_method == "tag":
            for img_tag in soup.find_all("img"):
                img_tag["src"] = self._get_new_image_src()
                rewrite_counter += 1

        logger.debug(f"{rewrite_counter} rewrites performed")

        return str(soup)

    def _match_bot_ua(self, user_agent: str) -> bool:
        return any(bot_ua in user_agent.casefold() for bot_ua in self._ua_contains)

    @staticmethod
    def _handle_zstd(body):
        decompressor = zstd.ZstdDecompressor()
        # Just guessing here what the max output size is
        return decompressor.decompress(body, max_output_size=10 * 1024 * 1000)

    async def handle_request(self, request):
        logger.info(f"I {request.remote} {request.url}")
        target = urljoin(self.upstream_url, request.path_qs)

        headers = dict(request.headers)
        headers.pop("Host")

        user_agent = request.headers.get("User-Agent", "")
        bot_detected = self._match_bot_ua(user_agent)

        try:
            async with self._session.request(
                method=request.method,
                url=target,
                headers=headers,
                data=await request.read() if request.body_exists else None,
                allow_redirects=False,
            ) as response:
                body = await response.read()

                if response.headers.get("Content-Encoding") == "zstd":
                    body = self._handle_zstd(body)

                if "text/html" in response.headers.get("Content-Type", "").lower():
                    body = self.rewrite(body, bot_detected)

                logger.info(
                    f"O {'B' if bot_detected else 'H'} {response.url} {response.status}"
                )

                proxied_response = web.Response(status=response.status, body=body)

                for key, value in response.headers.items():
                    if key.lower() not in (
                        "transfer-encoding",
                        "content-length",
                        "content-encoding",
                    ):
                        proxied_response.headers[key] = value

                return proxied_response

        except aiohttp.ClientError as exc:
            logger.error("Client error", exc)
            return web.Response(text="Client error")
        except Exception as exc:
            logger.error("Unhandled exception", exc)
            return web.Response(text="Unhandled error")


async def start_proxy(config):
    app = web.Application()
    alie = Alie(config)

    app.on_startup.append(lambda _: alie.start())
    app.on_cleanup.append(lambda _: alie.stop())

    app.router.add_route("*", "/{path:.*}", alie.handle_request)

    return app


if __name__ == "__main__":
    with open("config.toml", "rb") as fh:
        config = tomllib.load(fh)
    logging.basicConfig(
        level=getattr(logging, config["general"]["log_level"].upper()),
        format="[%(asctime)s - %(levelname)s] %(message)s",
    )
    logger = logging.getLogger("alie")
    app = start_proxy(config)
    web.run_app(
        app, host=config["server"]["listen_host"], port=config["server"]["listen_port"]
    )
