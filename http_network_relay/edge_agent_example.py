import argparse
import asyncio
import os
import random
import socket
import sys
from typing import Optional

from .edge_agent import EdgeAgent
from .pydantic_models import EtRStartMessage

debug = False
if os.getenv("DEBUG") == "1":
    debug = True


def eprint(*args, only_debug=False, **kwargs):
    if (debug and only_debug) or (not only_debug):
        print(*args, file=sys.stderr, **kwargs)


class EdgeAgentForAccessClients(EdgeAgent):
    def __init__(self, relay_url, name, secret):
        super().__init__(relay_url)
        self.name = name
        self.secret = secret

    async def create_start_message(
        self, last_error: Optional[str] = None
    ) -> EtRStartMessage:
        return EtRStartMessage(
            name=self.name,
            secret=self.secret,
            last_error=last_error,
        )


def main():
    parser = argparse.ArgumentParser(
        description="Edge agent for HTTP network relay, allows `access-client` to connect to it"
    )
    parser.add_argument(
        "--relay-url",
        help="The server URL",
        default=os.getenv(
            "HTTP_NETWORK_RELAY_URL", "ws://127.0.0.1:8000/ws_for_edge_agents"
        ),
    )
    parser.add_argument(
        "--name",
        help="The edge-agents name",
        default=os.getenv(
            "HTTP_NETWORK_RELAY_NAME",
            f"unnamed-fqdn-{socket.getfqdn()}-edge-agent-{random.randbytes(4).hex()}",
        ),
    )
    parser.add_argument(
        "--secret",
        help="The secret used to authenticate with the relay",
        default=os.getenv("HTTP_NETWORK_RELAY_SECRET", None),
    )
    args = parser.parse_args()

    if args.relay_url is None:
        raise ValueError("relay_url is required")
    if args.secret is None:
        raise ValueError("secret is required")

    agent = EdgeAgentForAccessClients(args.relay_url, args.name, args.secret)
    asyncio.run(agent.async_main())


if __name__ == "__main__":
    main()
