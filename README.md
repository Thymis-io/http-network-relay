# Tunnel TCP over HTTP using WebSockets

| <video src="https://github.com/user-attachments/assets/422eada0-0ae1-429d-855a-d268eba4d78e" /> |
| ----------------------------------------------------------------------------------------------- |
| Network Relay, Edge Agent and Access Client in action                                           |

Three binaries: `network-relay`, `edge-agent` and `access-client` are provided to tunnel TCP over HTTP using WebSockets.

The `network-relay` is supposed to be run on a machine with a public IP address.
Ideally, `network-relay` should be run behind a reverse proxy that terminates TLS, such that the clients can trust the server's identity.

The `edge-agent` is supposed to be run on a machine that needs to access the server's network.
The `edge-agent` will establish a WebSocket connection to the server.

The `access-client` is a script that can be used as a ProxyCommand in an SSH configuration file.
It will establish a WebSocket connection to the server and forward the SSH connection over the WebSocket connection.
It can also be used as a general purpose proxy command for other protocols.

If you're using `uv`, you can use this project directly by running `uvx --from git+https://github.com/Thymis-io/http-network-relay <command> [args...]`.
Where `<command>` is one of `network-relay`, `edge-agent` or `access-client`.

## Server Configuration and Usage

The **Network Relay** server binary is designed to operate on a machine with a public IP address. For enhanced security, it is recommended to deploy the server behind a reverse proxy that handles TLS termination, ensuring that clients can verify the server's identity.

Usage: `network-relay [-h] [--host HOST] [--port PORT] [--credentials-file CREDENTIALS_FILE]`

The credentials file is a JSON file structured as follows:

```json
{
  "edge-agents": {
    "<agent-name1>": "<agent-secret1>",
    "<agent-name2>": "<agent-secret2>"
  },
  "access-client-secrets": [
    "<access-client-secret1>",
    "<access-client-secret2>"
  ]
}
```

It can be set using the `--credentials-file` command line argument, or the environment variable `HTTP_NETWORK_RELAY_CREDENTIALS_FILE`.

## Edge Agent

The **Edge Agent** will establish a WebSocket connection to the server.

Usage: `edge-agent --relay-url <relay_url> --name <name> --secret <secret>`

It will connect to the server using the `--relay-url` command line argument.
The default value is `ws://127.0.0.1:8000/ws_for_edge_agents`.
This can be set using the environment variable `HTTP_NETWORK_RELAY_URL`.

The **Edge Agent** will identify itself to the server using the `--name` command line argument.
The **Edge Agent**'s `name` is a unique identifier for the running instance and
is used to authenticate the **Edge Agent** with the relay,
as well as identify the **Edge Agent** to the end users.
The **Edge Agent** will authenticate with the server using the `--secret` command line argument.
Both can be set using environment variables `HTTP_NETWORK_RELAY_NAME` and `HTTP_NETWORK_RELAY_SECRET`.

## Access Client

The `access-client` script provides a general purpose proxy command for other protocols.

Usage: `access-client <target_host_identifier> <target_ip> <target_port> <protocol> --relay-url <relay_url> --secret <secret>`

The `target_host_identifier` is the `name` of the **Edge Agent** that a connection is to be established with.
The `target_ip` and `target_port` are the IP address and port of the connection that the **Edge Agent** wants to establish.
The `protocol` is the protocol that the **Edge Agent** wants to use (e.g. 'udp' or 'tcp'). Currently, only 'tcp' is supported.

The `relay-url` is the URL of the server that the **Edge Agent** wants to connect to.
The default value is `ws://127.0.0.1:8000/ws_for_access_clients`.
It can also be set using the environment variable `HTTP_NETWORK_RELAY_URL`.

The `secret` is the secret that the **Edge Agent** uses to authenticate with the relay.
This is the secret that the **Edge Agent** uses to authenticate with the relay.
It can also be set using the environment variable `HTTP_NETWORK_RELAY_SECRET`.

The `access-client` script will establish a WebSocket connection to the server and forward its stdin and stdout to the server.
The server will forward the data to the **Edge Agent**, which will then establish the connection to the target connection details.
