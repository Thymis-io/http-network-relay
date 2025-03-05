import argparse
import os

from http_network_relay.access_client import AccessClient

parser = argparse.ArgumentParser(
    description="Connect to the HTTP network relay, "
    "request a connection to a target host running `edge-agent`.\n"
    "Send data from stdin to the target host and print data received "
    "from the target host to stdout."
)
parser.add_argument("target_host_identifier", help="The target host identifier")
parser.add_argument("target_ip", help="The target IP")
parser.add_argument("target_port", type=int, help="The target port")
parser.add_argument("protocol", help="The protocol to use (e.g. 'udp' or 'tcp')")

parser.add_argument(
    "--relay-url",
    help="The relay URL",
    default=os.getenv(
        "HTTP_NETWORK_RELAY_URL", "ws://127.0.0.1:8000/ws_for_access_clients"
    ),
)
parser.add_argument(
    "--secret",
    help="The secret used to authenticate with the relay",
    default=os.getenv("HTTP_NETWORK_RELAY_SECRET", None),
)


def main():
    args = parser.parse_args()
    access_client = AccessClient(
        args.target_host_identifier,
        args.target_ip,
        args.target_port,
        args.protocol,
        args.relay_url,
        args.secret,
    )
    access_client.run()


if __name__ == "__main__":
    main()
